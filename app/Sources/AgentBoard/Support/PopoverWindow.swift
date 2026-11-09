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

    weak var window: NSWindow? {
        didSet {
            guard window !== oldValue else { return }
            observe()
        }
    }

    /// Called when the popover's screen may have changed (moved to another
    /// display, display arrangement changed).
    var onScreenChange: (@MainActor () -> Void)?

    /// The menu bar's status item window for this app: the icon's position
    /// and screen. MenuBarExtra creates exactly one.
    static func statusItemWindow() -> NSWindow? {
        NSApp.windows.first { String(describing: type(of: $0)).contains("StatusBarWindow") }
    }

    /// The menu bar icon's screen and position, or nil before the status
    /// item window exists. Settings uses it without a popover window.
    static func iconScreen() -> PopoverScreen? {
        guard let item = statusItemWindow(), let screen = item.screen else { return nil }
        return PopoverScreen(visibleFrame: screen.visibleFrame, anchorX: item.frame.minX)
    }

    /// The icon's screen and position, or the popover's own window when the
    /// status item window is not found. Nil before either exists.
    func currentScreen() -> PopoverScreen? {
        if let screen = Self.iconScreen() { return screen }
        if let window, let screen = window.screen {
            return PopoverScreen(visibleFrame: screen.visibleFrame, anchorX: window.frame.minX)
        }
        return nil
    }

    deinit {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
    }

    /// The popover opened: forget the previous open's top edge.
    func reset() {
        top = nil
    }

    /// The size the view's content now has; the panel follows.
    func setContentSize(_ size: CGSize) {
        guard size != contentSize else { return }
        contentSize = size
        place()
    }

    /// Gives the panel the content's size, top-left anchored.
    func place() {
        guard let window, window.isVisible, let contentSize, let screen = currentScreen() else { return }
        let frame = window.frame
        if top == nil { top = frame.maxY }
        guard let top else { return }
        guard let target = PopoverSizing.frameToApply(current: frame, contentSize: contentSize, top: top,
                                                      anchorX: screen.anchorX, visibleFrame: screen.visibleFrame)
        else { return }
        log.debug("popover.place from=\(NSStringFromRect(frame), privacy: .public) to=\(NSStringFromRect(target), privacy: .public)")
        window.setFrame(target, display: true)
    }

    /// The screen may have changed: re-read it (the view's `readScreen` runs
    /// synchronously in `onScreenChange`) and re-place the panel now, since
    /// an unchanged content size would not otherwise move it onto the new
    /// screen.
    private func screenChanged() {
        top = nil
        onScreenChange?()
        place()
    }

    private func observe() {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
        observers = []
        guard let window else { return }
        let center = NotificationCenter.default
        // MenuBarExtra sizes and positions the panel as it opens; correct
        // that once it is up, and after any resize of its own.
        for name in [NSWindow.didResizeNotification, NSWindow.didBecomeKeyNotification] {
            observers.append(center.addObserver(forName: name, object: window, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.place() }
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
