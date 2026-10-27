import AppKit
import Testing
@testable import AgentBoard

private func finished(_ key: String, project: String = "widgets", title: String? = nil,
                      lastAssistant: String? = "All set.\nDetails follow.") -> Session {
    Fixtures.decoded(key, "review", alive: true, extra: [
        "cwd": "/tmp/\(project)", "project": project, "title": title ?? key,
        "last_assistant": lastAssistant ?? NSNull(),
        "turn_finished_at": "2026-10-24T09:05:00.000000Z",
    ])
}

@Suite("SessionNotificationPlan")
struct SessionNotificationPlanTests {
    private let away = SessionNotificationPlan.Context(dashboardIsKey: false, dashboardSelection: nil)

    @Test func theSelectedSessionIsSkippedWhileTheDashboardIsKey() {
        let context = SessionNotificationPlan.Context(dashboardIsKey: true, dashboardSelection: "a")
        let planned = SessionNotificationPlan.turnRequests(
            finished: [finished("a"), finished("b")], context: context)
        #expect(planned.map(\.identifier) == ["turn:b"])
    }

    @Test func itIsPostedWhenTheDashboardIsNotKeyOrAnotherSessionIsSelected() {
        let notKey = SessionNotificationPlan.Context(dashboardIsKey: false, dashboardSelection: "a")
        #expect(SessionNotificationPlan.turnRequests(finished: [finished("a")], context: notKey).count == 1)
        let other = SessionNotificationPlan.Context(dashboardIsKey: true, dashboardSelection: "b")
        #expect(SessionNotificationPlan.turnRequests(finished: [finished("a")], context: other).count == 1)
    }

    @Test func turnContent() throws {
        let planned = try #require(
            SessionNotificationPlan.turnRequests(finished: [finished("a", title: "Fix the flake")],
                                                 context: away).first)
        #expect(planned.identifier == "turn:a")
        #expect(planned.title == "widgets")
        #expect(planned.subtitle == "Fix the flake")
        #expect(planned.body == "All set.")
        #expect(planned.threadIdentifier == "a")
        #expect(planned.userInfo == ["sessionKey": "a", "kind": "turn"])
    }

    @Test func bodyFallsBackWhenThereIsNoReply() throws {
        let planned = try #require(
            SessionNotificationPlan.turnRequests(finished: [finished("a", lastAssistant: nil)],
                                                 context: away).first)
        #expect(planned.body == "Finished its turn")
        let blank = try #require(
            SessionNotificationPlan.turnRequests(finished: [finished("a", lastAssistant: "  \n")],
                                                 context: away).first)
        #expect(blank.body == "Finished its turn")
    }

    @Test func bodyDropsMarkdownBold() throws {
        let planned = try #require(
            SessionNotificationPlan.turnRequests(
                finished: [finished("a", lastAssistant: "\n**PR #9 merged** as abc")],
                context: away).first)
        #expect(planned.body == "PR #9 merged as abc")
    }

    @Test func subtitleIsLeftOutWhenItEqualsTheTitle() throws {
        let planned = try #require(
            SessionNotificationPlan.turnRequests(finished: [finished("a", title: "widgets")],
                                                 context: away).first)
        #expect(planned.title == "widgets")
        #expect(planned.subtitle == nil)
        // No project: the title is the session's own and no subtitle repeats it.
        let noProject = Fixtures.decoded("b", "review", extra: ["cwd": "", "project": "",
                                                                "title": "Untitled work"])
        let bare = try #require(
            SessionNotificationPlan.turnRequests(finished: [noProject], context: away).first)
        #expect(bare.title == "Untitled work")
        #expect(bare.subtitle == nil)
    }

    @Test func needsYouCarriesTheSameThreadAndUserInfo() {
        let session = Fixtures.decoded("a", "needs_you", extra: ["waiting_reason": "Bash"])
        let planned = SessionNotificationPlan.needsYou(session)
        #expect(planned.identifier == "needs_you:a")
        #expect(planned.threadIdentifier == "a")
        #expect(planned.userInfo == ["sessionKey": "a", "kind": "needs_you"])
        #expect(planned.title == "AgentBoard: needs you")
        #expect(planned.body == "Bash")
    }

    @Test func answeredKeysMapToTurnIdentifiers() {
        #expect(SessionNotificationPlan.removals(answered: ["a", "codex:b"]) == ["turn:a", "turn:codex:b"])
        #expect(SessionNotificationPlan.removals(answered: []).isEmpty)
    }

    @Test func theToggleGatesPostsButNotRemovals() {
        let changes = TurnFinishDetector.Changes(finished: [finished("a")], answered: ["b"])
        let off = SessionNotificationPlan.turnActions(changes, enabled: false, context: away)
        #expect(off.removals == ["turn:b"])
        #expect(off.posts.isEmpty)
        let on = SessionNotificationPlan.turnActions(changes, enabled: true, context: away)
        #expect(on.removals == ["turn:b"])
        #expect(on.posts.map(\.identifier) == ["turn:a"])
    }
}
