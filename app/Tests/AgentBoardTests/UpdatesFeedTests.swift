import AppKit
import Testing
@testable import AgentBoard

private func note(_ id: Int, _ kind: String = "update", _ text: String = "PR #1 merged",
                  lastTs: String, count: Int = 1) -> [String: Any] {
    ["id": id, "kind": kind, "text": text, "ts": lastTs, "last_ts": lastTs,
     "count": count, "source": "stop"]
}

private func session(_ key: String, _ state: String = "review", project: String = "proj",
                     tool: String = "claude", notes: [[String: Any]]) -> [String: Any] {
    Fixtures.session(key, state, extra: [
        "cwd": "/tmp/\(project)", "project": project, "tool": tool,
        "notes": notes, "notes_count": notes.count,
    ])
}

@Suite("UpdatesFeed")
struct UpdatesFeedTests {
    @Test func flattensAcrossSessionsNewestFirst() {
        let sessions = Fixtures.snapshot([
            session("a", notes: [note(1, lastTs: "2026-10-15T10:00:00Z"),
                                 note(4, lastTs: "2026-10-15T13:00:00.500000Z")]),
            session("b", "needs_you", project: "other", tool: "codex",
                    notes: [note(2, "needs_you", "USER_ACTION: approve", lastTs: "2026-10-15T12:00:00Z")]),
            session("c", notes: []),
        ]).sessions
        let feed = UpdatesFeed.make(from: sessions, query: SessionSearch(""))
        #expect(feed.map(\.id) == [4, 2, 1])
        #expect(feed.map(\.sessionKey) == ["a", "b", "a"])
        #expect(feed[1].project == "other")
        #expect(feed[1].toolBadge == "X")
        #expect(feed[1].note.kind == .needsYou)
        #expect(UpdatesFeed.header(for: feed, total: UpdatesFeed.total(from: sessions, query: SessionSearch("")))
                == "Updates (3)")
    }

    @Test func tiesKeepTheNewerNoteFirst() {
        let sessions = Fixtures.snapshot([
            session("a", notes: [note(3, lastTs: "2026-10-15T10:00:00Z")]),
            session("b", notes: [note(8, lastTs: "2026-10-15T10:00:00Z")]),
        ]).sessions
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("")).map(\.id) == [8, 3])
    }

    @Test func queryFiltersBySessionAndByNoteText() {
        let sessions = Fixtures.snapshot([
            session("alpha", notes: [note(1, "update", "Deployed the cache fix", lastTs: "2026-10-15T10:00:00Z")]),
            session("beta", notes: [note(2, "update", "PR #77 opened", lastTs: "2026-10-15T11:00:00Z")]),
        ]).sessions
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("#77")).map(\.id) == [2])
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("alpha")).map(\.id) == [1])
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("zzzz")).isEmpty)
    }

    @Test func queryFiltersNoteByNote() {
        let sessions = Fixtures.snapshot([
            session("alpha", project: "alpha", notes: [
                note(1, "update", "PR #77 opened", lastTs: "2026-10-15T10:00:00Z"),
                note(2, "needs_you", "USER_ACTION: approve the plan", lastTs: "2026-10-15T11:00:00Z"),
            ]),
        ]).sessions
        // A term that hits one note's text lists that note, not its sibling.
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("#77")).map(\.id) == [1])
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("approve")).map(\.id) == [2])
        // A term that hits the session's own fields lists all of its notes.
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("repo:alpha")).map(\.id) == [2, 1])
        // Terms are ANDed over the session's fields plus this one note.
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("alpha #77")).map(\.id) == [1])
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("#77 approve")).isEmpty)
        // The sibling's needs_you note is hidden from the feed, and counted.
        #expect(UpdatesFeed.hiddenNeedsYouNotes(from: sessions, query: SessionSearch("#77")) == 1)
        #expect(UpdatesFeed.hiddenNeedsYouNotes(from: sessions, query: SessionSearch("approve")) == 0)
        #expect(UpdatesFeed.hiddenNeedsYouNotes(from: sessions, query: SessionSearch("")) == 0)
    }

    @Test func countsFollowOneRuleAboveTheListCap() {
        // The CLI lists 20 notes per session but reports all 25 open ones.
        let listed = (1...20).map { i in
            note(i, "update", "PR #\(i) merged", lastTs: String(format: "2026-10-15T10:%02d:00Z", i))
        }
        let sessions = Fixtures.snapshot([
            Fixtures.session("busy", "review", extra: [
                "cwd": "/tmp/busy", "project": "busy", "notes": listed, "notes_count": 25,
            ]),
            session("calm", project: "calm", notes: [note(99, lastTs: "2026-10-15T09:00:00Z")]),
        ]).sessions
        let busy = sessions[0]
        let all = SessionSearch("")
        let feed = UpdatesFeed.make(from: sessions, query: all)
        // Rendered: the capped lists. Totals: the uncapped notesCount.
        #expect(feed.count == 21)
        #expect(UpdatesFeed.total(from: sessions, query: all) == 26)
        #expect(UpdatesFeed.header(for: feed, total: UpdatesFeed.total(from: sessions, query: all))
                == "Updates (21 of 26)")
        #expect(FilteredCounts(sessions).counts.notes == 26)
        // The row badge and the detail header of the capped session agree.
        #expect(busy.notesCount == 25)
        #expect(UpdatesFeed.header(listed: busy.notes.count, total: busy.notesCount) == "Updates (20 of 25)")
        // A query on the session's own fields keeps all of its open notes...
        let byRepo = SessionSearch("repo:busy")
        #expect(UpdatesFeed.make(from: sessions, query: byRepo).count == 20)
        #expect(UpdatesFeed.total(from: sessions, query: byRepo) == 25)
        #expect(FilteredCounts(byRepo.filter(sessions), query: byRepo).counts.notes == 25)
        // ...while one that only hits a note's text counts the notes it lists.
        let byNote = SessionSearch("\"PR #7 merged\"")
        #expect(UpdatesFeed.make(from: sessions, query: byNote).map(\.id) == [7])
        #expect(UpdatesFeed.total(from: sessions, query: byNote) == 1)
        #expect(UpdatesFeed.header(for: UpdatesFeed.make(from: sessions, query: byNote),
                                   total: UpdatesFeed.total(from: sessions, query: byNote)) == "Updates (1)")
        // The dashboard sidebar lists the whole session but counts the
        // notes the query shows, like the popover header (decided, PR #5).
        #expect(byNote.filter(sessions).map(\.key) == ["busy"])
        #expect(FilteredCounts(byNote.filter(sessions), query: byNote).counts.notes == 1)
    }

    @Test func headerNeverShowsFewerThanListed() {
        #expect(UpdatesFeed.header(listed: 3, total: 3) == "Updates (3)")
        #expect(UpdatesFeed.header(listed: 3, total: 1) == "Updates (3)")
        #expect(UpdatesFeed.header(listed: 0, total: 0) == "Updates (0)")
    }

    @Test func emptyWhenNoSessionHasNotes() {
        let sessions = Fixtures.snapshot([Fixtures.session("a", "review")]).sessions
        #expect(UpdatesFeed.make(from: sessions, query: SessionSearch("")).isEmpty)
    }

    @Test func bundledFixtureShowsItsNotes() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/snapshot.json")
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
        let feed = UpdatesFeed.make(from: snapshot.sessions, query: SessionSearch(""))
        #expect(feed.count == snapshot.counts.notes)
        #expect(feed.first?.note.kind == .needsYou)
    }
}

@Suite("Note decoding")
struct NoteDecodingTests {
    @Test func sessionWithoutNotesDecodesToEmpty() {
        let session = Fixtures.decoded("a", "review")
        #expect(session.notes.isEmpty)
        #expect(session.notesCount == 0)
    }

    @Test func countsWithoutNotesKeyDecode() throws {
        // A snapshot from a CLI that predates notes: no counts.notes, and
        // only some of the state keys.
        let json = """
        {"generated_at": "2026-10-15T00:00:00Z",
         "counts": {"needs_you": 2, "running": 1},
         "sessions": []}
        """
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        #expect(snapshot.counts.needsYou == 2)
        #expect(snapshot.counts.running == 1)
        #expect(snapshot.counts.done == 0)
        #expect(snapshot.counts.notes == 0)
    }

    @Test func notesDecodeWithCountAndUnknownKind() {
        let session = Fixtures.decoded("a", "review", extra: [
            "notes": [
                ["id": 5, "kind": "update", "text": "**PR #9 merged**\nmore", "ts": "2026-10-15T00:00:00Z",
                 "last_ts": "2026-10-15T01:00:00Z", "count": 3, "source": "stop"],
                ["id": 6, "kind": "something-new", "text": "x"],
            ],
            "notes_count": 25,
        ])
        #expect(session.notes.count == 2)
        #expect(session.notesCount == 25)
        #expect(session.notes[0].count == 3)
        #expect(session.notes[0].firstLine == "PR #9 merged")
        #expect(session.notes[0].hasMore)
        #expect(session.notes[1].kind == .unknown)
        #expect(session.notes[1].lastTs == "")
        #expect(!session.notes[1].hasMore)
    }

    @Test func singleBoldLineHasNothingMoreToShow() {
        let session = Fixtures.decoded("a", "review", extra: [
            "notes": [
                ["id": 1, "kind": "update", "text": "**PR #9 merged**", "ts": "t", "last_ts": "t"],
                ["id": 2, "kind": "update", "text": "  **PR #9 merged**  \n\n  ", "ts": "t", "last_ts": "t"],
                ["id": 3, "kind": "update", "text": "**PR #9 merged**\n\nCI green", "ts": "t", "last_ts": "t"],
            ],
        ])
        #expect(session.notes.map(\.firstLine) == ["PR #9 merged", "PR #9 merged", "PR #9 merged"])
        #expect(session.notes.map(\.hasMore) == [false, false, true])
    }

    @Test func notesCountDefaultsToTheListLength() {
        let session = Fixtures.decoded("a", "review", extra: [
            "notes": [["id": 1, "kind": "update", "text": "x", "ts": "t", "last_ts": "t"]],
        ])
        #expect(session.notesCount == 1)
    }

    @Test func dismissingANoteChangesSessionEquality() {
        let with = Fixtures.decoded("a", "review", extra: [
            "notes": [["id": 1, "kind": "update", "text": "x", "ts": "t", "last_ts": "t"]],
            "notes_count": 1,
        ])
        let without = Fixtures.decoded("a", "review")
        #expect(with != without)
    }
}
