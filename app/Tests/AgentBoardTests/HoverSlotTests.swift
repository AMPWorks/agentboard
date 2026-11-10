import AppKit
import SwiftUI
import Testing
@testable import AgentBoard

/// Hovering a row must not change its layout: the hover buttons sit in a
/// slot of fixed width that every row reserves, shown or not.
@Suite("Hover button slot")
struct HoverSlotTests {
    /// Sessions with and without Resume and a non-PR link: every way the
    /// dashboard's button set varies.
    static let variants: [Session] = [
        Fixtures.decoded("running", "running", alive: true),
        Fixtures.decoded("ended", "unconcluded"),
        Fixtures.decoded("linked", "unconcluded", extra: ["links": [
            ["kind": "ticket", "ref": "T-1", "url": "https://example.com/T-1"],
        ]]),
        Fixtures.decoded("pr-only", "running", alive: true, extra: ["links": [
            ["kind": "pr", "ref": "o/r#7", "url": "https://example.com/pr/7"],
        ]]),
    ]

    @Test func thePopoverSlotFitsCopyAndMarkDone() {
        let width = SessionRowLayout.hoverSlotWidth(compact: true)
        #expect(width == 2 * SessionRowLayout.hoverIconWidth + SessionRowLayout.hoverIconSpacing)
        #expect(width > 0)
        for session in Self.variants {
            #expect(SessionRowLayout.hoverButtons(for: session, compact: true) == [.copy, .markDone])
        }
    }

    @Test func theDashboardSlotFitsEveryButtonAnySessionCanShow() {
        let width = SessionRowLayout.hoverSlotWidth(compact: false)
        #expect(width == 5 * SessionRowLayout.hoverIconWidth + 4 * SessionRowLayout.hoverIconSpacing)
        #expect(width == SessionRowLayout.hoverSlotWidth(buttons: SessionRowLayout.HoverButton.allCases.count))
        for session in Self.variants {
            let buttons = SessionRowLayout.hoverButtons(for: session, compact: false)
            #expect(SessionRowLayout.hoverSlotWidth(buttons: buttons.count) <= width)
        }
    }

    @Test func theDashboardButtonsFollowTheSession() {
        let running = SessionRowLayout.hoverButtons(for: Self.variants[0], compact: false)
        #expect(running == [.copy, .markDone, .snooze])
        let ended = SessionRowLayout.hoverButtons(for: Self.variants[1], compact: false)
        #expect(ended == [.resume, .copy, .markDone, .snooze])
        let linked = SessionRowLayout.hoverButtons(for: Self.variants[2], compact: false)
        #expect(linked == SessionRowLayout.HoverButton.allCases)
        // A PR has its own button on the row, so it gets no open button.
        let prOnly = SessionRowLayout.hoverButtons(for: Self.variants[3], compact: false)
        #expect(prOnly == [.copy, .markDone, .snooze])
    }

    @Test func slotWidthCountsGapsOnlyBetweenButtons() {
        #expect(SessionRowLayout.hoverSlotWidth(buttons: 0) == 0)
        #expect(SessionRowLayout.hoverSlotWidth(buttons: 1) == SessionRowLayout.hoverIconWidth)
    }

    /// The property the row relies on: the slot is the same width hidden and
    /// shown, whatever its content's own width.
    @Test(arguments: [true, false])
    @MainActor func theSlotTakesItsWidthWhetherShownOrNot(visible: Bool) {
        for width in [SessionRowLayout.hoverSlotWidth(compact: true), SessionRowLayout.hoverSlotWidth(compact: false)] {
            let narrow = NSHostingView(rootView: HoverSlot(width: width, visible: visible) {
                Color.clear.frame(width: 10, height: 10)
            })
            #expect(narrow.fittingSize.width == width)
            let exact = NSHostingView(rootView: HoverSlot(width: width, visible: visible) {
                Color.clear.frame(width: width, height: 10)
            })
            #expect(exact.fittingSize.width == width)
            // Content wider than the slot: only a fixed `frame(width:)` keeps
            // the slot at `width` (a `minWidth:` frame passes the two cases
            // above but grows here). The slot does not clip, so content that
            // outgrows it draws over the text column instead of reflowing it,
            // which is why eachHoverGlyphFitsItsButton matters.
            let wide = NSHostingView(rootView: HoverSlot(width: width, visible: visible) {
                Color.clear.frame(width: width + 20, height: 10)
            })
            #expect(wide.fittingSize.width == width)
        }
    }

    /// The glyphs the hover buttons draw fit the frame each button gets, so
    /// they never overlap their neighbours, spill out of the slot, or draw
    /// outside the button's clickable shape. The symbols come from
    /// `HoverButton.systemImage`, the names the row draws.
    ///
    /// Assumes the default font: nothing between `SessionRowView` and the
    /// buttons sets `.font`, `.imageScale` or `.controlSize` (the dashboard's
    /// `.font(.callout)` is scoped to its counts bar). A font on the row would
    /// resize the real glyphs, so whoever styles the row must re-measure here
    /// at that font and revisit hoverIconWidth/hoverIconHeight.
    @Test(arguments: SessionRowLayout.HoverButton.allCases)
    @MainActor func eachHoverGlyphFitsItsButton(button: SessionRowLayout.HoverButton) {
        let glyph = NSHostingView(rootView: Image(systemName: button.systemImage))
        #expect(glyph.fittingSize.width > 0)
        #expect(glyph.fittingSize.width <= SessionRowLayout.hoverIconWidth)
        #expect(glyph.fittingSize.height <= SessionRowLayout.hoverIconHeight)
    }
}
