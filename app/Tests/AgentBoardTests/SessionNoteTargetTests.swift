import Testing
@testable import AgentBoard

private func note(_ id: Int, _ kind: String = "update", lastTs: String = "2026-10-15T10:00:00Z") -> [String: Any] {
    ["id": id, "kind": kind, "text": "note \(id)", "ts": lastTs, "last_ts": lastTs,
     "count": 1, "source": "stop"]
}

/// `notes` in the snapshot's order (newest first, as core lists them).
private func session(_ key: String = "a", notes: [[String: Any]], notesCount: Int? = nil) -> Session {
    Fixtures.decoded(key, "review", extra: ["notes": notes, "notes_count": notesCount ?? notes.count])
}

/// Which note a session row's update badge opens in the dashboard.
@Suite("Update badge target")
struct SessionNoteTargetTests {
    @Test func prefersTheNeedsYouNoteOverNewerUpdates() {
        let s = session(notes: [note(9), note(8, "error"), note(5, "needs_you"), note(3)])
        #expect(SessionNoteTarget.badgeTarget(for: s) == 5)
    }

    @Test func picksAnOlderNeedsYouNoteListedPastTheNewest20() {
        // core lists the newest 20 notes, then appends every older open
        // needs_you note (cli._listed_notes).
        let newest = (0..<20).map { note(100 - $0) }
        let s = session(notes: newest + [note(7, "needs_you")], notesCount: 30)
        #expect(SessionNoteTarget.badgeTarget(for: s) == 7)
    }

    @Test func picksTheFirstNeedsYouInListOrderWithoutResorting() {
        // Ids are not in time order; list order (newest first) decides.
        let s = session(notes: [note(2), note(4, "needs_you"), note(11, "needs_you")])
        #expect(SessionNoteTarget.badgeTarget(for: s) == 4)
    }

    @Test func picksTheFirstNoteWhenNoneNeedsYou() {
        let s = session(notes: [note(6, "error"), note(12), note(1)])
        #expect(SessionNoteTarget.badgeTarget(for: s) == 6)
    }

    @Test func isNilWithoutListedNotes() {
        #expect(SessionNoteTarget.badgeTarget(for: session(notes: [])) == nil)
        #expect(SessionNoteTarget.badgeTarget(for: session(notes: [], notesCount: 3)) == nil)
    }

    @Test func helpAndAccessibilityTextSayWhatAClickDoes() {
        #expect(SessionNoteTarget.badgeHelp(count: 1) == "1 open update; click to open it in the dashboard")
        #expect(SessionNoteTarget.badgeHelp(count: 3) == "3 open updates; click to open them in the dashboard")
        #expect(SessionNoteTarget.badgeAccessibilityLabel(count: 1) == "1 open update")
        #expect(SessionNoteTarget.badgeAccessibilityLabel(count: 2) == "2 open updates")
        #expect(SessionNoteTarget.badgeAccessibilityHint(count: 1) == "Opens it in the dashboard")
        #expect(SessionNoteTarget.badgeAccessibilityHint(count: 4) == "Opens them in the dashboard")
    }

    @Test func dashboardRowTextSaysTheClickScrollsTheDetail() {
        #expect(SessionNoteTarget.badgeHelp(count: 1, inDashboard: true)
            == "1 open update; click to show it in the session's detail")
        #expect(SessionNoteTarget.badgeHelp(count: 2, inDashboard: true)
            == "2 open updates; click to show them in the session's detail")
        #expect(SessionNoteTarget.badgeAccessibilityHint(count: 1, inDashboard: true)
            == "Shows it in the session's detail")
        #expect(SessionNoteTarget.badgeAccessibilityHint(count: 3, inDashboard: true)
            == "Shows them in the session's detail")
    }
}

/// When a selection asked for elsewhere clears the dashboard's filters.
@Suite("Pending dashboard selection")
struct PendingSelectionTests {
    private let known = ["a", "b", "c"]

    @Test func aListedSessionKeepsTheFilters() {
        #expect(!PendingSelection.clearsFilters(for: "b", listedKeys: ["a", "b"], knownKeys: known, sidebar: .all))
        #expect(!PendingSelection.clearsFilters(for: "a", listedKeys: ["a"], knownKeys: known,
                                                sidebar: .state(.review)))
        #expect(!PendingSelection.clearsFilters(for: "a", listedKeys: ["a"], knownKeys: known, sidebar: nil))
    }

    @Test func anUnlistedSessionClearsThem() {
        #expect(PendingSelection.clearsFilters(for: "c", listedKeys: ["a", "b"], knownKeys: known, sidebar: .all))
        #expect(PendingSelection.clearsFilters(for: "c", listedKeys: [], knownKeys: known,
                                               sidebar: .state(.needsYou)))
    }

    @Test func theDenialsViewAlwaysSwitchesBackToAll() {
        #expect(PendingSelection.clearsFilters(for: "a", listedKeys: ["a"], knownKeys: known, sidebar: .denials))
    }

    @Test func aKeyMissingFromTheSnapshotLeavesTheFiltersAlone() {
        // Clearing could not reveal a session the snapshot no longer has
        // (or hides while Show hidden is off).
        #expect(!PendingSelection.clearsFilters(for: "z", listedKeys: ["a"], knownKeys: known, sidebar: .all))
        #expect(!PendingSelection.clearsFilters(for: "z", listedKeys: [], knownKeys: known,
                                                sidebar: .state(.running)))
        #expect(!PendingSelection.clearsFilters(for: "z", listedKeys: [], knownKeys: known, sidebar: .denials))
    }
}
