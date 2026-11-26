import AppKit
import Combine
import OSLog

/// When the flash on the menu bar icon stops, beyond "the session left
/// needs_you", which always stops it. Backs the Settings picker.
enum FlashStopPolicy: String, CaseIterable, Identifiable, Sendable {
    /// Stops when the popover opens or the dashboard becomes key. No timeout.
    case onOpen
    case after30Seconds
    case after2Minutes
    /// Ignores acknowledgement: flashes until every flashing session is handled.
    case never

    var id: String { rawValue }

    var label: String {
        switch self {
        case .onOpen: return "When I open AgentBoard"
        case .after30Seconds: return "After 30 seconds"
        case .after2Minutes: return "After 2 minutes"
        case .never: return "Never (until handled)"
        }
    }

    var timeout: Duration? {
        switch self {
        case .after30Seconds: return .seconds(30)
        case .after2Minutes: return .seconds(120)
        case .onOpen, .never: return nil
        }
    }

    var honorsAcknowledgement: Bool { self != .never }
}

/// Decides whether the menu bar icon should flash because a session newly
/// needs the user, and drives the flash phase.
///
/// Acknowledgement is tracked per session key: a key that entered needs_you
/// flashes until it is acknowledged (popover opened, dashboard made key, or
/// the timeout), and stays acknowledged until it leaves needs_you. A new key,
/// or the same key re-entering later, flashes again.
@MainActor
final class AttentionController: ObservableObject {
    typealias Sleeper = @Sendable (Duration) async throws -> Void

    /// Half a flash cycle: the glyph alternates every 0.5 s, i.e. at 1 Hz.
    static let phaseInterval: Duration = .milliseconds(500)

    /// True while some needs_you session is unacknowledged and flashing is
    /// enabled. Under Reduce motion this stays true (the icon shows a static
    /// alert badge instead) but `flashPhase` never toggles.
    @Published private(set) var isFlashing = false
    /// `true` is the "on" half (solid glyph), `false` the dimmed half.
    @Published private(set) var flashPhase = true
    @Published private(set) var reduceMotion: Bool
    /// The remote diamond badge's pulse (Separate marker): `true` is the
    /// solid half. Toggles for `remotePulseHalfCycles` ticks after a remote
    /// session newly needs you, then holds solid; never under Reduce motion.
    @Published private(set) var remotePulsePhase = true
    /// Three on/off cycles at the flash's 1 Hz.
    static let remotePulseHalfCycles = 6
    private(set) var remotePulseRemaining = 0

    private(set) var needsYouKeys: Set<String> = []
    private(set) var acknowledgedKeys: Set<String> = []
    private(set) var popoverVisible = false
    private(set) var dashboardIsKey = false

    /// Settings. The menu bar label pushes these from its `@AppStorage`.
    var flashEnabled = true {
        didSet {
            guard flashEnabled != oldValue else { return }
            // Turning flashing off settles everything currently pending, so
            // turning it back on later does not flash for old sessions.
            if !flashEnabled {
                acknowledgedKeys.formUnion(needsYouKeys)
                endRemotePulse()
            }
            update()
        }
    }
    var policy: FlashStopPolicy = .onOpen {
        didSet {
            guard policy != oldValue else { return }
            elapsed = .zero
            // Leaving `.never` while the popover is open or the dashboard is
            // key: the acknowledgement `.never` ignored applies now, so the
            // icon does not keep flashing in front of the user. A no-op if
            // the new policy is still `.never`.
            if isAttended { acknowledge() }
            update()
        }
    }

    /// Settings → Machines → Notify for sessions on other machines.
    var remoteAlerts = true
    /// Settings → Machines → Other Macs in the menu bar icon. Only Merged
    /// lets a remote session flash the glyph; Separate pulses the badge.
    var remoteMode: RemoteIconMode = .separate {
        didSet {
            guard remoteMode != oldValue else { return }
            if remoteMode != .merged {
                // Pending remote keys stop flashing the glyph.
                acknowledgedKeys.formUnion(needsYouKeys.filter { remoteKeys.contains($0) })
            }
            if remoteMode != .separate { endRemotePulse() }
            update()
        }
    }
    /// Remote keys currently in needs_you.
    private var remoteKeys: Set<String> = []
    /// Remote session keys in the previous snapshot (any state), and each
    /// peer's `last_seen_at` as of the previous snapshot.
    private var seenRemoteKeys: Set<String> = []
    private var peerLastSeen: [String: Date] = [:]

    /// Whether a remote session that is newly in needs_you (by key) is a
    /// real entry: never while stale; yes when it was seen before in
    /// another state; for one first seen, only when it changed after its
    /// Mac's previous report (so a newly paired Mac's first export, or a
    /// launch, fires nothing). `updated_at` is on the peer's clock and
    /// `last_seen_at` on this Mac's, so `clockSkewSecs` (peer minus this
    /// Mac, from the cache) is taken off `updated_at` first.
    static func remoteEntryCounts(
        _ session: Session, seenBefore: Bool, previousLastSeen: Date?, clockSkewSecs: Double? = nil
    ) -> Bool {
        if session.stale { return false }
        if seenBefore { return true }
        guard let previous = previousLastSeen, let updated = session.updatedDate else { return false }
        return updated.addingTimeInterval(-(clockSkewSecs ?? 0)) > previous
    }

    private var pendingKeys: Set<String> { needsYouKeys.subtracting(acknowledgedKeys) }
    private var isAttended: Bool { popoverVisible || dashboardIsKey }

    private let sleeper: Sleeper
    private let log = Logger(subsystem: "io.github.agentboard", category: "attention")
    /// Flash time since the flash last (re)started, for the timeout policies.
    private var elapsed: Duration = .zero
    /// Holds the phase loop and the notification observers, and tears them
    /// down when the controller goes away (a nonisolated deinit can't touch
    /// MainActor state).
    private let resources = Resources()

    private var flashTask: Task<Void, Never>? {
        get { resources.task }
        set { resources.task = newValue }
    }

    /// Whether a phase loop is currently scheduled (for tests).
    var hasFlashTask: Bool { flashTask != nil }

    init(
        sleeper: @escaping Sleeper = { try await Task.sleep(for: $0) },
        reduceMotion: Bool = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion,
        notificationCenter: NotificationCenter = .default,
        workspaceNotificationCenter: NotificationCenter? = NSWorkspace.shared.notificationCenter
    ) {
        self.sleeper = sleeper
        self.reduceMotion = reduceMotion
        observeDashboardKey(notificationCenter)
        if let workspaceNotificationCenter { observeReduceMotion(workspaceNotificationCenter) }
    }

    // MARK: - Inputs

    /// Feeds a new snapshot. Returns the keys that newly entered needs_you,
    /// which `NotificationManager` also uses, so the diff lives in one place.
    /// Everything in needs_you on the first snapshot after launch counts as new.
    @discardableResult
    func note(snapshot: Snapshot) -> Set<String> {
        let needing = snapshot.sessions.filter { $0.state == .needsYou }
        let current = Set(needing.map(\.key))
        var entered = current.subtracting(needsYouKeys)
        // Remote sessions (peer sync): a stale one keeps its key in the
        // baseline but never flashes or fires, so stale to live is not an
        // entry; one first seen only seeds the baseline, unless it changed
        // after its Mac's previous report (a new session that already
        // needs you).
        var quiet: Set<String> = []
        let skews = Dictionary(snapshot.peers.map { ($0.id, $0.clockSkewSecs) }, uniquingKeysWith: { a, _ in a })
        for session in needing where session.isRemote && entered.contains(session.key) {
            let machineId = session.machine?.id ?? ""
            let counts = remoteAlerts && Self.remoteEntryCounts(
                session, seenBefore: seenRemoteKeys.contains(session.key),
                previousLastSeen: peerLastSeen[machineId], clockSkewSecs: skews[machineId] ?? nil)
            if !counts { quiet.insert(session.key) }
        }
        let staleKeys = Set(needing.filter(\.stale).map(\.key))
        seenRemoteKeys = Set(snapshot.sessions.filter(\.isRemote).map(\.key))
        for peer in snapshot.peers {
            if let seen = peer.lastSeenDate { peerLastSeen[peer.id] = seen }
        }
        entered.subtract(quiet)
        needsYouKeys = current
        remoteKeys = Set(needing.filter(\.isRemote).map(\.key))
        // A key that left needs_you forgets its acknowledgement, so re-entry flashes again.
        acknowledgedKeys.formIntersection(current)
        acknowledgedKeys.formUnion(quiet)
        acknowledgedKeys.formUnion(staleKeys)

        // Separate marker / Not shown: a remote entry still notifies (the
        // returned keys) but never flashes the glyph; Separate pulses the
        // badge instead.
        var flashing = entered
        if remoteMode != .merged {
            let remoteEntered = entered.intersection(remoteKeys)
            flashing.subtract(remoteEntered)
            acknowledgedKeys.formUnion(remoteEntered)
            if remoteMode == .separate, !remoteEntered.isEmpty, flashEnabled, !reduceMotion,
               !(isAttended && policy.honorsAcknowledgement) {
                remotePulseRemaining = Self.remotePulseHalfCycles
            }
        }
        if !flashing.isEmpty {
            if !flashEnabled || (isAttended && policy.honorsAcknowledgement) {
                // The user is looking at AgentBoard right now (or flashing is off).
                acknowledgedKeys.formUnion(flashing)
            } else {
                elapsed = .zero
            }
        }
        update()
        return entered
    }

    /// The user has seen the board. Ignored by the `.never` policy.
    func acknowledge() {
        guard policy.honorsAcknowledgement else { return }
        // Seeing the board also settles the badge's pulse.
        endRemotePulse()
        let newlyAcknowledged = needsYouKeys.subtracting(acknowledgedKeys)
        if !newlyAcknowledged.isEmpty {
            log.debug("flash ack keys=\(newlyAcknowledged.sorted().joined(separator: ","), privacy: .public)")
        }
        acknowledgedKeys.formUnion(needsYouKeys)
        update()
    }

    /// Called on every popover open; opening the popover acknowledges.
    func popoverDidAppear() {
        popoverVisible = true
        acknowledge()
    }

    func popoverDidDisappear() {
        popoverVisible = false
    }

    func setReduceMotion(_ value: Bool) {
        guard value != reduceMotion else { return }
        reduceMotion = value
        if value { endRemotePulse() }
        update()
    }

    /// Whether the badge is mid-pulse (for tests and the loop).
    var isPulsingRemote: Bool { remotePulseRemaining > 0 }

    private func endRemotePulse() {
        remotePulseRemaining = 0
        if !remotePulsePhase { remotePulsePhase = true }
    }

    // MARK: - Flash loop

    /// Advances the flash by one half-cycle. Driven by the phase loop; tests
    /// call it directly with a sleeper that never returns.
    func tick() {
        if remotePulseRemaining > 0 {
            remotePulseRemaining -= 1
            remotePulsePhase.toggle()
            if remotePulseRemaining == 0 {
                endRemotePulse()
                if !isFlashing { update() }
            }
        }
        guard isFlashing else { return }
        elapsed += Self.phaseInterval
        if let timeout = policy.timeout, elapsed >= timeout {
            acknowledgedKeys.formUnion(needsYouKeys)
            update()
            return
        }
        if !reduceMotion { flashPhase.toggle() }
    }

    private func update() {
        let shouldFlash = flashEnabled && !pendingKeys.isEmpty
        if isFlashing != shouldFlash {
            isFlashing = shouldFlash
            if shouldFlash {
                log.debug("flash start pending=\(self.pendingKeys.sorted().joined(separator: ","), privacy: .public) policy=\(self.policy.rawValue, privacy: .public)")
            } else {
                log.debug("flash stop")
            }
        }

        // The loop is needed for the phase toggle, and under Reduce motion
        // only to count down a timeout policy.
        let needsLoop = (shouldFlash && (!reduceMotion || policy.timeout != nil)) || remotePulseRemaining > 0
        if needsLoop {
            startLoopIfNeeded()
        } else {
            flashTask?.cancel()
            flashTask = nil
        }
        if !shouldFlash || reduceMotion, !flashPhase { flashPhase = true }
    }

    private func startLoopIfNeeded() {
        guard flashTask == nil else { return }
        let sleeper = self.sleeper
        flashTask = Task { [weak self] in
            while !Task.isCancelled {
                do { try await sleeper(Self.phaseInterval) } catch { return }
                guard !Task.isCancelled, let self else { return }
                self.tick()
            }
        }
    }

    // MARK: - Observers

    /// A long-open dashboard never re-triggers the store's visibility hook,
    /// so acknowledgement also comes from the dashboard window becoming key.
    private func observeDashboardKey(_ center: NotificationCenter) {
        resources.observe(center, NSWindow.didBecomeKeyNotification) { [weak self] note in
            let window = note.object as? NSWindow
            MainActor.assumeIsolated {
                guard Self.isDashboard(window) else { return }
                self?.dashboardIsKey = true
                self?.acknowledge()
            }
        }
        for name in [NSWindow.didResignKeyNotification, NSWindow.willCloseNotification] {
            resources.observe(center, name) { [weak self] note in
                let window = note.object as? NSWindow
                MainActor.assumeIsolated {
                    guard Self.isDashboard(window) else { return }
                    self?.dashboardIsKey = false
                }
            }
        }
    }

    private static func isDashboard(_ window: NSWindow?) -> Bool {
        window?.identifier?.rawValue == DashboardWindowController.windowID
    }

    private func observeReduceMotion(_ center: NotificationCenter) {
        resources.observe(center, NSWorkspace.accessibilityDisplayOptionsDidChangeNotification) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.setReduceMotion(NSWorkspace.shared.accessibilityDisplayShouldReduceMotion)
            }
        }
    }
}

/// Observer tokens and the flash loop task. Only touched from the main actor;
/// `@unchecked Sendable` so its deinit can run from the controller's.
private final class Resources: @unchecked Sendable {
    var task: Task<Void, Never>?
    private var tokens: [(NotificationCenter, NSObjectProtocol)] = []

    func observe(_ center: NotificationCenter, _ name: Notification.Name, _ block: @escaping @Sendable (Notification) -> Void) {
        tokens.append((center, center.addObserver(forName: name, object: nil, queue: .main, using: block)))
    }

    deinit {
        task?.cancel()
        for (center, token) in tokens { center.removeObserver(token) }
    }
}
