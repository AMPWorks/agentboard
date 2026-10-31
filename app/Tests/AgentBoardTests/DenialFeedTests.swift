import Foundation
import Testing
@testable import AgentBoard

private func denial(_ id: Int, _ ts: String, tool: String = "Bash", reason: String = "[Test]",
                    server: String? = nil, subagent: Bool = false) -> [String: Any] {
    var json: [String: Any] = ["id": id, "ts": ts, "tool_name": tool, "reason": reason,
                               "subagent": subagent]
    json["mcp_server"] = server ?? NSNull()
    return json
}

@Suite("Denial decoding and labels")
struct DenialDecodingTests {
    @Test func sessionWithDenialsDecodes() {
        let session = Fixtures.decoded("a", extra: [
            "denials": [
                denial(2, "2026-10-15T00:02:00Z", tool: "mcp__tracker__close_issue",
                       reason: "[External System Writes]", server: "tracker"),
                denial(1, "2026-10-15T00:01:00Z", subagent: true),
            ],
            "denials_count": 5,
        ])
        #expect(session.denials.map(\.id) == [2, 1])
        #expect(session.denialsCount == 5)
        #expect(session.denials[0].toolName == "mcp__tracker__close_issue")
        #expect(session.denials[0].mcpServer == "tracker")
        #expect(session.denials[0].reason == "[External System Writes]")
        #expect(session.denials[1].subagent)
        #expect(session.denials[1].mcpServer == nil)
        #expect(session.denials[1].date != nil)
    }

    @Test func sessionWithoutDenialsDefaults() {
        let session = Fixtures.decoded("a")
        #expect(session.denials.isEmpty)
        #expect(session.denialsCount == 0)
    }

    @Test func aMalformedDenialDropsOnlyItself() {
        let session = Fixtures.decoded("a", extra: [
            "title": "kept",
            "denials": [denial(1, "2026-10-15T00:01:00Z"), ["tool_name": "no id"], "junk",
                        ["id": 3]],
            "denials_count": 2,
        ])
        #expect(session.title == "kept")
        #expect(session.denials.map(\.id) == [1, 3])
        #expect(session.denials[1].toolName == "")
        #expect(session.denials[1].reason == "")
        #expect(session.denials[1].subagent == false)
    }

    @Test func aBadCountFallsBackToTheList() {
        let session = Fixtures.decoded("a", extra: [
            "denials": [denial(1, "2026-10-15T00:01:00Z")], "denials_count": "many",
        ])
        #expect(session.denialsCount == session.denials.count)
        #expect(session.denialsCount == 1)
    }

    @Test func countsWithoutDenialsDecodeToZero() throws {
        let json = #"{"generated_at": "x", "counts": {"running": 1}, "sessions": []}"#
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        #expect(snapshot.counts.denials == 0)
        let with = #"{"generated_at": "x", "counts": {"denials": 4}, "sessions": []}"#
        #expect(try JSONDecoder().decode(Snapshot.self, from: Data(with.utf8)).counts.denials == 4)
    }

    @Test func sessionEqualityNoticesDenialChanges() {
        let base: [String: Any] = ["denials": [denial(1, "2026-10-15T00:01:00Z"),
                                               denial(2, "2026-10-15T00:02:00Z")],
                                   "denials_count": 2]
        let before = Fixtures.decoded("a", extra: base)
        let cleared = Fixtures.decoded("a", extra: ["denials": [denial(2, "2026-10-15T00:02:00Z")],
                                                    "denials_count": 1])
        let countOnly = Fixtures.decoded("a", extra: base.merging(["denials_count": 3]) { $1 })
        #expect(before == Fixtures.decoded("a", extra: base))
        #expect(before != cleared)
        #expect(before != countOnly)
    }

    @Test func toolLabel() {
        #expect(Denial(id: 1, ts: "", toolName: "mcp__srv__delete_issue", reason: "",
                       mcpServer: "srv").toolLabel == "srv › delete_issue")
        #expect(Denial(id: 1, ts: "", toolName: "mcp__srv__delete_issue", reason: "").toolLabel
                == "mcp__srv__delete_issue")
        #expect(Denial(id: 1, ts: "", toolName: "mcp__other__x", reason: "", mcpServer: "srv").toolLabel
                == "mcp__other__x")
        #expect(Denial(id: 1, ts: "", toolName: "Bash", reason: "").toolLabel == "Bash")
        #expect(Denial(id: 1, ts: "", toolName: "", reason: "").toolLabel == "Unknown tool")
    }

    @Test func reasonLabel() {
        func label(_ reason: String) -> String { Denial(id: 1, ts: "", toolName: "Bash", reason: reason).reasonLabel }
        #expect(label("[Irreversible Local Destruction]") == "Irreversible Local Destruction")
        #expect(label("  [ Spaced ]  ") == "Spaced")
        #expect(label("[[Nested]]") == "[Nested]")
        #expect(label("Classifier unavailable") == "Classifier unavailable")
        #expect(label("") == "Denied")
        #expect(label("[]") == "Denied")
    }
}

@Suite("Denial feed")
struct DenialFeedTests {
    private var sessions: [Session] {
        Fixtures.snapshot([
            Fixtures.session("claude:a", "running", extra: [
                "title": "alpha work", "cwd": "/tmp/alpha", "branch": "feature-x",
                "denials": [
                    denial(3, "2026-10-15T00:03:00Z", reason: "[Irreversible Local Destruction]"),
                    denial(1, "2026-10-15T00:01:00Z", tool: "mcp__tracker__close_issue",
                           reason: "[External System Writes]", server: "tracker"),
                ],
                "denials_count": 4,
            ]),
            Fixtures.session("codex:b", "review", extra: [
                "tool": "codex", "title": "beta work", "cwd": "/tmp/beta", "branch": "main",
                "denials": [denial(2, "2026-10-15T00:02:00Z", reason: "[Interfere With Workloads]",
                                   subagent: true)],
                "denials_count": 1,
            ]),
            Fixtures.session("claude:c", "done", extra: ["title": "gamma", "cwd": "/tmp/gamma"]),
        ]).sessions
    }

    private func ids(_ query: String, tool: String = "all", project: String = "all") -> [Int] {
        DenialFeed.rows(from: sessions, tool: tool, project: project, query: SessionSearch(query))
            .map(\.denial.id)
    }

    @Test func rowsAreNewestFirstAcrossSessions() {
        let rows = DenialFeed.rows(from: sessions, query: SessionSearch(""))
        #expect(rows.map(\.denial.id) == [3, 2, 1])
        #expect(rows.map(\.sessionKey) == ["claude:a", "codex:b", "claude:a"])
        #expect(rows[0].id == "claude:a#3")
        #expect(Set(rows.map(\.id)).count == rows.count)
    }

    @Test func tiesOnTimeKeepTheHigherIDFirst() {
        let tied = Fixtures.snapshot([Fixtures.session("a", "running", extra: [
            "denials": [denial(4, "2026-10-15T00:00:00Z"), denial(9, "2026-10-15T00:00:00Z")],
        ])]).sessions
        #expect(DenialFeed.rows(from: tied, query: SessionSearch("")).map(\.denial.id) == [9, 4])
    }

    @Test func pickersFilter() {
        #expect(ids("", tool: "codex") == [2])
        #expect(ids("", tool: "claude") == [3, 1])
        #expect(ids("", project: "alpha") == [3, 1])
    }

    @Test func queryMatchesTheDenialOrItsSession() {
        #expect(ids("destruction") == [3])
        #expect(ids("tracker") == [1])
        #expect(ids("close_issue") == [1])
        #expect(ids("tracker › close") == [1])
        #expect(ids("beta") == [2])
        #expect(ids("branch:feature") == [3, 1])
        #expect(ids("branch:feature destruction") == [3])
        #expect(ids("branch:main destruction").isEmpty)
        #expect(ids("zzzz").isEmpty)
    }

    @Test func aDenialOnlyTermNeverListsTheSession() {
        #expect(SessionSearch("destruction").filter(sessions).isEmpty)
        #expect(SessionSearch("tracker").filter(sessions).isEmpty)
        #expect(SessionSearch("alpha").filter(sessions).map(\.key) == ["claude:a"])
    }

    @Test func totalCountsUncappedForSessionMatchesAndListedOtherwise() {
        #expect(DenialFeed.total(from: sessions, query: SessionSearch("")) == 5)
        #expect(DenialFeed.total(from: sessions, query: SessionSearch("alpha")) == 4)
        #expect(DenialFeed.total(from: sessions, query: SessionSearch("destruction")) == 1)
        #expect(DenialFeed.total(from: sessions, query: SessionSearch("zzzz")) == 0)
    }

    @Test func filteredCountsWithNoFiltersEqualTheSum() {
        #expect(FilteredCounts(sessions).counts.denials == 5)
    }

    @Test func header() {
        #expect(DenialFeed.header(listed: 3, total: 3) == "Permission denials (3)")
        #expect(DenialFeed.header(listed: 20, total: 25) == "Permission denials (20 of 25)")
        #expect(DenialFeed.header(listed: 2, total: 1) == "Permission denials (2)")
    }

    @Test func clearPlanWithNoFiltersClearsAll() {
        let plan = DenialFeed.clearPlan(from: sessions, query: SessionSearch("  "))
        #expect(plan.title == "Clear all")
        #expect(plan.help == "Dismiss every open denial, including ones of hidden sessions and ones not listed")
        #expect(plan.dismissals == [.all])
    }

    @Test func clearPlanWithFiltersClearsMatching() {
        let byQuery = DenialFeed.clearPlan(from: sessions, query: SessionSearch("work destruction"))
        #expect(byQuery.title == "Clear matching")
        #expect(byQuery.help == "Dismiss every denial that matches the filters")
        #expect(byQuery.dismissals == [.ids([3])])

        let bySession = DenialFeed.clearPlan(from: sessions, query: SessionSearch("alpha"))
        #expect(bySession.dismissals == [.session(tool: "claude", sessionId: "claude:a")])

        let byPicker = DenialFeed.clearPlan(from: sessions, tool: "codex", query: SessionSearch(""))
        #expect(byPicker.title == "Clear matching")
        #expect(byPicker.dismissals == [.session(tool: "codex", sessionId: "codex:b")])

        let mixed = DenialFeed.clearPlan(from: sessions, query: SessionSearch("work"))
        #expect(mixed.dismissals == [.session(tool: "claude", sessionId: "claude:a"),
                                     .session(tool: "codex", sessionId: "codex:b")])
        #expect(DenialFeed.clearPlan(from: sessions, query: SessionSearch("zzzz")).dismissals.isEmpty)
    }

    @Test func rowIDsMapBackToTheirSession() {
        #expect(DenialFeed.sessionKey(ofRowID: "claude:a#3") == "claude:a")
        #expect(DenialFeed.sessionKey(ofRowID: "claude:x#y#12") == "claude:x#y")
        #expect(DenialFeed.sessionKey(ofRowID: "claude:a") == nil)
        #expect(DenialFeed.sessionKey(ofRowID: "#3") == nil)
    }

    @Test func rowSelectionClearsWhenAnotherSessionIsSelected() {
        #expect(DenialFeed.rowSelection("claude:a#3", afterSessionKeyChangedTo: "codex:b") == nil)
        #expect(DenialFeed.rowSelection("claude:x#y#12", afterSessionKeyChangedTo: "claude:x") == nil)
    }

    @Test func rowSelectionIsKeptForTheSameSession() {
        #expect(DenialFeed.rowSelection("claude:a#3", afterSessionKeyChangedTo: "claude:a") == "claude:a#3")
        #expect(DenialFeed.rowSelection("claude:x#y#12", afterSessionKeyChangedTo: "claude:x#y")
                == "claude:x#y#12")
    }

    @Test func rowSelectionWhenTheSessionKeyBecomesNil() {
        #expect(DenialFeed.rowSelection("claude:a#3", afterSessionKeyChangedTo: nil) == nil)
        #expect(DenialFeed.rowSelection(nil, afterSessionKeyChangedTo: nil) == nil)
        #expect(DenialFeed.rowSelection(nil, afterSessionKeyChangedTo: "claude:a") == nil)
        #expect(DenialFeed.rowSelection("garbage", afterSessionKeyChangedTo: nil) == nil)
    }
}
