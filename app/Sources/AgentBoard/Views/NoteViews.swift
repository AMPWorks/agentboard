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

/// The popover's Updates section: the flattened, filtered feed of open notes.
struct UpdatesSectionView: View {
    let items: [UpdatesFeed.Item]
    /// Open notes the query keeps, uncapped (`UpdatesFeed.total`).
    let total: Int
    @ObservedObject var store: SnapshotStore

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(UpdatesFeed.header(for: items, total: total))
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Clear all") {
                    let notes = items.map(\.note)
                    Task { await store.dismissNotes(notes) }
                }
                .buttonStyle(.plain)
                .font(.caption)
                .foregroundStyle(.secondary)
                .help("Dismiss every update listed here")
            }
            .padding(.horizontal, 10)
            .padding(.top, 4)
            ForEach(items) { item in
                UpdateRowView(item: item, store: store)
                    .padding(.horizontal, 4)
            }
        }
    }
}

/// One note in the popover feed. A click focuses the owning session the way
/// a session row does; the chevron expands the full text; the x dismisses.
struct UpdateRowView: View {
    let item: UpdatesFeed.Item
    @ObservedObject var store: SnapshotStore

    @State private var expanded = false
    @State private var isHovering = false

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            NoteStyle.glyph(item.note.kind)
                .font(.caption)
                .frame(width: 14)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(item.toolBadge)
                        .font(.caption2.monospaced().bold())
                        .frame(width: 14, height: 14)
                        .background(item.session.tool == "codex" ? Color.purple.opacity(0.25) : Color.blue.opacity(0.25))
                        .clipShape(Circle())
                    Text(item.project.isEmpty ? item.session.title : item.project)
                        .font(.caption.bold())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    if let count = NoteStyle.countLabel(item.note) {
                        Text(count)
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .help("Seen \(item.note.count) times")
                    }
                    Spacer(minLength: 4)
                    Text(RelativeTime.string(from: item.note.lastTs))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if expanded {
                    NoteStyle.fullText(item.note.text)
                        .font(.caption)
                } else {
                    Text(item.note.firstLine)
                        .font(.caption)
                        .lineLimit(2)
                }
            }
            VStack(spacing: 4) {
                Button {
                    Task { await store.dismissNote(item.note, of: item.session) }
                } label: {
                    Image(systemName: "xmark.circle")
                }
                .help("Dismiss")
                .accessibilityLabel("Dismiss update")
                .opacity(isHovering ? 1 : 0.35)
                if item.note.hasMore {
                    Button {
                        expanded.toggle()
                    } label: {
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                    }
                    .help(expanded ? "Show less" : "Show the full text")
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
        .onTapGesture { focusSession() }
        .contextMenu { SessionContextMenu(session: item.session, store: store) }
    }

    /// Same as a popover session row: focus the terminal when there is one,
    /// otherwise open the dashboard on the session; then close the popover.
    private func focusSession() {
        let popoverWindow = NSApp.keyWindow
        let session = item.session
        if session.isFocusable {
            Task { await store.focus(session) }
        } else {
            store.requestSelection(session.key)
            DashboardWindowController.show()
        }
        popoverWindow?.close()
    }
}

/// The session detail's Updates section: every open note with its full text.
struct SessionNotesSection: View {
    let session: Session
    @ObservedObject var store: SnapshotStore

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
            }
        }
    }
}
