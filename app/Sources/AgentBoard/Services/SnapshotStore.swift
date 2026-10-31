import AppKit
import Foundation
import OSLog

/// Owns the current snapshot, polling loop, directory watcher, and the
/// background link-refresh / backfill timers. All state lives on MainActor;
/// CLI calls hop off to the cooperative pool via `CLIRunner`.
@MainActor
final class SnapshotStore: ObservableObject {
    @Published private(set) var snapshot: Snapshot = .empty
    @Published private(set) var lastUpdated: Date?
    /// The last snapshot failure, cleared by the next successful snapshot.
    @Published private(set) var lastError: String?
    /// The last failed action (mark, focus, dismiss, prune), cleared by the
    /// next successful action or after `StoreStatus.actionErrorLifetime`.
    @Published private(set) var lastActionError: ActionError?
    /// When the oldest `refresh()` still in flight began (reset when one
    /// succeeds); nil while none is running. Drives the stale banner.
    @Published private(set) var refreshStartedAt: Date?
    /// When `start()` ran.
    @Published private(set) var startedAt: Date?
    @Published var showAll: Bool = false
    /// Set by the popover when it needs the dashboard to open with a
    /// specific session selected; the dashboard clears it after reading it.
    @Published var pendingSelection: String?
    /// Set with `pendingSelection` when a popover update row asks the
    /// dashboard to show one note; the session detail scrolls to it,
    /// highlights it and clears it.
    @Published var pendingNote: NoteFocus?
    /// The session selected in the dashboard while it is open, so a
    /// finished turn the user is looking at does not notify. Written by
    /// `DashboardView`, cleared when it disappears.
    @Published var dashboardSelection: String?
    /// The popover's filter text. Held here, not in the view, so it
    /// survives closing and reopening the popover; kept in memory only
    /// (cleared on relaunch). The menu bar icon never reads it.
    @Published var popoverQuery: String = ""
    /// AgentBoard's data directory (`home` from `agentboard paths --json`,
    /// or the fallback when that fails), where agentboard.log lives. Nil
    /// until resolved at start.
    @Published private(set) var dataDirectory: String?

    /// `<data dir>/agentboard.log`, once the data directory is resolved.
    /// Settings → Diagnostics and the gear menu's Open Log reveal it.
    var logFileURL: URL? {
        dataDirectory.map { URL(fileURLWithPath: $0).appendingPathComponent("agentboard.log") }
    }

    /// Whether `logFileURL` exists yet (nothing has logged before it does).
    var logFileExists: Bool {
        logFileURL.map { FileManager.default.fileExists(atPath: $0.path) } ?? false
    }

    /// Reveals agentboard.log in Finder.
    func revealLog() {
        guard let url = logFileURL else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    /// `<data dir>/hook-errors.log`, the hook's own failure log.
    var hookErrorsLogURL: URL? {
        dataDirectory.map { URL(fileURLWithPath: $0).appendingPathComponent("hook-errors.log") }
    }

    /// hook-errors.log's size in bytes and last write, or nil when it is
    /// missing or empty (Settings → Diagnostics shows a row only then).
    var hookErrorsLogInfo: (size: Int64, modified: Date)? {
        guard let url = hookErrorsLogURL,
              let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = (attributes[.size] as? NSNumber)?.int64Value, size > 0,
              let modified = attributes[.modificationDate] as? Date
        else { return nil }
        return (size, modified)
    }

    /// Reveals hook-errors.log in Finder.
    func revealHookErrorsLog() {
        guard let url = hookErrorsLogURL else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    /// The status banner to show at `now`, or nil when healthy.
    func banner(now: Date = Date()) -> StoreStatus.Banner? {
        StoreStatus.banner(lastError: lastError, lastActionError: lastActionError,
                           lastUpdated: lastUpdated, refreshStartedAt: refreshStartedAt,
                           startedAt: startedAt, now: now)
    }

    /// The banner's x for an action error.
    func dismissActionError() {
        lastActionError = nil
    }

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
    /// `refresh()` calls in flight (the poll loop, the watcher and the
    /// views each start one).
    private var refreshesInFlight = 0
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
        startedAt = Date()
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
            // The launch prune most often fails because install.sh's own
            // backfill still holds the write lock; log it, retry once after
            // refresh-links, and only show the banner if that fails too.
            let launchPruneOK = await self?.prune(showError: false) ?? true
            var lastPrune = Date()
            await self?.refreshLinks()
            if !launchPruneOK {
                try? await Task.sleep(for: .seconds(Self.pruneLaunchRetryDelay))
                guard !Task.isCancelled else { return }
                await self?.prune()
                lastPrune = Date()
            }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(Self.linksInterval))
                guard !Task.isCancelled else { return }
                await self?.refreshLinks()
                if Date().timeIntervalSince(lastPrune) >= Self.pruneInterval {
                    await self?.prune()
                    lastPrune = Date()
                }
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
        refreshesInFlight += 1
        if refreshStartedAt == nil { refreshStartedAt = Date() }
        defer {
            refreshesInFlight -= 1
            if refreshesInFlight == 0 { refreshStartedAt = nil }
        }
        do {
            let snapshot = try await cli.snapshot(all: showAll)
            self.snapshot = snapshot
            self.lastUpdated = Date()
            self.lastError = nil
            // The CLI just answered; a refresh still running started before
            // that and is not yet a hang.
            if refreshesInFlight > 1 { refreshStartedAt = Date() }
        } catch {
            self.lastError = error.localizedDescription
            log.error("snapshot refresh failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Shows a failed action in the banner; it clears itself after
    /// `StoreStatus.actionErrorLifetime` unless a newer one replaced it.
    private func recordActionError(_ action: String, _ error: Error) {
        let entry = ActionError(action: action, message: error.localizedDescription, at: Date(),
                                remedy: (error as? FocusError)?.remedy)
        lastActionError = entry
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(StoreStatus.actionErrorLifetime))
            guard let self, self.lastActionError == entry else { return }
            self.lastActionError = nil
        }
    }

    private func refreshLinks() async {
        do {
            try await cli.refreshLinks()
        } catch {
            log.error("refresh-links failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// `refresh-links` cadence.
    static let linksInterval: TimeInterval = 600
    /// How often the app asks for `prune --auto`; the CLI's own 24 h gate
    /// decides whether anything runs.
    static let pruneInterval: TimeInterval = 6 * 3600
    /// How long the launch prune waits before its one retry.
    static let pruneLaunchRetryDelay: TimeInterval = 60

    /// Runs `prune --auto`; returns whether it succeeded. A failure is always
    /// logged, and shown in the banner unless `showError` is false.
    @discardableResult
    private func prune(showError: Bool = true) async -> Bool {
        do {
            try await cli.prune()
            return true
        } catch {
            log.error("prune failed: \(error.localizedDescription, privacy: .public)")
            if showError { recordActionError("Prune", error) }
            return false
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

    /// Returns false when `agentboard focus` failed (the tab was closed,
    /// the terminal quit), so a notification click can open the dashboard.
    @discardableResult
    func focus(_ session: Session) async -> Bool {
        await perform("focus", session) { try await cli.focus(tool: session.tool, sessionId: session.sessionId) }
    }

    /// Focus from a click (popover row, dashboard double-click/Return, the
    /// Focus menu item and button, a notification). On failure the terminal
    /// may already be in front (it is still brought forward when the tab is
    /// missing or macOS stayed on this desktop), so open the dashboard on the
    /// session in front of it: its banner says what went wrong.
    func focusOrShowDashboard(_ session: Session) async {
        guard await !focus(session) else { return }
        requestSelection(session.key)
        DashboardWindowController.show()
    }

    /// The banner's settings button for a failed focus.
    func openSettings(for remedy: FocusRemedy) {
        log.notice("open settings remedy=\(String(describing: remedy), privacy: .public)")
        NSWorkspace.shared.open(remedy.settingsURL)
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

    /// The popover's grouped-row x and its "Clear all"/"Clear matching"
    /// (`UpdatesFeed.clearPlan`): runs each dismissal in order, then
    /// refreshes once.
    func dismiss(_ dismissals: [NoteDismissal], action name: String) async {
        guard !dismissals.isEmpty else { return }
        log.notice("action \(name, privacy: .public) steps=\(dismissals.count, privacy: .public)")
        do {
            for dismissal in dismissals {
                try await cli.dismiss(dismissal)
            }
            lastActionError = nil
            await refresh()
        } catch {
            recordActionError(Self.actionTitle(name), error)
            log.error("action \(name, privacy: .public) failed: \(error.localizedDescription, privacy: .public)")
            await refresh()
        }
    }

    /// Asks the dashboard to select a session and, with `note`, to scroll
    /// its detail to that note. The note request is set first, so it is
    /// already in place when the dashboard consumes the selection.
    func requestSelection(_ key: String, note: Int? = nil) {
        pendingNote = note.map { NoteFocus(sessionKey: key, noteId: $0) }
        pendingSelection = key
    }

    func setShowAll(_ showAll: Bool) {
        guard showAll != self.showAll else { return }
        self.showAll = showAll
        Task { await refresh() }
    }

    /// "mark done" -> "Mark done", for the banner's "<action> failed: ...".
    static func actionTitle(_ name: String) -> String {
        name.prefix(1).uppercased() + name.dropFirst()
    }

    /// Runs a user action against the CLI and logs it by name, so `log
    /// show` ties a row click to the CLIRunner invocation that follows it.
    /// Returns whether the action succeeded.
    @discardableResult
    private func perform(_ name: String, _ session: Session, _ action: () async throws -> Void) async -> Bool {
        let key = session.key
        log.notice("action \(name, privacy: .public) key=\(key, privacy: .public)")
        do {
            try await action()
            lastActionError = nil
            await refresh()
            return true
        } catch {
            recordActionError(Self.actionTitle(name), error)
            log.error("action \(name, privacy: .public) key=\(key, privacy: .public) failed: \(error.localizedDescription, privacy: .public)")
            return false
        }
    }
}
