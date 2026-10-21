import SwiftUI
import AppKit

struct SessionDetailView: View {
    let session: Session
    @ObservedObject var store: SnapshotStore

    var body: some View {
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
                    SessionNotesSection(session: session, store: store)
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
                Text(session.title)
                    .font(.title2.bold())
            }
            Text(session.state.displayName)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            if let reason = session.waitingReason, !reason.isEmpty {
                Text(reason)
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
            if let status = session.statusText, !status.isEmpty {
                Text(status)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .italic()
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
            if session.state == .done {
                Button("Reopen") { Task { await store.markOpen(session) } }
            } else {
                Button("Mark Done") { Task { await store.markDone(session) } }
            }
            Button("Snooze 1d") { Task { await store.snooze(session, hours: 24) } }
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
                    if let urlString = link.url, let url = URL(string: urlString) {
                        Button(linkLabel(link)) { NSWorkspace.shared.open(url) }
                            .buttonStyle(.link)
                    } else {
                        Text(linkLabel(link))
                    }
                    if let state = link.state {
                        Text(state)
                            .font(.caption2)
                            .padding(.horizontal, 5)
                            .background(.secondary.opacity(0.2), in: Capsule())
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
