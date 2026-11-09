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

    @Test func resetSizeClearsTheSizeButKeepsAutoExpand() throws {
        let suite = "PopoverSizingTests-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
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

    @Test func moreBelowOnlyWhenContentPassesTheViewport() {
        #expect(PopoverSizing.hasMoreBelow(contentMaxY: 600, viewportHeight: 388))
        #expect(!PopoverSizing.hasMoreBelow(contentMaxY: 388, viewportHeight: 388))
        #expect(!PopoverSizing.hasMoreBelow(contentMaxY: 388.6, viewportHeight: 388))
        #expect(!PopoverSizing.hasMoreBelow(contentMaxY: 200, viewportHeight: 388))
    }
}
