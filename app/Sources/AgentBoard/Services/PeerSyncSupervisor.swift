import AppKit
import Foundation
import OSLog

/// Runs `agentboard peer-sync` while any Mac is paired: starts it, restarts
/// it with backoff when it exits, restarts it on wake (its ssh connections
/// died with the sleep), and stops it when the app quits. Never runs in
/// fixture mode. The process itself exits when the app goes away (it
/// watches its parent), so a crash leaves nothing behind.
@MainActor
final class PeerSyncSupervisor {
    private let cli: AgentBoardCLI
    private var process: Process?
    private var wanted = false
    private var restartTask: Task<Void, Never>?
    private var failures = 0
    private var startedAt: Date?
    private var observers: [NSObjectProtocol] = []
    private let log = Logger(subsystem: "io.github.agentboard", category: "peer-sync")

    init(cli: AgentBoardCLI = .shared) {
        self.cli = cli
        let center = NSWorkspace.shared.notificationCenter
        observers.append(center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil,
                                            queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.restartAfterWake() }
        })
        observers.append(NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.stop() }
        })
    }

    /// Whether peer-sync should run: some Mac is paired, the app is not on
    /// fixture data, and other machines' sessions are shown.
    nonisolated static func shouldRun(peerCount: Int, fixtureMode: Bool, showRemote: Bool) -> Bool {
        peerCount > 0 && !fixtureMode && showRemote
    }

    /// The wait before restart number `failures` (1-based): 2 s doubling,
    /// capped at 60 s.
    nonisolated static func restartDelay(failures: Int) -> TimeInterval {
        min(60, 2 * pow(2, Double(max(0, failures - 1))))
    }

    /// Called after every snapshot.
    func update(peerCount: Int, showRemote: Bool) {
        let run = Self.shouldRun(peerCount: peerCount, fixtureMode: cli.isFixtureMode, showRemote: showRemote)
        guard run != wanted else { return }
        wanted = run
        if run {
            failures = 0
            launch()
        } else {
            stop()
        }
    }

    func stop() {
        wanted = false
        restartTask?.cancel()
        restartTask = nil
        if let process, process.isRunning {
            process.terminate()
        }
        process = nil
    }

    private func restartAfterWake() {
        guard wanted else { return }
        log.notice("wake: restarting peer-sync")
        failures = 0
        let old = process
        process = nil
        old?.terminate()
        scheduleRestart(after: 1)
    }

    private func launch() {
        guard wanted, process == nil else { return }
        guard let executable = cli.resolveExecutablePath() else {
            log.error("peer-sync not started: agentboard CLI not found")
            return
        }
        let child = Process()
        child.executableURL = URL(fileURLWithPath: executable)
        child.arguments = ["peer-sync"]
        child.environment = CLIRunner.environment(
            base: ProcessInfo.processInfo.environment,
            verbose: UserDefaults.standard.bool(forKey: CLIRunner.verboseLoggingKey))
        child.standardInput = FileHandle.nullDevice
        child.standardOutput = FileHandle.nullDevice
        child.standardError = FileHandle.nullDevice
        child.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            Task { @MainActor in self?.exited(finished, status: status) }
        }
        do {
            try child.run()
            process = child
            startedAt = Date()
            log.notice("peer-sync started pid=\(child.processIdentifier, privacy: .public)")
        } catch {
            log.error("peer-sync launch failed: \(error.localizedDescription, privacy: .public)")
            failures += 1
            scheduleRestart(after: Self.restartDelay(failures: failures))
        }
    }

    private func exited(_ finished: Process, status: Int32) {
        guard finished === process else { return }
        process = nil
        log.notice("peer-sync exited status=\(status, privacy: .public)")
        guard wanted else { return }
        // A run that lasted a while was healthy: start the backoff over.
        if let startedAt, Date().timeIntervalSince(startedAt) > 120 { failures = 0 }
        failures += 1
        scheduleRestart(after: Self.restartDelay(failures: failures))
    }

    private func scheduleRestart(after delay: TimeInterval) {
        restartTask?.cancel()
        restartTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled else { return }
            self?.launch()
        }
    }
}
