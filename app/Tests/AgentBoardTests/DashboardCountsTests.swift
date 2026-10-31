import AppKit
import Testing
@testable import AgentBoard

/// Sidebar badge counts (`FilteredCounts` over `SessionSearch.filter`, the
/// path `DashboardView` runs) and the search against the bundled fixture,
/// which the dashboard and popover show in fixture mode.
@Suite("Dashboard counts and fixture search")
struct DashboardCountsTests {
    private static let fixture: Snapshot = {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/snapshot.json")
        let data = try! Data(contentsOf: url)
        return try! JSONDecoder().decode(Snapshot.self, from: data)
    }()

    private var sessions: [Session] { Self.fixture.sessions }

    /// What the dashboard sidebar shows for these pickers and this query.
    private func badges(_ query: String, in sessions: [Session]? = nil,
                        tool: String = "all", project: String = "all") -> FilteredCounts {
        let search = SessionSearch(query)
        return FilteredCounts(search.filter(sessions ?? self.sessions, tool: tool, project: project), query: search)
    }

    private func titles(_ query: String) -> [String] {
        SessionSearch(query).filter(sessions).map(\.title)
    }

    @Test func noFiltersEqualTheSnapshotCounts() {
        let counts = badges("")
        #expect(counts.counts == Self.fixture.counts)
        #expect(counts.total == sessions.count)
    }

    @Test func noFiltersEqualTheSnapshotCountsForSyntheticSnapshots() {
        let snapshot = Fixtures.snapshot([
            Fixtures.session("a", "needs_you"), Fixtures.session("b", "needs_you"),
            Fixtures.session("c", "done"), Fixtures.session("d", "snoozed"),
        ])
        #expect(badges("  ", in: snapshot.sessions).counts == snapshot.counts)
    }

    @Test func toolPickerNarrowsCounts() {
        let all = badges("")
        let codex = badges("", tool: "codex")
        let claude = badges("", tool: "claude")
        #expect(codex.total + claude.total == all.total)
        #expect(codex.total == sessions.filter { $0.tool == "codex" }.count)
        for state in SessionState.allCases {
            #expect(codex.count(for: state) + claude.count(for: state) == all.count(for: state))
        }
    }

    @Test func projectPickerNarrowsCounts() throws {
        let project = try #require(sessions.first?.project)
        let counts = badges("", project: project)
        #expect(counts.total == sessions.filter { $0.project == project }.count)
        for state in SessionState.allCases {
            #expect(counts.count(for: state) == sessions.filter { $0.project == project && $0.state == state }.count)
        }
    }

    @Test func queryNarrowsOnlyTheMatchingStates() {
        let counts = badges("state:review")
        #expect(counts.total == 3)
        #expect(counts.count(for: .review) == 3)
        for state in SessionState.allCases where state != .review {
            #expect(counts.count(for: state) == 0)
        }
    }

    @Test func pickersAndQueryCombine() {
        let counts = badges("retry", tool: "codex")
        #expect(counts.total == 1)
        #expect(counts.count(for: .snoozed) == 1)
    }

    @Test func unknownStatesCountOnlyInTheTotal() {
        let sessions = Fixtures.snapshot([Fixtures.session("x", "paused"), Fixtures.session("y", "done")]).sessions
        let counts = badges("", in: sessions)
        #expect(counts.total == 2)
        #expect(counts.count(for: .done) == 1)
        #expect(counts.count(for: .unknown) == 0)
    }

    // MARK: Hidden sessions that need the user

    @Test func noFiltersHideNothingThatNeedsYou() {
        let all = badges("")
        #expect(all.needingAttention > 0)
        #expect(all.hiddenNeedingAttention(from: all) == 0)
        #expect(FilteredCounts.hiddenAttentionNotice(0) == nil)
    }

    @Test func queryHidingNeedsYouSessionsIsReported() {
        let all = badges("")
        let none = badges("zzzz")
        #expect(none.hiddenNeedingAttention(from: all) == all.needingAttention)
        // state:review keeps no needs_you/interrupted session, so it hides them all.
        let review = badges("state:review")
        #expect(review.hiddenNeedingAttention(from: all) == all.needingAttention)
        let urgent = Fixtures.snapshot([
            Fixtures.session("a", "needs_you", extra: ["title": "alpha", "tool": "codex"]),
            Fixtures.session("b", "interrupted", extra: ["title": "beta", "tool": "claude"]),
            Fixtures.session("c", "done", extra: ["title": "alpha", "tool": "claude"]),
        ]).sessions
        let allUrgent = badges("", in: urgent)
        #expect(badges("alpha", in: urgent).hiddenNeedingAttention(from: allUrgent) == 1)
        #expect(badges("", in: urgent, tool: "codex").hiddenNeedingAttention(from: allUrgent) == 1)
        #expect(badges("", in: urgent, tool: "claude").hiddenNeedingAttention(from: allUrgent) == 1)
    }

    @Test func hiddenAttentionNoticeWording() {
        #expect(FilteredCounts.hiddenAttentionNotice(1) == "Filters hide 1 session that needs you")
        #expect(FilteredCounts.hiddenAttentionNotice(3) == "Filters hide 3 sessions that need you")
    }

    // MARK: Fixture queries (the plan's manual expectations)

    @Test func ticketRef() {
        #expect(titles("ABC-123") == ["Fix flaky retry logic in reconcile loop"])
    }

    @Test func prNumber() {
        #expect(titles("#42") == ["Add schema_version pragma + triage table migration"])
        #expect(titles("pr:42") == ["Add schema_version pragma + triage table migration"])
    }

    @Test func stateQualifier() {
        #expect(titles("state:review").count == 3)
    }

    @Test func branchQualifier() {
        let matched = SessionSearch("branch:codex").filter(sessions)
        #expect(matched.allSatisfy { ($0.branch ?? "").contains("codex") })
        #expect(matched.count == sessions.filter { ($0.branch ?? "").contains("codex") }.count)
        #expect(matched.contains { $0.title == "Backfill rollout parser for second CLI" })
        #expect(matched.contains { $0.state == .snoozed })
        #expect(matched.contains { $0.state == .done })
        #expect(titles("branch:fix-retry-flake") == ["Fix flaky retry logic in reconcile loop"])
    }

    @Test func promptWord() {
        #expect(Set(titles("retry")) == ["Fix flaky retry logic in reconcile loop", "Outage API client cleanup"])
    }

    @Test func noMatch() {
        #expect(titles("zzzz").isEmpty)
        #expect(PopoverSections.make(from: sessions, query: SessionSearch("zzzz")).hasNoMatches)
    }

    // MARK: Notes total

    @Test func notesTotalSumsNotesCountOverTheFilteredSessions() {
        let sessions = Fixtures.snapshot([
            Fixtures.session("a", "review", extra: ["title": "alpha", "notes_count": 3]),
            Fixtures.session("b", "needs_you", extra: ["title": "beta", "notes_count": 2]),
            Fixtures.session("c", "done", extra: ["title": "alpha two"]),
        ]).sessions
        #expect(badges("", in: sessions).counts.notes == 5)
        #expect(badges("alpha", in: sessions).counts.notes == 3)
        #expect(badges("zzzz", in: sessions).counts.notes == 0)
    }

    @Test(arguments: ["review", "snoozed", "done"])
    func filtersHidingANeedsYouNoteWarn(state: String) {
        let sessions = Fixtures.snapshot([
            Fixtures.session("a", "running", extra: ["title": "alpha"]),
            Fixtures.session("b", state, extra: [
                "title": "beta",
                "notes": [["id": 1, "kind": "needs_you", "text": "USER_ACTION: approve",
                           "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"],
                          ["id": 2, "kind": "update", "text": "PR #3 merged",
                           "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]],
                "notes_count": 2,
            ]),
        ]).sessions
        let all = badges("", in: sessions)
        let filtered = badges("alpha", in: sessions)
        #expect(filtered.hiddenNeedingAttention(from: all) == 0)
        #expect(filtered.hiddenNeedsYouNotes(from: all) == 1)
        #expect(FilteredCounts.hiddenAttentionNotice(0, notes: 1) == "Filters hide 1 update that needs you")
        #expect(badges("beta", in: sessions).hiddenNeedsYouNotes(from: all) == 0)
    }

    @Test func siblingNoteQueryWarnsLikeThePopover() {
        // The query keeps the session only through its update note, so its
        // needs_you note is hidden on both surfaces, note by note.
        let sessions = Fixtures.snapshot([
            Fixtures.session("b", "review", extra: [
                "title": "beta",
                "notes": [["id": 1, "kind": "needs_you", "text": "USER_ACTION: approve",
                           "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"],
                          ["id": 2, "kind": "update", "text": "PR #7 merged",
                           "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]],
                "notes_count": 2,
            ]),
        ]).sessions
        let query = "PR #7 merged"
        let dashboard = badges(query, in: sessions).hiddenNeedsYouNotes(from: badges("", in: sessions))
        let popover = PopoverSections.make(from: sessions, query: SessionSearch(query)).hiddenNeedsYouNotes
        #expect(dashboard == 1)
        #expect(dashboard == popover)
    }

    @Test(arguments: ["needs_you", "interrupted"])
    func hiddenNeedsYouSessionIsNotCountedTwiceForItsNote(state: String) {
        // The note is what holds the session in needs_you: the warning
        // names the session once, not the session and its note.
        let sessions = Fixtures.snapshot([
            Fixtures.session("a", "running", extra: ["title": "alpha"]),
            Fixtures.session("b", state, extra: [
                "title": "beta",
                "notes": [["id": 1, "kind": "needs_you", "text": "USER_ACTION: approve",
                           "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]],
                "notes_count": 1,
            ]),
        ]).sessions
        let all = badges("", in: sessions)
        let filtered = badges("alpha", in: sessions)
        #expect(filtered.hiddenNeedingAttention(from: all) == 1)
        #expect(filtered.hiddenNeedsYouNotes(from: all) == 0)
        #expect(FilteredCounts.hiddenAttentionNotice(1, notes: 0) == "Filters hide 1 session that needs you")
    }

    @Test func hiddenAttentionNoticeWordingWithUpdates() {
        #expect(FilteredCounts.hiddenAttentionNotice(0, notes: 0) == nil)
        #expect(FilteredCounts.hiddenAttentionNotice(0, notes: 3) == "Filters hide 3 updates that need you")
        #expect(FilteredCounts.hiddenAttentionNotice(1, notes: 1) == "Filters hide 1 session and 1 update that need you")
    }

    @Test func fixtureListsItsDenials() {
        #expect(Self.fixture.counts.denials == 3)
        #expect(badges("").counts.denials == 3)
        let rows = DenialFeed.rows(from: sessions, query: SessionSearch(""))
        #expect(rows.count == 3)
        #expect(DenialFeed.total(from: sessions, query: SessionSearch("")) == 3)
    }

    @Test func fixtureNotesTotalMatchesTheSnapshot() {
        #expect(badges("").counts.notes == Self.fixture.counts.notes)
        #expect(Self.fixture.counts.notes > 0)
    }
}
