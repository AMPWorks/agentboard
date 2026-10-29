import AppKit
import Testing
@testable import AgentBoard

@Suite("Permission mode tag")
struct PermissionModeTests {
    static let documented = ["default", "plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions"]

    @Test func noTagForUnknownEmptyOrDefault() {
        #expect(PermissionMode.tag(nil) == nil)
        #expect(PermissionMode.tag("") == nil)
        #expect(PermissionMode.tag("  ") == nil)
        #expect(PermissionMode.tag("default") == nil)
    }

    @Test func documentedLabels() {
        #expect(PermissionMode.tag("plan") == "plan")
        #expect(PermissionMode.tag("acceptEdits") == "accept edits")
        #expect(PermissionMode.tag("auto") == "auto mode")
        #expect(PermissionMode.tag("dontAsk") == "don't ask")
        #expect(PermissionMode.tag("bypassPermissions") == "bypass")
    }

    @Test func unknownModePassesThrough() {
        #expect(PermissionMode.tag("someNewMode") == "someNewMode")
        #expect(PermissionMode.help("someNewMode").contains("someNewMode"))
    }

    @Test(arguments: documented + ["someNewMode", ""])
    func everyHelpStringIsNonEmpty(mode: String) {
        #expect(!PermissionMode.help(mode).isEmpty)
    }

    @Test(arguments: documented + ["someNewMode"])
    func helpSaysAsOfTheLastHookEvent(mode: String) {
        #expect(PermissionMode.help(mode).hasSuffix("(as of the last hook event)"))
    }

    @Test func rowTagOnLiveSessions() {
        for state in ["running", "needs_you", "review", "interrupted", "snoozed", "unconcluded"] {
            let session = Fixtures.decoded("a", state, alive: true, extra: ["permission_mode": "plan"])
            #expect(PermissionMode.rowTag(for: session) == "plan", "\(state)")
        }
        let manual = Fixtures.decoded("a", "running", alive: true, extra: ["permission_mode": "default"])
        #expect(PermissionMode.rowTag(for: manual) == nil)
    }

    @Test func noRowTagOnceEndedOrDone() {
        let done = Fixtures.decoded("a", "done", extra: ["permission_mode": "bypassPermissions"])
        #expect(PermissionMode.rowTag(for: done) == nil)
        let ended = Fixtures.decoded(
            "b", "review", extra: ["permission_mode": "auto", "ended_at": "2026-10-15T00:00:00Z"])
        #expect(PermissionMode.rowTag(for: ended) == nil)
        // The detail view keeps showing the mode.
        #expect(PermissionMode.detail(done.permissionMode) == "bypassPermissions")
        #expect(PermissionMode.detail(ended.permissionMode) == "auto")
    }

    @Test func helpForNil() {
        #expect(!PermissionMode.help(nil).isEmpty)
    }

    @Test func detailValue() {
        #expect(PermissionMode.detail("acceptEdits") == "acceptEdits")
        #expect(PermissionMode.detail(nil) == "—")
        #expect(PermissionMode.detail("") == "—")
    }

    @Test func decodesFromASnapshotSession() {
        let session = Fixtures.decoded("a", extra: ["permission_mode": "plan"])
        #expect(session.permissionMode == "plan")
        #expect(Fixtures.decoded("b").permissionMode == nil)
        // A malformed value drops the field, not the row.
        #expect(Fixtures.decoded("c", extra: ["permission_mode": 3]).permissionMode == nil)
    }

    @Test func modeChangeMakesSessionsUnequal() {
        let plan = Fixtures.decoded("a", extra: ["permission_mode": "plan"])
        let auto = Fixtures.decoded("a", extra: ["permission_mode": "auto"])
        #expect(plan != auto)
    }

    @Test func sampleFixtureCarriesTheThreeModes() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/snapshot.json")
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
        let modes = Dictionary(uniqueKeysWithValues: snapshot.sessions.map { ($0.key, $0.permissionMode) })
        #expect(modes["claude:sess-needsyou-1"] == "plan")
        #expect(modes["claude:sess-running-1"] == "auto")
        #expect(modes["claude:sess-review-1"] == "default")
        #expect(modes["codex:sess-running-2"] == .some(nil))
        #expect(PermissionMode.tag(modes["claude:sess-review-1"] ?? nil) == nil)
    }
}
