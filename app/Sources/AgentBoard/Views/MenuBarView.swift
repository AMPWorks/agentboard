import SwiftUI
import AppKit

struct MenuBarView: View {
    @ObservedObject var store: SnapshotStore
    /// Not observed: the popover only reports that it is showing.
    let attention: AttentionController
    @Environment(\.openWindow) private var openWindow

    private var needsYou: [Session] {
        store.snapshot.sessions
            .filter { $0.state == .needsYou || $0.state == .interrupted }
            .sorted { $0.updatedAt > $1.updatedAt }
    }
    private var review: [Session] {
        store.snapshot.sessions.filter { $0.state == .review }.sorted { $0.updatedAt > $1.updatedAt }
    }
    private var running: [Session] {
        store.snapshot.sessions.filter { $0.state == .running }.sorted { $0.updatedAt > $1.updatedAt }
    }
    private var unconcluded: [Session] {
        store.snapshot.sessions.filter { $0.state == .unconcluded }.sorted { $0.updatedAt > $1.updatedAt }
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    section(title: "Needs you", sessions: needsYou)
                    section(title: "Review", sessions: review)
                    section(title: "Running", sessions: running)
                    unconcludedSection
                    if store.snapshot.sessions.isEmpty {
                        Text("No sessions yet")
                            .foregroundStyle(.secondary)
                            .padding()
                    }
                }
                .padding(.vertical, 8)
            }
            .frame(width: 380, height: 420)

            Divider()
            footer
        }
        .onAppear {
            store.setVisible(true)
            // Every popover open counts as seeing the board.
            attention.popoverDidAppear()
        }
        .onDisappear {
            store.setVisible(false)
            attention.popoverDidDisappear()
        }
    }

    @ViewBuilder
    private func section(title: String, sessions: [Session]) -> some View {
        if !sessions.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                Text(title)
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 10)
                    .padding(.top, 4)
                ForEach(sessions) { session in
                    SessionRowView(session: session, store: store)
                        .padding(.horizontal, 4)
                }
            }
        }
    }

    @ViewBuilder
    private var unconcludedSection: some View {
        if !unconcluded.isEmpty {
            DisclosureGroup("Unconcluded (\(unconcluded.count))") {
                ForEach(unconcluded) { session in
                    SessionRowView(session: session, store: store)
                        .padding(.horizontal, 4)
                }
            }
            .font(.caption.bold())
            .foregroundStyle(.secondary)
            .padding(.horizontal, 10)
        }
    }

    private var footer: some View {
        HStack {
            Button("Open Dashboard") {
                DashboardWindowController.show()
            }
            Spacer()
            Button("Refresh") {
                Task { await store.refresh() }
            }
            Button("Quit") {
                NSApplication.shared.terminate(nil)
            }
        }
        .padding(8)
        .buttonStyle(.plain)
        .font(.caption)
    }
}
