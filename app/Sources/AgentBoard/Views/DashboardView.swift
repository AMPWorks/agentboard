import SwiftUI

struct DashboardView: View {
    @ObservedObject var store: SnapshotStore
    @State private var selectedState: SessionState?
    @State private var toolFilter: String = "all"
    @State private var projectFilter: String = "all"
    @State private var searchText: String = ""
    @State private var selectedSessionKey: String?

    private static let sidebarStates: [SessionState] = [
        .needsYou, .interrupted, .review, .running, .unconcluded, .snoozed, .done
    ]

    private var projects: [String] {
        let names = Set(store.snapshot.sessions.map(\.project))
        return names.sorted()
    }

    /// Sessions that pass the tool and project pickers and the search
    /// query, in any state. The sidebar badges count these, and the list
    /// narrows them to the selected state, so both follow one rule.
    private var matchedSessions: [Session] {
        SessionSearch(searchText).filter(store.snapshot.sessions, tool: toolFilter, project: projectFilter)
    }

    private func listedSessions(from matched: [Session]) -> [Session] {
        matched
            .filter { selectedState == nil || $0.state == selectedState }
            .sorted { lhs, rhs in
                if lhs.state.sortRank != rhs.state.sortRank { return lhs.state.sortRank < rhs.state.sortRank }
                return lhs.updatedAt > rhs.updatedAt
            }
    }

    private var selectedSession: Session? {
        guard let key = selectedSessionKey else { return nil }
        return store.snapshot.sessions.first { $0.key == key }
    }

    /// A row tapped in the popover asks the dashboard to select a session;
    /// clear any filters that might hide it, then select it and consume the request.
    private func consumePendingSelection() {
        guard let key = store.pendingSelection else { return }
        clearFilters()
        selectedSessionKey = key
        store.pendingSelection = nil
    }

    var body: some View {
        let matched = matchedSessions
        let counts = FilteredCounts(matched, query: SessionSearch(searchText))
        let all = FilteredCounts(store.snapshot.sessions)
        NavigationSplitView {
            sidebar(
                counts: counts,
                hiddenNeedingAttention: counts.hiddenNeedingAttention(from: all),
                hiddenNeedsYouNotes: counts.hiddenNeedsYouNotes(from: all)
            )
        } content: {
            list(listedSessions(from: matched))
        } detail: {
            if let session = selectedSession {
                SessionDetailView(session: session, store: store)
            } else {
                ContentUnavailableView("Select a session", systemImage: "square.stack.3d.up")
            }
        }
        .searchable(text: $searchText, prompt: "Search title, prompts, repo, branch, PR…")
        .onAppear { consumePendingSelection() }
        .onChange(of: store.pendingSelection) { _, _ in consumePendingSelection() }
        .toolbar {
            ToolbarItemGroup {
                Picker("Tool", selection: $toolFilter) {
                    Text("All Tools").tag("all")
                    Text("Claude").tag("claude")
                    Text("Codex").tag("codex")
                }
                Picker("Project", selection: $projectFilter) {
                    Text("All Projects").tag("all")
                    ForEach(projects, id: \.self) { Text($0).tag($0) }
                }
                Toggle("Show hidden", isOn: Binding(
                    get: { store.showAll },
                    set: { store.setShowAll($0) }
                ))
                .help("Show non-interactive and empty sessions (passes --all to the snapshot)")
                Button {
                    Task { await store.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh")
            }
        }
        .navigationTitle("AgentBoard")
    }

    /// Clearing always lands on All, so nothing a filter hid stays hidden
    /// behind a selected sidebar state.
    private func clearFilters() {
        selectedState = nil
        toolFilter = "all"
        projectFilter = "all"
        searchText = ""
    }

    private func sidebar(counts: FilteredCounts, hiddenNeedingAttention: Int,
                         hiddenNeedsYouNotes: Int) -> some View {
        VStack(spacing: 0) {
            List(selection: $selectedState) {
                Label("All", systemImage: "tray.full")
                    .badge(counts.total)
                    .tag(SessionState?.none)
                ForEach(Self.sidebarStates, id: \.self) { state in
                    Label(state.displayName, systemImage: symbol(for: state))
                        .badge(counts.count(for: state))
                        .tag(SessionState?.some(state))
                }
            }
            // Open notes the filters keep, counted note by note like the
            // popover's Updates header (`UpdatesFeed.total`).
            HStack(spacing: 6) {
                Label("Updates", systemImage: "text.bubble")
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
                Text("\(counts.counts.notes)")
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            .font(.callout)
            .padding(.horizontal, 16)
            .padding(.vertical, 6)
            .help("Open updates (retained notes) the filters keep; dismiss them in the menu bar popover or a session's detail")
            // The badges follow the filters, so a leftover query or picker
            // could show "Needs you 0" while a session waits, or hide a
            // session in Review/Snoozed/Done that holds an open needs_you
            // note. Say so.
            if let notice = FilteredCounts.hiddenAttentionNotice(hiddenNeedingAttention,
                                                                  notes: hiddenNeedsYouNotes) {
                Divider()
                HStack(spacing: 6) {
                    Text(notice)
                        .font(.caption.bold())
                        .foregroundStyle(SessionColor.needsYou)
                        .lineLimit(2)
                    Spacer(minLength: 0)
                    // clearFilters also selects All, so the sessions the warning
                    // was about are on screen, not hidden by a sidebar state.
                    Button("Clear") { clearFilters() }
                        .controlSize(.small)
                        .help("Clear the search and the tool and project pickers, and show all sessions")
                }
                .padding(8)
            }
            Divider()
            SessionColorLegendView()
                .padding(8)
        }
        .navigationSplitViewColumnWidth(min: 160, ideal: 180)
    }

    private func list(_ sessions: [Session]) -> some View {
        List(sessions, selection: $selectedSessionKey) { session in
            SessionRowView(session: session, store: store, compact: false)
                .tag(session.key)
        }
        .contextMenu(forSelectionType: String.self) { keys in
            if let session = singleSelectedSession(in: keys) {
                SessionContextMenu(session: session, store: store)
            }
        } primaryAction: { keys in
            guard let session = singleSelectedSession(in: keys), session.isFocusable else { return }
            Task { await store.focus(session) }
        }
        .navigationSplitViewColumnWidth(min: 280, ideal: 340)
    }

    private func singleSelectedSession(in keys: Set<String>) -> Session? {
        guard keys.count == 1, let key = keys.first else { return nil }
        return store.snapshot.sessions.first { $0.key == key }
    }

    private func symbol(for state: SessionState) -> String {
        switch state {
        case .needsYou: return "exclamationmark.bubble"
        case .interrupted: return "exclamationmark.triangle"
        case .running: return "play.circle"
        case .review: return "eye"
        case .unconcluded: return "clock"
        case .snoozed: return "moon.zzz"
        case .done: return "checkmark.circle"
        case .unknown: return "questionmark.circle"
        }
    }
}
