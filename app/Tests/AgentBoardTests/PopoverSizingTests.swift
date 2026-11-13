import CoreGraphics
import Foundation
import Testing
@testable import AgentBoard

@Suite("Popover sizing")
struct PopoverSizingTests {
    // MARK: Defaults and stored values

    @Test func defaultsKeepTodaysHeightAndAutoWidth() {
        #expect(PopoverSizing.defaultHeight == 388)
        #expect(PopoverSizing.minHeight == 240)
        #expect(PopoverSizing.minWidth == 320)
        #expect(PopoverSizing.storedHeight(0) == 388)
        #expect(PopoverSizing.storedWidth(0) == nil)
    }

    @Test func storedValuesParseAndNonsenseFallsBack() {
        #expect(PopoverSizing.storedHeight(600) == 600)
        #expect(PopoverSizing.storedHeight(-5) == 388)
        #expect(PopoverSizing.storedHeight(.nan) == 388)
        #expect(PopoverSizing.storedHeight(.infinity) == 388)
        #expect(PopoverSizing.storedWidth(450) == 450)
        #expect(PopoverSizing.storedWidth(-1) == nil)
        #expect(PopoverSizing.storedWidth(.nan) == nil)
    }

    @Test func keysAreTheDocumentedOnesAndSettings() {
        #expect(PopoverSizing.heightKey == "popoverHeight")
        #expect(PopoverSizing.autoExpandKey == "popoverAutoExpand")
        #expect(PopoverSizing.widthKey == "popoverWidth")
        #expect(PopoverSizing.autoWidthKey == "popoverAutoWidth")
        for key in [PopoverSizing.heightKey, PopoverSizing.autoExpandKey, PopoverSizing.widthKey,
                    PopoverSizing.autoWidthKey] {
            #expect(SettingsKeys.all.contains(key))
        }
    }

    @Test func resetSizeClearsTheSizeButKeepsAutoExpand() {
        let defaults = TestDefaults().defaults
        defaults.set(700.0, forKey: PopoverSizing.heightKey)
        defaults.set(420.0, forKey: PopoverSizing.widthKey)
        defaults.set(false, forKey: PopoverSizing.autoWidthKey)
        defaults.set(true, forKey: PopoverSizing.autoExpandKey)
        PopoverSizing.resetSize(in: defaults)
        #expect(defaults.object(forKey: PopoverSizing.heightKey) == nil)
        #expect(defaults.object(forKey: PopoverSizing.widthKey) == nil)
        #expect(defaults.object(forKey: PopoverSizing.autoWidthKey) == nil)
        #expect(defaults.bool(forKey: PopoverSizing.autoExpandKey))
    }

    // MARK: Height

    @Test func maxHeightSubtractsChromeAndMargin() {
        #expect(PopoverSizing.maxHeight(screenVisibleHeight: 1000, chromeHeight: 120) == CGFloat(1000 - 120 - 8))
        #expect(PopoverSizing.maxHeight(screenVisibleHeight: 100, chromeHeight: 120) == 0)
    }

    @Test func clampBoundsHeightAndTheScreenWins() {
        #expect(PopoverSizing.clamp(height: 100, max: 800) == 240)
        #expect(PopoverSizing.clamp(height: 500, max: 800) == 500)
        #expect(PopoverSizing.clamp(height: 5000, max: 800) == 800)
        // A screen too short for the minimum: never run off it.
        #expect(PopoverSizing.clamp(height: 300, max: 200) == 200)
        #expect(PopoverSizing.clamp(height: 2000, screenVisibleHeight: 900, chromeHeight: 112) == CGFloat(900 - 112 - 8))
    }

    @Test func heightSavedOnATallerDisplayIsClampedOnlyForDisplay() {
        let stored = 1200.0
        let shown = PopoverSizing.clamp(height: PopoverSizing.storedHeight(stored),
                                        screenVisibleHeight: 800, chromeHeight: 110)
        #expect(shown == CGFloat(800 - 110 - 8))
        // Parsing never rewrites it, so the taller display gets it back.
        #expect(PopoverSizing.storedHeight(stored) == 1200)
        #expect(PopoverSizing.clamp(height: PopoverSizing.storedHeight(stored),
                                    screenVisibleHeight: 1400, chromeHeight: 110) == 1200)
    }

    @Test func autoHeightFitsContentFromNoneToMany() {
        #expect(PopoverSizing.autoHeight(contentBottom: nil, max: 900) == 240)
        #expect(PopoverSizing.autoHeight(contentBottom: 0, max: 900) == 240)
        #expect(PopoverSizing.autoHeight(contentBottom: 180, max: 900) == 240) // two short rows
        #expect(PopoverSizing.autoHeight(contentBottom: 2400, max: 900) == 900) // capped at the screen
    }

    @Test func autoHeightAddsTheListBottomPadding() {
        // The measured bottom is inside the list's padding; without it the
        // last row would sit flush against (or under) the footer divider.
        #expect(PopoverSizing.listPadding == 8)
        #expect(PopoverSizing.autoHeight(contentBottom: 610, max: 900) == 618)
        #expect(PopoverSizing.autoHeight(contentBottom: 610, bottomPadding: 0, max: 900) == 610)
        #expect(PopoverSizing.autoHeight(contentBottom: 895, max: 900) == 900)
    }

    /// Auto-expand off: the stored height caps the list and the list
    /// shrinks to its content, never below the minimum or past the screen.
    @Test func fittedHeightShrinksToContentUnderTheStoredHeight() {
        // Not measured yet (the first frame of an open): the stored height,
        // clamped, so a full board never opens at the minimum and jumps.
        #expect(PopoverSizing.fittedHeight(stored: 388, contentBottom: nil, max: 900) == 388)
        #expect(PopoverSizing.fittedHeight(stored: 1200, contentBottom: nil, max: 900) == 900)
        #expect(PopoverSizing.fittedHeight(stored: 100, contentBottom: nil, max: 900) == 240)
        // Measured with nothing rendered: the minimum.
        #expect(PopoverSizing.fittedHeight(stored: 388, contentBottom: 0, max: 900) == 240)
        // No session (an empty-board note) or one row: still the minimum.
        #expect(PopoverSizing.fittedHeight(stored: 388, contentBottom: 96, max: 900) == 240)
        // Content between the minimum and the stored height: content plus
        // the list's bottom padding.
        #expect(PopoverSizing.fittedHeight(stored: 388, contentBottom: 300, max: 900) == 308)
        #expect(PopoverSizing.fittedHeight(stored: 388, contentBottom: 300, bottomPadding: 0, max: 900) == 300)
        // Many sessions: the stored height, as before.
        #expect(PopoverSizing.fittedHeight(stored: 388, contentBottom: 1200, max: 900) == 388)
        #expect(PopoverSizing.fittedHeight(stored: 600, contentBottom: 500, max: 900) == 508)
        // A stored height under the content wins; the screen wins over both.
        #expect(PopoverSizing.fittedHeight(stored: 300, contentBottom: 500, max: 900) == 300)
        #expect(PopoverSizing.fittedHeight(stored: 1200, contentBottom: 2400, max: 900) == 900)
        #expect(PopoverSizing.fittedHeight(stored: 1200, contentBottom: 1000, max: 700) == 700)
        // A stored height under the minimum still shows the minimum.
        #expect(PopoverSizing.fittedHeight(stored: 100, contentBottom: 500, max: 900) == 240)
    }

    @Test func autoHeightIgnoresSmallWobbles() {
        #expect(PopoverSizing.settledAutoHeight(previous: nil, measured: 300) == 300)
        #expect(PopoverSizing.settledAutoHeight(previous: 300, measured: 302.5) == 300)
        #expect(PopoverSizing.settledAutoHeight(previous: 300, measured: 297) == 300)
        #expect(PopoverSizing.settledAutoHeight(previous: 300, measured: 304) == 304)
        #expect(PopoverSizing.settledAutoHeight(previous: 300, measured: 250) == 250)
    }

    @Test func draggedAndSteppedHeightStayInBounds() {
        #expect(PopoverSizing.dragged(startHeight: 388, translation: 100, screenMax: 900) == 488)
        #expect(PopoverSizing.dragged(startHeight: 388, translation: -300, screenMax: 900) == 240)
        #expect(PopoverSizing.dragged(startHeight: 388, translation: 900, screenMax: 900) == 900)
        #expect(PopoverSizing.stepped(height: 388, direction: 1, screenMax: 900) == 428)
        #expect(PopoverSizing.stepped(height: 388, direction: -1, screenMax: 900) == 348)
        #expect(PopoverSizing.stepped(height: 250, direction: -1, screenMax: 900) == 240)
        #expect(PopoverSizing.stepped(height: 880, direction: 3, screenMax: 900) == 900)
    }

    @Test func steppingAtABoundStoresNothing() {
        #expect(PopoverSizing.steppedHeightToStore(shown: 388, direction: 1, screenMax: 900) == 428)
        #expect(PopoverSizing.steppedHeightToStore(shown: 388, direction: -1, screenMax: 900) == 348)
        // At the screen cap (a larger stored height clamped for display):
        // stepping up cannot move, so the stored height is not overwritten.
        #expect(PopoverSizing.steppedHeightToStore(shown: 900, direction: 1, screenMax: 900) == nil)
        #expect(PopoverSizing.steppedHeightToStore(shown: 240, direction: -1, screenMax: 900) == nil)
        #expect(PopoverSizing.steppedWidthToStore(shown: 534, direction: -1, available: 534) == 494)
        #expect(PopoverSizing.steppedWidthToStore(shown: 534, direction: 1, available: 534) == nil)
        #expect(PopoverSizing.steppedWidthToStore(shown: 320, direction: -1, available: 534) == nil)
    }

    @Test func onlyNeedsYouReviewAndRunningAreActive() {
        let active = PopoverSections.Kind.allCases.filter(PopoverSizing.isActive)
        #expect(active == [.needsYou, .review, .running])
    }

    // MARK: Width

    @Test func availableWidthRunsFromTheIconToTheRightEdge() {
        #expect(PopoverSizing.availableWidth(anchorX: 1186, screenMaxX: 1728) == CGFloat(1728 - 8 - 1186))
    }

    @Test func clampWidthBounds() {
        #expect(PopoverSizing.clampWidth(width: 200, available: 600) == 320)
        #expect(PopoverSizing.clampWidth(width: 450, available: 600) == 450)
        #expect(PopoverSizing.clampWidth(width: 900, available: 600) == 600)
        // Less than the minimum available: the minimum wins.
        #expect(PopoverSizing.clampWidth(width: 400, available: 150) == 320)
    }

    @Test func autoWidthIsTheAvailableWidth() {
        #expect(PopoverSizing.autoWidth(available: 534) == 534)
        #expect(PopoverSizing.autoWidth(available: 100) == 320)
        #expect(PopoverSizing.width(auto: true, stored: 400, available: 534) == 534)
    }

    @Test func manualWidthIsClampedOnlyForDisplay() {
        #expect(PopoverSizing.width(auto: false, stored: 420, available: 534) == 420)
        // Saved on a wider display.
        #expect(PopoverSizing.width(auto: false, stored: 900, available: 534) == 534)
        #expect(PopoverSizing.width(auto: false, stored: 900, available: 1200) == 900)
        // Auto off but never dragged: the full width.
        #expect(PopoverSizing.width(auto: false, stored: 0, available: 534) == 534)
    }

    @Test func turningAutoWidthOffKeepsAWidth() {
        // Never dragged: the current full width is stored, so off is visible
        // as "stays this wide" instead of doing nothing until a drag.
        #expect(PopoverSizing.widthWhenAutoTurnsOff(stored: 0, available: 534) == 534)
        #expect(PopoverSizing.widthWhenAutoTurnsOff(stored: 0, available: 100) == 320)
        #expect(PopoverSizing.widthWhenAutoTurnsOff(stored: 0, available: nil) == 380)
        // A width dragged before comes back instead.
        #expect(PopoverSizing.widthWhenAutoTurnsOff(stored: 420, available: 534) == nil)
    }

    @Test func widthBeforeTheScreenIsKnown() {
        #expect(PopoverSizing.width(auto: true, stored: 0, available: nil) == 380)
        #expect(PopoverSizing.width(auto: false, stored: 450, available: nil) == 450)
        #expect(PopoverSizing.width(auto: false, stored: 100, available: nil) == 320)
    }

    @Test func cornerDragMovesBothAxes() {
        let start = CGSize(width: 534, height: 388)
        let size = PopoverSizing.dragged(start: start, translation: CGSize(width: -100, height: 200),
                                         available: 534, screenMax: 900)
        #expect(size == CGSize(width: 434, height: 588))
        let past = PopoverSizing.dragged(start: start, translation: CGSize(width: 400, height: -400),
                                         available: 534, screenMax: 900)
        #expect(past == CGSize(width: 534, height: 240))
    }

    @Test func dragTurnsAutoWidthOffAndStoresTheWidth() {
        let start = CGSize(width: 534, height: 388)
        let narrower = PopoverSizing.dragResult(start: start, end: CGSize(width: 434, height: 588), heightIsAuto: false)
        #expect(narrower == PopoverSizing.DragResult(height: 588, width: 434))
        #expect(narrower.turnsOffAutoWidth)
        // Straight down: the width did not move, so auto width stays on.
        let down = PopoverSizing.dragResult(start: start, end: CGSize(width: 534.4, height: 500), heightIsAuto: false)
        #expect(down == PopoverSizing.DragResult(height: 500, width: nil))
        #expect(!down.turnsOffAutoWidth)
        // Auto-expand owns the height: only the width is stored.
        let autoHeight = PopoverSizing.dragResult(start: start, end: CGSize(width: 400, height: 388), heightIsAuto: true)
        #expect(autoHeight == PopoverSizing.DragResult(height: nil, width: 400))
    }

    @Test func sidewaysDragLeavesTheStoredHeight() {
        // The start height is the shown one, possibly a larger stored height
        // clamped to this screen; a width-only drag must not store it.
        let start = CGSize(width: 534, height: 388)
        let sideways = PopoverSizing.dragResult(start: start, end: CGSize(width: 434, height: 388), heightIsAuto: false)
        #expect(sideways == PopoverSizing.DragResult(height: nil, width: 434))
        let wobble = PopoverSizing.dragResult(start: start, end: CGSize(width: 434, height: 388.6), heightIsAuto: false)
        #expect(wobble.height == nil)
        let still = PopoverSizing.dragResult(start: start, end: start, heightIsAuto: false)
        #expect(still == PopoverSizing.DragResult(height: nil, width: nil))
    }

    // MARK: Placement

    @Test func originStaysAtTheIconUnlessTheRightEdgeIsCrossed() {
        #expect(PopoverSizing.clampedOriginX(anchorX: 1186, width: 534, screenMinX: 0, screenMaxX: 1728) == 1186)
        // Icon near the right edge: shift left just enough.
        #expect(PopoverSizing.clampedOriginX(anchorX: 1600, width: 320, screenMinX: 0, screenMaxX: 1728)
                == CGFloat(1728 - 8 - 320))
        // Never past the left edge (a secondary display left of the main one).
        #expect(PopoverSizing.clampedOriginX(anchorX: -1500, width: 2000, screenMinX: -1440, screenMaxX: 0) == -1440)
    }

    @Test func clampedOriginKeepsTheRightEdgeOnScreen() {
        for anchor in stride(from: CGFloat(0), through: 1720, by: 40) {
            for width in [CGFloat(320), 380, 534, 900] {
                let x = PopoverSizing.clampedOriginX(anchorX: anchor, width: width, screenMinX: 0, screenMaxX: 1728)
                #expect(x + width <= 1728 - 8 + 0.001)
                #expect(x <= anchor)
            }
        }
    }

    @Test func panelFrameKeepsTheTopAndLeftEdges() {
        let visible = CGRect(x: 0, y: 0, width: 1728, height: 1080)
        let frame = PopoverSizing.panelFrame(contentSize: CGSize(width: 434, height: 701), top: 1082,
                                             anchorX: 1186, visibleFrame: visible)
        #expect(frame == CGRect(x: 1186, y: 1082 - 701, width: 434, height: 701))
        #expect(PopoverSizing.framesMatch(frame, frame.offsetBy(dx: 0.4, dy: -0.4)))
        #expect(!PopoverSizing.framesMatch(frame, frame.insetBy(dx: 1, dy: 0)))
        // Fractional layout sizes become whole points, rounded up.
        let fractional = PopoverSizing.panelFrame(contentSize: CGSize(width: 497.67, height: 507.33), top: 1082,
                                                  anchorX: 1219.4, visibleFrame: visible)
        #expect(fractional == CGRect(x: 1219, y: 1082 - 508, width: 498, height: 508))
    }

    /// The placer re-places the panel on every didResize, so placing must be
    /// a fixed point: once the panel has the frame, asking again sets nothing.
    @Test func frameToApplyIsIdempotent() {
        let visible = CGRect(x: 0, y: 0, width: 1728, height: 1080)
        for size in [CGSize(width: 434, height: 701), CGSize(width: 497.67, height: 507.33),
                     CGSize(width: 2000, height: 300.5)] {
            let start = CGRect(x: 1000, y: 400, width: 380, height: 500)
            guard let applied = PopoverSizing.frameToApply(current: start, contentSize: size, top: 1082.3,
                                                           anchorX: 1219.4, visibleFrame: visible) else {
                Issue.record("expected a frame for \(size)")
                continue
            }
            #expect(PopoverSizing.frameToApply(current: applied, contentSize: size, top: 1082.3,
                                               anchorX: 1219.4, visibleFrame: visible) == nil)
            // The window may report the frame back a hair off; still nothing to set.
            #expect(PopoverSizing.frameToApply(current: applied.offsetBy(dx: 0.25, dy: -0.25), contentSize: size,
                                               top: 1082.3, anchorX: 1219.4, visibleFrame: visible) == nil)
        }
    }

    /// MenuBarExtra can move the panel on reopen without resizing it; the
    /// didMove observer re-places it, so a same-size move must yield a frame.
    @Test func sameSizeMoveIsCorrected() {
        let visible = CGRect(x: 0, y: 54, width: 1728, height: 1030)
        let size = CGSize(width: 496, height: 600)
        let placed = PopoverSizing.panelFrame(contentSize: size, top: 1084, anchorX: 1224, visibleFrame: visible)
        let moved = placed.offsetBy(dx: -990, dy: 0)
        #expect(PopoverSizing.frameToApply(current: moved, contentSize: size, top: 1084, anchorX: 1224,
                                           visibleFrame: visible) == placed)
        #expect(PopoverSizing.frameToApply(current: placed, contentSize: size, top: 1084, anchorX: 1224,
                                           visibleFrame: visible) == nil)
    }

    // MARK: Anchor

    /// The geometry seen on a notched 1728 x 1117 display: the icon's status
    /// item window on the menu bar, plus AppKit's off-screen copies.
    private static let screenFrame = CGRect(x: 0, y: 0, width: 1728, height: 1117)
    private static let screenVisible = CGRect(x: 0, y: 54, width: 1728, height: 1030)
    private static let mainScreen = PopoverSizing.Screen(frame: screenFrame, visible: screenVisible)
    private static let icon = PopoverSizing.StatusItemCandidate(
        frame: CGRect(x: 1224, y: 1084, width: 38, height: 33), screen: mainScreen)
    /// One of the copies as AppKit reports it: at 0,-33 on no screen.
    private static let offScreenCopy = PopoverSizing.StatusItemCandidate(
        frame: CGRect(x: 0, y: -33, width: 38, height: 33), screen: nil)

    private static func resolve(_ items: [PopoverSizing.StatusItemCandidate], popoverScreen: CGRect?,
                                lastKnown: PopoverSizing.RememberedIcon? = nil,
                                screens: [PopoverSizing.Screen] = [mainScreen]) -> PopoverSizing.ResolvedAnchor? {
        PopoverSizing.resolveAnchor(statusItems: items, popoverScreen: popoverScreen, lastKnown: lastKnown,
                                    screens: screens)
    }

    /// A candidate on the main screen with the given frame.
    private static func onMain(_ frame: CGRect) -> PopoverSizing.StatusItemCandidate {
        PopoverSizing.StatusItemCandidate(frame: frame, screen: mainScreen)
    }

    @Test func oneStatusItemGivesItsLeftEdge() {
        #expect(Self.resolve([Self.icon], popoverScreen: Self.screenFrame) == PopoverSizing.ResolvedAnchor(
            screen: PopoverScreen(visibleFrame: Self.screenVisible, anchorX: 1224), screenFrame: Self.screenFrame,
            source: .statusItem))
    }

    @Test func noStatusItemUsesTheRememberedIcon() {
        let remembered = PopoverSizing.RememberedIcon(screenFrame: Self.screenFrame, anchorX: 1100)
        #expect(Self.resolve([], popoverScreen: Self.screenFrame, lastKnown: remembered)
                == PopoverSizing.ResolvedAnchor(screen: PopoverScreen(visibleFrame: Self.screenVisible, anchorX: 1100),
                                                screenFrame: Self.screenFrame, source: .remembered))
    }

    /// The remembered icon is what a status item read gives, and survives a
    /// Dock or menu bar change (same screen frame, new visible frame) with
    /// the visible frame taken from the screen as it is now.
    @Test func aRememberedIconSurvivesADockChange() {
        let read = Self.resolve([Self.icon], popoverScreen: nil)
        #expect(read?.remembered == PopoverSizing.RememberedIcon(screenFrame: Self.screenFrame, anchorX: 1224))
        let dockHidden = CGRect(x: 0, y: 0, width: 1728, height: 1084)
        let dockOnLeft = CGRect(x: 80, y: 0, width: 1648, height: 1084)
        for visible in [dockHidden, dockOnLeft] {
            let now = PopoverSizing.Screen(frame: Self.screenFrame, visible: visible)
            #expect(Self.resolve([Self.offScreenCopy], popoverScreen: nil, lastKnown: read?.remembered,
                                 screens: [now])
                    == PopoverSizing.ResolvedAnchor(screen: PopoverScreen(visibleFrame: visible, anchorX: 1224),
                                                    screenFrame: Self.screenFrame, source: .remembered))
        }
    }

    /// `reanchor()` forgets the top edge only when the anchor's display
    /// changes: a Dock change (same screen frame, new visible frame) or a
    /// switch between status item and remembered source on the same display
    /// is not a move.
    @Test func aDockChangeIsNotAScreenChange() {
        let read = Self.resolve([Self.icon], popoverScreen: Self.screenFrame)
        let dockHidden = PopoverSizing.Screen(frame: Self.screenFrame,
                                              visible: CGRect(x: 0, y: 0, width: 1728, height: 1084))
        let afterDock = Self.resolve([], popoverScreen: nil, lastKnown: read?.remembered, screens: [dockHidden])
        #expect(afterDock != nil && afterDock != read)
        #expect(!PopoverSizing.ResolvedAnchor.changedScreen(from: read, to: afterDock))
        #expect(!PopoverSizing.ResolvedAnchor.changedScreen(from: read, to: read))
        #expect(!PopoverSizing.ResolvedAnchor.changedScreen(from: nil, to: nil))
        let other = PopoverSizing.Screen(frame: CGRect(x: 1728, y: 0, width: 2560, height: 1440),
                                         visible: CGRect(x: 1728, y: 0, width: 2560, height: 1415))
        let otherIcon = PopoverSizing.StatusItemCandidate(
            frame: CGRect(x: 3900, y: 1415, width: 38, height: 25), screen: other)
        let onOther = Self.resolve([otherIcon], popoverScreen: other.frame, screens: [Self.mainScreen, other])
        #expect(onOther?.screenFrame == other.frame)
        #expect(PopoverSizing.ResolvedAnchor.changedScreen(from: read, to: onOther))
        #expect(PopoverSizing.ResolvedAnchor.changedScreen(from: nil, to: read))
        #expect(PopoverSizing.ResolvedAnchor.changedScreen(from: read, to: nil))
    }

    /// A remembered icon on a display that is gone (unplugged, rearranged or
    /// changed resolution since) is dropped rather than placing the panel
    /// against a frame that no longer exists.
    @Test func aRememberedIconOffTheCurrentScreensIsDropped() {
        let gone = PopoverSizing.RememberedIcon(screenFrame: CGRect(x: 1728, y: 0, width: 2560, height: 1440),
                                                anchorX: 3800)
        #expect(Self.resolve([], popoverScreen: nil, lastKnown: gone) == nil)
        #expect(Self.resolve([], popoverScreen: nil, lastKnown: gone, screens: []) == nil)
        // The main display changed resolution: same origin, new size.
        let resized = PopoverSizing.RememberedIcon(screenFrame: CGRect(x: 0, y: 0, width: 1512, height: 982),
                                                   anchorX: 1000)
        #expect(Self.resolve([Self.offScreenCopy], popoverScreen: Self.screenFrame, lastKnown: resized) == nil)
        // A status item read now still wins over it.
        #expect(Self.resolve([Self.icon], popoverScreen: nil, lastKnown: gone)?.source == .statusItem)
    }

    /// Nothing known: nil, so the panel stays where SwiftUI put it rather
    /// than anchoring to itself.
    @Test func noStatusItemAndNothingKnownGivesNoAnchor() {
        #expect(Self.resolve([], popoverScreen: Self.screenFrame) == nil)
        #expect(Self.resolve([], popoverScreen: nil) == nil)
    }

    @Test func theStatusItemOnThePopoversScreenWins() {
        // A second display to the left, its menu bar 25 pt tall.
        let leftFrame = CGRect(x: -1920, y: 0, width: 1920, height: 1080)
        let leftVisible = CGRect(x: -1920, y: 0, width: 1920, height: 1055)
        let leftIcon = PopoverSizing.StatusItemCandidate(
            frame: CGRect(x: -400, y: 1055, width: 38, height: 25),
            screen: PopoverSizing.Screen(frame: leftFrame, visible: leftVisible))
        let onLeft = Self.resolve([Self.icon, leftIcon], popoverScreen: leftFrame)
        #expect(onLeft?.screen == PopoverScreen(visibleFrame: leftVisible, anchorX: -400))
        let onMain = Self.resolve([leftIcon, Self.icon], popoverScreen: Self.screenFrame)
        #expect(onMain?.screen == PopoverScreen(visibleFrame: Self.screenVisible, anchorX: 1224))
    }

    @Test func nonIconCandidatesAreIgnored() {
        let remembered = PopoverSizing.RememberedIcon(screenFrame: Self.screenFrame, anchorX: 1100)
        // As wide as the menu bar, below the menu bar, and off the screen.
        let wide = Self.onMain(CGRect(x: 0, y: 1084, width: 1728, height: 33))
        let low = Self.onMain(CGRect(x: 1224, y: 400, width: 38, height: 33))
        let offScreen = Self.onMain(CGRect(x: 0, y: -33, width: 38, height: 33))
        for candidate in [wide, low, offScreen, Self.offScreenCopy] {
            #expect(!PopoverSizing.isIconWindow(candidate))
        }
        #expect(PopoverSizing.isIconWindow(Self.icon))
        #expect(Self.resolve([wide, low, offScreen, Self.offScreenCopy], popoverScreen: Self.screenFrame,
                             lastKnown: remembered)?.source == .remembered)
        // The icon is found whatever order the copies come in.
        #expect(Self.resolve([Self.offScreenCopy, offScreen, wide, Self.icon], popoverScreen: nil,
                             lastKnown: remembered)?.screen.anchorX == 1224)
    }

    /// The edges `isIconWindow` encodes: a menu bar auto-hidden parks the
    /// icon with its bottom at the screen's top (half a point of slack), the
    /// bottom may sit up to `maxIconHeight` below the top, and the icon is
    /// at most `maxIconWidth` wide.
    @Test func iconWindowBoundaries() {
        let top = Self.screenFrame.maxY
        let parked = Self.onMain(CGRect(x: 1224, y: top, width: 38, height: 33))
        let aboveParked = Self.onMain(CGRect(x: 1224, y: top + 1, width: 38, height: 33))
        let lowest = Self.onMain(CGRect(x: 1224, y: top - PopoverSizing.maxIconHeight, width: 38, height: 33))
        let tooLow = Self.onMain(CGRect(x: 1224, y: top - PopoverSizing.maxIconHeight - 1, width: 38, height: 33))
        let widest = Self.onMain(CGRect(x: 1224, y: 1084, width: PopoverSizing.maxIconWidth, height: 33))
        let tooWide = Self.onMain(CGRect(x: 1224, y: 1084, width: PopoverSizing.maxIconWidth + 1, height: 33))
        let tallest = Self.onMain(CGRect(x: 1224, y: top - PopoverSizing.maxIconHeight, width: 38,
                                         height: PopoverSizing.maxIconHeight))
        let tooTall = Self.onMain(CGRect(x: 1224, y: top - PopoverSizing.maxIconHeight, width: 38,
                                         height: PopoverSizing.maxIconHeight + 1))
        for accepted in [parked, lowest, widest, tallest] {
            #expect(PopoverSizing.isIconWindow(accepted), "\(accepted.frame)")
        }
        for rejected in [aboveParked, tooLow, tooWide, tooTall] {
            #expect(!PopoverSizing.isIconWindow(rejected), "\(rejected.frame)")
        }
    }

    /// A display arranged below the main one: its top edge is y 0, so an
    /// off-screen copy at 0,-33 sits exactly where an icon on its menu bar
    /// would. AppKit puts the copies on no screen, and the placer never gives
    /// them one from their frame, so they never count, even when the popover
    /// is on that display.
    @Test func offScreenCopiesNeverMatchADisplayBelow() {
        let belowFrame = CGRect(x: 0, y: -1080, width: 1920, height: 1080)
        let belowVisible = CGRect(x: 0, y: -1080, width: 1920, height: 1055)
        let copies = Array(repeating: Self.offScreenCopy, count: 6)
        let screens = [Self.mainScreen, PopoverSizing.Screen(frame: belowFrame, visible: belowVisible)]
        #expect(Self.resolve(copies + [Self.icon], popoverScreen: belowFrame, screens: screens)?.screen
                == PopoverScreen(visibleFrame: Self.screenVisible, anchorX: 1224))
        #expect(Self.resolve(copies, popoverScreen: belowFrame, screens: screens) == nil)
        #expect(Self.resolve(copies, popoverScreen: nil, screens: screens) == nil)
    }

    /// Settings asks with no popover window: the icon's anchor all the same.
    @Test func settingsWithoutAPopoverStillGetsTheIcon() {
        #expect(Self.resolve([Self.icon], popoverScreen: nil) == PopoverSizing.ResolvedAnchor(
            screen: PopoverScreen(visibleFrame: Self.screenVisible, anchorX: 1224), screenFrame: Self.screenFrame,
            source: .statusItem))
    }

    @Test func describeIsCompact() {
        #expect(PopoverSizing.describe(CGRect(x: 1224, y: 1084, width: 38, height: 33)) == "1224,1084 38x33")
        #expect(PopoverSizing.describe(CGRect(x: -0.24, y: 10.06, width: 497.67, height: 0)) == "-0.2,10.1 497.7x0")
    }

    /// A log helper must never trap: `CGRect.null` has an infinite origin
    /// and `Int(_:)` traps on that, or on a value past `Int.max`.
    @Test func describeNeverTrapsOnNonFiniteRects() {
        #expect(PopoverSizing.describe(.null) == "inf,inf 0x0")
        #expect(PopoverSizing.describe(.infinite).contains("e+"))
        #expect(PopoverSizing.describe(CGRect(x: CGFloat.nan, y: 1e300, width: 1, height: 2)) == "nan,1e+300 1x2")
    }

    // MARK: Undoing someone else's move

    private static let placed = CGRect(x: 1224, y: 484, width: 496, height: 600)
    private static let movedAway = placed.offsetBy(dx: -990, dy: 0)

    private static func decide(_ reason: PopoverSizing.PlaceReason, current: CGRect = movedAway,
                               applied: CGRect? = placed, state: PopoverSizing.UndoState = .init(),
                               now: TimeInterval = 100) -> PopoverSizing.PlaceDecision {
        PopoverSizing.placeDecision(current: current, applied: applied, reason: reason, state: state, now: now)
    }

    @Test func theFirstPlacementIsNotAnUndo() {
        for reason in [PopoverSizing.PlaceReason.open, .key, .content, .move, .resize, .screen] {
            #expect(Self.decide(reason, applied: nil)
                    == PopoverSizing.PlaceDecision(action: .place, undid: false, state: .init()))
        }
    }

    @Test func aFrameStillAtTheAppliedOneIsNotAnUndo() {
        // The content changed size, so the panel needs a new frame, but
        // nothing else moved it.
        #expect(Self.decide(.move, current: Self.placed)
                == PopoverSizing.PlaceDecision(action: .place, undid: false, state: .init()))
    }

    /// Six undone moves in a row are corrected; the seventh gives up (logged
    /// once) and later ones in the same fight are skipped.
    @Test func theSeventhUndoneMoveInARowGivesUp() {
        var state = PopoverSizing.UndoState()
        var actions: [PopoverSizing.PlaceAction] = []
        for step in 0..<9 {
            let decision = Self.decide(step.isMultiple(of: 2) ? .move : .resize, state: state,
                                       now: 100 + Double(step) * 0.1)
            #expect(decision.undid)
            actions.append(decision.action)
            state = decision.state
        }
        #expect(actions == Array(repeating: .place, count: PopoverSizing.maxUndone) + [.giveUp, .skip, .skip])
    }

    /// Giving up stops only the move/resize fight: the panel still follows
    /// the content's size (and opens, key and screen changes still place).
    @Test func aContentChangeStillPlacesAfterGivingUp() {
        let givenUp = PopoverSizing.UndoState(count: PopoverSizing.maxUndone + 1, last: 100)
        for reason in [PopoverSizing.PlaceReason.content, .open, .key, .screen] {
            let decision = Self.decide(reason, state: givenUp, now: 100.2)
            #expect(decision == PopoverSizing.PlaceDecision(action: .place, undid: true, state: givenUp))
        }
        #expect(Self.decide(.move, state: givenUp, now: 100.2).action == .skip)
    }

    /// Undos further apart than `fightWindow` are separate corrections, not a
    /// fight, so a long open never accumulates them into giving up; a fight
    /// given up on is corrected again once it has stopped.
    @Test func spacedOutUndosNeverGiveUp() {
        var state = PopoverSizing.UndoState()
        for step in 0..<20 {
            let decision = Self.decide(.move, state: state, now: 100 + Double(step) * (PopoverSizing.fightWindow + 1))
            #expect(decision.action == .place)
            #expect(decision.state.count == 1)
            state = decision.state
        }
        let givenUp = PopoverSizing.UndoState(count: PopoverSizing.maxUndone + 3, last: 100)
        #expect(Self.decide(.move, state: givenUp, now: 100 + PopoverSizing.fightWindow + 0.5)
                == PopoverSizing.PlaceDecision(action: .place, undid: true,
                                               state: .init(count: 1, last: 100 + PopoverSizing.fightWindow + 0.5)))
    }

    /// The `fightWindow` edge: an undo exactly `fightWindow` after the last
    /// continues the count, one just past it starts a new count.
    @Test func anUndoExactlyAFightWindowLaterStillCounts() {
        let state = PopoverSizing.UndoState(count: 3, last: 100)
        let atEdge = 100 + PopoverSizing.fightWindow
        #expect(Self.decide(.move, state: state, now: atEdge)
                == PopoverSizing.PlaceDecision(action: .place, undid: true, state: .init(count: 4, last: atEdge)))
        let pastEdge = atEdge + 0.001
        #expect(Self.decide(.move, state: state, now: pastEdge)
                == PopoverSizing.PlaceDecision(action: .place, undid: true, state: .init(count: 1, last: pastEdge)))
        // At the edge a fight already at the limit gives up rather than resetting.
        let atLimit = PopoverSizing.UndoState(count: PopoverSizing.maxUndone, last: 100)
        #expect(Self.decide(.resize, state: atLimit, now: atEdge).action == .giveUp)
        #expect(Self.decide(.resize, state: atLimit, now: pastEdge).action == .place)
    }

    @Test func moreBelowOnlyWhenContentPassesTheViewport() {
        #expect(PopoverSizing.hasMoreBelow(contentMaxY: 600, viewportHeight: 388))
        #expect(!PopoverSizing.hasMoreBelow(contentMaxY: 388, viewportHeight: 388))
        #expect(!PopoverSizing.hasMoreBelow(contentMaxY: 388.6, viewportHeight: 388))
        #expect(!PopoverSizing.hasMoreBelow(contentMaxY: 200, viewportHeight: 388))
    }
}
