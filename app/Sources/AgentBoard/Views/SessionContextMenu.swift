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
    /// session is focused through tmux. For another Mac's session it says
    /// what a click does in the CLI's order (`focus --machine`): an open
    /// Screen Sharing connection first, then the desktop app or browser.
    static func focusHelp(_ session: Session,
                          desktopAppInstalled: Bool = ClaudeDesktopApp.isInstalled) -> String {
        if let machine = session.machine {
            switch session.remoteOpen {
            case "tmux": return "Open a new terminal window attached over ssh to this session's tmux pane on \(machine.name)"
            case "remote-control":
                let target = desktopAppInstalled ? "the Claude desktop app" : "the browser"
                return "Brings this session forward on \(machine.name) when a Screen Sharing connection with it is open; otherwise opens this session's Remote Control link in \(target)"
            default: return "Bring this session forward on \(machine.name); opens Screen Sharing to it unless a connection with \(machine.name) is already open"
            }
        }
        if session.desktop { return focusDesktop }
        return session.tmux ? focusTmux : focus
    }

    /// The Focus action's title: a desktop app session has no terminal.
    static func focusTitle(_ session: Session,
                           desktopAppInstalled: Bool = ClaudeDesktopApp.isInstalled) -> String {
        if let machine = session.machine {
            switch session.remoteOpen {
            case "tmux": return "Attach on \(machine.name)"
            case "remote-control": return desktopAppInstalled ? "Open in Claude App" : "Open in Remote Control"
            default: return "Screen Share to \(machine.name)"
            }
        }
        return session.desktop ? "Open in Claude App" : "Focus"
    }

    /// The explicit Screen Sharing item on a live remote row outside tmux
    /// (`focus --via screen`), and the browser item on a Remote Control row
    /// (`focus --via browser`); nil when the row has none.
    static func screenShareTitle(_ session: Session) -> String? {
        guard let machine = session.machine, session.isLiveRemote,
              session.remoteOpen == "screen" || session.remoteOpen == "remote-control" else { return nil }
        return "Open Screen Sharing to \(machine.name)"
    }

    static func screenShareHelp(_ session: Session) -> String {
        "Open Screen Sharing to \(session.machine?.name ?? "its Mac") even when a connection is already open, after asking it to bring this session forward"
    }

    static func browserTitle(_ session: Session) -> String? {
        guard session.isLiveRemote, session.remoteOpen == "remote-control" else { return nil }
        return "Open Remote Control in Browser"
    }

    static let browserHelp = "Open this session's Remote Control link in the browser, even when a Screen Sharing connection is open or the Claude desktop app is installed"

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
    /// A held popover row whose session left the board meanwhile.
    static let noLongerOnBoard = "This session is no longer on the board; Update list in the footer shows the newest sessions"
    static let showInDashboard = "Open the dashboard on this session"

    static func open(_ link: Link) -> String { "Open \(link.kindLabel) \(link.ref) in the browser" }
}

/// Shared context menu content for a session, used both by the popover's
/// per-row `.contextMenu` and the dashboard list's
/// `.contextMenu(forSelectionType:)`.
struct SessionContextMenu: View {
    let session: Session
    @ObservedObject var store: SnapshotStore
    /// A held popover row whose session left the board: only copy and
    /// Show in Dashboard; the rest are disabled.
    var noLongerOnBoard: Bool = false

    var body: some View {
        Group {
            if noLongerOnBoard {
                goneMenu
            } else {
                liveMenu
            }
        }
    }

    @ViewBuilder
    private var goneMenu: some View {
        Button("Show in Dashboard") {
            let popoverWindow = NSApp.keyWindow
            store.requestSelection(session.key)
            DashboardWindowController.show()
            popoverWindow?.close()
        }
        .help(SessionActionHelp.showInDashboard)
        Button("Copy Resume Command") {
            Task {
                let command = await store.resumeCommand(for: session)
                ClipboardHelper.copy(command)
            }
        }
        .help(SessionActionHelp.copyResumeHelp(session))
        Divider()
        Button("Mark Done") {}
            .disabled(true)
            .help(SessionActionHelp.noLongerOnBoard)
        Button("Snooze 1 Day") {}
            .disabled(true)
            .help(SessionActionHelp.noLongerOnBoard)
    }

    @ViewBuilder
    private var liveMenu: some View {
        Group {
            if session.isFocusable {
                Button(SessionActionHelp.focusTitle(session)) { Task { await store.focusOrShowDashboard(session) } }
                    .help(SessionActionHelp.focusHelp(session))
                if let title = SessionActionHelp.screenShareTitle(session) {
                    Button(title) { Task { await store.focusOrShowDashboard(session, via: "screen") } }
                        .help(SessionActionHelp.screenShareHelp(session))
                }
                if let title = SessionActionHelp.browserTitle(session) {
                    Button(title) { Task { await store.focusOrShowDashboard(session, via: "browser") } }
                        .help(SessionActionHelp.browserHelp)
                }
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
