import Foundation

/// What a session row shows as its title, in one place so the popover, the
/// dashboard, the detail view and the Updates feed agree. A live
/// `agentboard status` text (what the session says it is doing right now)
/// leads; the session's own title (custom, generated, `agentboard title`,
/// Claude's summary or the first prompt, as the CLI resolved it) is then
/// the subtitle.
enum SessionDisplay {
    static func statusLine(_ session: Session) -> String? {
        guard let text = session.statusText?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else { return nil }
        return text
    }

    static func primaryTitle(_ session: Session) -> String {
        statusLine(session) ?? session.title
    }

    /// The session title under a status line, or nil when the title is
    /// already the primary line (or would only repeat it).
    static func secondaryTitle(_ session: Session) -> String? {
        guard let status = statusLine(session) else { return nil }
        let title = session.title.trimmingCharacters(in: .whitespacesAndNewlines)
        return title.isEmpty || title == status ? nil : title
    }
}

/// The PR buttons on a session row: one per PR link that has a URL, the
/// ones that need the user first, capped with a "+N" for the rest.
enum PRButtons {
    static let defaultCap = 3

    struct Layout: Equatable {
        var shown: [Link]
        /// PR links left out of `shown` (the "+N" button opens the detail view).
        var overflow: Int
    }

    /// Awaiting the user, then open, merged, closed, anything else; newest
    /// (highest number) first within each group.
    static func rank(_ link: Link) -> Int {
        if link.awaitsYou == true { return 0 }
        switch link.state {
        case "OPEN": return 1
        case "MERGED": return 2
        case "CLOSED": return 3
        default: return 4
        }
    }

    static func layout(for links: [Link], cap: Int = defaultCap) -> Layout {
        let prs = links.filter { $0.isPR && $0.openURL != nil }
        let ordered = prs.enumerated().sorted { lhs, rhs in
            let (l, r) = (rank(lhs.element), rank(rhs.element))
            if l != r { return l < r }
            let (ln, rn) = (lhs.element.number ?? -1, rhs.element.number ?? -1)
            if ln != rn { return ln > rn }
            return lhs.offset < rhs.offset
        }.map(\.element)
        let shown = Array(ordered.prefix(max(0, cap)))
        return Layout(shown: shown, overflow: ordered.count - shown.count)
    }
}
