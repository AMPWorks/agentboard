import SwiftUI
import AppKit

/// Tooltips for the session actions, shared by the context menu, the
/// detail view's action bar and the row hover buttons, so each action is
/// described the same way everywhere.
enum SessionActionHelp {
    static let focus = "Bring this session's terminal tab to the front, switching to its desktop"
    static let focusTmux = "Bring the window attached to this session's tmux session to the front, or open a new iTerm window attached to it"
    static let focusDesktop = "Open this session in the Claude desktop app"
    /// The desktop-app icon on a row (and the detail view's App row).
    static let desktopIcon = "Claude desktop app session; click to open it there"
    static let desktopLabel = "Claude desktop app session"

    /// The Focus tooltip: a desktop app session opens in the app, a tmux
    /// session is focused through tmux.
    static func focusHelp(_ session: Session) -> String {
        if let machine = session.machine {
            switch session.remoteOpen {
            case "tmux": return "Open a new terminal window attached over ssh to this session's tmux pane on \(machine.name)"
            case "remote-control": return "Open this session through Claude Code Remote Control"
            default: return "Bring this session forward on \(machine.name) and open Screen Sharing to it"
            }
        }
        if session.desktop { return focusDesktop }
        return session.tmux ? focusTmux : focus
    }

    /// The Focus action's title: a desktop app session has no terminal.
    static func focusTitle(_ session: Session) -> String {
        if let machine = session.machine {
            switch session.remoteOpen {
            case "tmux": return "Attach on \(machine.name)"
            case "remote-control": return "Open in Remote Control"
            default: return "Screen Share to \(machine.name)"
            }
        }
        return session.desktop ? "Open in Claude App" : "Focus"
    }

    static func resumeTitle(_ session: Session) -> String {
        session.machine.map { "Resume on \($0.name)" } ?? "Resume"
    }

    static func resumeHelp(_ session: Session) -> String {
        guard let machine = session.machine else { return resume }
        return "Open a new terminal window that resumes this session on \(machine.name) over ssh"
    }

    static func copyResumeHelp(_ session: Session) -> String {
        session.isRemote ? "Copy the command that opens this session on its Mac from a terminal here" : copyResume
    }

    /// The machine badge's tooltip: the Mac's name, and since when its
    /// state is old when it is offline.
    static func machineHelp(_ session: Session, lastSeen: String?, now: Date = Date()) -> String {
        guard let machine = session.machine else { return "" }
        guard session.stale else { return "Runs on \(machine.name)" }
        guard let lastSeen, !lastSeen.isEmpty else { return "Runs on \(machine.name), which is offline" }
        return "Runs on \(machine.name), offline; as of \(RelativeTime.string(from: lastSeen, relativeTo: now))"
    }

    static func tmuxTag(_ session: Session) -> String {
        if let name = session.tmuxSession, !name.isEmpty {
            return "Runs in tmux session \(name); click to focus or attach"
        }
        return "Runs in tmux; click to focus or attach"
    }

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
                Button(SessionActionHelp.focusTitle(session)) { Task { await store.focusOrShowDashboard(session) } }
                    .help(SessionActionHelp.focusHelp(session))
            }
            if session.resumeAvailable {
                Button(SessionActionHelp.resumeTitle(session)) { Task { await store.resume(session) } }
                    .help(SessionActionHelp.resumeHelp(session))
            }
            Button("Copy Resume Command") {
                Task {
                    let command = await store.resumeCommand(for: session)
                    ClipboardHelper.copy(command)
                }
            }
            .help(SessionActionHelp.copyResumeHelp(session))
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
