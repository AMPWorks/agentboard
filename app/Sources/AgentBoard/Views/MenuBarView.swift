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
    @AppStorage(PopoverUpdatesSettings.modeKey) private var updatesModeRaw: String = UpdatesPanelMode.default.rawValue
    @AppStorage(PopoverUpdatesSettings.fontSizeKey) private var updatesFontRaw: String = UpdatesFontSize.default.rawValue
    @AppStorage(PopoverUpdatesSettings.compactLimitKey) private var updatesLimitRaw: Int = UpdatesFeed.compactLimit
    @AppStorage(PopoverUpdatesSettings.lastShownKey) private var updatesLastShown: Bool = false // not-a-setting: remembered panel state, written by the Updates button
    /// The panel's state in Hidden until clicked mode. MenuBarView's state
    /// survives closing the popover, so `onAppear` resets it on every open.
    @State private var updatesOpenThisVisit = false

    /// While the panel is closed the needs_you notes the query would hide
    /// are pinned above the sessions, so the notice leaves them out.
    private var sections: PopoverSections {
        PopoverSections.make(from: store.snapshot.sessions, query: SessionSearch(store.popoverQuery),
                             needsYouNotesPinned: !updatesShown)
    }

    private var updatesMode: UpdatesPanelMode { UpdatesPanelMode.parse(updatesModeRaw) }
    private var updatesFont: UpdatesFontSize { UpdatesFontSize.parse(updatesFontRaw) }

    private var updatesShown: Bool {
        PopoverUpdatesSettings.panelShown(mode: updatesMode, remembered: updatesLastShown,
                                          toggledThisOpen: updatesOpenThisVisit)
    }

    var body: some View {
        let sections = sections
        let sessions = store.snapshot.sessions
        let query = SessionSearch(store.popoverQuery)
        let updates = UpdatesFeed.rows(from: sessions, query: query)
        let mode = updatesMode
        let shown = updatesShown
        let feed = UpdatesState(
            hasUpdates: PopoverUpdatesSettings.hasOpenUpdates(sessions),
            matching: UpdatesFeed.total(from: sessions, query: query),
            shown: shown
        )
        let pinned = PopoverUpdatesSettings.pinnedRows(from: sessions, panelShown: shown)
        VStack(spacing: 0) {
            searchBar(sections, feed: feed, mode: mode)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    // Above Needs you: what sessions said that is worth
                    // keeping, behind the Updates button unless Settings →
                    // Popover says otherwise. Compact (UpdatesSectionView),
                    // so it never pushes the sessions out of view.
                    if !pinned.isEmpty {
                        PinnedUpdatesView(rows: pinned, store: store, fontSize: updatesFont) {
                            setUpdatesShown(true)
                        }
                    }
                    if PopoverUpdatesSettings.panelVisible(mode: mode, shown: shown, hasUpdates: feed.hasUpdates,
                                                           hasRows: !updates.isEmpty) {
                        UpdatesSectionView(
                            rows: updates,
                            listed: updates.reduce(0) { $0 + $1.notes.count },
                            total: feed.matching,
                            clearPlan: UpdatesFeed.clearPlan(from: sessions, query: query),
                            store: store,
                            fontSize: updatesFont,
                            compactLimit: PopoverUpdatesSettings.compactLimit(updatesLimitRaw),
                            emptyText: "No updates match “\(store.popoverQuery)”",
                            onHide: mode == .alwaysShown ? nil : { setUpdatesShown(false) }
                        )
                    }
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
            updatesOpenThisVisit = false
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

    /// What the Updates button and the search hint need to know.
    private struct UpdatesState {
        /// Any open update at all, whatever the query.
        let hasUpdates: Bool
        /// Open updates the query keeps, uncapped (`UpdatesFeed.total`).
        let matching: Int
        let shown: Bool
    }

    private func setUpdatesShown(_ value: Bool) {
        if updatesMode == .rememberLast {
            updatesLastShown = value
        } else {
            updatesOpenThisVisit = value
        }
    }

    /// The one-click Updates button next to the search field: the bubble,
    /// the word and the count, tinted while the panel is open.
    private func updatesButton(_ feed: UpdatesState, filtering: Bool) -> some View {
        Button {
            setUpdatesShown(!feed.shown)
        } label: {
            HStack(spacing: 3) {
                Image(systemName: feed.shown ? "text.bubble.fill" : "text.bubble")
                Text("Updates")
                Text(PopoverUpdatesSettings.badge(feed.matching))
                    .monospacedDigit()
                    .fontWeight(.semibold)
            }
            .font(.caption)
            .foregroundStyle(feed.shown ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(.secondary))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(
                Capsule().fill(feed.shown ? AnyShapeStyle(Color.accentColor.opacity(0.18))
                                          : AnyShapeStyle(.quaternary.opacity(0.8)))
            )
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .help(PopoverUpdatesSettings.toggleHelp(shown: feed.shown, count: feed.matching, filtering: filtering))
        .accessibilityLabel(PopoverUpdatesSettings.toggleAccessibilityLabel(shown: feed.shown, count: feed.matching,
                                                                             filtering: filtering))
    }

    private func searchBar(_ sections: PopoverSections, feed: UpdatesState, mode: UpdatesPanelMode) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Filter sessions", text: $store.popoverQuery)
                    .textFieldStyle(.plain)
                    .focused($searchFocused)
                    .help("Filter sessions and updates: words, \"quoted phrases\", or field:value such as branch:main or state:review (⌘F)")
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
                if PopoverUpdatesSettings.toggleVisible(mode: mode, hasUpdates: feed.hasUpdates) {
                    updatesButton(feed, filtering: sections.isFiltering)
                }
                HelpButton(topic: .search, tooltip: "Explain search: words, quotes and field:value filters",
                           closesHostWindow: true)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 6))

            // CLI or database failures, right under the search bar.
            StatusBannerView(store: store)

            if let notice = sections.hiddenNotice {
                Text(notice)
                    .font(sections.noticeIsEmphasized ? .caption.bold() : .caption)
                    .foregroundStyle(sections.noticeIsEmphasized ? AnyShapeStyle(SessionColor.needsYou) : AnyShapeStyle(.secondary))
                    // Two lines, so a hidden-updates clause is never cut off.
                    .lineLimit(2)
                    .padding(.horizontal, 4)
            }
            if mode != .alwaysShown,
               let hint = PopoverUpdatesSettings.matchHint(panelShown: feed.shown, isFiltering: sections.isFiltering,
                                                           matching: feed.matching) {
                HStack(spacing: 4) {
                    Text(hint)
                        .foregroundStyle(.secondary)
                    Button("Show") { setUpdatesShown(true) }
                        .buttonStyle(.link)
                        .help("Open the Updates panel to see the updates that match the filter")
                }
                .font(.caption)
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
                .help("Clear the filter and show every session")
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
                .help(Self.sectionHelp(section.kind))
            ForEach(section.sessions) { session in
                SessionRowView(session: session, store: store)
                    .padding(.horizontal, 4)
            }
        }
    }

    static func sectionHelp(_ kind: PopoverSections.Kind) -> String {
        switch kind {
        case .needsYou: return "Sessions waiting on you (a prompt, a question, a failure) and interrupted ones"
        case .review: return "Sessions with something for you to act on; the last line of each row says what"
        case .running: return "Sessions working right now"
        case .unconcluded: return "Sessions that ended or stopped without finishing"
        case .snoozed: return "Snoozed sessions that match the filter"
        case .done: return "Done sessions that match the filter"
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
        .help("Sessions that ended or stopped without finishing; click to show or hide them")
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 4) {
            // Plan usage, one line per source, above the button row (full at 380 pt).
            UsageLineView(usage: store.snapshot.usage)
            HStack {
                Button("Open Dashboard") {
                    DashboardWindowController.show()
                }
                .help("Open the dashboard window with every session (⌃⌥⌘A)")
                Spacer()
                Button("Refresh") {
                    Task { await store.refresh() }
                }
                .help("Reload every session's state now")
                GearMenu(store: store, closesHostWindow: true)
            }
        }
        .padding(8)
        .buttonStyle(.plain)
        .font(.caption)
    }
}
