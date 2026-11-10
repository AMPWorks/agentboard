/// What a session row's update badge (the speech bubble with the open-update
/// count) opens: the dashboard on that session, scrolled to one note.
enum SessionNoteTarget {
    /// The note the badge click scrolls to and outlines: the first needs_you
    /// note in list order, else the first note, else nil (the dashboard then
    /// opens on the session without scrolling). `session.notes` is already
    /// newest first (core `_open_notes_by_session`, with any older needs_you
    /// note appended past the newest 20 by `_listed_notes`), so this never
    /// re-sorts: the first needs_you note is the newest one.
    static func badgeTarget(for session: Session) -> Int? {
        (session.notes.first { $0.kind == .needsYou } ?? session.notes.first)?.id
    }

    /// "1 open update" or "N open updates".
    static func countText(_ count: Int) -> String {
        count == 1 ? "1 open update" : "\(count) open updates"
    }

    /// The badge's tooltip. A popover row's badge opens the dashboard; a
    /// dashboard list row's (`inDashboard`) selects the session and scrolls
    /// its detail, since the user is already in the dashboard.
    static func badgeHelp(count: Int, inDashboard: Bool = false) -> String {
        let pronoun = count == 1 ? "it" : "them"
        return inDashboard
            ? "\(countText(count)); click to show \(pronoun) in the session's detail"
            : "\(countText(count)); click to open \(pronoun) in the dashboard"
    }

    /// The badge's VoiceOver label (the count) ...
    static func badgeAccessibilityLabel(count: Int) -> String {
        countText(count)
    }

    /// ... and hint (what a click does, as in `badgeHelp`).
    static func badgeAccessibilityHint(count: Int, inDashboard: Bool = false) -> String {
        let pronoun = count == 1 ? "it" : "them"
        return inDashboard ? "Shows \(pronoun) in the session's detail" : "Opens \(pronoun) in the dashboard"
    }
}

/// How the dashboard takes a selection asked for elsewhere (a popover row,
/// a row's update badge or "+N" button, a notification).
enum PendingSelection {
    /// Whether the dashboard clears its search, pickers and sidebar state
    /// before selecting `key`: only when the session is not in the list it
    /// shows now (`listedKeys`), so a click on a listed row never throws the
    /// user's search away. The Denials view lists no sessions, so it always
    /// switches back to All. A key that is not in the snapshot at all
    /// (`knownKeys`: the session left it between the click and now, or is
    /// hidden while Show hidden is off) leaves the filters alone, because
    /// clearing them could not reveal it.
    static func clearsFilters(for key: String, listedKeys: [String], knownKeys: [String],
                              sidebar: SidebarFilter?) -> Bool {
        guard knownKeys.contains(key) else { return false }
        if sidebar == .denials { return true }
        return !listedKeys.contains(key)
    }
}
