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
