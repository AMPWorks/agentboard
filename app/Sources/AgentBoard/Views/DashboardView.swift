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

    private var filteredSessions: [Session] {
        store.snapshot.sessions
            .filter { selectedState == nil || $0.state == selectedState }
            .filter { toolFilter == "all" || $0.tool == toolFilter }
            .filter { projectFilter == "all" || $0.project == projectFilter }
            .filter { matchesSearch($0) }
            .sorted { lhs, rhs in
                if lhs.state.sortRank != rhs.state.sortRank { return lhs.state.sortRank < rhs.state.sortRank }
                return lhs.updatedAt > rhs.updatedAt
            }
    }

    private func matchesSearch(_ session: Session) -> Bool {
        guard !searchText.isEmpty else { return true }
        let haystack = [session.title, session.project, session.branch ?? "", session.cwd]
            .joined(separator: " ")
            .lowercased()
        return haystack.contains(searchText.lowercased())
    }

    private var selectedSession: Session? {
        guard let key = selectedSessionKey else { return nil }
        return store.snapshot.sessions.first { $0.key == key }
    }

    /// A row tapped in the popover asks the dashboard to select a session;
    /// clear any filters that might hide it, then select it and consume the request.
    private func consumePendingSelection() {
        guard let key = store.pendingSelection else { return }
        selectedState = nil
        toolFilter = "all"
        projectFilter = "all"
        searchText = ""
        selectedSessionKey = key
        store.pendingSelection = nil
    }

    var body: some View {
        NavigationSplitView {
            sidebar
        } content: {
            list
        } detail: {
            if let session = selectedSession {
                SessionDetailView(session: session, store: store)
            } else {
                ContentUnavailableView("Select a session", systemImage: "square.stack.3d.up")
            }
        }
        .searchable(text: $searchText, prompt: "Search title, project, branch")
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

    private var sidebar: some View {
        VStack(spacing: 0) {
            List(selection: $selectedState) {
                Label("All", systemImage: "tray.full")
                    .badge(store.snapshot.sessions.count)
                    .tag(SessionState?.none)
                ForEach(Self.sidebarStates, id: \.self) { state in
                    Label(state.displayName, systemImage: symbol(for: state))
                        .badge(count(for: state))
                        .tag(SessionState?.some(state))
                }
            }
            Divider()
            SessionColorLegendView()
                .padding(8)
        }
        .navigationSplitViewColumnWidth(min: 160, ideal: 180)
    }

    private var list: some View {
        List(filteredSessions, selection: $selectedSessionKey) { session in
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

    private func count(for state: SessionState) -> Int {
        switch state {
        case .needsYou: return store.snapshot.counts.needsYou
        case .interrupted: return store.snapshot.counts.interrupted
        case .running: return store.snapshot.counts.running
        case .review: return store.snapshot.counts.review
        case .unconcluded: return store.snapshot.counts.unconcluded
        case .snoozed: return store.snapshot.counts.snoozed
        case .done: return store.snapshot.counts.done
        case .unknown: return 0
        }
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
