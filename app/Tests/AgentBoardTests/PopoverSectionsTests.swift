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

    @Test func headersAlwaysCarryTheCount() {
        let plain = make(everyState)
        #expect(plain.header(for: plain.section(.needsYou)!) == "Needs you (2)")
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

    @Test func summaryCountsEachSectionInOrder() {
        let plain = make(everyState)
        #expect(plain.summary.map(\.kind) == plain.sections.map(\.kind))
        #expect(plain.summary.first == .init(kind: .needsYou, count: 2))
        #expect(plain.summary.first?.text == "2 need you")
        #expect(!plain.summary.contains { $0.kind == .done || $0.kind == .snoozed })
        // The filter notice takes the line while a query is active.
        #expect(make(everyState, "claude").summary.isEmpty)
        #expect(make([]).summary.isEmpty)
        // One section: the line would only repeat its header.
        #expect(make(Fixtures.snapshot([session("r1", "review")]).sessions).summary.isEmpty)
        // Review's dot follows its rows: teal only while one is alive.
        let closedReview = make(Fixtures.snapshot([session("r1", "review"), session("g", "running")]).sessions)
        #expect(closedReview.summary.first { $0.kind == .review }?.anyAlive == false)
    }

    @Test func summaryTextReadsAsASentenceFragment() {
        #expect(PopoverSections.summaryText(.needsYou, count: 1) == "1 needs you")
        #expect(PopoverSections.summaryText(.needsYou, count: 3) == "3 need you")
        #expect(PopoverSections.summaryText(.review, count: 2) == "2 in review")
        #expect(PopoverSections.summaryText(.running, count: 5) == "5 running")
        #expect(PopoverSections.summaryText(.unconcluded, count: 1) == "1 unconcluded")
    }

    // MARK: - Grouped by machine

    /// A session on Mac `id` named `name`, keyed like the CLI's.
    private func remote(_ id: String, _ state: String, machine: String, name: String,
                        updated: String = "2026-10-15T00:00:00Z", stale: Bool = false) -> [String: Any] {
        Fixtures.session("claude:\(id)@\(machine)", state, alive: true, extra: [
            "session_id": id, "title": id, "updated_at": updated, "stale": stale,
            "machine": ["id": machine, "name": name],
        ])
    }

    private func grouped(_ sessions: [Session], _ query: String = "") -> PopoverSections {
        PopoverSections.make(from: sessions, query: SessionSearch(query), groupByMachine: true)
    }

    /// "<kind>/<machine or local>:<session ids>" per group, in display order.
    private func groups(_ sections: PopoverSections) -> [String] {
        sections.sections.flatMap { section in
            section.groups.map { "\($0.id):\($0.sessions.map(\.sessionId).joined(separator: ","))" }
        }
    }

    /// Running: local l1 (newest), l2; Mac "Mini" m1 between them; Review: only m2.
    private var mixed: [Session] {
        Fixtures.snapshot([
            session("l1", "running", updated: "2026-10-15T00:05:00Z"),
            session("l2", "running", updated: "2026-10-15T00:01:00Z"),
            remote("m1", "running", machine: "mini", name: "Mini", updated: "2026-10-15T00:03:00Z"),
            remote("m2", "review", machine: "mini", name: "Mini"),
        ]).sessions
    }

    @Test func defaultMakeNeverGroups() {
        let sections = make(mixed)
        #expect(!sections.groupsByMachine)
        #expect(sections.sections.allSatisfy { $0.groups.isEmpty })
        #expect(layout(sections) == ["review:claude:m2@mini", "running:l1,claude:m1@mini,l2"])
    }

    @Test func onlyLocalSessionsNeverGroup() {
        let sections = grouped(everyState)
        #expect(!sections.groupsByMachine)
        #expect(sections.sections.allSatisfy { $0.groups.isEmpty })
        #expect(layout(sections) == layout(make(everyState)))
    }

    @Test func groupsPutThisMacFirstAndKeepNewestFirst() {
        let sections = grouped(mixed)
        #expect(sections.groupsByMachine)
        #expect(groups(sections) == ["review/mini:m2", "running/local:l1,l2", "running/mini:m1"])
        // A section with only another Mac's sessions has no local group.
        #expect(sections.section(.review)?.groups.map(\.isLocal) == [false])
        for section in sections.sections {
            #expect(section.sessions == section.groups.flatMap(\.sessions))
            for group in section.groups {
                #expect(group.sessions == group.sessions.sorted { $0.updatedAt > $1.updatedAt })
            }
        }
    }

    @Test func otherMacsSortByNameThenId() {
        let sessions = Fixtures.snapshot([
            remote("a", "running", machine: "z1", name: "Beta"),
            remote("b", "running", machine: "z2", name: "alpha"),
            remote("c", "running", machine: "z3", name: "Mac 10"),
            remote("d", "running", machine: "z4", name: "Mac 2"),
            remote("e", "running", machine: "y2", name: "Twin"),
            remote("f", "running", machine: "y1", name: "Twin"),
        ]).sessions
        #expect(groups(grouped(sessions)) == [
            "running/z2:b", "running/z1:a", "running/z4:d", "running/z3:c", "running/y1:f", "running/y2:e",
        ])
    }

    @Test func groupIdsAreUniqueAcrossSections() {
        let ids = grouped(mixed).sections.flatMap { $0.groups.map(\.id) }
        #expect(Set(ids).count == ids.count)
    }

    @Test func aQueryLeavingOnlyLocalSessionsStillGroups() {
        let sections = grouped(mixed, "l1")
        #expect(sections.groupsByMachine)
        #expect(groups(sections) == ["running/local:l1"])
    }

    @Test func groupingKeepsCountsSummaryAndNotice() {
        for query in ["", "l1", "mini"] {
            let flat = make(mixed, query)
            let byMachine = grouped(mixed, query)
            #expect(byMachine.hiddenCount == flat.hiddenCount)
            #expect(byMachine.hiddenNotice == flat.hiddenNotice)
            #expect(byMachine.summary == flat.summary)
            #expect(byMachine.sections.map { byMachine.header(for: $0) } == flat.sections.map { flat.header(for: $0) })
        }
    }

    @Test func machineHeaders() {
        let sessions = Fixtures.snapshot([
            session("l", "running"),
            remote("on", "running", machine: "mini", name: "Mac mini"),
            remote("off1", "review", machine: "old", name: "Old Mac (2019)", stale: true),
            remote("off2", "review", machine: "old", name: "Old Mac (2019)", stale: true),
        ]).sessions
        let all = grouped(sessions).sections.flatMap(\.groups)
        let local = all.first { $0.isLocal }!
        #expect(PopoverSections.machineHeader(for: local, localName: "MacBook Pro") == "MacBook Pro (local) · 1")
        #expect(PopoverSections.machineHeader(for: local, localName: nil) == "This Mac (local) · 1")
        #expect(PopoverSections.machineHeader(for: local, localName: " ") == "This Mac (local) · 1")
        let online = all.first { $0.machine?.id == "mini" }!
        #expect(!online.isOffline)
        #expect(PopoverSections.machineHeader(for: online, localName: "x") == "Mac mini · 1")
        let offline = all.first { $0.machine?.id == "old" }!
        #expect(offline.isOffline)
        #expect(PopoverSections.machineHeader(for: offline, localName: "x") == "Old Mac (2019) · offline · 2")
        #expect(!local.isOffline)
    }

    @Test func aMacWithOnlySomeStaleSessionsIsNotOffline() {
        let sessions = Fixtures.snapshot([
            remote("a", "running", machine: "m", name: "M", stale: true),
            remote("b", "running", machine: "m", name: "M"),
        ]).sessions
        #expect(grouped(sessions).sections[0].groups.map(\.isOffline) == [false])
    }

    @Test func twoMacsWithOneAliasStillGetTwoGroups() {
        // The CLI puts a Mac's alias in machine.name; ids still tell them apart.
        let sessions = Fixtures.snapshot([
            remote("a", "running", machine: "id1", name: "Studio"),
            remote("b", "running", machine: "id2", name: "Studio"),
        ]).sessions
        let all = grouped(sessions).sections[0].groups
        #expect(all.map(\.machine?.id) == ["id1", "id2"])
        #expect(all.map { PopoverSections.machineHeader(for: $0, localName: nil) } == ["Studio · 1", "Studio · 1"])
    }
}
