import SwiftUI

/// The dashboard's content column while the sidebar's Denials row is
/// selected: the open permission denials the pickers and search keep,
/// newest first (`DenialFeed.rows`). A click selects the denial's session
/// for the detail column; double-click or Return focuses its terminal.
struct DenialListView: View {
    let rows: [DenialFeed.Row]
    /// Open denials the filters keep, uncapped (`DenialFeed.total`).
    let total: Int
    let clearPlan: DenialFeed.ClearPlan
    /// Any open denial at all in the snapshot, before filters: picks the
    /// empty state.
    let hasAnyDenials: Bool
    let searchText: String
    @Binding var selection: String?
    @ObservedObject var store: SnapshotStore

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Text(DenialFeed.header(listed: rows.count, total: total))
                    .font(.headline)
                    .help("Tool calls auto mode blocked, kept for 7 days; they never change a session's state")
                HelpButton(topic: .dashboard, tooltip: "Explain the Denials list in AgentBoard Help")
                Spacer()
                if total > 0 {
                    Button(clearPlan.title) {
                        let plan = clearPlan
                        Task { await store.dismissDenials(plan.dismissals, action: plan.title.lowercased()) }
                    }
                    .controlSize(.small)
                    .help(clearPlan.help)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            Divider()
            if rows.isEmpty {
                emptyState
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(rows, selection: $selection) { row in
                    DenialRowView(denial: row.denial, session: row.session, store: store)
                        .tag(row.id)
                }
                .contextMenu(forSelectionType: String.self) { ids in
                    if let session = session(for: ids) {
                        SessionContextMenu(session: session, store: store)
                    }
                } primaryAction: { ids in
                    guard let session = session(for: ids), session.isFocusable else { return }
                    Task { await store.focusOrShowDashboard(session) }
                }
            }
        }
    }

    @ViewBuilder
    private var emptyState: some View {
        if !hasAnyDenials {
            ContentUnavailableView(
                "No permission denials", systemImage: "hand.raised",
                description: Text("When auto mode blocks a tool call it is listed here for 7 days."))
        } else if !searchText.trimmingCharacters(in: .whitespaces).isEmpty {
            ContentUnavailableView.search(text: searchText)
        } else {
            ContentUnavailableView(
                "No denials match the filters", systemImage: "line.3.horizontal.decrease.circle",
                description: Text("Set the tool and project pickers to All to see every denial."))
        }
    }

    private func session(for ids: Set<String>) -> Session? {
        guard ids.count == 1, let id = ids.first else { return nil }
        return rows.first { $0.id == id }?.session
    }
}

/// One permission denial: the session line (tool badge, project · title,
/// time), then the blocked tool and the reason, with a subagent tag and an
/// x to clear it. `compact` (the session detail) drops the session line.
struct DenialRowView: View {
    let denial: Denial
    let session: Session
    var compact = false
    @ObservedObject var store: SnapshotStore

    @State private var isHovering = false

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            VStack(alignment: .leading, spacing: 3) {
                if !compact {
                    HStack(spacing: 4) {
                        Text(session.toolBadge)
                            .font(.caption2.monospaced().bold())
                            .frame(width: 14, height: 14)
                            .background(session.tool == "codex" ? Color.purple.opacity(0.25) : Color.blue.opacity(0.25))
                            .clipShape(Circle())
                            .help(SessionRowView.toolHelp(session))
                        Text(sessionLine)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        time
                    }
                }
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(denial.toolLabel)
                        .font(.callout.monospaced().bold())
                        .lineLimit(1)
                        .help(denial.toolName)
                    if denial.subagent {
                        Text("subagent")
                            .font(.caption2)
                            .padding(.horizontal, 5)
                            .background(Color.secondary.opacity(0.2), in: Capsule())
                            .help("A subagent of this session made the blocked call")
                    }
                    if compact {
                        Spacer(minLength: 4)
                        time
                    }
                }
                Text(denial.reasonLabel)
                    .font(.callout)
                    .lineLimit(2)
                    .help(denial.reason.isEmpty ? "No reason recorded" : denial.reason)
            }
            Button {
                Task { await store.dismissDenial(denial, of: session) }
            } label: {
                Image(systemName: "xmark.circle")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Dismiss this denial")
            .accessibilityLabel("Dismiss denial")
            .opacity(isHovering ? 1 : 0.35)
        }
        .padding(.vertical, 3)
        .contentShape(Rectangle())
        .onHover { isHovering = $0 }
    }

    private var sessionLine: String {
        let title = session.displayTitle
        return session.project.isEmpty ? title : "\(session.project) · \(title)"
    }

    private var time: some View {
        Text(RelativeTime.string(from: denial.ts))
            .font(.caption2)
            .foregroundStyle(.secondary)
            .help(denial.ts)
    }
}

/// The session detail's Permission denials section: the session's open
/// denials (compact rows), with Dismiss All when there is more than one.
struct SessionDenialsSection: View {
    let session: Session
    @ObservedObject var store: SnapshotStore

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(DenialFeed.header(listed: session.denials.count, total: session.denialsCount))
                    .font(.headline)
                Spacer()
                if max(session.denials.count, session.denialsCount) > 1 {
                    Button("Dismiss All") { Task { await store.dismissDenials(for: session) } }
                        .controlSize(.small)
                        .help("Dismiss every open denial of this session, including ones not listed")
                }
            }
            ForEach(session.denials) { denial in
                DenialRowView(denial: denial, session: session, compact: true, store: store)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 2)
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
            }
        }
    }
}
