import Foundation

enum SessionState: String, Codable, Sendable, CaseIterable {
    case needsYou = "needs_you"
    case interrupted
    case running
    case review
    case unconcluded
    case snoozed
    case done
    case unknown

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = try container.decode(String.self)
        self = SessionState(rawValue: raw) ?? .unknown
    }

    /// Sort priority matching the snapshot contract ordering.
    var sortRank: Int {
        switch self {
        case .needsYou: return 0
        case .interrupted: return 1
        case .review: return 2
        case .running: return 3
        case .unconcluded: return 4
        case .snoozed: return 5
        case .done: return 6
        case .unknown: return 7
        }
    }

    var displayName: String {
        switch self {
        case .needsYou: return "Needs you"
        case .interrupted: return "Interrupted"
        case .running: return "Running"
        case .review: return "Review"
        case .unconcluded: return "Unconcluded"
        case .snoozed: return "Snoozed"
        case .done: return "Done"
        case .unknown: return "Unknown"
        }
    }
}

struct Counts: Codable, Sendable, Equatable {
    var needsYou: Int = 0
    var interrupted: Int = 0
    var running: Int = 0
    var review: Int = 0
    var unconcluded: Int = 0
    var snoozed: Int = 0
    var done: Int = 0

    enum CodingKeys: String, CodingKey {
        case needsYou = "needs_you"
        case interrupted
        case running
        case review
        case unconcluded
        case snoozed
        case done
    }
}

struct Link: Codable, Sendable, Identifiable, Equatable, Hashable {
    var kind: String
    var ref: String
    var url: String?
    var state: String?
    var title: String?

    var id: String { "\(kind):\(ref)" }

    /// Display label for the link's kind. Kinds are an open set (pr, ticket,
    /// url, or whatever a provider/skill reports); unrecognized kinds fall
    /// back to a capitalized form of the raw string.
    var kindLabel: String {
        switch kind {
        case "pr": return "PR"
        case "ticket": return "Ticket"
        case "url": return "Link"
        default: return kind.capitalized
        }
    }
}

struct Triage: Codable, Sendable, Equatable {
    var status: String
    var until: String?
    var note: String?
}

struct Session: Codable, Sendable, Identifiable, Equatable, Hashable {
    var key: String
    var tool: String
    var sessionId: String
    var state: SessionState
    var waitingReason: String?
    var statusText: String?
    var title: String
    var cwd: String
    var project: String
    var branch: String?
    var firstPrompt: String?
    var lastPrompt: String?
    var lastAssistant: String?
    var startedAt: String
    var updatedAt: String
    var endedAt: String?
    var alive: Bool
    var interactive: Bool
    var canFocus: Bool
    var resumeCommand: String
    var links: [Link]
    var triage: Triage
    var autoDone: Bool?
    var doneReason: String?

    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, tool
        case sessionId = "session_id"
        case state
        case waitingReason = "waiting_reason"
        case statusText = "status_text"
        case title, cwd, project, branch
        case firstPrompt = "first_prompt"
        case lastPrompt = "last_prompt"
        case lastAssistant = "last_assistant"
        case startedAt = "started_at"
        case updatedAt = "updated_at"
        case endedAt = "ended_at"
        case alive, interactive
        case canFocus = "can_focus"
        case resumeCommand = "resume_command"
        case links, triage
        case autoDone = "auto_done"
        case doneReason = "done_reason"
    }

    /// The CLI emits `null` for cwd/project/resume_command when a session's
    /// working directory was never recorded (a hook payload without `cwd`,
    /// or a transcript with no cwd entry), and for started_at/updated_at
    /// when a backfilled transcript carried no timestamps. One such row
    /// must degrade to empty strings, not make the whole snapshot fail to
    /// decode and blank the app.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(String.self, forKey: .key)
        tool = try c.decode(String.self, forKey: .tool)
        sessionId = try c.decode(String.self, forKey: .sessionId)
        state = try c.decodeIfPresent(SessionState.self, forKey: .state) ?? .unknown
        waitingReason = try c.decodeIfPresent(String.self, forKey: .waitingReason)
        statusText = try c.decodeIfPresent(String.self, forKey: .statusText)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? sessionId
        cwd = try c.decodeIfPresent(String.self, forKey: .cwd) ?? ""
        project = try c.decodeIfPresent(String.self, forKey: .project)
            ?? (cwd.isEmpty ? "" : (cwd as NSString).lastPathComponent)
        branch = try c.decodeIfPresent(String.self, forKey: .branch)
        firstPrompt = try c.decodeIfPresent(String.self, forKey: .firstPrompt)
        lastPrompt = try c.decodeIfPresent(String.self, forKey: .lastPrompt)
        lastAssistant = try c.decodeIfPresent(String.self, forKey: .lastAssistant)
        startedAt = try c.decodeIfPresent(String.self, forKey: .startedAt) ?? ""
        updatedAt = try c.decodeIfPresent(String.self, forKey: .updatedAt) ?? ""
        endedAt = try c.decodeIfPresent(String.self, forKey: .endedAt)
        alive = try c.decodeIfPresent(Bool.self, forKey: .alive) ?? false
        interactive = try c.decodeIfPresent(Bool.self, forKey: .interactive) ?? true
        canFocus = try c.decodeIfPresent(Bool.self, forKey: .canFocus) ?? false
        resumeCommand = try c.decodeIfPresent(String.self, forKey: .resumeCommand) ?? ""
        links = try c.decodeIfPresent([Link].self, forKey: .links) ?? []
        triage = try c.decodeIfPresent(Triage.self, forKey: .triage)
            ?? Triage(status: "open", until: nil, note: nil)
        autoDone = try c.decodeIfPresent(Bool.self, forKey: .autoDone)
        doneReason = try c.decodeIfPresent(String.self, forKey: .doneReason)
    }

    static func == (lhs: Session, rhs: Session) -> Bool { lhs.key == rhs.key && lhs.updatedAt == rhs.updatedAt && lhs.state == rhs.state }
    func hash(into hasher: inout Hasher) { hasher.combine(key) }

    var updatedDate: Date? { ISO8601DateParsing.date(from: updatedAt) }
    var startedDate: Date? { ISO8601DateParsing.date(from: startedAt) }
    var endedDate: Date? { endedAt.flatMap(ISO8601DateParsing.date(from:)) }

    var toolBadge: String { tool == "codex" ? "X" : "C" }

    /// There's a live, focusable terminal to bring to the front.
    var isFocusable: Bool { alive && canFocus }

    /// Resume only makes sense once the session's own process is gone;
    /// hidden while it's still active (alive, or actively running/needing input).
    var resumeAvailable: Bool { !(alive || state == .running || state == .needsYou) }

    var isAutoDone: Bool { autoDone == true }
}

struct Snapshot: Codable, Sendable, Equatable {
    var generatedAt: String
    var counts: Counts
    var sessions: [Session]

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case counts, sessions
    }

    static let empty = Snapshot(generatedAt: "", counts: Counts(), sessions: [])
}

/// Parses the ISO8601 timestamps the CLI emits, tolerating fractional seconds.
enum ISO8601DateParsing {
    static func date(from string: String) -> Date? {
        let withFractional = ISO8601DateFormatter()
        withFractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFractional.date(from: string) { return date }

        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: string)
    }
}
