import SwiftUI
import AppKit

struct SessionDetailView: View {
    let session: Session
    @ObservedObject var store: SnapshotStore

    /// The note a popover update click opened, outlined until another
    /// session is shown.
    @State private var highlightedNote: Int?

    var body: some View {
        ScrollViewReader { proxy in
            content
                .onAppear { consumeNoteFocus(proxy) }
                .onChange(of: store.pendingNote) { _, _ in consumeNoteFocus(proxy) }
                .onChange(of: session.key) { _, _ in
                    highlightedNote = nil
                    consumeNoteFocus(proxy)
                }
        }
    }

    /// Scrolls to the note a popover update row asked for, once this
    /// detail shows that note's session, and clears the request either way,
    /// so a session with nothing to scroll to never keeps it for a later
    /// manual selection.
    private func consumeNoteFocus(_ proxy: ScrollViewProxy) {
        guard let focus = store.pendingNote, focus.sessionKey == session.key else { return }
        store.pendingNote = nil
        guard let target = focus.scrollTarget(in: session) else { return }
        highlightedNote = focus.noteId
        // Let the detail lay out for the new session before scrolling, and
        // scroll again once a newly opened dashboard window has laid out
        // (the first attempt can land before it has; repeating is a no-op).
        Task { @MainActor in
            withAnimation { proxy.scrollTo(target, anchor: .top) }
            try? await Task.sleep(for: .milliseconds(300))
            withAnimation { proxy.scrollTo(target, anchor: .top) }
        }
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                metadata
                if !session.links.isEmpty {
                    linksSection
                }
                textSection(title: "First prompt", text: session.firstPrompt)
                textSection(title: "Last prompt", text: session.lastPrompt)
                if !session.notes.isEmpty {
                    SessionNotesSection(session: session, store: store, highlighted: highlightedNote)
                }
                if !session.denials.isEmpty {
                    SessionDenialsSection(session: session, store: store)
                }
                textSection(title: "Last assistant message", text: session.lastAssistant, markdown: true)
            }
            .padding(20)
        }
        .frame(minWidth: 420)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(session.toolBadge)
                    .font(.caption.monospaced().bold())
                    .frame(width: 20, height: 20)
                    .background(session.tool == "codex" ? Color.purple.opacity(0.25) : Color.blue.opacity(0.25))
                    .clipShape(Circle())
                    .help(SessionRowView.toolHelp(session))
                Text(session.displayTitle)
                    .font(.title2.bold())
            }
            if let title = SessionDisplay.secondaryTitle(session) {
                Text(title)
                    .font(.headline)
                    .foregroundStyle(.secondary)
            }
            Text(session.state.displayName)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .help("The session's state; see AgentBoard Help → Session states")
            if let reason = session.waitingReason, !reason.isEmpty {
                Text(reason)
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
            if session.state == .done, let reason = session.doneReason, !reason.isEmpty {
                Text("Done: \(reason)")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            actionBar
        }
    }

    private var actionBar: some View {
        HStack {
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
            if session.state == .done {
                Button("Reopen") { Task { await store.markOpen(session) } }
                    .help(SessionActionHelp.reopen)
            } else {
                Button("Mark Done") { Task { await store.markDone(session) } }
                    .help(SessionActionHelp.markDone)
            }
            Button("Snooze 1d") { Task { await store.snooze(session, hours: 24) } }
                .help(SessionActionHelp.snooze)
        }
        .padding(.top, 4)
    }

    private var metadata: some View {
        VStack(alignment: .leading, spacing: 6) {
            metadataRow(label: "Project", value: session.project)
            metadataRow(label: "Directory", value: session.cwd)
            metadataRow(label: "Branch", value: session.branch ?? "—")
            metadataRow(label: "Started", value: RelativeTime.string(from: session.startedAt))
            metadataRow(label: "Updated", value: RelativeTime.string(from: session.updatedAt))
            if let endedAt = session.endedAt {
                metadataRow(label: "Ended", value: RelativeTime.string(from: endedAt))
            }
            metadataRow(label: "Alive", value: session.alive ? "Yes" : "No")
            metadataRow(label: "Permissions", value: PermissionMode.detail(session.permissionMode))
                .help(PermissionMode.help(session.permissionMode))
            if let model = session.model, !model.isEmpty {
                metadataRow(label: "Model", value: model)
                    .help("Shown on the row as \(ModelName.short(model)); reported by the agent's hooks, read from its transcript, or set with agentboard status --model")
            }
            if let context = SessionModelTag.contextDescription(session) {
                metadataRow(label: "Context", value: context)
                    .help("Tokens the session's latest request sent to the model, cached ones included; refreshed when a turn finishes or the agent reports it")
            }
        }
        .font(.caption)
    }

    private func metadataRow(label: String, value: String) -> some View {
        HStack(alignment: .top) {
            Text(label)
                .foregroundStyle(.secondary)
                .frame(width: 80, alignment: .leading)
            Text(value)
                .textSelection(.enabled)
        }
    }

    private var linksSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Links").font(.headline)
            ForEach(session.links) { link in
                HStack {
                    Text(link.kindLabel)
                        .font(.caption.bold())
                        .foregroundStyle(.secondary)
                        .help("What kind of link this is: a PR, a ticket or a URL")
                    if let urlString = link.url, let url = URL(string: urlString) {
                        Button(linkLabel(link)) { NSWorkspace.shared.open(url) }
                            .buttonStyle(.link)
                            .help(SessionActionHelp.open(link))
                    } else {
                        Text(linkLabel(link))
                    }
                    if let state = link.state {
                        let tint = link.isPR ? PRStyle.color(for: link) : Color.secondary
                        Text(state)
                            .font(.caption2)
                            .padding(.horizontal, 5)
                            .background(tint.opacity(0.2), in: Capsule())
                            .foregroundStyle(link.isPR ? tint : Color.primary)
                            .help("The link's state as last reported (refreshed every 10 minutes)")
                    }
                    if link.awaitsYou == true {
                        Label("awaits you", systemImage: "person.crop.circle.badge.exclamationmark")
                            .font(.caption2.bold())
                            .padding(.horizontal, 5)
                            .background(Color.orange.opacity(0.2), in: Capsule())
                            .foregroundStyle(.orange)
                            .help("You opened this PR and nobody but automation has reviewed or commented yet")
                    }
                }
            }
        }
    }

    private func linkLabel(_ link: Link) -> String {
        if let title = link.title, !title.isEmpty { return "\(link.ref) — \(title)" }
        return link.ref
    }

    @ViewBuilder
    private func textSection(title: String, text: String?, markdown: Bool = false) -> some View {
        if let text, !text.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Group {
                    if markdown, let attributed = try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)) {
                        Text(attributed)
                    } else {
                        Text(text)
                    }
                }
                .font(.callout)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
