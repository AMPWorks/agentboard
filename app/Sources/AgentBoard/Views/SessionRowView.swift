import SwiftUI
import AppKit

struct SessionRowView: View {
    let session: Session
    @ObservedObject var store: SnapshotStore
    var compact: Bool = true

    @State private var isHovering = false

    var body: some View {
        let decorated = rowContent
            .padding(.vertical, 4)
            .padding(.horizontal, 6)
            .contentShape(Rectangle())
            .onHover { isHovering = $0 }

        // Popover rows aren't in a List, so they drive their own click and
        // context menu. Dashboard rows are List rows: selection, the
        // context menu, and double-click/Return are all handled at the
        // List level in DashboardView so native single-click selection
        // isn't fought over by a competing gesture recognizer.
        Group {
            if compact {
                decorated
                    .onTapGesture { handlePopoverTap() }
                    .contextMenu { SessionContextMenu(session: session, store: store) }
            } else {
                decorated
            }
        }
    }

    private var rowContent: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(SessionColor.color(for: session))
                .frame(width: 7, height: 7)
                .padding(.top, 5)
                .help(SessionColor.legendTooltip)
                .accessibilityLabel(SessionColor.label(for: session))
            toolBadge
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(session.displayTitle)
                        .font(compact ? .body : .headline)
                        .foregroundStyle(SessionColor.color(for: session))
                        .lineLimit(1)
                    if session.state == .interrupted {
                        Text("Interrupted")
                            .font(.caption2)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(.orange.opacity(0.2), in: Capsule())
                            .foregroundStyle(.orange)
                            .help("The session was working when its process died or went quiet for 30 minutes")
                    }
                    if session.notesCount > 0 {
                        notesBadge
                    }
                    if session.isAutoDone {
                        Text("auto")
                            .font(.caption2)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(.secondary.opacity(0.2), in: Capsule())
                            .foregroundStyle(.secondary)
                            .help("Finished on its own: \(session.doneReason ?? "done")")
                    }
                    // The permission mode, when it is not the default and
                    // the session has not ended or been done.
                    if let modeTag = PermissionMode.rowTag(for: session) {
                        Text(modeTag)
                            .font(.caption2)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(.secondary.opacity(0.2), in: Capsule())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .help(PermissionMode.help(session.permissionMode))
                    }
                    Spacer()
                    Text(RelativeTime.string(from: session.updatedAt))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .help("Last activity")
                }
                // A status text is the primary line; the session's own
                // title then comes second (never the status twice).
                if let title = SessionDisplay.secondaryTitle(session) {
                    Text(title)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                HStack(spacing: 6) {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    PRLinkButtons(session: session, onOverflow: showDetail)
                    // Model and context: always the rightmost item. The branch
                    // truncates before it does; SessionModelTag caps the model
                    // part, so this stays short.
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
                if let reason = session.waitingReason, !reason.isEmpty {
                    Text(reason)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .help(reason)
                }
            }
            if isHovering {
                hoverButtons
            }
        }
    }

    private func handlePopoverTap() {
        let popoverWindow = NSApp.keyWindow
        if session.isFocusable {
            Task { await store.focusOrShowDashboard(session) }
        } else {
            store.requestSelection(session.key)
            DashboardWindowController.show()
        }
        popoverWindow?.close()
    }

    /// The "+N" PR button: the detail view lists every link.
    private func showDetail() {
        store.requestSelection(session.key)
        if compact {
            let popoverWindow = NSApp.keyWindow
            DashboardWindowController.show()
            popoverWindow?.close()
        }
    }

    private var subtitle: String {
        if let branch = session.branch, !branch.isEmpty {
            return "\(session.project) · \(branch)"
        }
        return session.project
    }

    /// Open notes on this session; red when one of them needs the user.
    private var notesBadge: some View {
        let urgent = session.notes.contains { $0.kind == .needsYou }
        let color: Color = urgent ? SessionColor.needsYou : .secondary
        return Label("\(session.notesCount)", systemImage: "text.bubble")
            .labelStyle(.titleAndIcon)
            .font(.caption2)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
            .help(session.notesCount == 1 ? "1 open update" : "\(session.notesCount) open updates")
            .accessibilityLabel(session.notesCount == 1 ? "1 open update" : "\(session.notesCount) open updates")
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

    @ViewBuilder
    private var hoverButtons: some View {
        HStack(spacing: 4) {
            if compact {
                compactButtons
            } else {
                fullButtons
            }
        }
        .buttonStyle(.plain)
        .foregroundStyle(.secondary)
    }

    /// Popover rows: copy and mark done. Focus/resume happen via the row's
    /// own click/double-click, not a dedicated button.
    @ViewBuilder
    private var compactButtons: some View {
        copyButton
        markDoneButton
    }

    /// Dashboard rows: every action except focus, which is reached by
    /// double-clicking the row (or the context menu / detail pane).
    @ViewBuilder
    private var fullButtons: some View {
        if session.resumeAvailable {
            iconButton("arrow.counterclockwise.circle", help: SessionActionHelp.resume) { Task { await store.resume(session) } }
        }
        copyButton
        markDoneButton
        iconButton("moon.zzz", help: SessionActionHelp.snooze) { Task { await store.snooze(session, hours: 24) } }
        // PRs have their own buttons on the row; this opens the first
        // other link (a ticket, a URL).
        if let firstLink = session.links.first(where: { !$0.isPR && $0.openURL != nil }),
           let url = firstLink.openURL {
            iconButton("arrow.up.right.square", help: SessionActionHelp.open(firstLink)) {
                NSWorkspace.shared.open(url)
            }
        }
    }

    private var copyButton: some View {
        iconButton("doc.on.doc", help: SessionActionHelp.copyResume) {
            Task {
                let command = await store.resumeCommand(for: session)
                ClipboardHelper.copy(command)
            }
        }
    }

    private var markDoneButton: some View {
        iconButton("checkmark.circle", help: SessionActionHelp.markDone) { Task { await store.markDone(session) } }
    }

    private func iconButton(_ systemImage: String, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemImage)
        }
        .help(help)
    }
}
