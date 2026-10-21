import Foundation
import OSLog

/// Owns the current snapshot, polling loop, directory watcher, and the
/// background link-refresh / backfill timers. All state lives on MainActor;
/// CLI calls hop off to the cooperative pool via `CLIRunner`.
@MainActor
final class SnapshotStore: ObservableObject {
    @Published private(set) var snapshot: Snapshot = .empty
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var lastError: String?
    @Published var showAll: Bool = false
    /// Set by the popover when it needs the dashboard to open with a
    /// specific session selected; the dashboard clears it after reading it.
    @Published var pendingSelection: String?
    /// The popover's filter text. Held here, not in the view, so it
    /// survives closing and reopening the popover; kept in memory only
    /// (cleared on relaunch). The menu bar icon never reads it.
    @Published var popoverQuery: String = ""
    /// AgentBoard's data directory (`home` from `agentboard paths --json`,
    /// or the fallback when that fails), where agentboard.log lives. Nil
    /// until resolved at start.
    @Published private(set) var dataDirectory: String?

    private let cli: AgentBoardCLI
    private let watcher = DirectoryWatcher()
    private var pollTask: Task<Void, Never>?
    private var linksTask: Task<Void, Never>?
    private var pathsTask: Task<Void, Never>?
    /// How many surfaces (the popover, the dashboard window) are currently
    /// showing sessions. A count rather than a flag: closing the popover
    /// while the dashboard is still open must not drop polling back to
    /// the slow background interval.
    private var visibleCount = 0
    private var isVisible: Bool { visibleCount > 0 }
    private let log = Logger(subsystem: "io.github.agentboard", category: "store")

    /// Fallback used only if `agentboard paths --json` can't be called (not
    /// found, fixture mode, non-zero exit, or unparseable output): the
    /// AGENTBOARD_HOME override, then the same default agentboard.db itself
    /// falls back to.
    private static func fallbackDataDirectory() -> String {
        if let override = ProcessInfo.processInfo.environment["AGENTBOARD_HOME"], !override.isEmpty {
            return (override as NSString).expandingTildeInPath
        }
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
        return base?.appendingPathComponent("AgentBoard").path
            ?? ("~/Library/Application Support/AgentBoard" as NSString).expandingTildeInPath
    }

    init(cli: AgentBoardCLI = .shared) {
        self.cli = cli
    }

    /// Resolves the directory the watcher should watch by calling `agentboard
    /// paths --json` once -- the CLI is the one source of truth for where it
    /// actually reads/writes (it alone knows about a baked-in AGENTBOARD_HOME
    /// from install time), so this must never be computed independently here.
    private func resolveDataDirectory() async -> String {
        do {
            return try await cli.paths().home
        } catch {
            log.error("agentboard paths --json failed, falling back: \(error.localizedDescription, privacy: .public)")
            return Self.fallbackDataDirectory()
        }
    }

    func start() {
        guard pollTask == nil else { return }
        pathsTask = Task { [weak self] in
            guard let self else { return }
            let directory = await self.resolveDataDirectory()
            guard !Task.isCancelled else { return }
            self.dataDirectory = directory
            self.watcher.start(path: directory) { [weak self] in
                Task { await self?.refresh() }
            }
        }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                let seconds = (self?.isVisible ?? false) ? 3 : 15
                try? await Task.sleep(for: .seconds(seconds))
            }
        }
        linksTask = Task { [weak self] in
            await self?.runInitialBackfill()
            guard !Task.isCancelled else { return }
            await self?.refreshLinks()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(600))
                guard !Task.isCancelled else { return }
                await self?.refreshLinks()
            }
        }
    }

    func stop() {
        pollTask?.cancel(); pollTask = nil
        linksTask?.cancel(); linksTask = nil
        pathsTask?.cancel(); pathsTask = nil
        watcher.stop()
    }

    func setVisible(_ visible: Bool) {
        visibleCount = max(0, visibleCount + (visible ? 1 : -1))
        if visible { Task { await refresh() } }
    }

    func refresh() async {
        do {
            let snapshot = try await cli.snapshot(all: showAll)
            self.snapshot = snapshot
            self.lastUpdated = Date()
            self.lastError = nil
        } catch {
            self.lastError = error.localizedDescription
            log.error("snapshot refresh failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    private func refreshLinks() async {
        do {
            try await cli.refreshLinks()
        } catch {
            log.error("refresh-links failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    private func runInitialBackfill() async {
        do {
            try await cli.backfill(days: 30)
        } catch {
            log.error("backfill failed: \(error.localizedDescription, privacy: .public)")
        }
        await refresh()
    }

    // MARK: - Actions

    func markDone(_ session: Session) async {
        await perform("mark done", session) { try await cli.mark(tool: session.tool, sessionId: session.sessionId, action: "done") }
    }

    func markOpen(_ session: Session) async {
        await perform("mark open", session) { try await cli.mark(tool: session.tool, sessionId: session.sessionId, action: "open") }
    }

    func snooze(_ session: Session, hours: Int) async {
        await perform("snooze \(hours)h", session) { try await cli.mark(tool: session.tool, sessionId: session.sessionId, action: "snooze", hours: hours) }
    }

    func focus(_ session: Session) async {
        await perform("focus", session) { try await cli.focus(tool: session.tool, sessionId: session.sessionId) }
    }

    /// Actually resumes the session (opens a new terminal tab running the resume command).
    func resume(_ session: Session) async {
        await perform("resume", session) { try await cli.resume(tool: session.tool, sessionId: session.sessionId, printOnly: false) }
    }

    /// Fetches the resume command text for copying, without launching anything.
    func resumeCommand(for session: Session) async -> String {
        (try? await cli.resume(tool: session.tool, sessionId: session.sessionId, printOnly: true)) ?? session.resumeCommand
    }

    /// Dismisses one note (the popover's and the detail view's per-note x).
    func dismissNote(_ note: Note, of session: Session) async {
        await perform("dismiss note \(note.id)", session) { try await cli.dismissNotes(ids: [note.id]) }
    }

    /// Dismisses every open note of one session.
    func dismissNotes(for session: Session) async {
        await perform("dismiss notes", session) { try await cli.dismissNotes(tool: session.tool, sessionId: session.sessionId) }
    }

    /// The popover's "Clear all": dismisses exactly the notes it shows (the
    /// filtered feed), not notes a query hides or the 20-per-session cap cut.
    func dismissNotes(_ notes: [Note]) async {
        let ids = notes.map(\.id)
        guard !ids.isEmpty else { return }
        log.notice("action dismiss notes count=\(ids.count, privacy: .public)")
        do {
            try await cli.dismissNotes(ids: ids)
            await refresh()
        } catch {
            self.lastError = error.localizedDescription
            log.error("action dismiss notes failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    func requestSelection(_ key: String) {
        pendingSelection = key
    }

    func setShowAll(_ showAll: Bool) {
        guard showAll != self.showAll else { return }
        self.showAll = showAll
        Task { await refresh() }
    }

    /// Runs a user action against the CLI and logs it by name, so `log
    /// show` ties a row click to the CLIRunner invocation that follows it.
    private func perform(_ name: String, _ session: Session, _ action: () async throws -> Void) async {
        let key = session.key
        log.notice("action \(name, privacy: .public) key=\(key, privacy: .public)")
        do {
            try await action()
            await refresh()
        } catch {
            self.lastError = error.localizedDescription
            log.error("action \(name, privacy: .public) key=\(key, privacy: .public) failed: \(error.localizedDescription, privacy: .public)")
        }
    }
}
