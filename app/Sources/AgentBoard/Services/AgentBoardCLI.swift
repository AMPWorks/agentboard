import Foundation
import OSLog

/// Thin wrapper over the `agentboard` CLI. In fixture mode (dev only), snapshot
/// data is read from a local JSON file and mutating actions are logged rather
/// than executed.
struct AgentBoardCLI: Sendable {
    static let shared = AgentBoardCLI()

    static let pathDefaultsKey = "agentboardPath"
    static let useFixtureDefaultsKey = "agentboardUseFixture"
    static let fixturePathDefaultsKey = "agentboardFixturePath"

    enum CLIError: Error, LocalizedError {
        case notFound

        var errorDescription: String? {
            switch self {
            case .notFound:
                return "agentboard CLI not found. Checked the bundled copy, $PATH, and the agentboardPath setting."
            }
        }
    }

    private let runner = CLIRunner()
    private let log = Logger(subsystem: "io.github.agentboard", category: "cli")

    /// A fixture file that wins over the environment and defaults (tests).
    private let fixtureOverride: URL?

    init(fixtureOverride: URL? = nil) {
        self.fixtureOverride = fixtureOverride
    }

    /// Resolves the agentboard CLI location in order: a copy bundled in the
    /// app's Resources, then $PATH, then the agentboardPath UserDefaults
    /// override. No path is hard-coded so the app stays portable.
    func resolveExecutablePath() -> String? {
        if let bundled = Self.bundledExecutableURL, FileManager.default.isExecutableFile(atPath: bundled.path) {
            return bundled.path
        }
        if let onPath = Self.findOnPath("agentboard") {
            return onPath
        }
        let defaults = UserDefaults.standard
        if let custom = defaults.string(forKey: Self.pathDefaultsKey), !custom.isEmpty {
            return (custom as NSString).expandingTildeInPath
        }
        return nil
    }

    private static var bundledExecutableURL: URL? {
        Bundle.main.url(forResource: "agentboard", withExtension: nil, subdirectory: "core/bin")
    }

    private static func findOnPath(_ name: String) -> String? {
        guard let pathVar = ProcessInfo.processInfo.environment["PATH"] else { return nil }
        for dir in pathVar.split(separator: ":") {
            let candidate = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }

    /// Non-nil when fixture data should be used instead of shelling out.
    var fixtureURL: URL? {
        if let fixtureOverride { return fixtureOverride }
        let env = ProcessInfo.processInfo.environment
        if let envPath = env["AGENTBOARD_FIXTURE_PATH"], !envPath.isEmpty {
            return URL(fileURLWithPath: (envPath as NSString).expandingTildeInPath)
        }
        if env["AGENTBOARD_USE_FIXTURE"] == "1" {
            return Self.bundledFixtureURL
        }
        let defaults = UserDefaults.standard
        if defaults.bool(forKey: Self.useFixtureDefaultsKey) {
            if let path = defaults.string(forKey: Self.fixturePathDefaultsKey), !path.isEmpty {
                return URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
            }
            return Self.bundledFixtureURL
        }
        return nil
    }

    private static var bundledFixtureURL: URL? {
        Bundle.main.url(forResource: "snapshot", withExtension: "json", subdirectory: "Fixtures")
    }

    var isFixtureMode: Bool { fixtureURL != nil }

    func snapshot(all: Bool) async throws -> Snapshot {
        if let fixtureURL {
            let data = try Data(contentsOf: fixtureURL)
            return try Self.decode(data)
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        var args = ["snapshot", "--json"]
        if all { args.append("--all") }
        let data = try await runner.run(executable: executablePath, arguments: args)
        return try Self.decode(data)
    }

    /// `--machine <id>` for a session of a paired Mac, else nothing.
    static func machineArguments(_ machine: String?) -> [String] {
        guard let machine, !machine.isEmpty else { return [] }
        return ["--machine", machine]
    }

    static func markArguments(tool: String, sessionId: String, action: String, hours: Int?,
                              note: String?, machine: String?) -> [String] {
        var args = ["mark", tool, sessionId, action]
        if let hours { args += ["--hours", String(hours)] }
        // A note is never sent to another Mac.
        if let note, machine == nil { args += ["--note", note] }
        return args + machineArguments(machine)
    }

    func mark(tool: String, sessionId: String, action: String, hours: Int? = nil, note: String? = nil,
              machine: String? = nil) async throws {
        if isFixtureMode {
            log.notice("fixture mode: skipping mark \(tool, privacy: .public):\(sessionId, privacy: .public) -> \(action, privacy: .public)")
            return
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        let args = Self.markArguments(tool: tool, sessionId: sessionId, action: action, hours: hours,
                                      note: note, machine: machine)
        _ = try await runner.run(executable: executablePath, arguments: args)
    }

    /// `focus` arguments; `--via` goes only with `--machine`.
    static func focusArguments(tool: String, sessionId: String, machine: String?,
                               via: String? = nil) -> [String] {
        var args = ["focus", tool, sessionId] + machineArguments(machine)
        if let via, !via.isEmpty, let machine, !machine.isEmpty {
            args += ["--via", via]
        }
        return args
    }

    func focus(tool: String, sessionId: String, machine: String? = nil, via: String? = nil) async throws {
        if isFixtureMode {
            log.notice("fixture mode: skipping focus \(tool, privacy: .public):\(sessionId, privacy: .public)")
            return
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        do {
            _ = try await runner.run(executable: executablePath,
                                     arguments: Self.focusArguments(tool: tool, sessionId: sessionId,
                                                                    machine: machine, via: via))
        } catch {
            // The CLI's stderr is already a sentence for the banner; its exit
            // code says which settings pane (if any) fixes it.
            throw FocusError.from(error)
        }
    }

    @discardableResult
    func resume(tool: String, sessionId: String, printOnly: Bool, machine: String? = nil) async throws -> String {
        if isFixtureMode {
            log.notice("fixture mode: skipping resume \(tool, privacy: .public):\(sessionId, privacy: .public)")
            return ""
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        var args = ["resume", tool, sessionId]
        if printOnly { args.append("--print") }
        args += Self.machineArguments(machine)
        let data = try await runner.run(executable: executablePath, arguments: args)
        return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    /// `agentboard notes --dismiss ID...`: hides those notes from Updates
    /// (and releases a needs_you a note was holding).
    func dismissNotes(ids: [Int]) async throws {
        guard !ids.isEmpty else { return }
        if isFixtureMode {
            log.notice("fixture mode: skipping notes --dismiss \(ids.map(String.init).joined(separator: " "), privacy: .public)")
            return
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        _ = try await runner.run(executable: executablePath, arguments: ["notes", "--dismiss"] + ids.map(String.init))
    }

    /// `agentboard notes --dismiss-all --session TOOL:ID [--kind K...]`: every
    /// open note of one session, or only those of the given kinds.
    /// A kind list with no CLI name in it dismisses nothing (it never widens
    /// to the whole session).
    func dismissNotes(tool: String, sessionId: String, kinds: [Note.Kind]? = nil) async throws {
        guard let arguments = Self.sessionDismissArguments(tool: tool, sessionId: sessionId, kinds: kinds) else {
            log.notice("notes --dismiss-all \(tool, privacy: .public):\(sessionId, privacy: .public) skipped: no dismissable kind")
            return
        }
        if isFixtureMode {
            log.notice("fixture mode: skipping \(arguments.joined(separator: " "), privacy: .public)")
            return
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        _ = try await runner.run(executable: executablePath, arguments: arguments)
    }

    /// The CLI arguments for a per-session dismiss-all, or nil when `kinds`
    /// scopes it to no kind the CLI knows (so there is nothing to dismiss).
    static func sessionDismissArguments(tool: String, sessionId: String, kinds: [Note.Kind]?) -> [String]? {
        guard let kindArgs = kindArguments(kinds) else { return nil }
        return ["notes", "--dismiss-all", "--session", "\(tool):\(sessionId)"] + kindArgs
    }

    /// `agentboard notes --dismiss-all`: every open note of every session,
    /// including ones past the snapshot's per-session cap.
    func dismissAllNotes() async throws {
        if isFixtureMode {
            log.notice("fixture mode: skipping notes --dismiss-all")
            return
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        _ = try await runner.run(executable: executablePath, arguments: ["notes", "--dismiss-all"])
    }

    /// `--kind update error` for a kind-scoped dismiss; empty for no scope
    /// (`kinds == nil`). `.unknown` has no CLI name and is left out, and a
    /// scope left with no names is nil: it must not become an unscoped
    /// dismiss.
    static func kindArguments(_ kinds: [Note.Kind]?) -> [String]? {
        guard let kinds else { return [] }
        let names = kinds.filter { $0 != .unknown }.map(\.rawValue)
        return names.isEmpty ? nil : ["--kind"] + names
    }

    /// Runs one dismissal (a grouped row's x, or a step of Clear all).
    func dismiss(_ dismissal: NoteDismissal) async throws {
        switch dismissal {
        case .all:
            try await dismissAllNotes()
        case let .session(tool, sessionId, kinds):
            try await dismissNotes(tool: tool, sessionId: sessionId, kinds: kinds)
        case let .ids(ids):
            try await dismissNotes(ids: ids)
        }
    }

    /// `agentboard denials --dismiss ID...`: clears those permission denials
    /// from the Denials list. Never changes a session's state.
    func dismissDenials(ids: [Int]) async throws {
        guard !ids.isEmpty else { return }
        try await runDenials(["denials", "--dismiss"] + ids.map(String.init))
    }

    /// `agentboard denials --dismiss-all --session TOOL:ID`: every open
    /// denial of one session, including ones past the snapshot's cap.
    func dismissDenials(tool: String, sessionId: String) async throws {
        try await runDenials(["denials", "--dismiss-all", "--session", "\(tool):\(sessionId)"])
    }

    /// `agentboard denials --dismiss-all`: every open denial of every session.
    func dismissAllDenials() async throws {
        try await runDenials(["denials", "--dismiss-all"])
    }

    /// Runs one denial dismissal (a row's x, or a step of Clear all).
    func dismiss(_ dismissal: DenialDismissal) async throws {
        switch dismissal {
        case .all:
            try await dismissAllDenials()
        case let .session(tool, sessionId):
            try await dismissDenials(tool: tool, sessionId: sessionId)
        case let .ids(ids):
            try await dismissDenials(ids: ids)
        }
    }

    private func runDenials(_ arguments: [String]) async throws {
        if isFixtureMode {
            log.notice("fixture mode: skipping \(arguments.joined(separator: " "), privacy: .public)")
            return
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        _ = try await runner.run(executable: executablePath, arguments: arguments)
    }

    func refreshLinks() async throws {
        if isFixtureMode { return }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        _ = try await runner.run(executable: executablePath, arguments: ["refresh-links"])
    }

    /// `agentboard prune --auto`: the retention sweep. The CLI gates it to
    /// one real run a day, so calling it more often costs nothing.
    func prune() async throws {
        if isFixtureMode { return }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        _ = try await runner.run(executable: executablePath, arguments: ["prune", "--auto"])
    }

    func backfill(days: Int) async throws {
        if isFixtureMode { return }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        _ = try await runner.run(executable: executablePath, arguments: ["backfill", "--days", String(days)])
    }

    /// `agentboard paths --json` -- the data/db/core directories the CLI
    /// itself resolved to (honoring AGENTBOARD_HOME), called once at launch
    /// so the directory watcher watches the exact same place the CLI reads
    /// and writes. Throws (rather than falling back here) in fixture mode
    /// or on any CLI failure; the caller decides the fallback.
    func paths() async throws -> AgentBoardPaths {
        if isFixtureMode { throw CLIError.notFound }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        let data = try await runner.run(executable: executablePath, arguments: ["paths", "--json"])
        return try JSONDecoder().decode(AgentBoardPaths.self, from: data)
    }

    // MARK: - Machines (peer sync)

    /// `agentboard peers list --json`: this Mac, its pairing code and each
    /// paired Mac (Settings → Machines).
    func peersList() async throws -> PeersInfo {
        if isFixtureMode { return PeersInfo.fixture }
        return try JSONDecoder().decode(PeersInfo.self, from: try await runPeers(["list", "--json"]))
    }

    /// `agentboard peers add --code CODE --dry-run`: checks a pasted code
    /// and returns what it names, changing nothing.
    func peersPreview(code: String) async throws -> PeerPreview {
        if isFixtureMode {
            return PeerPreview(id: "fixture", name: "Fixture Mac", user: "you", host: "fixture.local",
                               fingerprint: "0000-0000-0000-0000-0000-0000", own: false, known: false)
        }
        return try JSONDecoder().decode(PeerPreview.self,
                                        from: try await runPeers(["add", "--code", code, "--dry-run"]))
    }

    func peersAdd(code: String, host: String, allowOpen: Bool) async throws {
        _ = try await runPeers(Self.peersAddArguments(code: code, host: host, allowOpen: allowOpen))
    }

    /// `add` always passes `--host`: the address the sheet shows, which
    /// starts as the code's own. Typed values go as `--opt=value`, so one
    /// starting with `-` reaches the CLI's own check instead of argparse.
    static func peersAddArguments(code: String, host: String, allowOpen: Bool) -> [String] {
        ["add", "--code", code, "--host=\(host)", "--allow-open", allowOpen ? "on" : "off"]
    }

    func peersRemove(id: String) async throws {
        _ = try await runPeers(["remove", "--id", id])
    }

    func peersSet(id: String, host: String? = nil, name: String? = nil, allowOpen: Bool? = nil,
                  dismissAddress: String? = nil) async throws {
        var args = ["set", "--id", id]
        if let host { args.append("--host=\(host)") }
        if let name { args += ["--name", name] }
        if let allowOpen { args += ["--allow-open", allowOpen ? "on" : "off"] }
        if let dismissAddress { args.append("--dismiss-address=\(dismissAddress)") }
        _ = try await runPeers(args)
    }

    func peersSetSelf(name: String? = nil, share: String? = nil, address: String? = nil,
                      autoAddress: Bool = false) async throws {
        _ = try await runPeers(Self.peersSetSelfArguments(name: name, share: share, address: address,
                                                          autoAddress: autoAddress))
    }

    /// `--auto-address` wins over `address` (the CLI refuses both at once).
    static func peersSetSelfArguments(name: String? = nil, share: String? = nil, address: String? = nil,
                                      autoAddress: Bool = false) -> [String] {
        var args = ["set-self"]
        if let name { args += ["--name", name] }
        if let share { args += ["--share", share] }
        if autoAddress {
            args.append("--auto-address")
        } else if let address {
            args.append("--address=\(address)")
        }
        return args
    }

    private func runPeers(_ arguments: [String]) async throws -> Data {
        if isFixtureMode {
            log.notice("fixture mode: skipping peers \(arguments.first ?? "", privacy: .public)")
            return Data("{}".utf8)
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        return try await runner.run(executable: executablePath, arguments: ["peers"] + arguments)
    }

    private static func decode(_ data: Data) throws -> Snapshot {
        let decoder = JSONDecoder()
        return try decoder.decode(Snapshot.self, from: data)
    }
}

/// Decoded `agentboard paths --json` output: `{"home": "...", "db": "...", "core": "..."}`.
struct AgentBoardPaths: Decodable, Sendable {
    let home: String
    let db: String
    let core: String
}

/// A click on a remote session whose Mac is offline.
struct PeerUnreachable: Error, LocalizedError {
    var name: String
    var errorDescription: String? { "\(name) is offline" }
}

/// Decoded `agentboard peers list --json`.
struct PeersInfo: Decodable, Sendable, Equatable {
    var machine: Machine
    var code: String?
    var fingerprint: String?
    var host: String?
    var user: String?
    var codeError: String?
    /// `on`, `off` or `unknown`.
    var remoteLogin: String
    var share: String
    var peers: [Peer]
    /// The address in this Mac's pairing code and how it was chosen; nil
    /// from an older CLI.
    var address: PeerAddress?
    /// What Automatic picks now (shown in the Choose… menu).
    var addressAutomatic: PeerAddress?
    /// The detected addresses the Choose… menu offers.
    var addressCandidates: [AddressCandidate] = []

    enum CodingKeys: String, CodingKey {
        case machine, code, fingerprint, host, user, share, peers, address
        case codeError = "code_error"
        case remoteLogin = "remote_login"
        case addressAutomatic = "address_automatic"
        case addressCandidates = "address_candidates"
    }

    init(machine: Machine, code: String?, fingerprint: String?, host: String?, user: String?,
         codeError: String?, remoteLogin: String, share: String, peers: [Peer],
         address: PeerAddress? = nil, addressAutomatic: PeerAddress? = nil,
         addressCandidates: [AddressCandidate] = []) {
        self.address = address
        self.addressAutomatic = addressAutomatic
        self.addressCandidates = addressCandidates
        self.machine = machine
        self.code = code
        self.fingerprint = fingerprint
        self.host = host
        self.user = user
        self.codeError = codeError
        self.remoteLogin = remoteLogin
        self.share = share
        self.peers = peers
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        machine = try c.decode(Machine.self, forKey: .machine)
        code = (try? c.decodeIfPresent(String.self, forKey: .code)) ?? nil
        fingerprint = (try? c.decodeIfPresent(String.self, forKey: .fingerprint)) ?? nil
        host = (try? c.decodeIfPresent(String.self, forKey: .host)) ?? nil
        user = (try? c.decodeIfPresent(String.self, forKey: .user)) ?? nil
        codeError = (try? c.decodeIfPresent(String.self, forKey: .codeError)) ?? nil
        remoteLogin = ((try? c.decodeIfPresent(String.self, forKey: .remoteLogin)) ?? nil) ?? "unknown"
        share = ((try? c.decodeIfPresent(String.self, forKey: .share)) ?? nil) ?? "full"
        peers = (try? c.decodeIfPresent(LenientList<Peer>.self, forKey: .peers))?.items ?? []
        address = (try? c.decodeIfPresent(PeerAddress.self, forKey: .address)) ?? nil
        addressAutomatic = (try? c.decodeIfPresent(PeerAddress.self, forKey: .addressAutomatic)) ?? nil
        addressCandidates = (try? c.decodeIfPresent(LenientList<AddressCandidate>.self,
                                                    forKey: .addressCandidates))?.items ?? []
    }

    static let fixture = PeersInfo(
        machine: Machine(id: "fixture", name: "This Mac"), code: "agentboard-pair:fixture",
        fingerprint: "0000-0000-0000-0000-0000-0000", host: "this-mac.local", user: "you", codeError: nil,
        remoteLogin: "on", share: "full", peers: [],
        address: PeerAddress(address: "this-mac.local", source: "auto", kind: "local"),
        addressAutomatic: PeerAddress(address: "this-mac.local", source: "auto", kind: "local"),
        addressCandidates: [
            AddressCandidate(address: "this-mac.example.ts.net", kind: "vpn", label: "VPN name"),
            AddressCandidate(address: "this-mac.local", kind: "local", label: "Local name (same network only)"),
        ])
}

/// The address in this Mac's pairing code (`peers list` `address`).
struct PeerAddress: Decodable, Sendable, Equatable {
    var address: String
    /// `config` (set by the owner) or `auto`.
    var source: String
    /// `vpn`, `local`, `lan` or `custom`.
    var kind: String

    init(address: String, source: String = "auto", kind: String = "custom") {
        self.address = address
        self.source = source
        self.kind = kind
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        address = try c.decode(String.self, forKey: .address)
        source = ((try? c.decodeIfPresent(String.self, forKey: .source)) ?? nil) ?? "auto"
        kind = ((try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil) ?? "custom"
    }

    enum CodingKeys: String, CodingKey { case address, source, kind }

    var isChosen: Bool { source == "config" }
}

/// One detected address the Choose… menu offers (`address_candidates`).
struct AddressCandidate: Decodable, Sendable, Equatable, Identifiable {
    var address: String
    var kind: String
    var label: String
    var id: String { address }

    init(address: String, kind: String, label: String) {
        self.address = address
        self.kind = kind
        self.label = label
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        address = try c.decode(String.self, forKey: .address)
        kind = ((try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil) ?? "custom"
        label = ((try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil) ?? address
    }

    enum CodingKeys: String, CodingKey { case address, kind, label }
}

/// Decoded `agentboard peers add --code CODE --dry-run`.
struct PeerPreview: Decodable, Sendable, Equatable {
    var id: String
    var name: String
    var user: String
    var host: String
    var fingerprint: String
    /// The code is this Mac's own; already paired.
    var own: Bool
    var known: Bool
    /// The code has this Mac's machine id but another Mac's host key: that
    /// Mac's data folder was copied from this one, so it can't be paired.
    var copiedIdentity: Bool = false
    /// Why the code can't be added (own code, copied identity), in plain words.
    var error: String?
    /// The paired Mac this code replaces (same id, or the same key under another id).
    var replaces: Replaced?
    /// The address this Mac reaches the already-paired Mac at, else nil.
    var currentHost: String?

    struct Replaced: Decodable, Sendable, Equatable {
        var id: String
        var name: String
        var host: String?
        var allowOpen: Bool

        enum CodingKeys: String, CodingKey {
            case id, name, host
            case allowOpen = "allow_open"
        }

        init(id: String, name: String, host: String? = nil, allowOpen: Bool = true) {
            self.id = id
            self.name = name
            self.host = host
            self.allowOpen = allowOpen
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(String.self, forKey: .id)
            name = ((try? c.decodeIfPresent(String.self, forKey: .name)) ?? nil) ?? id
            host = (try? c.decodeIfPresent(String.self, forKey: .host)) ?? nil
            allowOpen = ((try? c.decodeIfPresent(Bool.self, forKey: .allowOpen)) ?? nil) ?? true
        }
    }

    enum CodingKeys: String, CodingKey {
        case id, name, user, host, fingerprint, own, known, error, replaces
        case copiedIdentity = "copied_identity"
        case currentHost = "current_host"
    }

    init(id: String, name: String, user: String, host: String, fingerprint: String,
         own: Bool, known: Bool, copiedIdentity: Bool = false, error: String? = nil,
         replaces: Replaced? = nil, currentHost: String? = nil) {
        self.currentHost = currentHost
        self.id = id
        self.name = name
        self.user = user
        self.host = host
        self.fingerprint = fingerprint
        self.own = own
        self.known = known
        self.copiedIdentity = copiedIdentity
        self.error = error
        self.replaces = replaces
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decode(String.self, forKey: .name)
        user = try c.decode(String.self, forKey: .user)
        host = try c.decode(String.self, forKey: .host)
        fingerprint = try c.decode(String.self, forKey: .fingerprint)
        own = ((try? c.decodeIfPresent(Bool.self, forKey: .own)) ?? nil) ?? false
        known = ((try? c.decodeIfPresent(Bool.self, forKey: .known)) ?? nil) ?? false
        copiedIdentity = ((try? c.decodeIfPresent(Bool.self, forKey: .copiedIdentity)) ?? nil) ?? false
        error = (try? c.decodeIfPresent(String.self, forKey: .error)) ?? nil
        replaces = (try? c.decodeIfPresent(Replaced.self, forKey: .replaces)) ?? nil
        currentHost = (try? c.decodeIfPresent(String.self, forKey: .currentHost)) ?? nil
    }

    /// The code can be added (not this Mac's own, not a copied identity).
    var canAdd: Bool { !own && !copiedIdentity }

    /// The sheet's message for a code that can't be added, else nil.
    var blockingMessage: String? {
        if copiedIdentity {
            return MachinesSettingsMessages.clean(error ?? "This code comes from a Mac whose AgentBoard data folder was copied from this one.")
        }
        if own { return "That is this Mac's own code: paste it on the other Mac instead." }
        return nil
    }

    /// What the sheet says about the Mac this code replaces, else nil.
    var replacementNote: String? {
        guard let replaces else { return known ? "Already paired: adding it again replaces it." : nil }
        let opening = replaces.allowOpen ? "on" : "off"
        let who = replaces.id == id ? replaces.name : "\(replaces.name) (paired under another id)"
        return "Already paired: adding it replaces \(who). \"Allow opening\" starts at its current setting (\(opening))."
    }
}
