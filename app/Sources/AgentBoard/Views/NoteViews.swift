import SwiftUI
import AppKit

/// Glyph and color for a note's kind, shared by the popover feed, the row
/// badge and the session detail.
enum NoteStyle {
    static func symbol(for kind: Note.Kind) -> String {
        switch kind {
        case .needsYou: return "exclamationmark.bubble.fill"
        case .error: return "exclamationmark.triangle.fill"
        case .update: return "info.circle"
        case .unknown: return "circle"
        }
    }

    static func color(for kind: Note.Kind) -> Color {
        switch kind {
        case .needsYou: return SessionColor.needsYou
        case .error: return .red
        case .update, .unknown: return .secondary
        }
    }

    static func label(for kind: Note.Kind) -> String {
        switch kind {
        case .needsYou: return "Needs you"
        case .error: return "Error"
        case .update: return "Update"
        case .unknown: return "Note"
        }
    }

    static func glyph(_ kind: Note.Kind) -> some View {
        Image(systemName: symbol(for: kind))
            .foregroundStyle(color(for: kind))
            .help(label(for: kind))
            .accessibilityLabel(label(for: kind))
    }

    /// The note's full text, rendered as inline markdown like the detail
    /// view's last assistant message.
    static func fullText(_ text: String) -> some View {
        Group {
            if let attributed = try? AttributedString(
                markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
            ) {
                Text(attributed)
            } else {
                Text(text)
            }
        }
        .textSelection(.enabled)
        .fixedSize(horizontal: false, vertical: true)
    }

    static func countLabel(_ note: Note) -> String? {
        note.count > 1 ? "×\(note.count)" : nil
    }
}

/// The popover's Updates section: the filtered feed of open notes as rows
/// (`UpdatesFeed.rows`), compact by default so it never pushes the session
/// list out of view: every needs_you row, and at most
/// `UpdatesFeed.compactLimit` others until "Show all N" expands it.
struct UpdatesSectionView: View {
    let rows: [UpdatesFeed.Row]
    /// Listed notes the rows stand for (the header's "N of M").
    let listed: Int
    /// Open notes the query keeps, uncapped (`UpdatesFeed.total`).
    let total: Int
    let clearPlan: UpdatesFeed.ClearPlan
    @ObservedObject var store: SnapshotStore

    @State private var expanded = false

    var body: some View {
        let compact = UpdatesFeed.compact(rows, expanded: expanded)
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(UpdatesFeed.header(listed: listed, total: total))
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                Spacer()
                Button(clearPlan.title) {
                    let plan = clearPlan
                    Task { await store.dismiss(plan.dismissals, action: plan.title.lowercased()) }
                }
                .buttonStyle(.plain)
                .font(.caption)
                .foregroundStyle(.secondary)
                .help(clearPlan.help)
            }
            .padding(.horizontal, 10)
            .padding(.top, 4)
            ForEach(compact.shown) { row in
                UpdateRowView(row: row, store: store)
                    .padding(.horizontal, 4)
            }
            if UpdatesFeed.needsToggle(rows) {
                Button(expanded ? "Show fewer" : "Show all \(rows.count)") {
                    expanded.toggle()
                }
                .buttonStyle(.plain)
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 10)
                .padding(.vertical, 2)
                .help(expanded ? "Show at most \(UpdatesFeed.compactLimit) updates" : "List every update here")
            }
        }
    }
}

/// One row of the popover feed: a single note, or a session's grouped
/// update/error notes. A click opens the dashboard on the owning session,
/// scrolled to the note's full text (a group: its newest note; a line of an
/// expanded group: that note); the chevron expands the full text in place
/// (a group: each of its notes); the x dismisses the row (a group: all of
/// the notes it stands for). The context menu still offers Focus.
struct UpdateRowView: View {
    let row: UpdatesFeed.Row
    @ObservedObject var store: SnapshotStore

    @State private var expanded = false
    @State private var isHovering = false

    private var canExpand: Bool { row.isGroup || row.note.hasMore }

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            NoteStyle.glyph(row.glyphKind)
                .font(.caption)
                .frame(width: 14)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(row.toolBadge)
                        .font(.caption2.monospaced().bold())
                        .frame(width: 14, height: 14)
                        .background(row.session.tool == "codex" ? Color.purple.opacity(0.25) : Color.blue.opacity(0.25))
                        .clipShape(Circle())
                    Text(row.project.isEmpty ? row.session.title : row.project)
                        .font(.caption.bold())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    if let group = row.groupLabel {
                        Text(group)
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .help("\(row.total) open updates from this session")
                    } else if let count = NoteStyle.countLabel(row.note) {
                        Text(count)
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .help("Seen \(row.note.count) times")
                    }
                    Spacer(minLength: 4)
                    Text(RelativeTime.string(from: row.note.lastTs))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if expanded && row.isGroup {
                    ForEach(row.notes) { note in
                        GroupedNoteLine(note: note, session: row.session, store: store) {
                            openInDashboard(note)
                        }
                    }
                    if row.total > row.notes.count {
                        Text("\(row.total - row.notes.count) older not listed")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                } else if expanded {
                    NoteStyle.fullText(row.note.text)
                        .font(.caption)
                } else {
                    Text(row.note.firstLine)
                        .font(.caption)
                        .lineLimit(2)
                }
            }
            VStack(spacing: 4) {
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark.circle")
                }
                .help(row.isGroup ? "Dismiss all \(row.total) updates from this session" : "Dismiss")
                .accessibilityLabel(row.isGroup ? "Dismiss this session's updates" : "Dismiss update")
                .opacity(isHovering ? 1 : 0.35)
                if canExpand {
                    Button {
                        expanded.toggle()
                    } label: {
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                    }
                    .help(expanded ? "Show less" : (row.isGroup ? "List this session's updates" : "Show the full text"))
                    .accessibilityLabel(expanded ? "Collapse update" : "Expand update")
                }
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .font(.caption)
        }
        .padding(.vertical, 4)
        .padding(.horizontal, 6)
        .contentShape(Rectangle())
        .onHover { isHovering = $0 }
        .onTapGesture { openInDashboard(row.note) }
        .help("Open in the dashboard")
        .contextMenu { SessionContextMenu(session: row.session, store: store) }
    }

    private func dismiss() {
        if row.isGroup {
            let dismissal = row.dismissal
            Task { await store.dismiss([dismissal], action: "dismiss grouped notes") }
        } else {
            Task { await store.dismissNote(row.note, of: row.session) }
        }
    }

    /// Opens the dashboard on the owning session, scrolled to `note` with
    /// its full text highlighted, then closes the popover.
    private func openInDashboard(_ note: Note) {
        let popoverWindow = NSApp.keyWindow
        store.requestSelection(row.session.key, note: note.id)
        DashboardWindowController.show()
        popoverWindow?.close()
    }
}

/// One note inside an expanded group row: its first line, time and its own
/// x; a click opens it in the dashboard.
private struct GroupedNoteLine: View {
    let note: Note
    let session: Session
    @ObservedObject var store: SnapshotStore
    let open: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Text(note.firstLine)
                .font(.caption)
                .lineLimit(2)
            Spacer(minLength: 4)
            Text(RelativeTime.string(from: note.lastTs))
                .font(.caption2)
                .foregroundStyle(.secondary)
            Button {
                Task { await store.dismissNote(note, of: session) }
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .font(.caption2)
            .help("Dismiss this update")
            .accessibilityLabel("Dismiss update")
        }
        .padding(.top, 2)
        .contentShape(Rectangle())
        .onTapGesture { open() }
    }
}

/// A popover update row's request to show one note in the dashboard.
struct NoteFocus: Equatable, Sendable {
    /// Scroll id of the session detail's Updates header.
    static let sectionID = "updates-section"

    let sessionKey: String
    let noteId: Int

    static func scrollID(for noteId: Int) -> String { "note-\(noteId)" }

    /// Where `session`'s detail should scroll for this request: the note
    /// when it is listed, else the Updates header (the note is past the
    /// 20-note list or was dismissed meanwhile); nil when the request is for
    /// another session or the session has no listed notes.
    func scrollTarget(in session: Session) -> String? {
        guard sessionKey == session.key, !session.notes.isEmpty else { return nil }
        return session.notes.contains { $0.id == noteId } ? Self.scrollID(for: noteId) : Self.sectionID
    }
}

/// The session detail's Updates section: every open note with its full
/// text; `highlighted` marks the note a popover click opened.
struct SessionNotesSection: View {
    let session: Session
    @ObservedObject var store: SnapshotStore
    var highlighted: Int? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(UpdatesFeed.header(listed: session.notes.count, total: session.notesCount))
                    .font(.headline)
                Spacer()
                if session.notes.count > 1 {
                    Button("Dismiss All") { Task { await store.dismissNotes(for: session) } }
                        .controlSize(.small)
                }
            }
            .id(NoteFocus.sectionID)
            ForEach(session.notes) { note in
                HStack(alignment: .top, spacing: 8) {
                    NoteStyle.glyph(note.kind)
                        .padding(.top, 2)
                    VStack(alignment: .leading, spacing: 4) {
                        NoteStyle.fullText(note.text)
                            .font(.callout)
                        HStack(spacing: 6) {
                            Text(RelativeTime.string(from: note.lastTs))
                            if let count = NoteStyle.countLabel(note) {
                                Text(count).help("Seen \(note.count) times, first \(RelativeTime.string(from: note.ts))")
                            }
                            Text(note.source)
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 8)
                    Button("Dismiss") { Task { await store.dismissNote(note, of: session) } }
                        .controlSize(.small)
                }
                .padding(8)
                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
                .overlay {
                    if note.id == highlighted {
                        RoundedRectangle(cornerRadius: 6).strokeBorder(Color.accentColor, lineWidth: 2)
                    }
                }
                .id(NoteFocus.scrollID(for: note.id))
            }
        }
    }
}
