import SwiftUI
import AppKit

struct SessionRowView: View {
    let session: Session
    @ObservedObject var store: SnapshotStore
    var compact: Bool = true
    /// Popover rows: the live session a click on this (possibly held) row
    /// acts on, looked up by key at click time (`PopoverHold.resolve`); nil
    /// from the closure means the session left the board. Unset (dashboard
    /// rows), the row acts on `session`.
    var resolveLive: ((Session) -> Session?)? = nil
    /// What a held row's session is now (`PopoverHold.status`).
    var holdStatus: PopoverHold.RowStatus = .current

    @State private var isHovering = false

    /// The session this row's actions use: the live one with the same key,
    /// nil when it is no longer on the board.
    private var actionSession: Session? {
        guard let resolveLive else { return session }
        return resolveLive(session)
    }

    var body: some View {
        // A remote session whose Mac is offline (its last-known state), or
        // a held row whose session changed meanwhile: dimmed.
        let decorated = rowContent
            .opacity(session.stale || holdStatus.isDimmed ? 0.55 : 1)
            .padding(.vertical, 4)
            .padding(.horizontal, 6)
            .contentShape(Rectangle())
            .onHover { isHovering = $0 }
            .optionalHelp(holdStatus.help)
            .optionalAccessibilityValue(holdStatus.label)

        // Popover rows aren't in a List, so they drive their own click and
        // context menu. Dashboard rows are List rows: selection, the
        // context menu, and double-click/Return are all handled at the
        // List level in DashboardView so native single-click selection
        // isn't fought over by a competing gesture recognizer.
        Group {
            if compact {
                decorated
                    .onTapGesture { handlePopoverTap() }
                    .contextMenu {
                        let live = actionSession
                        SessionContextMenu(session: live ?? session, store: store, noLongerOnBoard: live == nil)
                    }
            } else {
                decorated
            }
        }
    }

    private var rowContent: some View {
        HStack(alignment: .top, spacing: 8) {
            StateMarker(shape: SessionColor.markerShape(for: session), color: SessionColor.color(for: session))
                .padding(.top, 5)
                .help(SessionColor.markerTooltip(for: session))
                .accessibilityLabel(SessionColor.markerLabel(for: session))
            // The fixed icon column (SessionRowLayout.iconColumn): the tool
            // badge, the permission mode icon under it, then the desktop app
            // button, so they line up from row to row and never shift text.
            VStack(spacing: 3) {
                ForEach(SessionRowLayout.iconColumn(for: session), id: \.columnID) { icon in
                    switch icon {
                    case .tool: toolBadge
                    case .machine: machineIcon
                    case .mode(let mode): modeIcon(mode)
                    case .desktop: desktopButton
                    }
                }
            }
            .frame(width: 18, alignment: .top)
            // The text lines in SessionRowLayout.lines order: the repo is
            // always the second line.
            VStack(alignment: .leading, spacing: 2) {
                ForEach(SessionRowLayout.lines(for: session), id: \.self) { line in
                    switch line {
                    case .title: titleLine
                    case .repo: repoLine
                    case .secondaryTitle: secondaryTitleLine
                    case .reason: reasonLine
                    }
                }
            }
            // Always laid out, at a fixed width: hovering only shows the
            // buttons, so the text column is the same width either way and
            // the row never re-wraps or shifts its time and PR buttons.
            HoverSlot(width: SessionRowLayout.hoverSlotWidth(compact: compact), visible: isHovering) {
                hoverButtons
            }
        }
    }

    private var titleLine: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(session.displayTitle)
                .font(compact ? .body : .headline)
                .foregroundStyle(SessionColor.textColor(for: session))
                .wrapsInPopover(compact)
            // Dashboard rows are one line, so they leave out "closed" (the
            // dot and the detail pane say it) to keep room for the title.
            ForEach(SessionRowLayout.titleTags(for: session).filter { compact || $0 != .closed }, id: \.self) { tag in
                switch tag {
                case .interrupted: interruptedTag
                case .notes: notesBadge
                case .autoDone: autoDoneTag
                case .closed: closedTag
                }
            }
            Spacer(minLength: 4)
            Text(RelativeTime.string(from: session.updatedAt))
                .font(.caption2)
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .fixedSize()
                .help("Last activity")
        }
    }

    private var repoLine: some View {
        // First-baseline alignment keeps the trailing tags on the repo's
        // first line when a long branch wraps.
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(SessionRowLayout.repoText(session))
                .font(.caption)
                .foregroundStyle(.secondary)
                .wrapsInPopover(compact)
            Spacer(minLength: 4)
            // SessionRowLayout.repoTags order: the PR buttons last, so they
            // never move with the optional tags.
            ForEach(SessionRowLayout.repoTags(for: session), id: \.self) { tag in
                switch tag {
                case .machine: machineTag
                case .tmux: tmuxTag
                case .background: backgroundTag
                case .model: modelTag
                case .prs: PRLinkButtons(session: session, onOverflow: showDetail)
                }
            }
        }
    }

    private var machineHelp: String {
        SessionActionHelp.machineHelp(session, lastSeen: store.snapshot.peer(for: session)?.lastSeenAt)
    }

    /// A remote session's Mac as its chip, in the fixed icon column.
    @ViewBuilder
    private var machineIcon: some View {
        if let machine = session.machine {
            MachineChipView(label: store.snapshot.chipLabel(for: machine), offline: session.stale)
                .frame(width: 18, height: 14)
                .help(machineHelp)
                .accessibilityLabel(machineHelp)
        }
    }

    /// A remote session's Mac by name, first on the repo line.
    /// Whole or not at all: a fixedSize tag next to the model tag and three
    /// PR buttons made the row wider than the popover (which centred and
    /// clipped the whole list), and a truncating one could shrink to a bare
    /// "…" capsule. The chip in the icon column still says which Mac.
    @ViewBuilder
    private var machineTag: some View {
        if let machine = session.machine {
            ViewThatFits(in: .horizontal) {
                Text(machine.name)
                    .font(.caption2)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(.secondary.opacity(0.15), in: Capsule())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .fixedSize()
                    .help(machineHelp)
                Color.clear.frame(width: 0, height: 0)
            }
            // Offered space before the wrapping repo text, so the tag only
            // drops out when the row really has no room for it.
            .layoutPriority(1)
        }
    }

    @ViewBuilder
    private var tmuxTag: some View {
        if let tag = session.tmuxTag {
            Label(tag, systemImage: "rectangle.split.3x1")
                .labelStyle(.titleAndIcon)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .fixedSize()
                .help(SessionActionHelp.tmuxTag(session))
        }
    }

    /// A Running row's background tasks (SessionRowLayout.backgroundTaskCount),
    /// folded from its reason line; the tooltip keeps the full reason.
    @ViewBuilder
    private var backgroundTag: some View {
        if let count = SessionRowLayout.backgroundTaskCount(session) {
            Label(SessionRowLayout.backgroundTagText(count), systemImage: "hourglass")
                .labelStyle(.titleAndIcon)
                .font(.caption2)
                .padding(.horizontal, 5)
                .padding(.vertical, 1)
                .background(SessionColor.running.opacity(0.15), in: Capsule())
                .foregroundStyle(SessionColor.runningText)
                .lineLimit(1)
                .fixedSize()
                .help(session.waitingReason ?? "")
                .accessibilityLabel(session.waitingReason ?? "")
        }
    }

    /// Model and context. A long branch wraps (dashboard rows truncate it)
    /// before this does; SessionModelTag caps the model part, so this stays
    /// short.
    @ViewBuilder
    private var modelTag: some View {
        if let tag = SessionModelTag.text(session) {
            Text(tag)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .fixedSize()
                .layoutPriority(1)
                .help(SessionModelTag.help(session))
        }
    }

    /// The line under the title (SessionDisplay.secondaryTitle): the
    /// session's own title under a status text, or on a Needs you row the
    /// status text under the title.
    @ViewBuilder
    private var secondaryTitleLine: some View {
        if let title = SessionDisplay.secondaryTitle(session) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
                .wrapsInPopover(compact)
        }
    }

    /// What the session needs (in plain words, SessionDisplay.reasonText),
    /// as legible as the repo line when it is something to act on.
    @ViewBuilder
    private var reasonLine: some View {
        if let reason = SessionDisplay.reasonText(session) {
            let raw = session.waitingReason?.trimmingCharacters(in: .whitespacesAndNewlines) ?? reason
            // A glyph for the kind (lock, question bubble, warning), so a
            // permission prompt, a question and a failure tell apart at a glance.
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                if let kind = SessionDisplay.reasonKind(session) {
                    Image(systemName: kind.systemImage)
                        .font(.caption2)
                        .accessibilityLabel(kind.label)
                }
                Text(reason)
                    .font(SessionDisplay.reasonIsActionable(session) ? .caption.weight(.medium) : .caption)
                    .wrapsInPopover(compact)
            }
            .foregroundStyle(.secondary)
            .help(raw == reason ? raw : "\(reason) (\(raw))")
        }
    }

    private var interruptedTag: some View {
        Text("Interrupted")
            .font(.caption2)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(SessionColor.interrupted.opacity(0.18), in: Capsule())
            .foregroundStyle(SessionColor.interruptedText)
            .lineLimit(1)
            .fixedSize()
            .help("The session was working when its process died or went quiet for 30 minutes")
    }

    private var autoDoneTag: some View {
        Text("auto-done")
            .font(.caption2)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(.secondary.opacity(0.2), in: Capsule())
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .fixedSize()
            .help("Finished on its own: \(session.doneReason ?? "done")")
    }

    /// A Review row whose process has exited: the amber color, in words
    /// ("exited", so it is never read as a closed PR).
    private var closedTag: some View {
        Text("exited")
            .font(.caption2)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(.secondary.opacity(0.2), in: Capsule())
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .fixedSize()
            .help("Its process has exited; resume it to continue")
    }

    /// The permission mode, as an icon in the fixed column under the tool
    /// badge (SessionRowLayout.iconColumn decides when it shows).
    private func modeIcon(_ mode: String) -> some View {
        Image(systemName: PermissionMode.symbol(mode))
            .font(.system(size: 11))
            // Bypass is the one mode that never asks: tint it.
            .foregroundStyle(mode == "bypassPermissions" ? AnyShapeStyle(SessionColor.interruptedText)
                                                         : AnyShapeStyle(.secondary))
            .frame(width: 18, height: 14)
            .help(PermissionMode.help(mode))
            .accessibilityLabel(PermissionMode.accessibilityLabel(mode))
    }

    /// Opens a Claude desktop app session in the app (`agentboard focus`).
    /// In the popover it takes the row click's path (focus, close the
    /// popover) so the two never drift; a desktop row is always focusable.
    private var desktopButton: some View {
        Button {
            if compact {
                handlePopoverTap()
            } else {
                Task { await store.focusOrShowDashboard(session) }
            }
        } label: {
            Image(systemName: "macwindow")
                .font(.system(size: 11))
                .frame(width: 18, height: 16)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(.secondary)
        .help(SessionActionHelp.desktopIcon)
        .accessibilityLabel(SessionActionHelp.desktopLabel)
        .accessibilityHint(SessionActionHelp.focusDesktop)
    }

    /// What a popover row click (or its desktop icon) does.
    enum PopoverTap: Equatable {
        case focus, showDashboard
    }

    static func popoverTap(for session: Session) -> PopoverTap {
        session.isFocusable ? .focus : .showDashboard
    }

    /// A click on a row that shows `held`: acts on the live session with
    /// the same key (its present state decides focus or dashboard), never on
    /// whatever sits at the row's position; a session no longer on the
    /// board opens the dashboard on its key.
    static func popoverTap(held: Session, live: [Session]) -> (session: Session, tap: PopoverTap) {
        popoverTap(held: held, resolved: PopoverHold.resolve(held, live: live))
    }

    static func popoverTap(held: Session, resolved: Session?) -> (session: Session, tap: PopoverTap) {
        guard let resolved else { return (held, .showDashboard) }
        return (resolved, popoverTap(for: resolved))
    }

    /// Which actions a held row offers: every one while its session is on
    /// the board (on the live value), only copy and show-in-dashboard (on
    /// the held value) once it is gone.
    enum HeldActions: Equatable {
        case all, copyAndDashboard
    }

    static func allowedActions(held: Session, live: [Session]) -> HeldActions {
        PopoverHold.resolve(held, live: live) == nil ? .copyAndDashboard : .all
    }

    /// Row click (and the desktop icon) in the popover: acts on the
    /// session, then closes the popover, whatever section the row is in.
    private func handlePopoverTap() {
        let popoverWindow = NSApp.keyWindow
        let target = Self.popoverTap(held: session, resolved: actionSession)
        switch target.tap {
        case .focus:
            let live = target.session
            Task { await store.focusOrShowDashboard(live) }
        case .showDashboard:
            store.requestSelection(target.session.key)
            DashboardWindowController.show()
        }
        popoverWindow?.close()
    }

    /// The "+N" PR button: the detail view lists every link.
    private func showDetail() {
        showInDashboard(note: nil)
    }

    /// Selects this session in the dashboard and, with `note`, scrolls its
    /// detail to that note and outlines it. From the popover it also shows
    /// the dashboard and closes the popover; a dashboard row only changes
    /// the selection (the dashboard keeps its filters while the row is
    /// listed, `PendingSelection.clearsFilters`).
    private func showInDashboard(note: Int?) {
        store.requestSelection(session.key, note: note)
        if compact {
            let popoverWindow = NSApp.keyWindow
            DashboardWindowController.show()
            popoverWindow?.close()
        }
    }

    /// Open notes on this session; red when one of them needs the user. A
    /// button of its own, so a click opens the dashboard on the session's
    /// updates (the needs_you one, else the newest, `SessionNoteTarget`)
    /// and never runs the row's own click (focus the terminal).
    private var notesBadge: some View {
        let urgent = session.notes.contains { $0.kind == .needsYou }
        let color: Color = urgent ? SessionColor.needsYouText : .secondary
        let count = session.notesCount
        return Button {
            showInDashboard(note: SessionNoteTarget.badgeTarget(for: session))
        } label: {
            Label("\(count)", systemImage: "text.bubble")
                .labelStyle(.titleAndIcon)
                .font(.caption2)
                .padding(.horizontal, 5)
                .padding(.vertical, 1)
                .background(color.opacity(0.15), in: Capsule())
                .foregroundStyle(color)
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .lineLimit(1)
        .fixedSize()
        .help(SessionNoteTarget.badgeHelp(count: count, inDashboard: !compact))
        .accessibilityLabel(SessionNoteTarget.badgeAccessibilityLabel(count: count))
        .accessibilityHint(SessionNoteTarget.badgeAccessibilityHint(count: count, inDashboard: !compact))
    }

    private var toolBadge: some View {
        Text(session.toolBadge)
            .font(.caption.monospaced().bold())
            .frame(width: 18, height: 18)
            .background(session.tool == "codex" ? Color.purple.opacity(0.25) : Color.blue.opacity(0.25))
            .clipShape(Circle())
            .help(Self.toolHelp(session))
    }

    static func toolHelp(_ session: Session) -> String {
        session.tool == "codex" ? "A Codex CLI session" : "A Claude Code session"
    }

    /// The buttons SessionRowLayout.hoverButtons lists: popover rows copy
    /// and mark done (focus is the row's own click); dashboard rows every
    /// action except focus (double-click the row, or the context menu).
    private var hoverButtons: some View {
        HStack(spacing: SessionRowLayout.hoverIconSpacing) {
            ForEach(SessionRowLayout.hoverButtons(for: session, compact: compact), id: \.self) { button in
                hoverButton(button)
            }
        }
        .buttonStyle(.plain)
        .foregroundStyle(.secondary)
    }

    @ViewBuilder
    private func hoverButton(_ button: SessionRowLayout.HoverButton) -> some View {
        // State-changing actions use the live session; a session no longer
        // on the board keeps its buttons in place, disabled.
        let live = actionSession
        switch button {
        case .resume:
            iconButton(button.systemImage, help: live.map(SessionActionHelp.resumeHelp) ?? SessionActionHelp.noLongerOnBoard) {
                if let live { Task { await store.resume(live) } }
            }
            .disabled(live == nil)
        case .copy:
            iconButton(button.systemImage, help: SessionActionHelp.copyResumeHelp(session)) {
                Task {
                    let command = await store.resumeCommand(for: session)
                    ClipboardHelper.copy(command)
                }
            }
        case .markDone:
            iconButton(button.systemImage, help: live == nil ? SessionActionHelp.noLongerOnBoard : SessionActionHelp.markDone) {
                if let live { Task { await store.markDone(live) } }
            }
            .disabled(live == nil)
        case .snooze:
            iconButton(button.systemImage, help: live == nil ? SessionActionHelp.noLongerOnBoard : SessionActionHelp.snooze) {
                if let live { Task { await store.snooze(live, hours: 24) } }
            }
            .disabled(live == nil)
        case .openLink:
            // A ticket or URL; PRs have their own buttons on the row.
            if let link = SessionRowLayout.hoverLink(for: session), let url = link.openURL {
                iconButton(button.systemImage, help: SessionActionHelp.open(link)) {
                    NSWorkspace.shared.open(url)
                }
            }
        }
    }

    /// One hover button, framed at SessionRowLayout.hoverIconWidth by
    /// hoverIconHeight so the buttons fill exactly the slot the row reserves
    /// for them. Nothing between the row and here sets `.font`, `.imageScale`
    /// or `.controlSize`; HoverSlotTests measures the glyphs at the default
    /// font on that basis, so styling the row means re-measuring them.
    private func iconButton(_ systemImage: String, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .frame(width: SessionRowLayout.hoverIconWidth, height: SessionRowLayout.hoverIconHeight)
                .contentShape(Rectangle())
        }
        .help(help)
    }
}

private extension SessionRowLayout.Icon {
    /// A stable identity for `ForEach`: each icon appears at most once.
    var columnID: String {
        switch self {
        case .tool: return "tool"
        case .machine: return "machine"
        case .mode: return "mode"
        case .desktop: return "desktop"
        }
    }
}

extension View {
    /// `.help(text)` when there is one; nothing otherwise, so a row's
    /// controls keep their own tooltips.
    @ViewBuilder
    func optionalHelp(_ text: String?) -> some View {
        if let text { help(text) } else { self }
    }

    @ViewBuilder
    func optionalAccessibilityValue(_ text: String?) -> some View {
        if let text { accessibilityValue(text) } else { self }
    }
}

private extension View {
    /// Popover rows wrap a long line onto more lines instead of truncating
    /// it, at whatever width the popover has; dashboard rows stay one line.
    @ViewBuilder
    func wrapsInPopover(_ compact: Bool) -> some View {
        if compact {
            lineLimit(nil).fixedSize(horizontal: false, vertical: true)
        } else {
            lineLimit(1)
        }
    }
}
