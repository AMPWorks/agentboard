import Combine
import Foundation
import Testing
@testable import AgentBoard

/// `SnapshotStore.lastOwnAction`: published only after the action's
/// refresh has landed, so the popover's handler reads the new snapshot.
@MainActor
@Suite("SnapshotStoreOwnAction")
struct SnapshotStoreOwnActionTests {
    private struct Failure: Error {}

    /// A store whose CLI reads a fixture snapshot with session "a".
    private func store() throws -> (SnapshotStore, URL) {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentboard-ownaction-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("snapshot.json")
        let json: [String: Any] = [
            "generated_at": "2026-10-15T00:00:00Z",
            "counts": ["needs_you": 0, "interrupted": 0, "running": 1, "review": 0,
                       "unconcluded": 0, "snoozed": 0, "done": 0],
            "sessions": [Fixtures.session("a", "running")],
        ]
        try JSONSerialization.data(withJSONObject: json).write(to: file)
        return (SnapshotStore(cli: AgentBoardCLI(fixtureOverride: file)), dir)
    }

    /// Records each published action with the session keys in
    /// `store.snapshot` at that moment.
    private final class Recorder {
        var seen: [(SnapshotStore.OwnAction, [String])] = []
        var cancellable: AnyCancellable?
    }

    private func record(_ store: SnapshotStore) -> Recorder {
        let recorder = Recorder()
        recorder.cancellable = store.$lastOwnAction.dropFirst().sink { action in
            if let action { recorder.seen.append((action, store.snapshot.sessions.map(\.key))) }
        }
        return recorder
    }

    @Test func aSuccessfulActionPublishesItsKeyAfterTheRefresh() async throws {
        let (store, dir) = try store()
        defer { try? FileManager.default.removeItem(at: dir) }
        let recorder = record(store)
        let session = Fixtures.decoded("a")
        #expect(await store.perform("test", session) {})
        #expect(await store.perform("test", session) {})
        #expect(recorder.seen.map(\.0.kind) == [.session(key: "a"), .session(key: "a")])
        #expect(recorder.seen.map(\.0.sequence) == [1, 2])
        // The refreshed snapshot was already in place when each published.
        #expect(recorder.seen.map(\.1) == [["a"], ["a"]])
    }

    @Test func aFailedActionPublishesNothing() async throws {
        let (store, dir) = try store()
        defer { try? FileManager.default.removeItem(at: dir) }
        let recorder = record(store)
        #expect(await store.perform("test", Fixtures.decoded("a")) { throw Failure() } == false)
        #expect(recorder.seen.isEmpty)
        #expect(store.lastOwnAction == nil)
    }

    @Test func noteDismissalsPublishOnlyTheirNotes() async throws {
        let (store, dir) = try store()
        defer { try? FileManager.default.removeItem(at: dir) }
        let recorder = record(store)
        let session = Fixtures.decoded("a")
        await store.dismissNote(Fixtures.snapshot([Fixtures.session("a", "running", extra: [
            "notes": [["id": 4, "kind": "update", "text": "n", "ts": "2026-10-15T00:00:00Z",
                       "last_ts": "2026-10-15T00:00:00Z"]],
        ])]).sessions[0].notes[0], of: session)
        await store.dismissNotes(for: session)
        #expect(recorder.seen.map(\.0.kind) == [
            .notes([.ids([4])]),
            .notes([.session(tool: "claude", sessionId: "a", kinds: nil)]),
        ])
    }

    @Test func runStepsPublishesOnBothBranches() async throws {
        let (store, dir) = try store()
        defer { try? FileManager.default.removeItem(at: dir) }
        let recorder = record(store)
        await store.runSteps([1, 2], action: "test", ownAction: .notes([.all])) { _ in }
        await store.runSteps([1, 2], action: "test", ownAction: .notes([.all])) { step in
            if step == 2 { throw Failure() }
        }
        #expect(recorder.seen.map(\.0.kind) == [.notes([.all]), .notes([.all])])
        #expect(recorder.seen.map(\.1) == [["a"], ["a"]])
        // Denials: nothing the popover lists, nothing published.
        await store.runSteps([1], action: "test", ownAction: nil) { _ in }
        #expect(await store.perform("test", Fixtures.decoded("a"), listed: false) {})
        #expect(recorder.seen.count == 2)
    }
}
