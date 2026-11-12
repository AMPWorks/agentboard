import Testing
@testable import AgentBoard

private func note(_ id: Int, _ kind: String = "update", _ text: String = "Tick summary",
                  minute: Int) -> [String: Any] {
    let ts = String(format: "2026-10-15T10:%02d:00Z", minute)
    return ["id": id, "kind": kind, "text": text, "ts": ts, "last_ts": ts, "count": 1, "source": "stop"]
}

private func session(_ key: String, _ state: String = "review", project: String? = nil,
                     notes: [[String: Any]], notesCount: Int? = nil) -> [String: Any] {
    let project = project ?? key
    return Fixtures.session(key, state, extra: [
        "cwd": "/tmp/\(project)", "project": project,
        "notes": notes, "notes_count": notesCount ?? notes.count,
    ])
}

@Suite("Updates rows")
struct UpdatesRowsTests {
    private let all = SessionSearch("")

    @Test func groupsALoopSessionsUpdatesIntoOneRow() {
        let sessions = Fixtures.snapshot([
            session("loop", notes: [
                note(5, "update", "Newest tick", minute: 50),
                note(4, "error", "API Error: overloaded", minute: 40),
                note(3, "needs_you", "USER_ACTION: approve the plan", minute: 30),
                note(2, "update", "Older tick", minute: 20),
            ]),
            session("calm", notes: [note(9, "update", "PR #9 merged", minute: 45)]),
        ]).sessions
        let rows = UpdatesFeed.rows(from: sessions, query: all)
        #expect(rows.map(\.id) == ["group:loop", "note:9", "note:3"])
        let group = rows[0]
        #expect(group.isGroup)
        #expect(group.note.text == "Newest tick")
        #expect(group.notes.map(\.id) == [5, 4, 2])
        #expect(group.total == 3)
        #expect(group.groupLabel == "×3 notes")
        #expect(group.dismissal == .session(tool: "claude", sessionId: "loop", kinds: [.update, .error]))
        // A single update stays an ordinary row with its own dismissal.
        #expect(!rows[1].isGroup)
        #expect(rows[1].groupLabel == nil)
        #expect(rows[1].dismissal == .ids([9]))
    }

    @Test func aGroupWithAnErrorShowsTheErrorGlyph() {
        let sessions = Fixtures.snapshot([
            session("loop", notes: [
                note(5, "update", "Newest tick", minute: 50),
                note(4, "error", "API Error: overloaded", minute: 40),
            ]),
            session("calm", notes: [
                note(8, "update", "Newer tick", minute: 30),
                note(7, "update", "Older tick", minute: 20),
            ]),
            session("solo", notes: [note(9, "error", "API Error: overloaded", minute: 10)]),
        ]).sessions
        let rows = UpdatesFeed.rows(from: sessions, query: all)
        #expect(rows.map(\.id) == ["group:loop", "group:calm", "note:9"])
        #expect(rows[0].note.kind == .update)
        #expect(rows[0].glyphKind == .error)
        #expect(rows[1].glyphKind == .update)
        #expect(rows[2].glyphKind == .error)
    }

    /// Always shown: needs_you and error rows (a group with an error too)
    /// go above the sessions; plain updates and plain groups go below.
    @Test func alwaysShownPartitionKeepsUrgentRowsAbove() {
        let sessions = Fixtures.snapshot([
            session("asks", "needs_you", notes: [note(1, "needs_you", "USER_ACTION: approve", minute: 10)]),
            session("broke", notes: [note(2, "error", "API Error: overloaded", minute: 20)]),
            session("loop", notes: [
                note(3, "update", "Newest tick", minute: 30),
                note(4, "error", "API Error: overloaded", minute: 25),
            ]),
            session("calm", notes: [
                note(5, "update", "Newer tick", minute: 50),
                note(6, "update", "Older tick", minute: 40),
            ]),
            session("info", notes: [note(7, "update", "PR #9 merged", minute: 55)]),
        ]).sessions
        let rows = UpdatesFeed.rows(from: sessions, query: all)
        let split = UpdatesFeed.partitionForAlwaysShown(rows)
        // Feed order (newest first) within each part.
        #expect(split.above.map(\.id) == ["group:loop", "note:2", "note:1"])
        #expect(split.below.map(\.id) == ["note:7", "group:calm"])
        #expect(split.above.count + split.below.count == rows.count)
        #expect(UpdatesFeed.counts(of: split.above) == (listed: 4, total: 4))
        #expect(UpdatesFeed.counts(of: split.below) == (listed: 3, total: 3))
        #expect(UpdatesFeed.partitionForAlwaysShown([]).above.isEmpty)
    }

    @Test func splitPartsTitleAndClearOnlyThemselvesWhileBothHaveRows() {
        let sessions = Fixtures.snapshot([
            session("asks", "needs_you", notes: [note(1, "needs_you", "USER_ACTION: approve", minute: 10)]),
            session("info", notes: [note(7, "update", "PR #9 merged", minute: 55)], notesCount: 1),
        ]).sessions
        let split = UpdatesFeed.partitionForAlwaysShown(UpdatesFeed.rows(from: sessions, query: all))
        let whole = UpdatesFeed.clearPlan(from: sessions, query: all)
        #expect(UpdatesFeed.partTitle(above: true, otherPartHasRows: true) == "Urgent updates")
        #expect(UpdatesFeed.partTitle(above: false, otherPartHasRows: true) == "Other updates")
        #expect(UpdatesFeed.partTitle(above: false, otherPartHasRows: false) == "Updates")
        let below = UpdatesFeed.partClearPlan(split.below, whole: whole, otherPartHasRows: true)
        #expect(below.dismissals == [.ids([7])])
        #expect(below.title == "Clear these")
        // A part alone clears like the whole panel did.
        #expect(UpdatesFeed.partClearPlan(split.below, whole: whole, otherPartHasRows: false) == whole)
        #expect(UpdatesFeed.header(listed: 1, total: 1, title: "Urgent updates") == "Urgent updates (1)")
        #expect(UpdatesFeed.header(listed: 3, total: 5, title: "Other updates") == "Other updates (3 of 5)")
    }

    /// A session with a needs_you note and a group of update notes: the
    /// lower part's clear dismisses the group by kind (update and error
    /// only), never the session's needs_you note and never everything; the
    /// upper part's clear holds only the needs_you note.
    @Test func clearingTheLowerPartNeverDismissesTheSameSessionsRequest() {
        let sessions = Fixtures.snapshot([
            session("loop", "needs_you", notes: [
                note(3, "update", "Newest tick", minute: 50),
                note(2, "needs_you", "USER_ACTION: approve the plan", minute: 40),
                note(1, "update", "Older tick", minute: 30),
            ]),
        ]).sessions
        let split = UpdatesFeed.partitionForAlwaysShown(UpdatesFeed.rows(from: sessions, query: all))
        let whole = UpdatesFeed.clearPlan(from: sessions, query: all)
        #expect(split.above.map(\.id) == ["note:2"])
        #expect(split.below.map(\.id) == ["group:loop"])
        let below = UpdatesFeed.partClearPlan(split.below, whole: whole, otherPartHasRows: true)
        #expect(below.dismissals == [.session(tool: "claude", sessionId: "loop", kinds: [.update, .error])])
        let above = UpdatesFeed.partClearPlan(split.above, whole: whole, otherPartHasRows: true)
        #expect(above.dismissals == [.ids([2])])
    }

    /// Two notes of one project from different sessions tell apart by the
    /// session's title (else its branch) after the project.
    @Test func theRowHeaderTellsTwoNotesOfOneProjectApart() {
        func s(_ key: String, title: String, branch: String? = nil, project: String = "agentboard",
               note n: [String: Any]) -> [String: Any] {
            var extra: [String: Any] = ["project": project, "title": title, "notes": [n], "notes_count": 1]
            if let branch { extra["branch"] = branch }
            return Fixtures.session(key, "review", extra: extra)
        }
        let sessions = Fixtures.snapshot([
            s("a", title: "Fix the hook", branch: "main", note: note(1, "update", "Done", minute: 1)),
            s("b", title: "Widen the list", branch: "main", note: note(2, "update", "Done", minute: 2)),
            s("c", title: " ", branch: "popover-polish", note: note(3, "update", "Done", minute: 3)),
            s("d", title: "Untitled project", project: "", note: note(4, "update", "Done", minute: 4)),
        ]).sessions
        let rows = Dictionary(uniqueKeysWithValues: UpdatesFeed.rows(from: sessions, query: all).map { ($0.sessionKey, $0) })
        #expect(rows["a"]?.headerTitle == "agentboard")
        #expect(rows["a"]?.headerDetail == "Fix the hook")
        #expect(rows["b"]?.headerDetail == "Widen the list")
        #expect(rows["c"]?.headerDetail == "popover-polish")
        #expect(rows["d"]?.headerTitle == "Untitled project")
        #expect(rows["d"]?.headerDetail == nil)
    }

    @Test func aRepeatedNoteSaysHowOftenInWords() {
        let repeated = Fixtures.decoded("a", extra: ["notes": [
            ["id": 1, "kind": "update", "text": "Tick", "ts": "2026-10-15T10:00:00Z",
             "last_ts": "2026-10-15T10:05:00Z", "count": 3, "source": "stop"],
            ["id": 2, "kind": "update", "text": "Once", "ts": "2026-10-15T10:00:00Z",
             "last_ts": "2026-10-15T10:00:00Z", "count": 1, "source": "stop"],
        ]])
        #expect(NoteStyle.countLabel(repeated.notes[0]) == "seen 3 times")
        #expect(NoteStyle.countLabel(repeated.notes[1]) == nil)
    }

    @Test func needsYouNotesAreNeverGrouped() {
        let sessions = Fixtures.snapshot([
            session("asks", "needs_you", notes: [
                note(2, "needs_you", "USER_ACTION: rotate the token", minute: 20),
                note(1, "needs_you", "USER_ACTION: approve the plan", minute: 10),
            ]),
        ]).sessions
        let rows = UpdatesFeed.rows(from: sessions, query: all)
        #expect(rows.map(\.id) == ["note:2", "note:1"])
        #expect(rows.allSatisfy { !$0.isGroup && $0.isNeedsYou })
    }

    @Test func groupCountsUpdatesPastTheSnapshotCap() {
        let listed = (1...20).map { note($0, minute: $0) }
            + [note(100, "needs_you", "USER_ACTION: approve", minute: 0)]
        let sessions = Fixtures.snapshot([
            session("loop", notes: listed, notesCount: 70),
        ]).sessions
        let rows = UpdatesFeed.rows(from: sessions, query: all)
        #expect(rows.count == 2)
        // 70 open: one needs_you, so 69 update/error notes, 20 of them listed.
        #expect(rows[0].total == 69)
        #expect(rows[0].notes.count == 20)
        #expect(rows[1].isNeedsYou)
    }

    @Test func queryOnANoteGroupsOnlyTheMatchedNotes() {
        let sessions = Fixtures.snapshot([
            session("loop", notes: [
                note(3, "update", "Deploy step one", minute: 30),
                note(2, "update", "Deploy step two", minute: 20),
                note(1, "update", "Unrelated tick", minute: 10),
            ], notesCount: 30),
        ]).sessions
        let byNote = UpdatesFeed.rows(from: sessions, query: SessionSearch("deploy"))
        #expect(byNote.count == 1)
        #expect(byNote[0].notes.map(\.id) == [3, 2])
        #expect(byNote[0].total == 2)
        #expect(byNote[0].dismissal == .ids([3, 2]))
        // One match is a plain row.
        let one = UpdatesFeed.rows(from: sessions, query: SessionSearch("unrelated"))
        #expect(one.map(\.id) == ["note:1"])
        // A query on the session's own fields keeps (and dismisses) them all.
        let byRepo = UpdatesFeed.rows(from: sessions, query: SessionSearch("repo:loop"))
        #expect(byRepo[0].total == 30)
        #expect(byRepo[0].dismissal == .session(tool: "claude", sessionId: "loop", kinds: [.update, .error]))
    }

    @Test func compactShowsThreeUpdateRowsAndEveryNeedsYouRow() {
        let sessions = Fixtures.snapshot((1...6).map { i in
            session("s\(i)", notes: [note(i, "update", "PR #\(i) merged", minute: i)])
        } + [
            session("ask", "needs_you", notes: [
                note(50, "needs_you", "USER_ACTION: old request", minute: 0),
                note(51, "needs_you", "USER_ACTION: new request", minute: 59),
            ]),
        ]).sessions
        let rows = UpdatesFeed.rows(from: sessions, query: all)
        #expect(rows.count == 8)
        let compact = UpdatesFeed.compact(rows, expanded: false)
        #expect(compact.shown.map(\.id) == ["note:51", "note:6", "note:5", "note:4", "note:50"])
        #expect(compact.hidden == 3)
        #expect(UpdatesFeed.needsToggle(rows))
        let expanded = UpdatesFeed.compact(rows, expanded: true)
        #expect(expanded.shown == rows)
        #expect(expanded.hidden == 0)
    }

    @Test func compactNeedsNoToggleAtOrUnderTheCap() {
        let sessions = Fixtures.snapshot((1...3).map { i in
            session("s\(i)", notes: [note(i, minute: i)])
        } + [
            session("ask", "needs_you", notes: (10...14).map { note($0, "needs_you", "USER_ACTION: \($0)", minute: $0) }),
        ]).sessions
        let rows = UpdatesFeed.rows(from: sessions, query: all)
        let compact = UpdatesFeed.compact(rows, expanded: false)
        #expect(compact.shown == rows)
        #expect(compact.hidden == 0)
        #expect(!UpdatesFeed.needsToggle(rows))
    }

    @Test func clearWithoutAQueryDismissesEverything() {
        let sessions = Fixtures.snapshot([
            session("a", notes: [note(1, minute: 1)], notesCount: 40),
        ]).sessions
        let plan = UpdatesFeed.clearPlan(from: sessions, query: all)
        #expect(plan.title == "Clear all")
        #expect(plan.dismissals == [.all])
        #expect(plan.help.contains("not listed"))
    }

    @Test func clearWithAQueryDismissesOnlyMatches() {
        let sessions = Fixtures.snapshot([
            session("alpha", notes: [note(1, "update", "Deploy done", minute: 1)], notesCount: 25),
            session("beta", notes: [
                note(2, "update", "alpha mention in a note", minute: 2),
                note(3, "update", "Unrelated", minute: 3),
            ]),
            session("gamma", notes: []),
        ]).sessions
        let plan = UpdatesFeed.clearPlan(from: sessions, query: SessionSearch("alpha"))
        #expect(plan.title == "Clear matching")
        #expect(plan.help.contains("matches"))
        // alpha matches as a whole (all 25 open notes); beta only through
        // one note's text; gamma has nothing to dismiss.
        #expect(plan.dismissals == [
            .session(tool: "claude", sessionId: "alpha", kinds: nil),
            .ids([2]),
        ])
        #expect(UpdatesFeed.clearPlan(from: sessions, query: SessionSearch("zzzz")).dismissals.isEmpty)
    }

    @Test func kindArgumentsForTheCLI() {
        #expect(AgentBoardCLI.kindArguments(nil) == [])
        #expect(AgentBoardCLI.kindArguments([]) == nil)
        #expect(AgentBoardCLI.kindArguments([.update, .error]) == ["--kind", "update", "error"])
        #expect(AgentBoardCLI.kindArguments([.unknown]) == nil)
    }

    @Test func aKindScopeWithNoCLINameDismissesNothing() {
        #expect(AgentBoardCLI.sessionDismissArguments(tool: "claude", sessionId: "s1", kinds: nil)
            == ["notes", "--dismiss-all", "--session", "claude:s1"])
        #expect(AgentBoardCLI.sessionDismissArguments(tool: "claude", sessionId: "s1", kinds: [.update, .error])
            == ["notes", "--dismiss-all", "--session", "claude:s1", "--kind", "update", "error"])
        // A scoped call must never widen to the whole session (needs_you too).
        #expect(AgentBoardCLI.sessionDismissArguments(tool: "claude", sessionId: "s1", kinds: [.unknown]) == nil)
        #expect(AgentBoardCLI.sessionDismissArguments(tool: "claude", sessionId: "s1", kinds: []) == nil)
    }
}

@Suite("Note focus")
struct NoteFocusTests {
    private let sessions = Fixtures.snapshot([
        session("loop", notes: [
            note(5, "update", "Newest tick", minute: 50),
            note(4, "update", "Older tick", minute: 40),
        ], notesCount: 30),
        session("quiet", notes: []),
    ]).sessions

    @Test func scrollsToAListedNote() {
        let loop = sessions[0]
        #expect(NoteFocus(sessionKey: loop.key, noteId: 4).scrollTarget(in: loop) == "note-4")
    }

    @Test func fallsBackToTheUpdatesHeaderForAnUnlistedNote() {
        let loop = sessions[0]
        // Past the 20-note list, or dismissed after the click.
        #expect(NoteFocus(sessionKey: loop.key, noteId: 1).scrollTarget(in: loop) == NoteFocus.sectionID)
    }

    @Test func ignoresAnotherSessionsRequestAndASessionWithoutNotes() {
        let loop = sessions[0], quiet = sessions[1]
        #expect(NoteFocus(sessionKey: quiet.key, noteId: 5).scrollTarget(in: loop) == nil)
        #expect(NoteFocus(sessionKey: quiet.key, noteId: 5).scrollTarget(in: quiet) == nil)
    }
}
