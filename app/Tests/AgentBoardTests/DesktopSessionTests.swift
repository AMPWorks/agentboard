import Foundation
import Testing
@testable import AgentBoard

@Suite("Claude desktop app sessions")
struct DesktopSessionTests {
    @Test func decodesDesktopFields() {
        let session = Fixtures.decoded("d1", "review", extra: [
            "entrypoint": "claude-desktop", "desktop": true, "can_focus": true,
        ])
        #expect(session.desktop)
        #expect(session.entrypoint == "claude-desktop")
    }

    @Test func missingFieldsAreNotDesktop() {
        let session = Fixtures.decoded("s1")
        #expect(!session.desktop)
        #expect(session.entrypoint == nil)
        #expect(SessionActionHelp.focusTitle(session) == "Focus")
        #expect(SessionActionHelp.focusHelp(session) == SessionActionHelp.focus)
    }

    @Test func entrypointAloneImpliesDesktopWhenTheFlagIsMissing() {
        #expect(Fixtures.decoded("d1", extra: ["entrypoint": "claude-desktop"]).desktop)
        #expect(!Fixtures.decoded("c1", extra: ["entrypoint": "cli"]).desktop)
    }

    @Test func malformedValuesDegrade() {
        let session = Fixtures.decoded("d1", extra: ["entrypoint": 7, "desktop": "yes"])
        #expect(!session.desktop)
        #expect(session.entrypoint == nil)
    }

    @Test func aDesktopSessionIsFocusableEvenWhenItsProcessIsGone() {
        let idle = Fixtures.decoded("d1", "review", alive: false, extra: [
            "desktop": true, "can_focus": true,
        ])
        #expect(idle.isFocusable)
        // A dead terminal session is not.
        let dead = Fixtures.decoded("t1", "review", alive: false, extra: ["can_focus": true])
        #expect(!dead.isFocusable)
    }

    @Test func resumeIsHiddenForDesktopSessions() {
        let desktop = Fixtures.decoded("d1", "review", alive: false, extra: [
            "desktop": true, "can_focus": true,
        ])
        #expect(!desktop.resumeAvailable)
        let terminal = Fixtures.decoded("t1", "review", alive: false)
        #expect(terminal.resumeAvailable)
    }

    @Test func focusHelpAndTitleNameTheDesktopApp() {
        let session = Fixtures.decoded("d1", extra: [
            "desktop": true, "can_focus": true, "tmux": true,
        ])
        #expect(SessionActionHelp.focusHelp(session) == SessionActionHelp.focusDesktop)
        #expect(SessionActionHelp.focusTitle(session) == "Open in Claude App")
        #expect(SessionActionHelp.desktopIcon.contains("Claude desktop app"))
    }

    @Test func aDesktopFlagChangeIsAChange() {
        let before = Fixtures.decoded("d1", extra: ["desktop": false])
        let after = Fixtures.decoded("d1", extra: ["desktop": true])
        #expect(before != after)
    }

    /// Desktop rows reached only through a query (the popover's Snoozed and
    /// Done sections) take the same click path as any other section: focus
    /// in the desktop app, never the dashboard fallback.
    @Test func queryOnlySectionsFocusDesktopRows() {
        let sessions = Fixtures.snapshot(["snoozed", "done", "review"].map { state in
            Fixtures.session("d-\(state)", state, extra: [
                "entrypoint": "claude-desktop", "desktop": true, "can_focus": true,
            ])
        }).sessions
        let plain = PopoverSections.make(from: sessions, query: SessionSearch(""))
        #expect(plain.section(.snoozed) == nil && plain.section(.done) == nil)
        let filtered = PopoverSections.make(from: sessions, query: SessionSearch("desktop"))
        for kind in [PopoverSections.Kind.snoozed, .done, .review] {
            let rows = filtered.section(kind)?.sessions ?? []
            #expect(rows.count == 1, "\(kind)")
            #expect(rows.allSatisfy { SessionRowView.popoverTap(for: $0) == .focus }, "\(kind)")
        }
        let dead = Fixtures.decoded("t1", "done", alive: false, extra: ["can_focus": true])
        #expect(SessionRowView.popoverTap(for: dead) == .showDashboard)
    }

    @Test func fixtureHasOneDesktopSession() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/snapshot.json")
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
        let desktop = snapshot.sessions.filter(\.desktop)
        #expect(desktop.map(\.key) == ["claude:sess-review-2"])
        #expect(desktop.allSatisfy { $0.isFocusable && !$0.resumeAvailable })
    }
}
