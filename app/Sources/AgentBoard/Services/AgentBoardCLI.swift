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

    func mark(tool: String, sessionId: String, action: String, hours: Int? = nil, note: String? = nil) async throws {
        if isFixtureMode {
            log.notice("fixture mode: skipping mark \(tool, privacy: .public):\(sessionId, privacy: .public) -> \(action, privacy: .public)")
            return
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        var args = ["mark", tool, sessionId, action]
        if let hours { args += ["--hours", String(hours)] }
        if let note { args += ["--note", note] }
        _ = try await runner.run(executable: executablePath, arguments: args)
    }

    func focus(tool: String, sessionId: String) async throws {
        if isFixtureMode {
            log.notice("fixture mode: skipping focus \(tool, privacy: .public):\(sessionId, privacy: .public)")
            return
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        _ = try await runner.run(executable: executablePath, arguments: ["focus", tool, sessionId])
    }

    @discardableResult
    func resume(tool: String, sessionId: String, printOnly: Bool) async throws -> String {
        if isFixtureMode {
            log.notice("fixture mode: skipping resume \(tool, privacy: .public):\(sessionId, privacy: .public)")
            return ""
        }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        var args = ["resume", tool, sessionId]
        if printOnly { args.append("--print") }
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

    func refreshLinks() async throws {
        if isFixtureMode { return }
        guard let executablePath = resolveExecutablePath() else { throw CLIError.notFound }
        _ = try await runner.run(executable: executablePath, arguments: ["refresh-links"])
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
