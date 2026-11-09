import Foundation

/// The order of what a session row shows, kept out of `SessionRowView` so
/// it is testable and so every row lays out the same way: the same line in
/// the same place, the same icon in the same column.
enum SessionRowLayout {
    /// The row's text lines, top to bottom.
    enum Line: Hashable {
        /// The status text, else the title, with the tags and the time.
        case title
        /// project · branch, with tmux, PR and model tags on the right.
        /// Always the second line, so the repo is where the eye expects it.
        case repo
        /// The session's own title, under a status text that leads.
        case secondaryTitle
        /// What the session needs, or why it is in Review.
        case reason
    }

    /// The fixed column under the dot: the tool badge, then the permission
    /// mode icon, then the desktop app button, each only when it applies.
    enum Icon: Equatable {
        case tool
        /// A raw permission mode (`plan`, `acceptEdits`, ...).
        case mode(String)
        case desktop
    }

    /// The small tags after the first line's text, in this order.
    enum TitleTag: Hashable {
        case interrupted
        case notes
        case autoDone
        /// A Review row whose process has exited (its amber color).
        case closed
    }

    static func lines(for session: Session) -> [Line] {
        var lines: [Line] = [.title, .repo]
        if SessionDisplay.secondaryTitle(session) != nil { lines.append(.secondaryTitle) }
        if SessionDisplay.reasonText(session) != nil { lines.append(.reason) }
        return lines
    }

    static func iconColumn(for session: Session) -> [Icon] {
        var icons: [Icon] = [.tool]
        if let mode = PermissionMode.rowMode(for: session) { icons.append(.mode(mode)) }
        if session.desktop { icons.append(.desktop) }
        return icons
    }

    static func titleTags(for session: Session) -> [TitleTag] {
        var tags: [TitleTag] = []
        if session.state == .interrupted { tags.append(.interrupted) }
        if session.notesCount > 0 { tags.append(.notes) }
        if session.isAutoDone { tags.append(.autoDone) }
        if session.state == .review, !session.alive { tags.append(.closed) }
        return tags
    }

    /// The repo line's text: "project · branch", or the project alone.
    static func repoText(_ session: Session) -> String {
        if let branch = session.branch?.trimmingCharacters(in: .whitespacesAndNewlines), !branch.isEmpty {
            return session.project.isEmpty ? branch : "\(session.project) · \(branch)"
        }
        return session.project
    }
}
