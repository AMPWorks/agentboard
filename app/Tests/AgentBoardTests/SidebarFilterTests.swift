import Testing
@testable import AgentBoard

/// The dashboard sidebar's selection: "All" is a real tag, so clicking it
/// after a state row lists every state again.
@Suite("Dashboard sidebar filter")
struct SidebarFilterTests {
    @Test func allListsEveryState() {
        #expect(SidebarFilter.state(for: .all) == nil)
    }

    @Test func aStateRowNarrowsToThatState() {
        #expect(SidebarFilter.state(for: .state(.review)) == .review)
        #expect(SidebarFilter.state(for: .state(.needsYou)) == .needsYou)
    }

    @Test func anEmptySelectionListsEveryState() {
        #expect(SidebarFilter.state(for: nil) == nil)
    }

    @Test func allIsDistinctFromEveryStateRow() {
        let states: [SessionState] = [.needsYou, .interrupted, .review, .running, .unconcluded, .snoozed, .done]
        let tags = Set([SidebarFilter.all] + states.map(SidebarFilter.state))
        #expect(tags.count == states.count + 1)
    }
}
