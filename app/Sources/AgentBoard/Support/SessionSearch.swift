import Foundation

/// A parsed search query, shared by the menu bar popover and the dashboard
/// so both surfaces agree on what a query means.
///
/// Syntax:
/// - Whitespace separates terms; every term must match (AND).
/// - Double quotes keep a phrase together: `"menu bar"`.
/// - Matching is case- and diacritic-insensitive substring matching.
/// - `field:value` narrows a term to one field group (the value may be
///   quoted: `branch:"feat x"`). Fields: `repo:`/`project:` (project, cwd),
///   `path:` (cwd), `branch:`, `tool:`, `state:` (raw value and display
///   name), `pr:` (links of kind `pr`), `link:` (any link). Spaces after
///   the colon are ignored: `branch: main` is `branch:main`, and
///   `state: "needs you"` is `state:"needs you"`. An unknown field
///   (`foo: bar` stays two plain terms) or a known one with nothing after it
///   is searched as plain text, colon included.
/// - An empty or whitespace-only query matches everything.
struct SessionSearch: Sendable, Equatable {
    enum Field: Sendable, Equatable {
        case repo, path, branch, tool, state, pr, link

        init?(qualifier: String) {
            switch qualifier.lowercased() {
            case "repo", "project": self = .repo
            case "path": self = .path
            case "branch": self = .branch
            case "tool": self = .tool
            case "state": self = .state
            case "pr": self = .pr
            case "link": self = .link
            default: return nil
            }
        }
    }

    struct Term: Sendable, Equatable {
        /// Nil for a plain term, which searches every text field.
        var field: Field?
        /// Already folded (see `SessionSearch.fold`).
        var text: String
    }

    /// The query as typed.
    let query: String
    let terms: [Term]

    init(_ query: String) {
        self.query = query
        self.terms = Self.parse(query)
    }

    /// True when the query matches everything (no terms).
    var isEmpty: Bool { terms.isEmpty }

    func matches(_ session: Session) -> Bool {
        guard !terms.isEmpty else { return true }
        // Built at most once per session per query, and only if a plain
        // term needs it.
        var plain: String?
        for term in terms {
            if let field = term.field {
                guard Self.values(field, of: session).contains(where: { Self.fold($0).contains(term.text) })
                else { return false }
            } else {
                if plain == nil { plain = Self.plainHaystack(session) }
                guard plain!.contains(term.text) else { return false }
            }
        }
        return true
    }

    /// One note of `session` matches when the query matches the session
    /// with only that note's text among its notes: a term may hit the
    /// session's own fields or this note, never a sibling note's text. The
    /// popover Updates feed filters note by note with this, so a query that
    /// finds one note's PR number lists that note, not every note of its
    /// session. The session's fields include `waitingReason` and
    /// `lastAssistant`, which for a session raised by a note (or the Stop
    /// that produced its newest note) repeat that note's wording, so a term
    /// from them keeps every note of the session, as it keeps the session.
    func matches(_ note: Note, in session: Session) -> Bool {
        guard !terms.isEmpty else { return true }
        var scoped = session
        scoped.notes = [note]
        return matches(scoped)
    }

    /// The query matches the session on its own fields, without any note
    /// text, so every one of its notes (listed or not) matches too.
    func matchesIgnoringNotes(_ session: Session) -> Bool {
        guard !terms.isEmpty else { return true }
        var bare = session
        bare.notes = []
        return matches(bare)
    }

    /// Sessions that pass the dashboard's tool and project pickers (`"all"`
    /// disables a picker) and this query.
    func filter(_ sessions: [Session], tool: String = "all", project: String = "all") -> [Session] {
        sessions.filter { session in
            (tool == "all" || session.tool == tool)
                && (project == "all" || session.project == project)
                && matches(session)
        }
    }

    // MARK: - Folding and fields

    static func fold(_ text: String) -> String {
        text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
    }

    /// Joins fields with a unit separator, which no typed term contains, so
    /// a phrase never matches across two fields.
    private static let separator = "\u{1F}"

    private static func plainHaystack(_ session: Session) -> String {
        var parts: [String] = [
            session.title,
            session.firstPrompt ?? "",
            session.lastPrompt ?? "",
            session.lastAssistant ?? "",
            session.project,
            session.cwd,
            session.branch ?? "",
            session.tmuxSession ?? "",
            session.tool,
            session.state.rawValue,
            session.state.displayName,
            session.waitingReason ?? "",
            session.statusText ?? "",
            session.triage.note ?? "",
            session.sessionId,
        ]
        for link in session.links {
            parts.append(link.kind)
            parts.append(link.ref)
            parts.append(link.title ?? "")
            parts.append(link.state ?? "")
        }
        for note in session.notes {
            parts.append(note.text)
        }
        return fold(parts.joined(separator: separator))
    }

    private static func values(_ field: Field, of session: Session) -> [String] {
        switch field {
        case .repo: return [session.project, session.cwd]
        case .path: return [session.cwd]
        case .branch: return [session.branch ?? ""]
        case .tool: return [session.tool]
        case .state: return [session.state.rawValue, session.state.displayName]
        case .pr:
            return session.links.filter { $0.kind == "pr" }
                .flatMap { [$0.ref, $0.title ?? "", $0.url ?? ""] }
        case .link:
            return session.links.flatMap {
                [$0.kind, $0.kindLabel, $0.ref, $0.title ?? "", $0.url ?? "", $0.state ?? ""]
            }
        }
    }

    // MARK: - Parsing

    /// A whitespace-delimited token with its quotes removed. `quoteStart` is
    /// the offset in `text` where the first quoted run began, so a colon
    /// inside quotes (`"a:b"`) is never read as a qualifier.
    private struct Token {
        var text = ""
        var quoteStart: Int?
    }

    private static func tokenize(_ query: String) -> [Token] {
        var tokens: [Token] = []
        var current = Token()
        var inQuotes = false
        var started = false
        for character in query {
            if character == "\"" {
                if !inQuotes && current.quoteStart == nil {
                    current.quoteStart = current.text.count
                }
                inQuotes.toggle()
                started = true
            } else if character.isWhitespace && !inQuotes {
                if started { tokens.append(current) }
                current = Token()
                started = false
            } else {
                current.text.append(character)
                started = true
            }
        }
        if started { tokens.append(current) }
        return tokens
    }

    /// The field and the (quote-stripped, trimmed) value of a token whose
    /// first colon is unquoted and follows a known qualifier; nil otherwise.
    /// The value is empty for a bare `branch:`.
    private static func qualifier(of token: Token) -> (field: Field, value: String)? {
        guard let colon = token.text.firstIndex(of: ":") else { return nil }
        let offset = token.text.distance(from: token.text.startIndex, to: colon)
        if let quoteStart = token.quoteStart, offset >= quoteStart { return nil }
        guard let field = Field(qualifier: String(token.text[..<colon])) else { return nil }
        let value = String(token.text[token.text.index(after: colon)...])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (field, value)
    }

    private static func parse(_ query: String) -> [Term] {
        let tokens = tokenize(query)
        var terms: [Term] = []
        var index = 0
        while index < tokens.count {
            let token = tokens[index]
            index += 1
            if let qualified = qualifier(of: token) {
                if !qualified.value.isEmpty {
                    terms.append(Term(field: qualified.field, text: fold(qualified.value)))
                    continue
                }
                // A bare `branch:` (no quotes, so not `branch:""`) takes the
                // next term as its value, so `branch: main` means
                // `branch:main`. A next term that is itself a qualifier is
                // left alone, and so is an empty one.
                if token.quoteStart == nil, index < tokens.count, qualifier(of: tokens[index]) == nil {
                    let next = tokens[index].text.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !next.isEmpty {
                        terms.append(Term(field: qualified.field, text: fold(next)))
                        index += 1
                        continue
                    }
                }
            }
            let text = token.text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty { terms.append(Term(field: nil, text: fold(text))) }
        }
        return terms
    }
}

/// Session counts by state plus the total, for the dashboard sidebar. The
/// dashboard builds it from `SessionSearch.filter` output, so with no
/// filters it equals the snapshot's own `counts` (the CLI builds those from
/// the same session list).
struct FilteredCounts: Sendable, Equatable {
    var counts = Counts()
    /// Every counted session, including any in an unknown state.
    var total = 0

    /// Open `needs_you` notes among the listed notes of these sessions that
    /// `query` matches note by note (the CLI lists at most 20 per session;
    /// only listed notes carry a kind), as the popover's Updates feed does.
    /// Sessions in Needs you or Interrupted are skipped: `needingAttention`
    /// already counts them, so a hidden one is never reported twice.
    var needsYouNotes = 0

    /// `counts.notes` follows `UpdatesFeed.total` for `query` (the query the
    /// sessions were filtered with), so a query that only hits one note's
    /// text counts that note, as the popover's Updates header does.
    init(_ sessions: [Session], query: SessionSearch = SessionSearch("")) {
        total = sessions.count
        for session in sessions {
            counts.increment(session.state)
            if session.state != .needsYou && session.state != .interrupted {
                needsYouNotes += session.notes.filter { $0.kind == .needsYou && query.matches($0, in: session) }.count
            }
        }
        counts.notes = UpdatesFeed.total(from: sessions, query: query)
    }

    func count(for state: SessionState) -> Int { counts.count(for: state) }

    /// Needs you plus Interrupted.
    var needingAttention: Int { counts.needsYou + counts.interrupted }

    /// How many sessions that need the user (Needs you or Interrupted) are
    /// in `all` but not in these counts, i.e. hidden by the filters.
    func hiddenNeedingAttention(from all: FilteredCounts) -> Int {
        max(0, all.needingAttention - needingAttention)
    }

    /// How many open `needs_you` notes are on sessions in `all` but not in
    /// these counts. A `needs_you` note outlives its state effect (a newer
    /// prompt releases it; snooze and done win over it), so a hidden session
    /// in Review, Snoozed or Done can still hold an unanswered request.
    func hiddenNeedsYouNotes(from all: FilteredCounts) -> Int {
        max(0, all.needsYouNotes - needsYouNotes)
    }

    /// The dashboard sidebar's warning when filters hide sessions or updates
    /// that need the user, e.g. "Filters hide 2 sessions that need you" or
    /// "Filters hide 1 session and 2 updates that need you". Nil for zero.
    static func hiddenAttentionNotice(_ hidden: Int, notes: Int = 0) -> String? {
        let sessions = hidden == 1 ? "1 session" : "\(hidden) sessions"
        let updates = notes == 1 ? "1 update" : "\(notes) updates"
        switch (hidden > 0, notes > 0) {
        case (false, false): return nil
        case (true, false): return "Filters hide \(sessions) that need\(hidden == 1 ? "s" : "") you"
        case (false, true): return "Filters hide \(updates) that need\(notes == 1 ? "s" : "") you"
        case (true, true): return "Filters hide \(sessions) and \(updates) that need you"
        }
    }
}

extension Counts {
    func count(for state: SessionState) -> Int {
        switch state {
        case .needsYou: return needsYou
        case .interrupted: return interrupted
        case .running: return running
        case .review: return review
        case .unconcluded: return unconcluded
        case .snoozed: return snoozed
        case .done: return done
        case .unknown: return 0
        }
    }

    mutating func increment(_ state: SessionState) {
        switch state {
        case .needsYou: needsYou += 1
        case .interrupted: interrupted += 1
        case .running: running += 1
        case .review: review += 1
        case .unconcluded: unconcluded += 1
        case .snoozed: snoozed += 1
        case .done: done += 1
        case .unknown: break
        }
    }
}
