import Foundation

/// The order of what a session row shows, kept out of `SessionRowView` so
/// it is testable and so every row lays out the same way: the same line in
/// the same place, the same icon in the same column.
enum SessionRowLayout {
    /// The row's text lines, top to bottom.
    enum Line: Hashable {
        /// The status text, else the title (on a Needs you row the title
        /// first, `SessionDisplay.primaryTitle`), with the tags and the time.
        case title
        /// project · branch, with tmux, PR and model tags on the right.
        /// Always the second line, so the repo is where the eye expects it.
        case repo
        /// The other of the two (`SessionDisplay.secondaryTitle`).
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

    /// The repo line's trailing items, leading to trailing.
    enum RepoTag: Hashable {
        case tmux
        /// A Running row's background tasks, folded from its reason line
        /// (`backgroundTaskCount`).
        case background
        /// Model and context (`SessionModelTag`).
        case model
        /// The PR buttons (`PRLinkButtons`, which draws nothing without a PR).
        case prs
    }

    /// The quick buttons a row shows while the mouse is over it.
    enum HoverButton: CaseIterable, Hashable {
        case resume
        case copy
        case markDone
        case snooze
        case openLink

        /// The SF Symbol the button draws. The row and the glyph-fit test
        /// both read it, so a renamed symbol is measured where it is drawn.
        var systemImage: String {
            switch self {
            case .resume: "arrow.counterclockwise.circle"
            case .copy: "doc.on.doc"
            case .markDone: "checkmark.circle"
            case .snooze: "moon.zzz"
            case .openLink: "arrow.up.right.square"
            }
        }
    }

    /// Each hover button's frame width. The buttons draw at exactly this
    /// width (their glyphs differ), so the reserved slot and the drawn
    /// buttons come from the same numbers.
    static let hoverIconWidth: CGFloat = 18
    /// Each hover button's frame height. The glyphs fit inside it, so every
    /// drawn pixel of an icon is inside the button's clickable shape:
    /// measured at the default font, `doc.on.doc` is 18pt tall and
    /// `moon.zzz` 17pt. The slot sits beside the row's two or more text
    /// lines, so this never makes a row taller.
    static let hoverIconHeight: CGFloat = 18
    /// The gap between two hover buttons.
    static let hoverIconSpacing: CGFloat = 4

    /// Popover rows: copy the resume command, mark done. Focus/resume go
    /// through the row's own click.
    static let popoverHoverButtons: [HoverButton] = [.copy, .markDone]

    /// The hover buttons this session's row shows, leading to trailing.
    /// Dashboard rows show every action but focus (double-click the row):
    /// Resume once the process has exited, and the first non-PR link.
    static func hoverButtons(for session: Session, compact: Bool) -> [HoverButton] {
        if compact { return popoverHoverButtons }
        var buttons: [HoverButton] = []
        if session.resumeAvailable { buttons.append(.resume) }
        buttons += [.copy, .markDone, .snooze]
        if hoverLink(for: session) != nil { buttons.append(.openLink) }
        return buttons
    }

    /// The link the dashboard row's open button opens: PRs have their own
    /// buttons on the row, so the first other link with a URL.
    static func hoverLink(for session: Session) -> Link? {
        session.links.first { !$0.isPR && $0.openURL != nil }
    }

    /// The width every row reserves at its trailing edge for the hover
    /// buttons, whether or not the mouse is over it, so hovering never takes
    /// width from the text. Dashboard rows reserve room for every button any
    /// session can show, so the time and PR columns line up row to row.
    static func hoverSlotWidth(compact: Bool) -> CGFloat {
        hoverSlotWidth(buttons: compact ? popoverHoverButtons.count : HoverButton.allCases.count)
    }

    /// `count` buttons at `hoverIconWidth`, `hoverIconSpacing` apart.
    static func hoverSlotWidth(buttons count: Int) -> CGFloat {
        guard count > 0 else { return 0 }
        return CGFloat(count) * hoverIconWidth + CGFloat(count - 1) * hoverIconSpacing
    }

    static func lines(for session: Session) -> [Line] {
        var lines: [Line] = [.title, .repo]
        if SessionDisplay.secondaryTitle(session) != nil { lines.append(.secondaryTitle) }
        if SessionDisplay.reasonText(session) != nil, backgroundTaskCount(session) == nil { lines.append(.reason) }
        return lines
    }

    /// A Running row's "Waiting on N background task(s)[: types]" reason
    /// (reconcile.BACKGROUND_WAIT_REASON) as its count: the row shows it as
    /// a small tag on the repo line instead of a line of its own, so the
    /// row is a line shorter. Nil for any other reason or state.
    static func backgroundTaskCount(_ session: Session) -> Int? {
        guard session.state == .running,
              let raw = session.waitingReason?.trimmingCharacters(in: .whitespacesAndNewlines) else { return nil }
        let prefix = "Waiting on "
        guard raw.hasPrefix(prefix) else { return nil }
        let rest = raw.dropFirst(prefix.count)
        let digits = rest.prefix(while: { $0.isASCII && $0.isNumber })
        guard let count = Int(digits), count > 0 else { return nil }
        let after = rest.dropFirst(digits.count)
        guard after.hasPrefix(" background task") else { return nil }
        let tail = after.dropFirst(" background task".count)
        guard tail.isEmpty || tail == "s" || tail.hasPrefix(":") || tail.hasPrefix("s:") else { return nil }
        return count
    }

    /// The tag's text: "1 task", "3 tasks" (the tooltip has the reason).
    static func backgroundTagText(_ count: Int) -> String {
        count == 1 ? "1 task" : "\(count) tasks"
    }

    /// The PR buttons are always last, at the row's trailing edge, so they
    /// sit in the same place whether or not the row has a tmux or model tag
    /// (an optional tag after them used to push them left).
    static func repoTags(for session: Session) -> [RepoTag] {
        var tags: [RepoTag] = []
        if session.tmuxTag != nil { tags.append(.tmux) }
        if backgroundTaskCount(session) != nil { tags.append(.background) }
        if SessionModelTag.text(session) != nil { tags.append(.model) }
        tags.append(.prs)
        return tags
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
