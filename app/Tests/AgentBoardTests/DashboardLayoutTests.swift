import CoreGraphics
import Testing
@testable import AgentBoard

/// The dashboard's list column leaves a row's title, branch and repo real
/// text beside the reserved hover slot.
@Suite("Dashboard list column")
struct DashboardLayoutTests {
    @Test func theMinimumIsUnchanged() {
        #expect(DashboardLayout.listColumnMin == 280)
    }

    @Test func theIdealIsWiderThanTheOld340AndWithinTheBounds() {
        #expect(DashboardLayout.listColumnIdeal > 340)
        #expect(DashboardLayout.listColumnMin < DashboardLayout.listColumnIdeal)
        #expect(DashboardLayout.listColumnIdeal < DashboardLayout.listColumnMax)
    }

    @Test func theIdealLeavesTheTextMoreThanTheHoverSlot() {
        // The slot no longer takes the larger part of a row at the ideal width.
        let slot = SessionRowLayout.hoverSlotWidth(compact: false)
        #expect(DashboardLayout.listColumnIdeal - slot > 3 * slot)
    }
}
