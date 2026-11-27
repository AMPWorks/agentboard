import CoreGraphics
import Foundation

/// The popover list's hold: while the pointer is over the open popover
/// (or one of its menus is open), the list keeps the sessions it showed
/// when the pointer came in, so no row moves, appears or vanishes under
/// it. The newest snapshot applies when the pointer leaves, the popover
/// closes, the filter changes, or the owner clicks "Update list". Pure,
/// so the rules are testable as snapshot in, displayed list out; the menu
/// bar icon, notifications and the dashboard never read it.
struct PopoverHold: Sendable, Equatable {
    /// The sessions the list was built from when the hold began; nil while live.
    private(set) var held: [Session]?
    /// The filter text the held sessions were captured under.
    private(set) var query: String = ""

    var isHolding: Bool { held != nil }

    /// Holds only while the popover is open and the pointer is inside it
    /// or one of its menus is open.
    static func shouldHold(popoverVisible: Bool, pointerInside: Bool, menuOpen: Bool) -> Bool {
        popoverVisible && (pointerInside || menuOpen)
    }

    /// The pointer (screen coordinates) is inside the window's frame, its
    /// edges included; no window means outside.
    static func pointerInside(mouse: CGPoint, windowFrame: CGRect?) -> Bool {
        guard let frame = windowFrame, !frame.isNull, !frame.isEmpty else { return false }
        return mouse.x >= frame.minX && mouse.x <= frame.maxX && mouse.y >= frame.minY && mouse.y <= frame.maxY
    }

    /// Not holding clears the hold; holding with none captures what the
    /// list showed (`previous`, the snapshot before the one that just
    /// arrived, when the hold begins on a snapshot change; else `live`); a
    /// new query recaptures from `live` (typing never waits); otherwise the
    /// held sessions stay.
    mutating func update(live: [Session], query: String, holding: Bool, previous: [Session]? = nil) {
        guard holding else {
            held = nil
            self.query = query
            return
        }
        if query != self.query {
            held = live
            self.query = query
        } else if held == nil {
            // Same query: the live list on screen was built from `previous`.
            held = previous ?? live
        }
    }

    /// What the list shows: the held sessions, else `live`. With `query`,
    /// a query other than the captured one shows `live` already, before
    /// `update` recaptures.
    func displayed(live: [Session], query: String? = nil) -> [Session] {
        guard let held else { return live }
        if let query, query != self.query { return live }
        return held
    }

    /// "Update list": shows the newest sessions while still holding.
    mutating func release(live: [Session]) {
        guard held != nil else { return }
        held = live
    }

    /// The live session with the same key: the one a click on a held row
    /// acts on. Keys include the Mac, so two Macs' sessions never match.
    static func resolve(_ heldSession: Session, live: [Session]) -> Session? {
        live.first { $0.key == heldSession.key }
    }

    /// The popover section a state is listed in, nil for `.unknown`.
    static func kind(of state: SessionState) -> PopoverSections.Kind? {
        PopoverSections.Kind.allCases.first { $0.contains(state) }
    }

    /// What a held row's session is now.
    enum RowStatus: Sendable, Equatable {
        case current
        /// In another section now (nil: a state no section lists).
        case moved(to: PopoverSections.Kind?)
        /// No longer in the snapshot.
        case gone

        var isDimmed: Bool { self != .current }

        /// The short form, for the accessibility value.
        var label: String? {
            switch self {
            case .current: return nil
            case .moved(let kind): return kind.map { "Now in \($0.title)" } ?? "Now in another state"
            case .gone: return "No longer on the board"
            }
        }

        /// The row's tooltip.
        var help: String? {
            label.map { "\($0); the list is held while the pointer is over it (Update list shows the newest)" }
        }
    }

    func status(of heldSession: Session, live: [Session]) -> RowStatus {
        guard held != nil else { return .current }
        guard let now = Self.resolve(heldSession, live: live) else { return .gone }
        let before = Self.kind(of: heldSession.state)
        let after = Self.kind(of: now.state)
        return before == after ? .current : .moved(to: after)
    }

    /// What "Update list" would change, over the sessions the query keeps
    /// and the popover can list.
    struct Pending: Sendable, Equatable {
        var added = 0
        var moved = 0
        var gone = 0
        var notes = 0
        /// New or moved sessions now in Needs you or Interrupted, plus new
        /// `needs_you` notes.
        var needsYou = 0

        var total: Int { added + moved + gone + notes }

        /// The footer control's text, nil when nothing is pending.
        var label: String? {
            guard total > 0 else { return nil }
            if needsYou > 0 {
                return "\(needsYou) \(needsYou == 1 ? "needs" : "need") you · Update list"
            }
            return "\(total) \(total == 1 ? "change" : "changes") · Update list"
        }
    }

    /// Sessions the popover lists under `search`: the query's matches in
    /// a state some section shows (Snoozed and Done only while filtering).
    private static func listable(_ sessions: [Session], search: SessionSearch) -> [Session] {
        let filtering = !search.isEmpty
        return sessions.filter { session in
            guard let kind = kind(of: session.state), filtering || !kind.queryOnly else { return false }
            return !filtering || search.matches(session)
        }
    }

    private static func needsYou(_ state: SessionState) -> Bool {
        state == .needsYou || state == .interrupted
    }

    func pending(live: [Session], query: String) -> Pending {
        // A new query already shows `live` (`displayed(live:query:)`).
        guard let held, query == self.query else { return Pending() }
        let search = SessionSearch(query)
        let shownHeld = Self.listable(held, search: search)
        let shownLive = Self.listable(live, search: search)
        let heldKeys = Set(shownHeld.map(\.key))
        let liveByKey = Dictionary(live.map { ($0.key, $0) }, uniquingKeysWith: { first, _ in first })
        var result = Pending()
        // One session counts once in `needsYou`, however many reasons it has.
        var needsYouKeys: Set<String> = []
        for session in shownLive where !heldKeys.contains(session.key) {
            result.added += 1
            if Self.needsYou(session.state) { needsYouKeys.insert(session.key) }
        }
        for session in shownHeld {
            guard let now = liveByKey[session.key] else {
                result.gone += 1
                continue
            }
            if Self.kind(of: session.state) != Self.kind(of: now.state) {
                result.moved += 1
                if Self.needsYou(now.state) { needsYouKeys.insert(session.key) }
            }
        }
        let heldNotes = Set(UpdatesFeed.make(from: held, query: search).map(\.id))
        for item in UpdatesFeed.make(from: live, query: search) where !heldNotes.contains(item.id) {
            result.notes += 1
            if item.note.kind == .needsYou { needsYouKeys.insert(item.sessionKey) }
        }
        result.needsYou = needsYouKeys.count
        return result
    }

    /// One of the owner's own actions, as the held list applies it
    /// (`SnapshotStore.lastOwnAction`).
    enum OwnAction: Sendable, Equatable {
        /// Mark done, snooze, reopen, focus, resume: that session's held
        /// entry takes its live value.
        case session(key: String)
        /// Note dismissals: only the notes they name leave the held list.
        case notes([NoteDismissal])
    }

    /// Applies the owner's own action to the held list and nothing else.
    mutating func apply(_ action: OwnAction, live: [Session]) {
        switch action {
        case .session(let key): applyOwnAction(sessionKey: key, live: live)
        case .notes(let dismissals): applyDismissals(dismissals, live: live)
        }
    }

    /// The owner acted on one session (mark done, snooze, reopen): its
    /// held entry takes the live value, or leaves when the session is
    /// gone. Nothing else pending applies, and nothing is added.
    mutating func applyOwnAction(sessionKey: String, live: [Session]) {
        guard var sessions = held, let index = sessions.firstIndex(where: { $0.key == sessionKey }) else { return }
        if let now = live.first(where: { $0.key == sessionKey }) {
            sessions[index] = now
        } else {
            sessions.remove(at: index)
        }
        held = sessions
    }

    /// The owner dismissed notes: held notes that a dismissal names and
    /// that are no longer open in the live session with the same key drop
    /// out, and its count lowers to match. A note closed by something else
    /// meanwhile stays (that is pending, not the owner's click). Never
    /// adds a note or a session, and never changes a session's state.
    mutating func applyDismissals(_ dismissals: [NoteDismissal], live: [Session]) {
        guard var sessions = held, !dismissals.isEmpty else { return }
        let liveByKey = Dictionary(live.map { ($0.key, $0) }, uniquingKeysWith: { first, _ in first })
        for index in sessions.indices {
            let session = sessions[index]
            // A session that left the board has no open notes.
            let open = Set((liveByKey[session.key]?.notes ?? []).map(\.id))
            let kept = session.notes.filter { note in
                open.contains(note.id) || !dismissals.contains { Self.names($0, note, of: session) }
            }
            let removed = sessions[index].notes.count - kept.count
            sessions[index].notes = kept
            sessions[index].notesCount = max(kept.count, sessions[index].notesCount - removed)
        }
        held = sessions
    }

    /// While held, a row's x dismisses only what it shows: a group row
    /// that dismisses its whole session (`.session`) narrows to its listed
    /// notes when that session has an open note in `live` the held list
    /// does not show, so a note that arrived during the hold is never
    /// dismissed unseen. Live, or with nothing new, rows are unchanged.
    func scoped(_ rows: [UpdatesFeed.Row], live: [Session]) -> [UpdatesFeed.Row] {
        guard held != nil else { return rows }
        let liveByKey = Dictionary(live.map { ($0.key, $0) }, uniquingKeysWith: { first, _ in first })
        return rows.map { row in
            guard case .session(_, _, let kinds) = row.dismissal, !row.session.isRemote,
                  let now = liveByKey[row.session.key] else { return row }
            let shown = Set(row.session.notes.map(\.id))
            let unseen = now.notes.contains { !shown.contains($0.id) && (kinds?.contains($0.kind) ?? true) }
            guard unseen else { return row }
            return UpdatesFeed.Row(session: row.session, notes: row.notes, total: row.notes.count,
                                   isGroup: row.isGroup, dismissal: .ids(row.notes.map(\.id)))
        }
    }

    /// While held with updates pending, the panel's Clear all / Clear
    /// matching would also dismiss notes that arrived during the hold and
    /// were never shown, so it becomes "Clear these" over the listed
    /// `rows` (pass them through `scoped` first). Otherwise `plan` as is.
    func clearPlan(_ plan: UpdatesFeed.ClearPlan, rows: [UpdatesFeed.Row], live: [Session],
                   query: String) -> UpdatesFeed.ClearPlan {
        guard pending(live: live, query: query).notes > 0 else { return plan }
        return UpdatesFeed.ClearPlan(
            title: "Clear these",
            help: "Dismiss the updates listed here; ones that arrived while the list is held stay until Update list",
            dismissals: rows.map(\.dismissal))
    }

    /// The dismissal covers this note. Note ids and `--session` are this
    /// Mac's, so a remote session's notes are never covered.
    static func names(_ dismissal: NoteDismissal, _ note: Note, of session: Session) -> Bool {
        guard !session.isRemote else { return false }
        switch dismissal {
        case .all: return true
        case .ids(let ids): return ids.contains(note.id)
        case .session(let tool, let sessionId, let kinds):
            return session.tool == tool && session.sessionId == sessionId && (kinds?.contains(note.kind) ?? true)
        }
    }
}
