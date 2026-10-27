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
    /// Open retained notes across the sessions the snapshot emits.
    var notes: Int = 0

    enum CodingKeys: String, CodingKey {
        case needsYou = "needs_you"
        case interrupted
        case running
        case review
        case unconcluded
        case snoozed
        case done
        case notes
    }

    init() {}

    /// Every key is optional: the synthesized decoder ignores property
    /// defaults and would make a key added later (like `notes`) required,
    /// so a snapshot from an older CLI or an old fixture file would fail to
    /// decode and blank the whole app.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        needsYou = try c.decodeIfPresent(Int.self, forKey: .needsYou) ?? 0
        interrupted = try c.decodeIfPresent(Int.self, forKey: .interrupted) ?? 0
        running = try c.decodeIfPresent(Int.self, forKey: .running) ?? 0
        review = try c.decodeIfPresent(Int.self, forKey: .review) ?? 0
        unconcluded = try c.decodeIfPresent(Int.self, forKey: .unconcluded) ?? 0
        snoozed = try c.decodeIfPresent(Int.self, forKey: .snoozed) ?? 0
        done = try c.decodeIfPresent(Int.self, forKey: .done) ?? 0
        notes = try c.decodeIfPresent(Int.self, forKey: .notes) ?? 0
    }
}

/// A retained note: one of the few things a session said that is worth
/// keeping (a request for the user, a milestone, an outage), deduplicated
/// with a repeat count. See `agentboard notes`.
struct Note: Codable, Sendable, Identifiable, Equatable, Hashable {
    enum Kind: String, Codable, Sendable {
        case needsYou = "needs_you"
        case update
        case error
        case unknown

        init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Kind(rawValue: raw) ?? .unknown
        }
    }

    var id: Int
    var kind: Kind
    var text: String
    var ts: String
    var lastTs: String
    var count: Int
    var source: String

    enum CodingKeys: String, CodingKey {
        case id, kind, text, ts
        case lastTs = "last_ts"
        case count, source
    }

    init(id: Int, kind: Kind, text: String, ts: String, lastTs: String, count: Int = 1, source: String = "cli") {
        self.id = id
        self.kind = kind
        self.text = text
        self.ts = ts
        self.lastTs = lastTs
        self.count = count
        self.source = source
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        kind = try c.decodeIfPresent(Kind.self, forKey: .kind) ?? .unknown
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        ts = try c.decodeIfPresent(String.self, forKey: .ts) ?? ""
        lastTs = try c.decodeIfPresent(String.self, forKey: .lastTs) ?? ts
        count = try c.decodeIfPresent(Int.self, forKey: .count) ?? 1
        source = try c.decodeIfPresent(String.self, forKey: .source) ?? ""
    }

    var lastDate: Date? { ISO8601DateParsing.date(from: lastTs) }

    /// The first non-empty line, without surrounding markdown emphasis.
    var firstLine: String {
        for line in text.split(whereSeparator: \.isNewline) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "*"))
                .trimmingCharacters(in: .whitespaces)
            if !trimmed.isEmpty { return trimmed }
        }
        return ""
    }

    /// More than the first line to show (the row can expand). Counts
    /// non-empty lines, so a single bold line (`**PR #9 merged**`, which
    /// `firstLine` shows without its `**`) has nothing more to show.
    var hasMore: Bool {
        text.split(whereSeparator: \.isNewline)
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }.count > 1
    }
}

struct Link: Codable, Sendable, Identifiable, Equatable, Hashable {
    var kind: String
    var ref: String
    var url: String?
    var state: String?
    var title: String?
    /// The PR's head branch (or the session's branch when it was reported).
    /// Missing from older CLIs, like `owned` and `awaitsYou`.
    var branch: String?
    /// The session opened this PR itself (reported it, or gh found it on
    /// the session's branch created during the session).
    var owned: Bool?
    /// Owned, open, and nobody but automation has reviewed or commented.
    var awaitsYou: Bool?

    var id: String { "\(kind):\(ref)" }

    enum CodingKeys: String, CodingKey {
        case kind, ref, url, state, title, branch, owned
        case awaitsYou = "awaits_you"
    }

    init(kind: String, ref: String, url: String? = nil, state: String? = nil, title: String? = nil,
         branch: String? = nil, owned: Bool? = nil, awaitsYou: Bool? = nil) {
        self.kind = kind
        self.ref = ref
        self.url = url
        self.state = state
        self.title = title
        self.branch = branch
        self.owned = owned
        self.awaitsYou = awaitsYou
    }

    var isPR: Bool { kind == "pr" }

    /// The PR number in `#123` (or `owner/repo#123`), for ordering.
    var number: Int? {
        guard let hash = ref.lastIndex(of: "#") else { return Int(ref) }
        return Int(ref[ref.index(after: hash)...])
    }

    var openURL: URL? { url.flatMap { $0.isEmpty ? nil : URL(string: $0) } }

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
    /// Open notes, newest first (the CLI caps the list at 20).
    var notes: [Note] = []
    /// Open notes, uncapped.
    var notesCount: Int = 0
    /// The newest parent Stop's time while nothing has happened since (no
    /// newer prompt, not ended, no background tasks listed): one value per
    /// finished turn. Nil while running, waiting, ended, or from older CLIs.
    var turnFinishedAt: String?
    /// What started the latest turn: `human`, `loop`, `cron`, `injected`,
    /// or nil (legacy rows, read as human; older CLIs).
    var turnPromptKind: String?
    /// Background tasks the last parent Stop listed (nil when unknown).
    var backgroundTasks: Int?
    /// The latest turn was injected right after a Stop that listed
    /// background tasks: it reports a background task's result back.
    var turnEndsBackgroundWait: Bool = false
    /// The model the session last ran (full id, e.g. `claude-opus-5-5`),
    /// its context size in tokens, and when that size was measured. Missing
    /// from older CLIs and unknown until a SessionStart or Stop reports them.
    var model: String?
    var contextTokens: Int?
    var contextAt: String?

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
        case notes
        case notesCount = "notes_count"
        case turnFinishedAt = "turn_finished_at"
        case turnPromptKind = "turn_prompt_kind"
        case backgroundTasks = "background_tasks"
        case turnEndsBackgroundWait = "turn_ends_background_wait"
        case model
        case contextTokens = "context_tokens"
        case contextAt = "context_at"
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
        notes = try c.decodeIfPresent([Note].self, forKey: .notes) ?? []
        notesCount = try c.decodeIfPresent(Int.self, forKey: .notesCount) ?? notes.count
        turnFinishedAt = try c.decodeIfPresent(String.self, forKey: .turnFinishedAt)
        turnPromptKind = try c.decodeIfPresent(String.self, forKey: .turnPromptKind)
        backgroundTasks = try c.decodeIfPresent(Int.self, forKey: .backgroundTasks)
        turnEndsBackgroundWait = try c.decodeIfPresent(Bool.self, forKey: .turnEndsBackgroundWait)
            ?? false
        // Optional extras: a malformed value drops the field, not the row.
        model = (try? c.decodeIfPresent(String.self, forKey: .model)) ?? nil
        contextTokens = (try? c.decodeIfPresent(Int.self, forKey: .contextTokens)) ?? nil
        contextAt = (try? c.decodeIfPresent(String.self, forKey: .contextAt)) ?? nil
    }

    static func == (lhs: Session, rhs: Session) -> Bool {
        lhs.key == rhs.key && lhs.updatedAt == rhs.updatedAt && lhs.state == rhs.state
            && lhs.notesCount == rhs.notesCount && lhs.notes == rhs.notes
            && lhs.title == rhs.title && lhs.statusText == rhs.statusText
            && lhs.waitingReason == rhs.waitingReason && lhs.links == rhs.links
            && lhs.turnFinishedAt == rhs.turnFinishedAt
            // The post-Stop re-parse updates these without touching updatedAt.
            && lhs.model == rhs.model && lhs.contextTokens == rhs.contextTokens
            && lhs.contextAt == rhs.contextAt
    }
    func hash(into hasher: inout Hasher) { hasher.combine(key) }

    var updatedDate: Date? { ISO8601DateParsing.date(from: updatedAt) }
    var startedDate: Date? { ISO8601DateParsing.date(from: startedAt) }
    var endedDate: Date? { endedAt.flatMap(ISO8601DateParsing.date(from:)) }
    var turnFinishedDate: Date? { turnFinishedAt.flatMap(ISO8601DateParsing.date(from:)) }
    var contextDate: Date? { contextAt.flatMap(ISO8601DateParsing.date(from:)) }

    var toolBadge: String { tool == "codex" ? "X" : "C" }

    /// There's a live, focusable terminal to bring to the front.
    var isFocusable: Bool { alive && canFocus }

    /// Resume only makes sense once the session's own process is gone;
    /// hidden while it's still active (alive, or actively running/needing input).
    var resumeAvailable: Bool { !(alive || state == .running || state == .needsYou) }

    var isAutoDone: Bool { autoDone == true }

    /// The row's primary line (see `SessionDisplay`).
    var displayTitle: String { SessionDisplay.primaryTitle(self) }
}

struct Snapshot: Codable, Sendable, Equatable {
    var generatedAt: String
    var counts: Counts
    var sessions: [Session]
    /// Plan-usage sources (Claude's local cache, `usage/*.json` drop files,
    /// Codex rollouts); empty when there is none or the CLI is older.
    var usage: [UsageSource] = []

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case counts, sessions, usage
    }

    static let empty = Snapshot(generatedAt: "", counts: Counts(), sessions: [])
}

extension Snapshot {
    /// Declared in an extension so the memberwise init stays. `usage` is
    /// decoded leniently, source by source: it comes from files other tools
    /// write, and a surprise in one must never drop the others or fail the
    /// whole snapshot and blank the board.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generatedAt = try c.decode(String.self, forKey: .generatedAt)
        counts = try c.decode(Counts.self, forKey: .counts)
        sessions = try c.decode([Session].self, forKey: .sessions)
        usage = (try? c.decodeIfPresent(LenientList<UsageSource>.self, forKey: .usage))?.items ?? []
    }
}

/// A JSON array decoded element by element, dropping any element that
/// fails instead of failing the array.
struct LenientList<Element: Decodable>: Decodable {
    var items: [Element]

    /// Accepts any value, so a failed element can be stepped over.
    private struct Skip: Decodable {
        init(from decoder: Decoder) throws {}
    }

    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        var items: [Element] = []
        while !c.isAtEnd {
            let index = c.currentIndex
            if (try? c.decodeNil()) == true { continue }
            if let item = try? c.decode(Element.self) {
                items.append(item)
            } else {
                _ = try? c.decode(Skip.self)
            }
            // Never spin on an element the decoder refuses to step over.
            if c.currentIndex == index { break }
        }
        self.items = items
    }
}

/// One plan-usage window, labelled by its source (`5h`, `7d`, `spend
/// limit`, `monthly`, ...); nothing here knows a window's name.
struct UsageWindow: Codable, Sendable, Equatable {
    var label: String
    var usedPercentage: Double
    var resetsAt: String?
    var usedUsd: Double?
    var limitUsd: Double?
    var period: String?

    enum CodingKeys: String, CodingKey {
        case label
        case usedPercentage = "used_percentage"
        case resetsAt = "resets_at"
        case usedUsd = "used_usd"
        case limitUsd = "limit_usd"
        case period
    }

    init(label: String, usedPercentage: Double, resetsAt: String? = nil, usedUsd: Double? = nil,
         limitUsd: Double? = nil, period: String? = nil) {
        self.label = label
        self.usedPercentage = usedPercentage
        self.resetsAt = resetsAt
        self.usedUsd = usedUsd
        self.limitUsd = limitUsd
        self.period = period
    }

    /// `label` and `used_percentage` are required (a window without them
    /// fails and is dropped by `UsageSource`); every other field degrades
    /// to nil.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = try c.decode(String.self, forKey: .label)
        usedPercentage = try c.decode(Double.self, forKey: .usedPercentage)
        resetsAt = (try? c.decodeIfPresent(String.self, forKey: .resetsAt)) ?? nil
        usedUsd = (try? c.decodeIfPresent(Double.self, forKey: .usedUsd)) ?? nil
        limitUsd = (try? c.decodeIfPresent(Double.self, forKey: .limitUsd)) ?? nil
        period = (try? c.decodeIfPresent(String.self, forKey: .period)) ?? nil
    }

    var resetsDate: Date? { resetsAt.flatMap(ISO8601DateParsing.date(from:)) }
}

/// One entry of the snapshot's `usage` list (see SPEC.md): a source such as
/// Claude's local cache, Codex, or any tool's `usage/<source>.json`.
struct UsageSource: Codable, Sendable, Equatable {
    var source: String
    var label: String
    var updatedAt: String?
    /// Where the numbers were read from, for the tooltip.
    var origin: String?
    var windows: [UsageWindow]

    enum CodingKeys: String, CodingKey {
        case source, label
        case updatedAt = "updated_at"
        case origin, windows
    }

    init(source: String, label: String? = nil, updatedAt: String? = nil, origin: String? = nil,
         windows: [UsageWindow]) {
        self.source = source
        self.label = label ?? source
        self.updatedAt = updatedAt
        self.origin = origin
        self.windows = windows
    }

    /// `source` is required; `label` falls back to it; windows decode one
    /// by one, dropping any that fails.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        source = try c.decode(String.self, forKey: .source)
        let label = (try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil
        self.label = (label?.isEmpty == false ? label : nil) ?? source
        updatedAt = (try? c.decodeIfPresent(String.self, forKey: .updatedAt)) ?? nil
        origin = (try? c.decodeIfPresent(String.self, forKey: .origin)) ?? nil
        windows = (try? c.decodeIfPresent(LenientList<UsageWindow>.self, forKey: .windows))?
            .items ?? []
    }

    var updatedDate: Date? { updatedAt.flatMap(ISO8601DateParsing.date(from:)) }
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
