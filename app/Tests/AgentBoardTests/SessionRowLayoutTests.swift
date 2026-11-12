import Foundation
import Testing
@testable import AgentBoard

@Suite("Session row layout")
struct SessionRowLayoutTests {
    /// Every combination of the optional lines: the repo is always second.
    @Test(arguments: [false, true], [false, true])
    func theRepoIsAlwaysTheSecondLine(status: Bool, reason: Bool) {
        var extra: [String: Any] = ["title": "Fix the hook", "branch": "main"]
        if status { extra["status_text"] = "Reviewing PR #9" }
        if reason { extra["waiting_reason"] = "Bash" }
        let lines = SessionRowLayout.lines(for: Fixtures.decoded("s1", "needs_you", extra: extra))
        #expect(lines.prefix(2) == [.title, .repo])
        #expect(lines.contains(.secondaryTitle) == status)
        #expect(lines.contains(.reason) == reason)
        #expect(lines.last == (reason ? .reason : (status ? .secondaryTitle : .repo)))
    }

    @Test func aBlankReasonAddsNoLine() {
        let session = Fixtures.decoded("s1", extra: ["waiting_reason": "  "])
        #expect(SessionRowLayout.lines(for: session) == [.title, .repo])
    }

    @Test func repoTextJoinsProjectAndBranch() {
        #expect(SessionRowLayout.repoText(Fixtures.decoded("a", extra: ["project": "api", "branch": "fix-x"]))
                == "api · fix-x")
        #expect(SessionRowLayout.repoText(Fixtures.decoded("b", extra: ["project": "api", "branch": " "])) == "api")
        #expect(SessionRowLayout.repoText(Fixtures.decoded("c", extra: ["project": "api"])) == "api")
        // The project falls back to the working directory's name.
        #expect(SessionRowLayout.repoText(Fixtures.decoded("d", extra: ["cwd": "/x/widget"])) == "widget")
    }

    @Test func theModeIconSitsUnderTheToolBadgeAboveTheDesktopButton() {
        let both = Fixtures.decoded("a", alive: true, extra: ["permission_mode": "plan", "desktop": true])
        #expect(SessionRowLayout.iconColumn(for: both) == [.tool, .mode("plan"), .desktop])
        let modeOnly = Fixtures.decoded("b", alive: true, extra: ["permission_mode": " auto "])
        #expect(SessionRowLayout.iconColumn(for: modeOnly) == [.tool, .mode("auto")])
        let desktopOnly = Fixtures.decoded("c", alive: true, extra: ["desktop": true])
        #expect(SessionRowLayout.iconColumn(for: desktopOnly) == [.tool, .desktop])
    }

    @Test func noModeIconForTheDefaultModeOrAFinishedSession() {
        let manual = Fixtures.decoded("a", alive: true, extra: ["permission_mode": "default"])
        #expect(SessionRowLayout.iconColumn(for: manual) == [.tool])
        let done = Fixtures.decoded("b", "done", extra: ["permission_mode": "plan"])
        #expect(SessionRowLayout.iconColumn(for: done) == [.tool])
        let ended = Fixtures.decoded("c", "review", extra: ["permission_mode": "plan",
                                                             "ended_at": "2026-10-15T00:00:00Z"])
        #expect(SessionRowLayout.iconColumn(for: ended) == [.tool])
    }

    @Test func titleTagsKeepAFixedOrder() {
        let all = Fixtures.decoded("a", "interrupted", extra: ["notes_count": 2, "auto_done": true])
        #expect(SessionRowLayout.titleTags(for: all) == [.interrupted, .notes, .autoDone])
        #expect(SessionRowLayout.titleTags(for: Fixtures.decoded("b")) == [])
        // Only a Review row whose process has exited is tagged closed.
        #expect(SessionRowLayout.titleTags(for: Fixtures.decoded("c", "review")) == [.closed])
        #expect(SessionRowLayout.titleTags(for: Fixtures.decoded("d", "review", alive: true)) == [])
        #expect(SessionRowLayout.titleTags(for: Fixtures.decoded("e", "unconcluded")) == [])
    }

    @Test(arguments: PermissionModeTests.documented.filter { $0 != "default" } + ["someNewMode"])
    func everyModeHasAnIconAndANamedLabel(mode: String) {
        #expect(!PermissionMode.symbol(mode).isEmpty)
        let label = PermissionMode.accessibilityLabel(mode)
        #expect(label.hasPrefix("Permission mode: "))
        #expect(label.hasSuffix(PermissionMode.tag(mode) ?? mode))
    }

    @Test func knownModesHaveDistinctIcons() {
        let modes = ["plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions"]
        #expect(Set(modes.map(PermissionMode.symbol)).count == modes.count)
        #expect(!modes.map(PermissionMode.symbol).contains(PermissionMode.symbol("someNewMode")))
    }

    @Test func aBareToolNameOnANeedsYouRowIsAPermissionPrompt() {
        for tool in ["Bash", "WebFetch", "mcp__github__create_issue"] {
            let session = Fixtures.decoded("a", "needs_you", extra: ["waiting_reason": tool])
            #expect(SessionDisplay.reasonText(session) == "Needs permission: \(tool)")
        }
        // Not on other states, and not for text that is not a tool name.
        let review = Fixtures.decoded("b", "review", extra: ["waiting_reason": "Summary"])
        #expect(SessionDisplay.reasonText(review) == "Summary")
        let sentence = Fixtures.decoded("c", "needs_you", extra: ["waiting_reason": "Claude needs your permission to use Bash"])
        #expect(SessionDisplay.reasonText(sentence) == "Claude needs your permission to use Bash")
        // A one-word needs_you note headline (reconcile.note_headline) is
        // the note's own text, not a permission prompt: shown as is.
        func note(_ text: String, kind: String = "needs_you") -> [String: Any] {
            ["id": 1, "kind": kind, "text": text, "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]
        }
        for (reason, text) in [("Blocked", "**Blocked**\nwaiting on the schema review"),
                               ("Deploy", "USER_ACTION: Deploy"), ("Deploy", "- Deploy")] {
            let headline = Fixtures.decoded("d", "needs_you", extra: ["waiting_reason": reason, "notes": [note(text)]])
            #expect(SessionDisplay.reasonText(headline) == reason)
        }
        // An update note, or a note that only mentions the tool inside a
        // word, leaves a real permission prompt alone.
        let update = Fixtures.decoded("e", "needs_you", extra: ["waiting_reason": "Bash", "notes": [note("Bash", kind: "update")]])
        #expect(SessionDisplay.reasonText(update) == "Needs permission: Bash")
        let inWord = Fixtures.decoded("f", "needs_you", extra: ["waiting_reason": "Bash", "notes": [note("Run SubBash")]])
        #expect(SessionDisplay.reasonText(inWord) == "Needs permission: Bash")
    }

    @Test func rawNotificationTypesAndPrefixesBecomeWords() {
        func text(_ raw: String) -> String? {
            SessionDisplay.reasonText(Fixtures.decoded("a", "needs_you", extra: ["waiting_reason": raw]))
        }
        #expect(text("permission_prompt") == "Needs your permission")
        #expect(text("elicitation_dialog") == "Asks you a question")
        #expect(text("elicitation_dialog: Confirm the migration?") == "Asks you a question: Confirm the migration?")
        #expect(text("agent_needs_input") == "Needs your input")
        #expect(text("question") == "Asks you a question")
        #expect(text("plan approval") == "Waiting for your plan approval")
        #expect(text("StopFailure: uncaught ValueError") == "Turn failed: uncaught ValueError")
        // hook.failure_reason's "<error>: <first line>" shape.
        #expect(text("rate_limit: API Error: Rate limit reached") == "Turn failed (rate limit): API Error: Rate limit reached")
        #expect(text("server_error") == "Turn failed (API server error)")
        #expect(text("overloaded: upstream busy") == "Turn failed (API overloaded): upstream busy")
        #expect(text("rate_limited_but_not_a_type: x") == "rate_limited_but_not_a_type: x")
        #expect(text("github: Which repo?") == "github: Which repo?")
        #expect(text("  ") == nil)
        // An interrupted session has no reason from the CLI; it gets one.
        let interrupted = Fixtures.decoded("i", "interrupted")
        #expect(SessionDisplay.reasonText(interrupted) == SessionDisplay.interruptedReason)
        #expect(SessionRowLayout.lines(for: interrupted).last == .reason)
        #expect(SessionDisplay.reasonText(Fixtures.decoded("r", "running")) == nil)
    }

    @Test func actionableReasonsAreTheOnesTheUserActsOn() {
        #expect(SessionDisplay.reasonIsActionable(Fixtures.decoded("a", "needs_you")))
        #expect(SessionDisplay.reasonIsActionable(Fixtures.decoded("b", "interrupted")))
        #expect(SessionDisplay.reasonIsActionable(Fixtures.decoded("c", "review")))
        #expect(!SessionDisplay.reasonIsActionable(Fixtures.decoded("d", "running")))
    }

    @Test func reasonKindFollowsTheRawPrefix() {
        func kind(_ raw: String, _ state: String = "needs_you") -> SessionDisplay.ReasonKind? {
            SessionDisplay.reasonKind(Fixtures.decoded("a", state, extra: ["waiting_reason": raw]))
        }
        #expect(kind("permission_prompt") == .permission)
        #expect(kind("permission_prompt: Claude needs your permission to use Bash") == .permission)
        #expect(kind("Bash") == .permission)
        #expect(kind("mcp__github__create_issue") == .permission)
        for raw in ["elicitation_dialog", "elicitation_dialog: Confirm the migration?", "elicitation_url_dialog",
                    "agent_needs_input", "question", "plan approval"] {
            #expect(kind(raw) == .question, "\(raw)")
        }
        for raw in ["StopFailure: uncaught ValueError", "rate_limit: API Error: Rate limit reached",
                    "server_error", "overloaded: upstream busy"] {
            #expect(kind(raw) == .failure, "\(raw)")
        }
        // An idle prompt, free text (an MCP question reads like any other
        // text), a near-miss error type, a blank reason: no glyph.
        for raw in ["idle_prompt", "github: Which repo?", "rate_limited_but_not_a_type: x", "  ",
                    "question: not a tool reason", "Claude needs your permission to use Bash"] {
            #expect(kind(raw) == nil, "\(raw)")
        }
        // A bare tool name is a permission prompt only on a Needs you row.
        #expect(kind("Summary", "review") == nil)
        #expect(SessionDisplay.reasonKind(Fixtures.decoded("i", "interrupted")) == nil)
        #expect(SessionDisplay.reasonKind(Fixtures.decoded("r", "running")) == nil)
        // A one-word needs_you note headline is the note's text, not a tool.
        let note: [String: Any] = ["id": 1, "kind": "needs_you", "text": "**Blocked**",
                                   "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]
        let headline = Fixtures.decoded("d", "needs_you", extra: ["waiting_reason": "Blocked", "notes": [note]])
        #expect(SessionDisplay.reasonKind(headline) == nil)
    }

    @Test func reasonKindsHaveDistinctGlyphs() {
        let glyphs = SessionDisplay.ReasonKind.allCases.map(\.systemImage)
        #expect(Set(glyphs).count == glyphs.count)
    }

    /// The PR buttons stay at the trailing edge whatever optional tags the
    /// row has, so they line up from row to row.
    @Test(arguments: [false, true], [false, true])
    func thePRButtonsAreAlwaysLast(model: Bool, tmux: Bool) {
        var extra: [String: Any] = ["links": [["kind": "pr", "ref": "o/r#7", "url": "https://example.com/pr/7"]]]
        if model { extra["model"] = "claude-opus-5-5" }
        if tmux {
            extra["tmux"] = true
            extra["tmux_session"] = "work"
        }
        let tags = SessionRowLayout.repoTags(for: Fixtures.decoded("s1", "running", alive: true, extra: extra))
        #expect(tags.last == .prs)
        #expect(tags.contains(.model) == model)
        #expect(tags.contains(.tmux) == tmux)
        #expect(tags.firstIndex(of: .prs) == tags.count - 1)
    }

    /// A Running row's background-task reason is a tag on line 2, not a
    /// line of its own, on every row whatever the session count.
    @Test func aRunningRowFoldsItsBackgroundTasksIntoARepoTag() {
        func row(_ reason: String?, _ state: String = "running") -> Session {
            var extra: [String: Any] = ["title": "Fix the hook", "branch": "main"]
            if let reason { extra["waiting_reason"] = reason }
            return Fixtures.decoded("s1", state, alive: true, extra: extra)
        }
        for (reason, count) in [("Waiting on 1 background task", 1), ("Waiting on 1 background task: shell", 1),
                                ("Waiting on 3 background tasks: shell, subagent", 3),
                                ("Waiting on 12 background tasks", 12)] {
            let session = row(reason)
            #expect(SessionRowLayout.backgroundTaskCount(session) == count, "\(reason)")
            #expect(SessionRowLayout.lines(for: session) == [.title, .repo], "\(reason)")
            #expect(SessionRowLayout.repoTags(for: session) == [.background, .prs], "\(reason)")
        }
        #expect(SessionRowLayout.backgroundTagText(1) == "1 task")
        #expect(SessionRowLayout.backgroundTagText(3) == "3 tasks")
        // Another reason, another state, or a near miss keeps its line.
        for session in [row("Loop idle between ticks"), row("Waiting on 0 background tasks"),
                        row("Waiting on the CI"), row("Waiting on 2 background taskforce"),
                        row("Waiting on 2 background tasksmith"), row("Waiting on 2 background tasks still pending"),
                        row("Waiting on 2 background tasks", "review")] {
            #expect(SessionRowLayout.backgroundTaskCount(session) == nil)
            #expect(SessionRowLayout.lines(for: session).last == .reason)
            #expect(!SessionRowLayout.repoTags(for: session).contains(.background))
        }
        #expect(SessionRowLayout.backgroundTaskCount(row(nil)) == nil)
    }
}
