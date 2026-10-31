import SwiftUI
import AppKit

/// Tooltips for the session actions, shared by the context menu, the
/// detail view's action bar and the row hover buttons, so each action is
/// described the same way everywhere.
enum SessionActionHelp {
    static let focus = "Bring this session's terminal tab to the front, switching to its desktop"
    static let resume = "Open a new terminal tab that resumes this session"
    static let copyResume = "Copy the command that resumes this session"
    static let markDone = "Mark this session done and hide it; a new prompt reopens it"
    static let reopen = "Put this session back on the board"
    static let snooze = "Hide this session for 24 hours"

    static func open(_ link: Link) -> String { "Open \(link.kindLabel) \(link.ref) in the browser" }
}

/// Shared context menu content for a session, used both by the popover's
/// per-row `.contextMenu` and the dashboard list's
/// `.contextMenu(forSelectionType:)`.
struct SessionContextMenu: View {
    let session: Session
    @ObservedObject var store: SnapshotStore

    var body: some View {
        Group {
            if session.isFocusable {
                Button("Focus") { Task { await store.focusOrShowDashboard(session) } }
                    .help(SessionActionHelp.focus)
            }
            if session.resumeAvailable {
                Button("Resume") { Task { await store.resume(session) } }
                    .help(SessionActionHelp.resume)
            }
            Button("Copy Resume Command") {
                Task {
                    let command = await store.resumeCommand(for: session)
                    ClipboardHelper.copy(command)
                }
            }
            .help(SessionActionHelp.copyResume)
            Divider()
            if session.state == .done {
                Button("Reopen") { Task { await store.markOpen(session) } }
                    .help(SessionActionHelp.reopen)
            } else {
                Button("Mark Done") { Task { await store.markDone(session) } }
                    .help(SessionActionHelp.markDone)
            }
            Button("Snooze 1 Day") { Task { await store.snooze(session, hours: 24) } }
                .help(SessionActionHelp.snooze)
            if !session.links.isEmpty {
                Divider()
                ForEach(session.links) { link in
                    if let urlString = link.url, let url = URL(string: urlString) {
                        Button("Open \(link.kindLabel) \(link.ref)") {
                            NSWorkspace.shared.open(url)
                        }
                        .help(SessionActionHelp.open(link))
                    }
                }
            }
        }
    }
}
