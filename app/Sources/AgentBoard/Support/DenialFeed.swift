import Foundation

/// One way to dismiss open permission denials through the CLI: what a row's
/// x, the session detail's Dismiss All and the Denials header's "Clear
/// all"/"Clear matching" resolve to.
enum DenialDismissal: Sendable, Equatable {
    /// `denials --dismiss-all`: every open denial, listed or not, of every
    /// session (hidden ones included).
    case all
    /// `denials --dismiss-all --session TOOL:ID`: every open denial of one
    /// session.
    case session(tool: String, sessionId: String)
    /// `denials --dismiss ID...`: exactly these denials.
    case ids([Int])
}

/// The dashboard's Denials view: every listed open denial of the sessions
/// the pickers keep, flattened and newest first, each carrying its session.
/// Pure so ordering, filtering and counting are testable without SwiftUI.
enum DenialFeed {
    struct Row: Sendable, Equatable, Identifiable {
        let denial: Denial
        let session: Session

        /// Unique across sessions, so a list selection never collides.
        var id: String { "\(session.key)#\(denial.id)" }
        var sessionKey: String { session.key }
    }

    /// The session key a `Row.id` was built from: everything before its last
    /// `#` (the denial id after it is a number). Nil for anything else.
    static func sessionKey(ofRowID id: String) -> String? {
        guard let hash = id.lastIndex(of: "#"), Int(id[id.index(after: hash)...]) != nil else { return nil }
        let key = String(id[..<hash])
        return key.isEmpty ? nil : key
    }

    /// The Denials list's row selection once the selected session changes to
    /// `sessionKey` (from the All list, the popover, or the row itself): kept
    /// only while it still points at that session, so a stale highlight never
    /// survives and clicking that row again changes the selection.
    static func rowSelection(_ rowID: String?, afterSessionKeyChangedTo sessionKey: String?) -> String? {
        guard let rowID, let key = self.sessionKey(ofRowID: rowID), key == sessionKey else { return nil }
        return rowID
    }

    /// Sessions passing the tool and project pickers, whatever the query.
    /// The Denials view and its badge start from these: a session whose
    /// only hit is a denial's reason does not match the query by itself.
    static func pickerFiltered(_ sessions: [Session], tool: String = "all",
                               project: String = "all") -> [Session] {
        SessionSearch("").filter(sessions, tool: tool, project: project)
    }

    /// The listed denials the pickers and the query keep, newest first
    /// (ties: the higher id first).
    static func rows(from sessions: [Session], tool: String = "all", project: String = "all",
                     query: SessionSearch) -> [Row] {
        let rows = pickerFiltered(sessions, tool: tool, project: project).flatMap { session in
            session.denials
                .filter { query.matches($0, in: session) }
                .map { Row(denial: $0, session: session) }
        }
        return rows.sorted { a, b in
            let da = a.denial.date ?? .distantPast
            let db = b.denial.date ?? .distantPast
            if da != db { return da > db }
            return a.denial.id > b.denial.id
        }
    }

    /// How many open denials the query keeps, uncapped. A session the query
    /// matches on its own fields keeps every open denial, listed or not;
    /// otherwise only its listed denials can be checked, so those that match
    /// count. With no query this is the snapshot's `counts.denials` for the
    /// same sessions.
    static func total(from sessions: [Session], query: SessionSearch) -> Int {
        sessions.reduce(0) { sum, session in
            if query.matches(session) {
                return sum + max(session.denialsCount, session.denials.count)
            }
            return sum + session.denials.filter { query.matches($0, in: session) }.count
        }
    }

    /// A Denials header: "Permission denials (3)", or "(20 of 25)" when the
    /// per-session cap leaves some out.
    static func header(listed: Int, total: Int) -> String {
        let total = max(total, listed)
        return listed < total
            ? "Permission denials (\(listed) of \(total))"
            : "Permission denials (\(total))"
    }

    /// What the Denials header's clear button does for these filters.
    struct ClearPlan: Sendable, Equatable {
        let title: String
        let help: String
        let dismissals: [DenialDismissal]
    }

    /// With no query and both pickers on "all", "Clear all" dismisses every
    /// open denial (past the cap and on hidden sessions too). Otherwise
    /// "Clear matching" dismisses every denial the filters keep: all open
    /// denials of each session the query matches on its own fields, plus
    /// the listed denials it matches elsewhere.
    static func clearPlan(from sessions: [Session], tool: String = "all", project: String = "all",
                          query: SessionSearch) -> ClearPlan {
        if query.isEmpty && tool == "all" && project == "all" {
            return ClearPlan(
                title: "Clear all",
                help: "Dismiss every open denial, including ones of hidden sessions and ones not listed",
                dismissals: [.all])
        }
        var dismissals: [DenialDismissal] = []
        var ids: [Int] = []
        for session in pickerFiltered(sessions, tool: tool, project: project) {
            if query.matches(session) {
                if session.denialsCount > 0 || !session.denials.isEmpty {
                    dismissals.append(.session(tool: session.tool, sessionId: session.sessionId))
                }
            } else {
                ids += session.denials.filter { query.matches($0, in: session) }.map(\.id)
            }
        }
        if !ids.isEmpty { dismissals.append(.ids(ids)) }
        return ClearPlan(title: "Clear matching",
                         help: "Dismiss every denial that matches the filters",
                         dismissals: dismissals)
    }
}
