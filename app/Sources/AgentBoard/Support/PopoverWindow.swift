import AppKit
import OSLog

/// The screen geometry the popover is sized against: where the menu bar
/// icon is and how much room its screen leaves.
struct PopoverScreen: Equatable {
    /// The screen's frame less the menu bar and Dock.
    var visibleFrame: CGRect
    /// The menu bar icon's left edge, where the popover's left edge sits.
    var anchorX: CGFloat

    var availableWidth: CGFloat {
        PopoverSizing.availableWidth(anchorX: anchorX, screenMaxX: visibleFrame.maxX)
    }
}

/// Sizes and places the MenuBarExtra panel. SwiftUI sizes the panel to its
/// content when it opens but not afterwards: a later change of the content's
/// size (the grip, auto-expand, the width setting) only re-centers the
/// content in the old frame. So the view reports its content size here and
/// this sets the panel's frame to it, with the top edge kept where the panel
/// opened (under the menu bar) and the left edge at the menu bar icon
/// (`PopoverSizing.panelFrame`).
///
/// The placer owns the one anchor (`anchor`) each open uses: the view sizes
/// the content from it and `place` positions the panel from it, so width and
/// origin always agree. It comes from the status item window, or the last
/// icon position read; never from the panel's own frame.
@MainActor
final class PopoverWindowPlacer {
    private let log = Logger(subsystem: "io.github.agentboard", category: "popover")
    /// Removed in `deinit`, which is nonisolated; NotificationCenter's
    /// removeObserver is thread-safe and nothing else touches this then.
    nonisolated(unsafe) private var observers: [NSObjectProtocol] = []
    /// The panel's top edge this open, read once it is first visible.
    private var top: CGFloat?
    /// The size the view's content last laid out at.
    private var contentSize: CGSize?
    /// The frame `place` last set this open, to tell when something else
    /// (MenuBarExtra) moved the panel afterwards.
    private var applied: CGRect?
    /// Placements in a row that undid someone else's move or resize.
    private var undone = PopoverSizing.UndoState()
    /// The `popover.open` line is still to be logged: it waits for the
    /// first `place` with this open's panel up, since at `opened()` the
    /// window is still the last open's (or nil on the first open).
    private var openPending = false

    /// The anchor this open uses, nil while no icon position is known (the
    /// panel is then left where SwiftUI put it).
    private(set) var anchor: PopoverSizing.ResolvedAnchor?
    var screen: PopoverScreen? { anchor?.screen }

    /// The last icon position read from a status item window, kept for when
    /// none can be read. Shared, so Settings sees it too.
    private static var lastKnownIcon: PopoverSizing.RememberedIcon?

    weak var window: NSWindow? {
        didSet {
            guard window !== oldValue else { return }
            observe()
            resolveAnchor()
        }
    }

    /// Called when `anchor` changes (an open, the screen changed, the icon
    /// moved), so the view re-reads it.
    var onAnchorChange: (@MainActor () -> Void)?

    /// This app's status item windows, except the popover's own window, each
    /// with the screen AppKit puts it on. A window on no screen (the
    /// off-screen copies) keeps none and is never a candidate.
    private static func statusItemWindows(excluding popover: NSWindow?) -> [PopoverSizing.StatusItemCandidate] {
        NSApp.windows
            .filter { $0 !== popover && String(describing: type(of: $0)).contains("StatusBarWindow") }
            .map { window in
                PopoverSizing.StatusItemCandidate(
                    frame: window.frame,
                    screen: window.screen.map { PopoverSizing.Screen(frame: $0.frame, visible: $0.visibleFrame) })
            }
    }

    /// The current screens, to find a remembered icon position's screen.
    private static var currentScreens: [PopoverSizing.Screen] {
        NSScreen.screens.map { PopoverSizing.Screen(frame: $0.frame, visible: $0.visibleFrame) }
    }

    /// The menu bar icon's screen and position, or the last one read; nil
    /// before either exists. Settings uses it without a popover window.
    static func iconScreen() -> PopoverScreen? {
        let resolved = PopoverSizing.resolveAnchor(statusItems: statusItemWindows(excluding: nil), popoverScreen: nil,
                                                   lastKnown: lastKnownIcon, screens: currentScreens)
        if let resolved, resolved.source == .statusItem { lastKnownIcon = resolved.remembered }
        return resolved?.screen
    }

    deinit {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
    }

    /// Re-reads the anchor (and remembers an icon position read from a
    /// status item window). Returns the status item windows it looked at.
    @discardableResult
    private func resolveAnchor() -> [PopoverSizing.StatusItemCandidate] {
        let items = Self.statusItemWindows(excluding: window)
        let resolved = PopoverSizing.resolveAnchor(statusItems: items, popoverScreen: window?.screen?.frame,
                                                   lastKnown: Self.lastKnownIcon, screens: Self.currentScreens)
        if let resolved, resolved.source == .statusItem { Self.lastKnownIcon = resolved.remembered }
        if resolved != anchor {
            anchor = resolved
            onAnchorChange?()
        }
        return items
    }

    /// Re-reads the anchor and, when it moved to another screen (the icon
    /// was clicked on another display), forgets the top edge so it is read
    /// again from the panel there. A Dock or menu bar change on the same
    /// display keeps the top edge.
    @discardableResult
    private func reanchor() -> [PopoverSizing.StatusItemCandidate] {
        let before = anchor
        let items = resolveAnchor()
        if PopoverSizing.ResolvedAnchor.changedScreen(from: before, to: anchor) { top = nil }
        return items
    }

    /// The popover opened: forget the previous open's top edge and undo
    /// count, re-read the anchor (so the view can size its content), and
    /// place the panel on the next main-queue turn, when it may be up (an
    /// unchanged content size would not otherwise place it, and MenuBarExtra
    /// positions the panel itself on every open). Like every placement, that
    /// one re-reads the anchor against the panel's screen first, and the
    /// first one with this open's panel up logs the open line.
    func opened() {
        top = nil
        applied = nil
        undone = PopoverSizing.UndoState()
        openPending = true
        resolveAnchor()
        DispatchQueue.main.async { [weak self] in self?.place(reason: .open) }
    }

    /// The size the view's content now has; the panel follows.
    func setContentSize(_ size: CGSize) {
        guard size != contentSize else { return }
        contentSize = size
        place(reason: .content)
    }

    /// Gives the panel the content's size, top-left anchored, after
    /// re-reading the anchor for the screen the panel is on now. Every
    /// placement goes through here, so none can use an anchor read against
    /// another panel or display. Does nothing without a known anchor
    /// (SwiftUI's own placement stays) or while the panel is not up.
    func place(reason: PopoverSizing.PlaceReason) {
        guard let window, window.isVisible else { return }
        let items = reanchor()
        // The first placement of this open with its panel up (whatever its
        // reason) logs the open line, before any `popover.place` line.
        if openPending {
            openPending = false
            logOpen(items)
        }
        guard let contentSize, let anchor else { return }
        let frame = window.frame
        if top == nil { top = frame.maxY }
        guard let top else { return }
        guard let target = PopoverSizing.frameToApply(current: frame, contentSize: contentSize, top: top,
                                                      anchorX: anchor.screen.anchorX,
                                                      visibleFrame: anchor.screen.visibleFrame)
        else { return }
        let decision = PopoverSizing.placeDecision(current: frame, applied: applied, reason: reason, state: undone,
                                                   now: ProcessInfo.processInfo.systemUptime)
        undone = decision.state
        switch decision.action {
        case .skip:
            return
        case .giveUp:
            log.notice("popover.place gave-up reason=\(reason.rawValue, privacy: .public) undone=\(PopoverSizing.maxUndone) frame=\(PopoverSizing.describe(frame), privacy: .public) target=\(PopoverSizing.describe(target), privacy: .public)")
            return
        case .place:
            break
        }
        log.notice("popover.place reason=\(reason.rawValue, privacy: .public) source=\(anchor.source.rawValue, privacy: .public) anchorX=\(Double(anchor.screen.anchorX)) visible=\(PopoverSizing.describe(anchor.screen.visibleFrame), privacy: .public) content=\(Double(contentSize.width))x\(Double(contentSize.height)) from=\(PopoverSizing.describe(frame), privacy: .public) to=\(PopoverSizing.describe(target), privacy: .public) undid=\(decision.undid)")
        applied = target
        window.setFrame(target, display: true)
    }

    /// One line per open: the anchor, where it came from, every status item
    /// window looked at, the content size and the frame it gets.
    private func logOpen(_ items: [PopoverSizing.StatusItemCandidate]) {
        let listed = items.map { item in
            "\(PopoverSizing.describe(item.frame))@\(item.screen.map { PopoverSizing.describe($0.frame) } ?? "none")"
        }.joined(separator: "; ")
        let frame = window.map { PopoverSizing.describe($0.frame) } ?? "none"
        var target = "none"
        if let window, let contentSize, let anchor {
            target = PopoverSizing.describe(PopoverSizing.panelFrame(
                contentSize: contentSize, top: top ?? window.frame.maxY, anchorX: anchor.screen.anchorX,
                visibleFrame: anchor.screen.visibleFrame))
        }
        let source = anchor?.source.rawValue ?? "none"
        let anchorX = anchor.map { String(Double($0.screen.anchorX)) } ?? "none"
        let visible = anchor.map { PopoverSizing.describe($0.screen.visibleFrame) } ?? "none"
        let content = contentSize.map { "\(Double($0.width))x\(Double($0.height))" } ?? "none"
        log.notice("popover.open source=\(source, privacy: .public) anchorX=\(anchorX, privacy: .public) visible=\(visible, privacy: .public) candidates=\(items.count) [\(listed, privacy: .public)] content=\(content, privacy: .public) frame=\(frame, privacy: .public) target=\(target, privacy: .public)")
    }

    /// The screen may have changed: re-read the anchor (the view's
    /// `onAnchorChange` runs synchronously; also with the panel closed, for
    /// the next open) and re-place the panel now, since an unchanged content
    /// size would not otherwise move it onto the new screen.
    private func screenChanged() {
        top = nil
        resolveAnchor()
        place(reason: .screen)
    }

    private func observe() {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
        observers = []
        guard let window else { return }
        let center = NotificationCenter.default
        // MenuBarExtra sizes and positions the panel as it opens, and may
        // move it again (same size) on a later open, possibly onto another
        // display: on becoming key and after any resize or move, correct the
        // frame (`place` re-reads the icon for the panel's screen as it is
        // now, so the order of didMove and didChangeScreen does not matter).
        let events: [(Notification.Name, PopoverSizing.PlaceReason)] = [
            (NSWindow.didBecomeKeyNotification, .key), (NSWindow.didResizeNotification, .resize),
            (NSWindow.didMoveNotification, .move),
        ]
        for (name, reason) in events {
            observers.append(center.addObserver(forName: name, object: window, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.place(reason: reason) }
            })
        }
        observers.append(center.addObserver(forName: NSWindow.didChangeScreenNotification, object: window,
                                            queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.screenChanged() }
        })
        observers.append(center.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                            object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.screenChanged() }
        })
    }
}
