import Foundation

/// The menu bar popover's sections, computed from the snapshot and the
/// popover's query: filter first, then group. Pure so the grouping, the
/// query-only Snoozed/Done sections and the hidden-sessions notice are
/// testable without SwiftUI.
struct PopoverSections: Sendable, Equatable {
    enum Kind: String, Sendable, CaseIterable {
        case needsYou, review, running, unconcluded, snoozed, done

        var title: String {
            switch self {
            case .needsYou: return "Needs you"
            case .review: return "Review"
            case .running: return "Running"
            case .unconcluded: return "Unconcluded"
            case .snoozed: return "Snoozed"
            case .done: return "Done"
            }
        }

        /// Snoozed and Done appear only while a query is active.
        var queryOnly: Bool { self == .snoozed || self == .done }

        func contains(_ state: SessionState) -> Bool {
            switch self {
            case .needsYou: return state == .needsYou || state == .interrupted
            case .review: return state == .review
            case .running: return state == .running
            case .unconcluded: return state == .unconcluded
            case .snoozed: return state == .snoozed
            case .done: return state == .done
            }
        }
    }

    struct Section: Sendable, Equatable, Identifiable {
        let kind: Kind
        let sessions: [Session]
        var id: Kind { kind }
    }

    /// Non-empty sections in display order.
    let sections: [Section]
    /// A query is active (non-empty after parsing).
    let isFiltering: Bool
    /// The snapshot holds no sessions at all.
    let snapshotIsEmpty: Bool
    /// Sessions in the snapshot that no section shows because of the query.
    /// Zero when not filtering. Sessions in a state no section can show
    /// (`.unknown`, from a newer CLI) are never counted, since the query is
    /// not what hides them.
    let hiddenCount: Int
    /// How many of the hidden sessions are Needs you or Interrupted.
    let hiddenNeedingAttention: Int
    /// Open `needs_you` notes the Updates feed leaves out because of the
    /// query (see `UpdatesFeed.hiddenNeedsYouNotes`). A session in Review,
    /// Snoozed or Done can hold one, so the state count above misses it;
    /// notes on Needs you / Interrupted sessions are left to that count.
    let hiddenNeedsYouNotes: Int

    static func make(from sessions: [Session], query: SessionSearch) -> PopoverSections {
        let filtering = !query.isEmpty
        let matched = filtering ? sessions.filter(query.matches) : sessions
        let kinds = Kind.allCases.filter { filtering || !$0.queryOnly }
        let built = kinds.compactMap { kind -> Section? in
            let members = matched
                .filter { kind.contains($0.state) }
                .sorted { $0.updatedAt > $1.updatedAt }
            return members.isEmpty ? nil : Section(kind: kind, sessions: members)
        }
        var hidden = 0
        var hiddenAttention = 0
        if filtering {
            let shown = Set(built.flatMap { $0.sessions.map(\.key) })
            for session in sessions where !shown.contains(session.key) && session.state != .unknown {
                hidden += 1
                if session.state == .needsYou || session.state == .interrupted { hiddenAttention += 1 }
            }
        }
        return PopoverSections(
            sections: built,
            isFiltering: filtering,
            snapshotIsEmpty: sessions.isEmpty,
            hiddenCount: hidden,
            hiddenNeedingAttention: hiddenAttention,
            hiddenNeedsYouNotes: UpdatesFeed.hiddenNeedsYouNotes(from: sessions, query: query)
        )
    }

    func section(_ kind: Kind) -> Section? { sections.first { $0.kind == kind } }

    /// Headers carry a count while a query is active (Unconcluded's
    /// disclosure label always does).
    func header(for section: Section) -> String {
        if isFiltering || section.kind == .unconcluded {
            return "\(section.kind.title) (\(section.sessions.count))"
        }
        return section.kind.title
    }

    /// The Unconcluded disclosure opens by itself while a query is active
    /// and it has matches.
    var expandsUnconcluded: Bool { isFiltering && section(.unconcluded) != nil }

    /// A query is active, the snapshot has sessions, and none match.
    var hasNoMatches: Bool { isFiltering && !snapshotIsEmpty && sections.isEmpty }

    /// One line under the search field while a query is active, e.g.
    /// "Filtered: 3 sessions hidden" or
    /// "Filtered: 3 sessions hidden, 1 needs you, 2 updates need you".
    var hiddenNotice: String? {
        guard isFiltering else { return nil }
        let noun = hiddenCount == 1 ? "session" : "sessions"
        var text = "Filtered: \(hiddenCount) \(noun) hidden"
        if hiddenNeedingAttention > 0 {
            text += ", \(hiddenNeedingAttention) need\(hiddenNeedingAttention == 1 ? "s" : "") you"
        }
        if hiddenNeedsYouNotes > 0 {
            text += hiddenNeedsYouNotes == 1
                ? ", 1 update needs you"
                : ", \(hiddenNeedsYouNotes) updates need you"
        }
        return text
    }

    /// Emphasize the notice when the query hides a session or an open
    /// `needs_you` note that needs the user.
    var noticeIsEmphasized: Bool { hiddenNeedingAttention > 0 || hiddenNeedsYouNotes > 0 }
}

/// The popover's Updates section: every open note of the (filtered)
/// sessions, flattened and newest first, each carrying its session so a row
/// can show the project and tool badge and focus the owning terminal. Pure
/// so the ordering and filtering are testable without SwiftUI.
enum UpdatesFeed {
    struct Item: Sendable, Equatable, Identifiable {
        let note: Note
        let session: Session

        var id: Int { note.id }
        var sessionKey: String { session.key }
        var project: String { session.project }
        var toolBadge: String { session.toolBadge }
    }

    /// The listed notes the query keeps, filtered note by note: a note
    /// shows when the query matches its own text or its session's fields
    /// (`SessionSearch.matches(_:in:)`), so a query for a PR number lists
    /// the note that mentions it, not its siblings. Ties on time keep the
    /// higher note id (the newer row) first.
    static func make(from sessions: [Session], query: SessionSearch) -> [Item] {
        let items = sessions.flatMap { session in
            session.notes
                .filter { query.matches($0, in: session) }
                .map { Item(note: $0, session: session) }
        }
        return items.sorted { a, b in
            let da = a.note.lastDate ?? .distantPast
            let db = b.note.lastDate ?? .distantPast
            if da != db { return da > db }
            return a.note.id > b.note.id
        }
    }

    /// Open `needs_you` notes among the listed ones that the query leaves out
    /// of the feed. Zero with no query. Sessions in Needs you or Interrupted
    /// are skipped: their state already says they need the user (usually
    /// because of this very note), and `hiddenNeedingAttention` counts them
    /// when hidden, so one request is never reported twice.
    static func hiddenNeedsYouNotes(from sessions: [Session], query: SessionSearch) -> Int {
        guard !query.isEmpty else { return 0 }
        return sessions.reduce(0) { sum, session in
            guard session.state != .needsYou && session.state != .interrupted else { return sum }
            return sum + session.notes.filter { $0.kind == .needsYou && !query.matches($0, in: session) }.count
        }
    }

    /// How many open notes the query keeps, uncapped: the one counting rule
    /// for Updates totals. The popover header and the dashboard sidebar
    /// (`FilteredCounts(_:query:)`) both count with it, note by note like
    /// the feed; the row badge and the detail header show a session's own
    /// uncapped `notesCount`, which is this with no query. A session
    /// the query matches on its own fields keeps every open note, listed or
    /// not; otherwise only its listed notes can be checked, so those that
    /// match count. The CLI lists at most 20 per session, so this can exceed
    /// the feed's length.
    static func total(from sessions: [Session], query: SessionSearch) -> Int {
        sessions.reduce(0) { sum, session in
            if query.matchesIgnoringNotes(session) {
                return sum + max(session.notesCount, session.notes.count)
            }
            return sum + session.notes.filter { query.matches($0, in: session) }.count
        }
    }

    /// An Updates header: the uncapped total, with how many are listed when
    /// the per-session cap leaves some out, e.g. "Updates (3)" or
    /// "Updates (20 of 25)". The popover and the session detail both use it.
    static func header(listed: Int, total: Int) -> String {
        let total = max(total, listed)
        return listed < total ? "Updates (\(listed) of \(total))" : "Updates (\(total))"
    }

    /// The popover section header for this feed.
    static func header(for items: [Item], total: Int) -> String {
        header(listed: items.count, total: total)
    }
}

/// One way to dismiss open notes through the CLI: what a grouped row's x and
/// the Updates "Clear all"/"Clear matching" button resolve to.
enum NoteDismissal: Sendable, Equatable {
    /// `notes --dismiss-all`: every open note, listed or not.
    case all
    /// `notes --dismiss-all --session TOOL:ID [--kind K...]`: every open note
    /// of one session (only the given kinds when `kinds` is set).
    case session(tool: String, sessionId: String, kinds: [Note.Kind]?)
    /// `notes --dismiss ID...`: exactly these notes.
    case ids([Int])
}

extension UpdatesFeed {
    /// Kinds a session's feed row groups: everything but `needs_you`, which
    /// always gets a row of its own.
    static let groupedKinds: [Note.Kind] = [.update, .error]

    /// The popover shows at most this many update/error rows until the
    /// user expands the section.
    static let compactLimit = 3

    /// A row of the popover's Updates section: one note, or (`isGroup`) all
    /// of one session's update/error notes the query keeps, which a loop
    /// session piles up tick after tick.
    struct Row: Sendable, Equatable, Identifiable {
        let session: Session
        /// The listed notes this row stands for, newest first; one unless
        /// grouped.
        let notes: [Note]
        /// Open notes this row stands for, uncapped (a group of a session
        /// with notes past the snapshot's cap counts those too).
        let total: Int
        let isGroup: Bool
        /// What the row's x dismisses.
        let dismissal: NoteDismissal

        /// The newest note: its text and time lead the row.
        var note: Note { notes[0] }
        var id: String { isGroup ? "group:\(session.key)" : "note:\(note.id)" }
        var sessionKey: String { session.key }
        var project: String { session.project }
        var toolBadge: String { session.toolBadge }
        var isNeedsYou: Bool { !isGroup && note.kind == .needsYou }
        /// The kind the row's glyph shows: a group with an error among its
        /// notes shows the error, so an outage stays visible while collapsed.
        var glyphKind: Note.Kind {
            isGroup && notes.contains { $0.kind == .error } ? .error : note.kind
        }
        /// "×12 notes" on a group row, nil otherwise.
        var groupLabel: String? { isGroup ? "×\(total) notes" : nil }
    }

    /// A session's open update/error notes, uncapped. The snapshot never
    /// caps out a `needs_you` note, so every unlisted note is one of these.
    static func groupedTotal(of session: Session) -> Int {
        let listed = session.notes.filter { groupedKinds.contains($0.kind) }.count
        return listed + max(0, session.notesCount - session.notes.count)
    }

    /// The feed as rows: each `needs_you` note on its own, and each
    /// session's update/error notes folded into one row once there are two
    /// or more of them, placed where its newest note would be. A query that
    /// matches the session on its own fields keeps every note, so the group
    /// counts and dismisses all of them; otherwise it holds, counts and
    /// dismisses only the listed notes the query matched.
    static func rows(from sessions: [Session], query: SessionSearch) -> [Row] {
        let items = make(from: sessions, query: query)
        var grouped: [String: [Note]] = [:]
        for item in items where groupedKinds.contains(item.note.kind) {
            grouped[item.sessionKey, default: []].append(item.note)
        }
        var emitted = Set<String>()
        var rows: [Row] = []
        for item in items {
            let session = item.session
            guard groupedKinds.contains(item.note.kind) else {
                rows.append(single(item))
                continue
            }
            let members = grouped[item.sessionKey] ?? [item.note]
            let whole = query.matchesIgnoringNotes(session)
            let total = whole ? max(groupedTotal(of: session), members.count) : members.count
            guard total > 1 else {
                rows.append(single(item))
                continue
            }
            guard emitted.insert(item.sessionKey).inserted else { continue }
            let dismissal: NoteDismissal = whole
                ? .session(tool: session.tool, sessionId: session.sessionId, kinds: groupedKinds)
                : .ids(members.map(\.id))
            rows.append(Row(session: session, notes: members, total: total, isGroup: true,
                            dismissal: dismissal))
        }
        return rows
    }

    private static func single(_ item: Item) -> Row {
        Row(session: item.session, notes: [item.note], total: 1, isGroup: false,
            dismissal: .ids([item.note.id]))
    }

    /// The popover's compact view of `rows`: every `needs_you` row, and the
    /// first `limit` others, in feed order. Expanded, every row. `hidden`
    /// is how many rows the compact view leaves out (0 when expanded).
    static func compact(_ rows: [Row], expanded: Bool, limit: Int = compactLimit)
        -> (shown: [Row], hidden: Int)
    {
        guard !expanded else { return (rows, 0) }
        var others = 0
        let shown = rows.filter { row in
            if row.isNeedsYou { return true }
            others += 1
            return others <= limit
        }
        return (shown, rows.count - shown.count)
    }

    /// Whether the compact view of `rows` leaves anything out, i.e. the
    /// section needs a "Show all N" / "Show fewer" toggle.
    static func needsToggle(_ rows: [Row], limit: Int = compactLimit) -> Bool {
        compact(rows, expanded: false, limit: limit).hidden > 0
    }

    /// What the Updates section's clear button does for this query.
    struct ClearPlan: Sendable, Equatable {
        let title: String
        let help: String
        let dismissals: [NoteDismissal]
    }

    /// With no query, "Clear all" dismisses every open note: past the
    /// snapshot's per-session cap and on sessions the popover does not list.
    /// With a query, "Clear matching" dismisses every note the query keeps:
    /// all open notes of each session it matches on its own fields, plus
    /// the listed notes it matches elsewhere.
    static func clearPlan(from sessions: [Session], query: SessionSearch) -> ClearPlan {
        guard !query.isEmpty else {
            return ClearPlan(title: "Clear all",
                             help: "Dismiss every open update, including ones not listed here",
                             dismissals: [.all])
        }
        var dismissals: [NoteDismissal] = []
        var ids: [Int] = []
        for session in sessions {
            if query.matchesIgnoringNotes(session) {
                if session.notesCount > 0 || !session.notes.isEmpty {
                    dismissals.append(.session(tool: session.tool, sessionId: session.sessionId, kinds: nil))
                }
            } else {
                ids += session.notes.filter { query.matches($0, in: session) }.map(\.id)
            }
        }
        if !ids.isEmpty { dismissals.append(.ids(ids)) }
        return ClearPlan(title: "Clear matching",
                         help: "Dismiss every update that matches the filter",
                         dismissals: dismissals)
    }
}
