import Foundation

/// The popover's size (Settings → Popover and the corner grip): the pure
/// arithmetic, testable without a window. Heights are the session list's
/// height (the scroll area between the search bar and the footer); widths
/// are the whole popover's. All values are points.
enum PopoverSizing {
    /// The list height the grip last left (default `defaultHeight`).
    static let heightKey = "popoverHeight"
    /// Fit the list height to the active sessions (default off).
    static let autoExpandKey = "popoverAutoExpand"
    /// The width the grip last left; 0 means never dragged.
    static let widthKey = "popoverWidth"
    /// Use the full width from the menu bar icon to the screen's right edge
    /// (default on; dragging the width turns it off).
    static let autoWidthKey = "popoverAutoWidth"

    /// The list height before this setting existed.
    static let defaultHeight: CGFloat = 388
    static let minHeight: CGFloat = 240
    static let minWidth: CGFloat = 320
    /// The width before this setting existed, used until the screen is known.
    static let fallbackWidth: CGFloat = 380
    /// Room kept between the popover and the screen's bottom and right edges.
    static let screenMargin: CGFloat = 8
    /// One VoiceOver adjust step on the grip.
    static let step: CGFloat = 40
    /// Auto-expand ignores content height changes smaller than this, so a
    /// sub-point layout wobble never resizes the panel.
    static let autoThreshold: CGFloat = 4
    /// The session list's padding above and below its content. The bottom
    /// one sits below the measured sections, so auto height adds it.
    static let listPadding: CGFloat = 8

    /// The sections auto-expand fits: Needs you, Review and Running (not
    /// Unconcluded, which is collapsed by default, nor Snoozed or Done).
    static func isActive(_ kind: PopoverSections.Kind) -> Bool {
        switch kind {
        case .needsYou, .review, .running: return true
        case .unconcluded, .snoozed, .done: return false
        }
    }

    /// Settings → Reset size and a double-click on the grip: the default
    /// height and auto width. Auto-expand is a mode, not a size, so it stays.
    static func resetSize(in defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: PopoverSizing.heightKey)
        defaults.removeObject(forKey: PopoverSizing.widthKey)
        defaults.removeObject(forKey: PopoverSizing.autoWidthKey)
    }

    /// The list fades its last `moreBelowFade` points while content
    /// continues below the visible area, so a row cut by the footer reads
    /// as "scroll for more" rather than as clipping.
    static let moreBelowFade: CGFloat = 14

    /// Whether content continues below the list's visible area: the
    /// content's bottom edge, in the viewport's coordinates, lies more than
    /// a point past the viewport (sub-point slack is layout rounding).
    static func hasMoreBelow(contentMaxY: CGFloat, viewportHeight: CGFloat) -> Bool {
        contentMaxY > viewportHeight + 1
    }

    // MARK: - Height

    /// The tallest list that keeps the whole popover on screen: the room
    /// below the menu bar, less the search bar, footer and other chrome.
    /// Never below zero; it can be below `minHeight` on a very short screen.
    static func maxHeight(screenVisibleHeight: CGFloat, chromeHeight: CGFloat) -> CGFloat {
        max(0, screenVisibleHeight - chromeHeight - screenMargin)
    }

    /// `height` within [`minHeight`, `maxHeight`]; the screen wins when the
    /// two disagree, so the popover never runs off a short screen.
    static func clamp(height: CGFloat, max upper: CGFloat) -> CGFloat {
        min(max(height, minHeight), upper)
    }

    static func clamp(height: CGFloat, screenVisibleHeight: CGFloat, chromeHeight: CGFloat) -> CGFloat {
        clamp(height: height, max: maxHeight(screenVisibleHeight: screenVisibleHeight, chromeHeight: chromeHeight))
    }

    /// A stored height, or the default for a missing (0) or nonsense value.
    /// Not clamped: a height saved on a taller display is kept as it is and
    /// only clamped for display (`clamp`), so it returns on that display.
    static func storedHeight(_ raw: Double) -> CGFloat {
        raw.isFinite && raw > 0 ? CGFloat(raw) : defaultHeight
    }

    /// The auto-expand list height: down to the bottom of the last active
    /// section (or of everything shown while filtering) plus the list's
    /// bottom padding, clamped. Nil (nothing active) is the minimum.
    static func autoHeight(contentBottom: CGFloat?, bottomPadding: CGFloat = listPadding,
                           max upper: CGFloat) -> CGFloat {
        clamp(height: contentBottom.map { $0 + bottomPadding } ?? 0, max: upper)
    }

    /// The auto height to show next: `previous` unless `measured` moved by
    /// at least `autoThreshold`.
    static func settledAutoHeight(previous: CGFloat?, measured: CGFloat) -> CGFloat {
        guard let previous, abs(measured - previous) < autoThreshold else { return measured }
        return previous
    }

    /// The list height while dragging: the start height plus how far the
    /// pointer moved down (`translation`, positive down), clamped.
    static func dragged(startHeight: CGFloat, translation: CGFloat, screenMax: CGFloat) -> CGFloat {
        clamp(height: startHeight + translation, max: screenMax)
    }

    /// One VoiceOver adjust step: `direction` +1 taller, -1 shorter.
    static func stepped(height: CGFloat, direction: Int, screenMax: CGFloat) -> CGFloat {
        clamp(height: height + CGFloat(direction.signum()) * step, max: screenMax)
    }

    /// What one VoiceOver step on the grip stores for the height: the step
    /// from the shown (clamped) height, or nil when it changes nothing (at
    /// a bound). A step that does move is an explicit choice, so it replaces
    /// a larger size saved on another display; a step that cannot move never
    /// writes the clamp over it.
    static func steppedHeightToStore(shown: CGFloat, direction: Int, screenMax: CGFloat) -> Double? {
        let next = stepped(height: shown, direction: direction, screenMax: screenMax)
        return abs(next - shown) < 1 ? nil : Double(next)
    }

    // MARK: - Width

    /// Room from the popover's left edge (at the menu bar icon) to the
    /// screen's right edge, less the margin.
    static func availableWidth(anchorX: CGFloat, screenMaxX: CGFloat) -> CGFloat {
        screenMaxX - screenMargin - anchorX
    }

    /// `width` within [`minWidth`, available]. When less than `minWidth` is
    /// available (an icon near the right edge), the minimum wins and
    /// `clampedOriginX` moves the popover left just enough to stay on screen.
    static func clampWidth(width: CGFloat, available: CGFloat) -> CGFloat {
        min(max(width, minWidth), max(minWidth, available))
    }

    /// Auto width: everything to the screen's right edge.
    static func autoWidth(available: CGFloat) -> CGFloat {
        max(minWidth, available)
    }

    /// A stored width, or nil when never dragged (0) or nonsense.
    static func storedWidth(_ raw: Double) -> CGFloat? {
        raw.isFinite && raw > 0 ? CGFloat(raw) : nil
    }

    /// The width to show. Nil `available` (screen not known yet) falls back
    /// to the stored width or `fallbackWidth`. Like the height, a stored
    /// width wider than this screen allows is clamped only for display.
    static func width(auto: Bool, stored: Double, available: CGFloat?) -> CGFloat {
        guard let available else { return storedWidth(stored).map { max($0, minWidth) } ?? fallbackWidth }
        if auto { return autoWidth(available: available) }
        return clampWidth(width: storedWidth(stored) ?? autoWidth(available: available), available: available)
    }

    /// What one VoiceOver step on the grip stores for the width; nil when
    /// the step changes nothing (see `steppedHeightToStore`).
    static func steppedWidthToStore(shown: CGFloat, direction: Int, available: CGFloat) -> Double? {
        let next = clampWidth(width: shown + CGFloat(direction.signum()) * step, available: available)
        return abs(next - shown) < 1 ? nil : Double(next)
    }

    /// The width to store when the Settings toggle turns auto width off, so
    /// the popover keeps a width of its own: nil when a dragged width is
    /// already stored (it comes back), else the current full width (or
    /// `fallbackWidth` before the screen is known).
    static func widthWhenAutoTurnsOff(stored: Double, available: CGFloat?) -> Double? {
        guard storedWidth(stored) == nil else { return nil }
        return Double(available.map { autoWidth(available: $0) } ?? fallbackWidth)
    }

    /// The width while dragging: start plus how far the pointer moved right.
    static func dragged(startWidth: CGFloat, translation: CGFloat, available: CGFloat) -> CGFloat {
        clampWidth(width: startWidth + translation, available: available)
    }

    /// Both axes at once (the corner grip). `translation.height` is
    /// positive down.
    static func dragged(start: CGSize, translation: CGSize, available: CGFloat, screenMax: CGFloat) -> CGSize {
        CGSize(width: dragged(startWidth: start.width, translation: translation.width, available: available),
               height: dragged(startHeight: start.height, translation: translation.height, screenMax: screenMax))
    }

    /// The popover's left edge: at the menu bar icon (`anchorX`), moved left
    /// only when `width` would cross the screen's right edge, and never past
    /// its left edge.
    static func clampedOriginX(anchorX: CGFloat, width: CGFloat, screenMinX: CGFloat, screenMaxX: CGFloat) -> CGFloat {
        max(screenMinX, min(anchorX, screenMaxX - screenMargin - width))
    }

    /// The panel's frame (AppKit coordinates, y up) for content of
    /// `contentSize`: its top edge at `top`, its left edge from
    /// `clampedOriginX`. Whole points (size rounded up so nothing is cut),
    /// so the window never rounds it and asks to be placed again.
    static func panelFrame(contentSize: CGSize, top: CGFloat, anchorX: CGFloat, visibleFrame: CGRect) -> CGRect {
        let width = contentSize.width.rounded(.up)
        let height = contentSize.height.rounded(.up)
        let x = clampedOriginX(anchorX: anchorX, width: width,
                               screenMinX: visibleFrame.minX, screenMaxX: visibleFrame.maxX).rounded(.down)
        return CGRect(x: x, y: top.rounded() - height, width: width, height: height)
    }

    /// The frame to give a panel now at `current`, or nil when it already
    /// has it. Applying the result and asking again returns nil (the frame
    /// is whole points), so the didResize observer that re-places the panel
    /// can never feed a resize loop.
    static func frameToApply(current: CGRect, contentSize: CGSize, top: CGFloat, anchorX: CGFloat,
                             visibleFrame: CGRect) -> CGRect? {
        let target = panelFrame(contentSize: contentSize, top: top, anchorX: anchorX, visibleFrame: visibleFrame)
        return framesMatch(current, target) ? nil : target
    }

    /// Within half a point on every edge, so rounding never loops a resize.
    static func framesMatch(_ a: CGRect, _ b: CGRect) -> Bool {
        abs(a.minX - b.minX) <= 0.5 && abs(a.minY - b.minY) <= 0.5
            && abs(a.width - b.width) <= 0.5 && abs(a.height - b.height) <= 0.5
    }

    // MARK: - Anchor

    /// Where this open's anchor (the popover's left edge) came from.
    enum AnchorSource: String, Equatable, Sendable {
        /// A status item window (the menu bar icon) read just now.
        case statusItem
        /// The last icon position read, kept while no status item window can
        /// be read. Never the popover's own frame.
        case remembered
    }

    /// A screen as a status item window or the anchor sees it.
    struct Screen: Equatable, Sendable {
        /// The screen's full frame and its frame less the menu bar and Dock.
        var frame: CGRect
        var visible: CGRect
    }

    /// A window that may be the menu bar icon's status item window, with the
    /// screen AppKit reports it on. AppKit keeps several `NSStatusBarWindow`s
    /// per status item, most of them off screen (seen: one on the menu bar
    /// and six at 0,-33 with no screen, all of them `isVisible`), so the
    /// class name alone does not find the icon.
    struct StatusItemCandidate: Equatable, Sendable {
        var frame: CGRect
        /// Nil when AppKit puts the window on no screen. Never filled in from
        /// the frame: an off-screen copy at 0,-33 sits exactly where an icon
        /// would on the menu bar of a display arranged below the main one.
        var screen: Screen?
    }

    /// The anchor the popover is sized and placed against, the full frame
    /// of its screen, and its source.
    struct ResolvedAnchor: Equatable {
        var screen: PopoverScreen
        var screenFrame: CGRect
        var source: AnchorSource

        /// What to keep for when no status item window can be read.
        var remembered: RememberedIcon { RememberedIcon(screenFrame: screenFrame, anchorX: screen.anchorX) }

        /// Whether the anchor moved to another screen (or appeared or went
        /// away). Compares the screen's full frame, not its visible frame,
        /// so a Dock or menu bar change on the same display is not a move.
        static func changedScreen(from before: ResolvedAnchor?, to after: ResolvedAnchor?) -> Bool {
            before?.screenFrame != after?.screenFrame
        }
    }

    /// The last icon position read: its left edge and its screen's full
    /// frame. Not the visible frame, which changes whenever the Dock is
    /// shown, hidden, resized or moved, or the menu bar's auto-hide setting
    /// changes; that is re-read from the current screen with this frame.
    struct RememberedIcon: Equatable, Sendable {
        var screenFrame: CGRect
        var anchorX: CGFloat
    }

    /// The largest status item window that still counts as the icon. The
    /// icon is about 38 x 33 pt (a notched display's menu bar is taller).
    static let maxIconWidth: CGFloat = 160
    static let maxIconHeight: CGFloat = 60

    /// Icon-sized, on a screen, and on that screen's menu bar: horizontally
    /// on the screen, its bottom edge within `maxIconHeight` of the screen's
    /// top and at most at the top (where an auto-hidden menu bar parks it).
    static func isIconWindow(_ candidate: StatusItemCandidate) -> Bool {
        guard let screen = candidate.screen?.frame else { return false }
        let frame = candidate.frame
        guard frame.width > 0, frame.width <= maxIconWidth, frame.height > 0, frame.height <= maxIconHeight
        else { return false }
        return frame.midX >= screen.minX && frame.midX <= screen.maxX
            && frame.minY >= screen.maxY - maxIconHeight && frame.minY <= screen.maxY + 0.5
    }

    /// The popover's anchor: the left edge of the status item window that is
    /// the icon (preferring the one on the popover's screen, when known),
    /// else `lastKnown` while a screen with its frame is still among
    /// `screens` (the current screens), with that screen's visible frame as
    /// it is now, else nil, in which case the panel is left where SwiftUI put
    /// it. The popover's own frame is never an input, so a panel at a wrong x
    /// can never become its own anchor, and a display that was unplugged,
    /// moved or changed resolution since the icon was last read never
    /// anchors it either. A Dock or menu bar change keeps the remembered icon.
    static func resolveAnchor(statusItems: [StatusItemCandidate], popoverScreen: CGRect?,
                              lastKnown: RememberedIcon?, screens: [Screen]) -> ResolvedAnchor? {
        let icons = statusItems.filter(isIconWindow)
        let onPopoverScreen = popoverScreen.flatMap { screen in icons.first { $0.screen?.frame == screen } }
        if let icon = onPopoverScreen ?? icons.first, let screen = icon.screen {
            return ResolvedAnchor(screen: PopoverScreen(visibleFrame: screen.visible, anchorX: icon.frame.minX),
                                  screenFrame: screen.frame, source: .statusItem)
        }
        guard let lastKnown, let screen = screens.first(where: { $0.frame == lastKnown.screenFrame })
        else { return nil }
        return ResolvedAnchor(screen: PopoverScreen(visibleFrame: screen.visible, anchorX: lastKnown.anchorX),
                              screenFrame: screen.frame, source: .remembered)
    }

    /// A rect for the log: `x,y wxh`, one decimal at most. Never traps: a
    /// non-finite or huge value (`CGRect.null` has infinite origin) is
    /// printed as text.
    static func describe(_ rect: CGRect) -> String {
        func number(_ value: CGFloat) -> String {
            let rounded = (value * 10).rounded() / 10
            guard rounded.isFinite, abs(rounded) < 1e9 else { return String(describing: Double(value)) }
            return rounded == rounded.rounded() ? String(Int(rounded)) : String(format: "%.1f", Double(rounded))
        }
        return "\(number(rect.minX)),\(number(rect.minY)) \(number(rect.width))x\(number(rect.height))"
    }

    // MARK: - Undoing someone else's move

    /// Why the placer is placing the panel.
    enum PlaceReason: String, Sendable {
        case open, key, content, resize, move, screen

        /// The panel's frame changed under the placer (a move or resize
        /// notification), so placing again may fight another placer. The
        /// others are the placer's own events and always place.
        var isExternalChange: Bool { self == .move || self == .resize }
    }

    /// Placements in a row that undid someone else's move or resize, each
    /// within `fightWindow` of the one before, and when the last one was.
    struct UndoState: Equatable, Sendable {
        var count = 0
        var last: TimeInterval?
    }

    /// After this many undos in a row the placer stops correcting moves and
    /// resizes until they stop for `fightWindow`, so a fight with another
    /// placer can never loop.
    static let maxUndone = 6
    static let fightWindow: TimeInterval = 1

    enum PlaceAction: Equatable, Sendable {
        /// Set the frame.
        case place
        /// Stop correcting this fight: log it once and leave the frame.
        case giveUp
        /// Still in a fight given up on: leave the frame, log nothing.
        case skip
    }

    struct PlaceDecision: Equatable, Sendable {
        var action: PlaceAction
        /// The panel is no longer at the frame the placer last set.
        var undid: Bool
        var state: UndoState
    }

    /// Whether to set the panel's frame, now at `current`, when the placer
    /// last set `applied` (nil before its first placement this open). Only
    /// a move or resize that undid the placer's frame counts towards giving
    /// up; a content size change, an open, becoming key or a screen change
    /// always places, so a given-up fight never stops the panel following
    /// the content's size. An undo more than `fightWindow` after the last
    /// one starts a new count, so occasional corrections over a long open
    /// never add up to giving up.
    static func placeDecision(current: CGRect, applied: CGRect?, reason: PlaceReason, state: UndoState,
                              now: TimeInterval) -> PlaceDecision {
        let undid = applied.map { !framesMatch(current, $0) } ?? false
        guard undid, reason.isExternalChange else { return PlaceDecision(action: .place, undid: undid, state: state) }
        let inFight = state.last.map { now - $0 <= fightWindow } ?? false
        let count = inFight ? state.count + 1 : 1
        let action: PlaceAction = count <= maxUndone ? .place : count == maxUndone + 1 ? .giveUp : .skip
        return PlaceDecision(action: action, undid: true, state: UndoState(count: count, last: now))
    }

    // MARK: - Finishing a drag

    /// What a finished grip drag stores.
    struct DragResult: Equatable {
        /// The new `heightKey` value, or nil to leave it (auto-expand on, or
        /// the height did not move).
        var height: Double?
        /// The new `widthKey` value, or nil to leave it (the width did not move).
        var width: Double?
        /// True when the drag changed the width, which turns auto width off.
        var turnsOffAutoWidth: Bool { width != nil }
    }

    /// Each axis counts as dragged once it moves by a point or more, so a
    /// straight-down drag leaves auto width on and a sideways drag leaves the
    /// stored height alone (the start height is the shown one, which may be
    /// a larger stored height clamped to this screen).
    static func dragResult(start: CGSize, end: CGSize, heightIsAuto: Bool) -> DragResult {
        DragResult(height: heightIsAuto || abs(end.height - start.height) < 1 ? nil : Double(end.height),
                   width: abs(end.width - start.width) >= 1 ? Double(end.width) : nil)
    }
}
