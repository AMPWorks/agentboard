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

    /// One of the owner's own actions finished and its refresh landed: the
    /// popover's held list (`PopoverHold`) applies that action alone. The
    /// sequence number makes a repeat of the same action publish again.
    struct OwnAction: Equatable, Sendable {
        /// What the held list applies: one session's live value, or only
        /// the notes a dismissal named.
        let kind: PopoverHold.OwnAction
        let sequence: Int
    }

    @Published private(set) var lastOwnAction: OwnAction?

    private func recordOwnAction(_ kind: PopoverHold.OwnAction) {
        lastOwnAction = OwnAction(kind: kind, sequence: (lastOwnAction?.sequence ?? 0) + 1)
    }

    /// `<data dir>/agentboard.log`, once the data directory is resolved.
    /// Settings → Advanced → Diagnostics and the gear menu's Open Log reveal it.
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
    /// missing or empty (Settings → Advanced → Diagnostics shows a row only then).
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
    private lazy var peerSync = PeerSyncSupervisor(cli: cli)
    /// The latest snapshot as the CLI sent it, before Settings → Machines'
    /// "Show sessions from other machines" filter.
    private var rawSnapshot: Snapshot = .empty
    /// Paired Macs, this Mac's pairing code and Remote Login status, for
    /// Settings → Machines (`refreshPeers`).
    @Published private(set) var peersInfo: PeersInfo?
    @Published private(set) var peersError: String?
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
    /// The fallback is the one exception, and its database path is a best
    /// guess: if the CLI ever renamed or moved the database, the watcher
    /// would stamp a file that never changes and only the poll would
    /// refresh, so the guessed paths are logged with the failure.
    private func resolveDataDirectory() async -> (home: String, db: String) {
        do {
            let paths = try await cli.paths()
            return (paths.home, paths.db)
        } catch {
            let home = Self.fallbackDataDirectory()
            let db = (home as NSString).appendingPathComponent("agentboard.db")
            log.error("agentboard paths --json failed, guessing home \(home, privacy: .public) and db \(db, privacy: .public): \(error.localizedDescription, privacy: .public)")
            return (home, db)
        }
    }

    /// The files whose writes mean the snapshot may have changed: the
    /// database, its WAL and config.json. The watcher ignores directory
    /// events that leave all three untouched, such as the `-wal`/`-shm`
    /// files each `snapshot` run creates and deletes (`DirectoryWatcher`).
    /// `peers.stamp` is rewritten by `peer-sync` after each update from a
    /// paired Mac (its caches live in `peers/`, which the non-recursive
    /// watcher doesn't see).
    static func stampedFiles(home: String, db: String) -> [String] {
        [db, db + "-wal", (home as NSString).appendingPathComponent("config.json"),
         (home as NSString).appendingPathComponent("peers.stamp")]
    }

    func start() {
        guard pollTask == nil else { return }
        startedAt = Date()
        pathsTask = Task { [weak self] in
            guard let self else { return }
            let (directory, db) = await self.resolveDataDirectory()
            guard !Task.isCancelled else { return }
            self.dataDirectory = directory
            self.watcher.start(path: directory,
                               stampedFiles: Self.stampedFiles(home: directory, db: db)) { [weak self] in
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
        peerSync.stop()
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
            rawSnapshot = snapshot
            applyRemoteFilter()
            self.lastUpdated = Date()
            self.lastError = nil
            // The CLI just answered; a refresh still running started before
            // that and is not yet a hang.
            if refreshesInFlight > 1 { refreshStartedAt = Date() }
        } catch {
            self.lastError = StoreStatus.snapshotErrorMessage(error)
            log.error("snapshot refresh failed: \(String(describing: error), privacy: .public)")
        }
    }

    static var showRemote: Bool {
        UserDefaults.standard.object(forKey: SettingsKeys.showRemoteSessions) as? Bool ?? true
    }

    /// Publishes `rawSnapshot`, without remote sessions when they are
    /// hidden, and starts or stops `peer-sync` to match.
    func applyRemoteFilter() {
        let show = Self.showRemote
        let shown = show ? rawSnapshot : rawSnapshot.withoutRemote()
        if shown != snapshot { snapshot = shown }
        peerSync.update(peerCount: rawSnapshot.peers.count, showRemote: show)
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
        await perform("mark done", session) { try await cli.mark(tool: session.tool, sessionId: session.sessionId, action: "done", machine: session.machine?.id) }
    }

    func markOpen(_ session: Session) async {
        await perform("mark open", session) { try await cli.mark(tool: session.tool, sessionId: session.sessionId, action: "open", machine: session.machine?.id) }
    }

    func snooze(_ session: Session, hours: Int) async {
        await perform("snooze \(hours)h", session) { try await cli.mark(tool: session.tool, sessionId: session.sessionId, action: "snooze", hours: hours, machine: session.machine?.id) }
    }

    /// Returns false when `agentboard focus` failed (the tab was closed,
    /// the terminal quit), so a notification click can open the dashboard.
    @discardableResult
    func focus(_ session: Session, via: String? = nil) async -> Bool {
        await perform("focus", session) {
            try Self.checkReachable(session)
            try await cli.focus(tool: session.tool, sessionId: session.sessionId, machine: session.machine?.id,
                                via: via)
        }
    }

    /// Focus from a click (popover row, dashboard double-click/Return, the
    /// Focus menu item and button, a notification). On failure the terminal
    /// may already be in front (it is still brought forward when the tab is
    /// missing or macOS stayed on this desktop), so open the dashboard on the
    /// session in front of it: its banner says what went wrong.
    /// `via` ("screen", "browser") is only for the explicit context menu
    /// items on another Mac's rows; every other click passes nil.
    func focusOrShowDashboard(_ session: Session, via: String? = nil) async {
        guard await !focus(session, via: via) else { return }
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
        await perform("resume", session) {
            try Self.checkReachable(session)
            try await cli.resume(tool: session.tool, sessionId: session.sessionId, printOnly: false,
                                 machine: session.machine?.id)
        }
    }

    /// Fetches the resume command text for copying, without launching
    /// anything. For a remote session it is the local `peer-open` command.
    func resumeCommand(for session: Session) async -> String {
        (try? await cli.resume(tool: session.tool, sessionId: session.sessionId, printOnly: true,
                               machine: session.machine?.id)) ?? session.resumeCommand
    }

    /// A stale remote session's Mac is offline: say so instead of opening a
    /// terminal whose ssh would only hang.
    static func checkReachable(_ session: Session) throws {
        guard session.isRemote, session.stale else { return }
        throw PeerUnreachable(name: session.machine?.name ?? "The other Mac")
    }

    // MARK: - Machines (Settings → Machines)

    /// Reloads `peersInfo` (`agentboard peers list --json`).
    func refreshPeers() async {
        do {
            peersInfo = try await cli.peersList()
            peersError = nil
        } catch {
            peersError = error.localizedDescription
            log.error("peers list failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Checks a pasted pairing code without changing anything.
    func previewPeer(code: String) async -> Result<PeerPreview, Error> {
        do { return .success(try await cli.peersPreview(code: code)) } catch { return .failure(error) }
    }

    /// Runs one `peers` change, then reloads the list and the board.
    /// Returns the error message on failure.
    @discardableResult
    func changePeers(_ name: String, _ change: () async throws -> Void) async -> String? {
        log.notice("action \(name, privacy: .public)")
        do {
            try await change()
            peersError = nil
            await refreshPeers()
            await refresh()
            return nil
        } catch {
            let message = error.localizedDescription
            peersError = message
            log.error("action \(name, privacy: .public) failed: \(message, privacy: .public)")
            await refreshPeers()
            return message
        }
    }

    func addPeer(code: String, host: String, allowOpen: Bool) async -> String? {
        await changePeers("add machine") { try await cli.peersAdd(code: code, host: host, allowOpen: allowOpen) }
    }

    func removePeer(_ id: String) async -> String? {
        await changePeers("remove machine") { try await cli.peersRemove(id: id) }
    }

    func setPeer(_ id: String, host: String? = nil, name: String? = nil, allowOpen: Bool? = nil,
                 dismissAddress: String? = nil, alias: String? = nil, clearAlias: Bool = false) async -> String? {
        await changePeers("set machine") {
            try await cli.peersSet(id: id, host: host, name: name, allowOpen: allowOpen, dismissAddress: dismissAddress,
                                   alias: alias, clearAlias: clearAlias)
        }
    }

    func setThisMac(name: String? = nil, share: String? = nil, address: String? = nil,
                    autoAddress: Bool = false) async -> String? {
        await changePeers("set this mac") {
            try await cli.peersSetSelf(name: name, share: share, address: address, autoAddress: autoAddress)
        }
    }

    /// Dismisses one note (the popover's and the detail view's per-note x).
    func dismissNote(_ note: Note, of session: Session) async {
        // Note ids are per database: never send a remote one to this Mac's CLI.
        guard !session.isRemote else { return }
        await perform("dismiss note \(note.id)", session, ownAction: .notes([.ids([note.id])])) { try await cli.dismissNotes(ids: [note.id]) }
    }

    /// Dismisses every open note of one session.
    func dismissNotes(for session: Session) async {
        guard !session.isRemote else { return }
        await perform("dismiss notes", session,
                      ownAction: .notes([.session(tool: session.tool, sessionId: session.sessionId, kinds: nil)])) { try await cli.dismissNotes(tool: session.tool, sessionId: session.sessionId) }
    }

    /// The popover's grouped-row x and its "Clear all"/"Clear matching"
    /// (`UpdatesFeed.clearPlan`): runs each dismissal in order, then
    /// refreshes once.
    func dismiss(_ dismissals: [NoteDismissal], action name: String) async {
        await runSteps(dismissals, action: name, ownAction: .notes(dismissals)) { try await cli.dismiss($0) }
    }

    /// Clears one permission denial (a Denials row's or the detail's x).
    func dismissDenial(_ denial: Denial, of session: Session) async {
        guard !session.isRemote else { return }
        await perform("dismiss denial \(denial.id)", session, listed: false) { try await cli.dismissDenials(ids: [denial.id]) }
    }

    /// Clears every open denial of one session.
    func dismissDenials(for session: Session) async {
        guard !session.isRemote else { return }
        await perform("dismiss denials", session, listed: false) {
            try await cli.dismissDenials(tool: session.tool, sessionId: session.sessionId)
        }
    }

    /// The Denials header's "Clear all"/"Clear matching"
    /// (`DenialFeed.clearPlan`): runs each dismissal in order, then
    /// refreshes once.
    func dismissDenials(_ dismissals: [DenialDismissal], action name: String) async {
        await runSteps(dismissals, action: name, ownAction: nil) { try await cli.dismiss($0) }
    }

    /// `ownAction` is published once the refresh lands, on both branches
    /// (earlier steps may have dismissed notes even when a later one
    /// failed); nil publishes nothing (denials, which the popover does not
    /// list). Internal for tests.
    func runSteps<Step: Sendable>(_ steps: [Step], action name: String, ownAction: PopoverHold.OwnAction?,
                                  _ run: (Step) async throws -> Void) async {
        guard !steps.isEmpty else { return }
        log.notice("action \(name, privacy: .public) steps=\(steps.count, privacy: .public)")
        do {
            for step in steps {
                try await run(step)
            }
            lastActionError = nil
            await refresh()
        } catch {
            recordActionError(Self.actionTitle(name), error)
            log.error("action \(name, privacy: .public) failed: \(error.localizedDescription, privacy: .public)")
            await refresh()
        }
        if let ownAction { recordOwnAction(ownAction) }
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
    /// Returns whether the action succeeded. On success, after the refresh,
    /// publishes `ownAction` (nil: the session's own key) unless `listed`
    /// is false (an action the popover list does not show). Internal for
    /// tests.
    @discardableResult
    func perform(_ name: String, _ session: Session, ownAction: PopoverHold.OwnAction? = nil, listed: Bool = true,
                 _ action: () async throws -> Void) async -> Bool {
        let key = session.key
        log.notice("action \(name, privacy: .public) key=\(key, privacy: .public)")
        do {
            try await action()
            lastActionError = nil
            await refresh()
            if listed { recordOwnAction(ownAction ?? .session(key: key)) }
            return true
        } catch {
            recordActionError(Self.actionTitle(name), error)
            log.error("action \(name, privacy: .public) key=\(key, privacy: .public) failed: \(error.localizedDescription, privacy: .public)")
            return false
        }
    }
}
