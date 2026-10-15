import SwiftUI
import AppKit

/// Shared context menu content for a session, used both by the popover's
/// per-row `.contextMenu` and the dashboard list's
/// `.contextMenu(forSelectionType:)`.
struct SessionContextMenu: View {
    let session: Session
    @ObservedObject var store: SnapshotStore

    var body: some View {
        Group {
            if session.isFocusable {
                Button("Focus") { Task { await store.focus(session) } }
            }
            if session.resumeAvailable {
                Button("Resume") { Task { await store.resume(session) } }
            }
            Button("Copy Resume Command") {
                Task {
                    let command = await store.resumeCommand(for: session)
                    ClipboardHelper.copy(command)
                }
            }
            Divider()
            if session.state == .done {
                Button("Reopen") { Task { await store.markOpen(session) } }
            } else {
                Button("Mark Done") { Task { await store.markDone(session) } }
            }
            Button("Snooze 1 Day") { Task { await store.snooze(session, hours: 24) } }
            if !session.links.isEmpty {
                Divider()
                ForEach(session.links) { link in
                    if let urlString = link.url, let url = URL(string: urlString) {
                        Button("Open \(link.kindLabel) \(link.ref)") {
                            NSWorkspace.shared.open(url)
                        }
                    }
                }
            }
        }
    }
}
