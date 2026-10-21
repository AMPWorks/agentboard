import AppKit
import Testing
@testable import AgentBoard

@Suite("PopoverSections")
struct PopoverSectionsTests {
    private func session(_ key: String, _ state: String, updated: String = "2026-10-15T00:00:00Z",
                         title: String? = nil) -> [String: Any] {
        Fixtures.session(key, state, extra: ["updated_at": updated, "title": title ?? key])
    }

    /// One session in every state, titled after its key.
    private var everyState: [Session] {
        Fixtures.snapshot([
            session("n", "needs_you"),
            session("i", "interrupted"),
            session("r", "review"),
            session("g", "running"),
            session("u", "unconcluded"),
            session("s", "snoozed"),
            session("d", "done"),
        ]).sessions
    }

    private func make(_ sessions: [Session], _ query: String = "") -> PopoverSections {
        PopoverSections.make(from: sessions, query: SessionSearch(query))
    }

    private func layout(_ sections: PopoverSections) -> [String] {
        sections.sections.map { "\($0.kind.rawValue):\($0.sessions.map(\.key).joined(separator: ","))" }
    }

    @Test func noQueryGroupsActiveStatesInOrder() {
        let sections = make(everyState)
        #expect(layout(sections) == ["needsYou:n,i", "review:r", "running:g", "unconcluded:u"])
        #expect(!sections.isFiltering)
        #expect(sections.hiddenNotice == nil)
        #expect(!sections.expandsUnconcluded)
        #expect(!sections.hasNoMatches)
    }

    @Test func sectionsSortNewestFirst() {
        let sessions = Fixtures.snapshot([
            session("old", "needs_you", updated: "2026-10-15T00:00:00Z"),
            session("new", "interrupted", updated: "2026-10-16T00:00:00Z"),
        ]).sessions
        #expect(layout(make(sessions)) == ["needsYou:new,old"])
    }

    @Test func queryAddsSnoozedAndDoneSections() {
        let sections = make(everyState, "claude")   // every fixture session is claude
        #expect(layout(sections) == [
            "needsYou:n,i", "review:r", "running:g", "unconcluded:u", "snoozed:s", "done:d",
        ])
        #expect(sections.hiddenCount == 0)
        #expect(sections.hiddenNotice == "Filtered: 0 sessions hidden")
    }

    @Test func queryFiltersEachSection() {
        let sessions = Fixtures.snapshot([
            session("n1", "needs_you", title: "retry storm"),
            session("n2", "needs_you", title: "other"),
            session("r1", "review", title: "retry doc"),
            session("d1", "done", title: "retry cleanup"),
            session("s1", "snoozed", title: "unrelated"),
        ]).sessions
        let sections = make(sessions, "retry")
        #expect(layout(sections) == ["needsYou:n1", "review:r1", "done:d1"])
        #expect(sections.hiddenCount == 2)
        #expect(sections.hiddenNeedingAttention == 1)
    }

    @Test func emptySectionsAreOmitted() {
        let sessions = Fixtures.snapshot([session("g", "running")]).sessions
        #expect(layout(make(sessions)) == ["running:g"])
        #expect(make(sessions).section(.needsYou) == nil)
    }

    @Test func headersCountOnlyWhileFiltering() {
        let plain = make(everyState)
        #expect(plain.header(for: plain.section(.needsYou)!) == "Needs you")
        #expect(plain.header(for: plain.section(.unconcluded)!) == "Unconcluded (1)")
        let filtered = make(everyState, "claude")
        #expect(filtered.header(for: filtered.section(.needsYou)!) == "Needs you (2)")
        #expect(filtered.header(for: filtered.section(.done)!) == "Done (1)")
    }

    @Test func hiddenNoticeCountsSessionsTheQueryHides() {
        let sections = make(everyState, "state:review")
        #expect(layout(sections) == ["review:r"])
        #expect(sections.hiddenCount == 6)
        #expect(sections.hiddenNeedingAttention == 2)
        #expect(sections.hiddenNotice == "Filtered: 6 sessions hidden, 2 need you")
        #expect(sections.noticeIsEmphasized)
    }

    @Test func hiddenNoticeIsPlainWhenNothingUrgentIsHidden() {
        let urgentShown = make(everyState, "tool:claude")
        #expect(!urgentShown.noticeIsEmphasized)
        let oneHidden = make(Fixtures.snapshot([session("a", "running"), session("b", "done")]).sessions,
                             "state:running")
        #expect(oneHidden.hiddenNotice == "Filtered: 1 session hidden")
        #expect(!oneHidden.noticeIsEmphasized)
        let oneUrgent = make(Fixtures.snapshot([session("a", "running"), session("b", "needs_you")]).sessions,
                             "state:running")
        #expect(oneUrgent.hiddenNotice == "Filtered: 1 session hidden, 1 needs you")
        #expect(oneUrgent.noticeIsEmphasized)
    }

    @Test(arguments: ["review", "snoozed", "done"])
    func hiddenNeedsYouNoteEmphasizesTheNotice(state: String) {
        // The note's state effect is gone (released by a prompt, or snooze /
        // done wins), but the request is unanswered: a query that hides the
        // session must still say so.
        let sessions = Fixtures.snapshot([
            session("a", "running"),
            Fixtures.session("b", state, extra: [
                "title": "b",
                "notes": [["id": 9, "kind": "needs_you", "text": "USER_ACTION: approve the plan",
                           "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]],
                "notes_count": 1,
            ]),
        ]).sessions
        let sections = make(sessions, "state:running")
        #expect(sections.hiddenCount == 1)
        #expect(sections.hiddenNeedingAttention == 0)
        #expect(sections.hiddenNeedsYouNotes == 1)
        #expect(sections.hiddenNotice == "Filtered: 1 session hidden, 1 update needs you")
        #expect(sections.noticeIsEmphasized)
        // A query that keeps the note leaves the notice plain.
        let kept = make(sessions, "approve")
        #expect(kept.hiddenNeedsYouNotes == 0)
        #expect(!kept.noticeIsEmphasized)
    }

    @Test(arguments: ["needs_you", "interrupted"])
    func hiddenNeedsYouSessionIsNotCountedTwiceForItsNote(state: String) {
        // The usual USER_ACTION case: the open note is what holds the
        // session in needs_you, so hiding it is one request, not two.
        let sessions = Fixtures.snapshot([
            session("a", "running"),
            Fixtures.session("b", state, extra: [
                "title": "b",
                "notes": [["id": 9, "kind": "needs_you", "text": "USER_ACTION: approve the plan",
                           "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]],
                "notes_count": 1,
            ]),
        ]).sessions
        let sections = make(sessions, "state:running")
        #expect(sections.hiddenNeedingAttention == 1)
        #expect(sections.hiddenNeedsYouNotes == 0)
        #expect(sections.hiddenNotice == "Filtered: 1 session hidden, 1 needs you")
        #expect(sections.noticeIsEmphasized)
    }

    @Test func hiddenUpdateNoticeCombinesWithHiddenSessions() {
        let urgentNote: [String: Any] = ["id": 1, "kind": "needs_you", "text": "USER_ACTION: x",
                                         "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]
        let sessions = Fixtures.snapshot([
            session("a", "running"),
            session("b", "needs_you"),
            Fixtures.session("c", "review", extra: ["title": "c", "notes": [urgentNote, urgentNote.merging(["id": 2]) { $1 }]]),
            // An update-kind note never counts.
            Fixtures.session("d", "done", extra: ["title": "d", "notes": [urgentNote.merging(["id": 3, "kind": "update"]) { $1 }]]),
        ]).sessions
        let sections = make(sessions, "state:running")
        #expect(sections.hiddenNotice == "Filtered: 3 sessions hidden, 1 needs you, 2 updates need you")
        #expect(sections.noticeIsEmphasized)
    }

    @Test func unconcludedExpandsOnlyWithMatchesWhileFiltering() {
        #expect(make(everyState, "state:unconcluded").expandsUnconcluded)
        #expect(!make(everyState, "state:review").expandsUnconcluded)
        #expect(!make(everyState).expandsUnconcluded)
    }

    @Test func noMatchesIsDistinctFromAnEmptySnapshot() {
        let none = make(everyState, "zzzz")
        #expect(none.hasNoMatches)
        #expect(!none.snapshotIsEmpty)
        let empty = make([], "zzzz")
        #expect(!empty.hasNoMatches)
        #expect(empty.snapshotIsEmpty)
        #expect(make([]).snapshotIsEmpty)
    }

    @Test func unknownStatesAreNeverCountedAsHidden() {
        let sessions = Fixtures.snapshot([
            session("x", "paused", title: "alpha"),
            session("y", "paused", title: "beta"),
            session("r", "review", title: "alpha"),
            session("g", "running", title: "beta"),
        ]).sessions
        // "x" matches but no section can show it; "y" does not match. Only
        // "g" is hidden by the query.
        let sections = make(sessions, "alpha")
        #expect(layout(sections) == ["review:r"])
        #expect(sections.hiddenCount == 1)
        #expect(make(sessions, "\"\"").hiddenCount == 0)
        #expect(make(sessions, "a").hiddenCount == 0)
    }

    @Test func whitespaceQueryIsNotFiltering() {
        let sections = make(everyState, "   ")
        #expect(!sections.isFiltering)
        #expect(sections.section(.done) == nil)
    }
}
