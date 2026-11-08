import Foundation
import Testing
@testable import AgentBoard

/// Every `agentboard snapshot` creates and deletes the database's -wal/-shm
/// files, a write to the watched directory; the watcher must not count that
/// as a change, or each refresh would trigger the next one forever.
@Suite("DirectoryWatcher")
@MainActor
struct DirectoryWatcherTests {
    @Test func everyEventCountsWithoutStampedFiles() {
        let watcher = DirectoryWatcher(stamp: { _ in [] })
        watcher.start(path: "/nonexistent-agentboard-test-dir") {}
        #expect(watcher.consumeEventIfStampsMoved())
        #expect(watcher.consumeEventIfStampsMoved())
        watcher.stop()
    }

    @Test func onlyEventsThatMoveAStampCount() {
        var stamps = [FileStamp(inode: 1, size: 10, modifiedSeconds: 100, modifiedNanoseconds: 0), .missing]
        let watcher = DirectoryWatcher(stamp: { _ in stamps })
        watcher.start(path: "/nonexistent-agentboard-test-dir", stampedFiles: ["db", "db-wal"]) {}
        // A snapshot run: -wal came and went, the database is untouched.
        #expect(!watcher.consumeEventIfStampsMoved())
        // A hook is mid-write: the WAL exists.
        stamps[1] = FileStamp(inode: 2, size: 4096, modifiedSeconds: 101, modifiedNanoseconds: 5)
        #expect(watcher.consumeEventIfStampsMoved())
        #expect(!watcher.consumeEventIfStampsMoved())
        // Checkpointed into the database and the WAL removed.
        stamps = [FileStamp(inode: 1, size: 10, modifiedSeconds: 101, modifiedNanoseconds: 9), .missing]
        #expect(watcher.consumeEventIfStampsMoved())
        #expect(!watcher.consumeEventIfStampsMoved())
        watcher.stop()
    }

    @Test func stampsTellAWriteFromARead() throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentboard-watcher-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let db = dir.appendingPathComponent("agentboard.db").path
        let wal = db + "-wal"
        try Data("one".utf8).write(to: URL(fileURLWithPath: db))
        let before = FileStamp.of([db, wal])
        #expect(before[1] == .missing)
        // A reader's -wal created and deleted leaves the stamps as they were,
        // including while the empty -wal still exists (a snapshot mid-run).
        try Data().write(to: URL(fileURLWithPath: wal))
        #expect(FileStamp.of([db, wal]) == before)
        try FileManager.default.removeItem(atPath: wal)
        _ = try Data(contentsOf: URL(fileURLWithPath: db))
        #expect(FileStamp.of([db, wal]) == before)
        // A write to the database moves its stamp.
        try Data("two!".utf8).write(to: URL(fileURLWithPath: db))
        #expect(FileStamp.of([db, wal]) != before)
    }

    /// The whole path on a real directory: directory event, debounce, stamp
    /// check, `onChange`.
    @Test func onlyAWriteToAStampedFileReachesOnChange() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentboard-watcher-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let db = dir.appendingPathComponent("agentboard.db").path
        let wal = db + "-wal"
        try Data("one".utf8).write(to: URL(fileURLWithPath: db))

        var calls = 0
        let watcher = DirectoryWatcher(debounceInterval: .milliseconds(50))
        watcher.start(path: dir.path, stampedFiles: [db, wal]) { calls += 1 }
        defer { watcher.stop() }

        // A snapshot's empty -wal and an unrelated file, each created and
        // deleted: directory events that touch no stamped content.
        let other = dir.appendingPathComponent("unrelated").path
        try Data("x".utf8).write(to: URL(fileURLWithPath: other))
        try FileManager.default.removeItem(atPath: other)
        try Data().write(to: URL(fileURLWithPath: wal))
        try await Task.sleep(for: .milliseconds(300))
        try FileManager.default.removeItem(atPath: wal)
        try await Task.sleep(for: .milliseconds(300))
        #expect(calls == 0)

        // Replacing the database (atomic: a rename, so the directory sees it).
        try Data("two!".utf8).write(to: URL(fileURLWithPath: db), options: .atomic)
        let deadline = ContinuousClock.now + .seconds(5)
        while calls == 0 && ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        try await Task.sleep(for: .milliseconds(300))
        #expect(calls == 1)
    }

    @Test func storeStampsTheDatabaseItsWalAndConfig() {
        #expect(SnapshotStore.stampedFiles(home: "/data", db: "/data/agentboard.db")
                == ["/data/agentboard.db", "/data/agentboard.db-wal", "/data/config.json"])
    }
}
