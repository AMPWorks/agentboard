import SwiftUI
import AppKit

struct MenuBarView: View {
    @ObservedObject var store: SnapshotStore
    /// Not observed: the popover only reports that it is showing.
    let attention: AttentionController
    @Environment(\.openWindow) private var openWindow

    @FocusState private var searchFocused: Bool
    @State private var keyMonitor = PopoverKeyMonitor()
    /// Unconcluded's disclosure state with no query (collapsed by default)
    /// and while a query is active (opened whenever it gains matches).
    @State private var unconcludedExpanded = false
    @State private var unconcludedExpandedWhileFiltering = true
    /// Set on each open and consumed by the first time the popover's window
    /// becomes key, so later key changes (a context menu, an alert) do not
    /// re-select a query the user is editing.
    @State private var focusOnNextKey = false

    private var sections: PopoverSections {
        PopoverSections.make(from: store.snapshot.sessions, query: SessionSearch(store.popoverQuery))
    }

    var body: some View {
        let sections = sections
        VStack(spacing: 0) {
            searchBar(sections)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(sections.sections) { section in
                        if section.kind == .unconcluded {
                            unconcludedSection(section, in: sections)
                        } else {
                            sessionSection(section, in: sections)
                        }
                    }
                    if sections.snapshotIsEmpty {
                        Text("No sessions yet")
                            .foregroundStyle(.secondary)
                            .padding()
                    } else if sections.hasNoMatches {
                        noMatches
                    }
                }
                .padding(.vertical, 8)
            }
            // 388 + the search bar keeps the popover's overall height.
            .frame(width: 380, height: 388)

            Divider()
            footer
        }
        .background(WindowReader { keyMonitor.window = $0 })
        .onAppear {
            store.setVisible(true)
            // Every popover open counts as seeing the board.
            attention.popoverDidAppear()
            keyMonitor.start(
                queryIsEmpty: { store.popoverQuery.isEmpty },
                clearQuery: { clearQuery() },
                // Through focusSearchField, whose second set after a main-queue
                // hop still lands if @FocusState has not yet mirrored a resign.
                focusSearch: { focusSearchField() }
            )
            focusOnNextKey = true
            focusSearchField()
            // If the panel is already key, no didBecomeKey follows; spend the
            // flag now so a later key change cannot re-select a query mid-edit.
            DispatchQueue.main.async {
                if keyMonitor.window?.isKeyWindow == true { focusOnNextKey = false }
            }
        }
        .onDisappear {
            focusOnNextKey = false
            store.setVisible(false)
            attention.popoverDidDisappear()
            keyMonitor.stop()
        }
        // The panel may not be key yet when `onAppear` runs, so focus again
        // when the popover's window first becomes key after each open.
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didBecomeKeyNotification)) { note in
            if focusOnNextKey, let window = keyMonitor.window, note.object as? NSWindow === window {
                focusOnNextKey = false
                focusSearchField()
            }
        }
        .onChange(of: sections.expandsUnconcluded) { _, expands in
            if expands { unconcludedExpandedWhileFiltering = true }
        }
    }

    // MARK: - Search

    private func searchBar(_ sections: PopoverSections) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Filter sessions", text: $store.popoverQuery)
                    .textFieldStyle(.plain)
                    .focused($searchFocused)
                if !store.popoverQuery.isEmpty {
                    Button {
                        clearQuery()
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .help("Clear filter")
                    .accessibilityLabel("Clear filter")
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 6))

            if let notice = sections.hiddenNotice {
                Text(notice)
                    .font(sections.noticeIsEmphasized ? .caption.bold() : .caption)
                    .foregroundStyle(sections.noticeIsEmphasized ? AnyShapeStyle(SessionColor.needsYou) : AnyShapeStyle(.secondary))
                    .lineLimit(1)
                    .padding(.horizontal, 4)
            }
        }
        .padding(8)
        .frame(width: 380, alignment: .leading)
    }

    /// Focus now and once more after the window settles: a MenuBarExtra
    /// panel may not be key yet when `onAppear` runs. Focusing the field
    /// selects its text, so a kept query is replaced by typing.
    private func focusSearchField() {
        searchFocused = true
        DispatchQueue.main.async { searchFocused = true }
    }

    private func clearQuery() {
        store.popoverQuery = ""
        searchFocused = true
    }

    private var noMatches: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("No sessions match “\(store.popoverQuery)”")
                .foregroundStyle(.secondary)
                .lineLimit(2)
            Button("Clear") { clearQuery() }
        }
        .padding()
    }

    // MARK: - Sections

    private func sessionSection(_ section: PopoverSections.Section, in sections: PopoverSections) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(sections.header(for: section))
                .font(.caption.bold())
                .foregroundStyle(.secondary)
                .padding(.horizontal, 10)
                .padding(.top, 4)
            ForEach(section.sessions) { session in
                SessionRowView(session: session, store: store)
                    .padding(.horizontal, 4)
            }
        }
    }

    private func unconcludedSection(_ section: PopoverSections.Section, in sections: PopoverSections) -> some View {
        let expanded = Binding(
            get: { sections.isFiltering ? unconcludedExpandedWhileFiltering : unconcludedExpanded },
            set: { value in
                if sections.isFiltering {
                    unconcludedExpandedWhileFiltering = value
                } else {
                    unconcludedExpanded = value
                }
            }
        )
        return DisclosureGroup(sections.header(for: section), isExpanded: expanded) {
            ForEach(section.sessions) { session in
                SessionRowView(session: session, store: store)
                    .padding(.horizontal, 4)
            }
        }
        .font(.caption.bold())
        .foregroundStyle(.secondary)
        .padding(.horizontal, 10)
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
