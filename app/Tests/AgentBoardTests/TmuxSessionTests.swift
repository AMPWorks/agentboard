import Foundation
import Testing
@testable import AgentBoard

@Suite("tmux sessions")
struct TmuxSessionTests {
    @Test func decodesTmuxFields() {
        let session = Fixtures.decoded("t1", alive: true,
                                       extra: ["tmux": true, "tmux_session": "amp-agent", "can_focus": true])
        #expect(session.tmux)
        #expect(session.tmuxSession == "amp-agent")
        #expect(session.tmuxTag == "tmux \u{00B7} amp-agent")
        #expect(session.isFocusable)
        #expect(SessionActionHelp.focusHelp(session) == SessionActionHelp.focusTmux)
        #expect(SessionActionHelp.tmuxTag(session).contains("amp-agent"))
    }

    @Test func missingFieldsDefaultToNotTmux() {
        let session = Fixtures.decoded("s1")
        #expect(!session.tmux)
        #expect(session.tmuxSession == nil)
        #expect(session.tmuxTag == nil)
        #expect(SessionActionHelp.focusHelp(session) == SessionActionHelp.focus)
    }

    @Test func malformedOrUnresolvedValuesDegrade() {
        let unresolved = Fixtures.decoded("t1", extra: ["tmux": true, "tmux_session": NSNull()])
        #expect(unresolved.tmux)
        #expect(unresolved.tmuxSession == nil)
        #expect(unresolved.tmuxTag == "tmux")
        let malformed = Fixtures.decoded("t2", extra: ["tmux": "yes", "tmux_session": 7])
        #expect(!malformed.tmux)
        #expect(malformed.tmuxSession == nil)
    }

    @Test func aRenameIsAChange() {
        let before = Fixtures.decoded("t1", extra: ["tmux": true, "tmux_session": "amp-agent"])
        let after = Fixtures.decoded("t1", extra: ["tmux": true, "tmux_session": "amp2"])
        #expect(before != after)
    }

    @Test func searchMatchesTheTmuxSessionName() {
        let session = Fixtures.decoded("t1", extra: ["tmux": true, "tmux_session": "amp-agent"])
        let other = Fixtures.decoded("s2")
        #expect(SessionSearch("amp-agent").filter([session, other]).map(\.key) == [session.key])
    }

    @Test func searchMatchesTheWordTmuxOnTmuxRows() {
        let named = Fixtures.decoded("t1", extra: ["tmux": true, "tmux_session": "amp-agent"])
        let unnamed = Fixtures.decoded("t2", extra: ["tmux": true, "tmux_session": NSNull()])
        let other = Fixtures.decoded("s2")
        #expect(SessionSearch("tmux").filter([named, unnamed, other]).map(\.key)
                == [named.key, unnamed.key])
    }

    @Test func fixtureHasOneTmuxSession() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/snapshot.json")
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
        let tmux = snapshot.sessions.filter(\.tmux)
        #expect(tmux.map(\.key) == ["claude:sess-running-1"])
        #expect(tmux.first?.tmuxSession == "agentboard-dev")
        // Every other row lacks the fields and decodes as not in tmux.
        #expect(snapshot.sessions.filter { !$0.tmux }.allSatisfy { $0.tmuxSession == nil })
    }
}
