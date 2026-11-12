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

    // Size (Settings → Popover size and the corner grip; see PopoverSizing).
    @AppStorage(PopoverSizing.heightKey) private var storedHeight: Double = Double(PopoverSizing.defaultHeight)
    @AppStorage(PopoverSizing.autoExpandKey) private var autoExpand: Bool = false
    @AppStorage(PopoverSizing.widthKey) private var storedWidth: Double = 0
    @AppStorage(PopoverSizing.autoWidthKey) private var autoWidth: Bool = true
    @State private var placer = PopoverWindowPlacer()
    /// The placer's anchor (the icon's screen and position), mirrored so the
    /// width updates when it changes; `place` positions from the same value.
    @State private var screen: PopoverScreen?
    /// Search bar and footer heights, measured; the list gets the rest.
    @State private var searchBarHeight: CGFloat = 60
    @State private var footerHeight: CGFloat = 50
    /// Auto-expand: the bottom of the content the list must fit, settled
    /// so changes under `PopoverSizing.autoThreshold` are ignored.
    @State private var contentBottom: CGFloat?
    /// Auto-expand off: the bottom of everything the list renders (every
    /// section, row, empty state and Updates part), settled the same way,
    /// so the stored height caps the list instead of padding it with blank
    /// space (`PopoverSizing.fittedHeight`).
    @State private var fullContentBottom: CGFloat?
    /// The size while the grip is dragged (list height, popover width).
    @State private var dragSize: CGSize?
    @State private var dragOrigin: DragOrigin?
    /// The grip pushed its resize cursor (popped on leaving or closing).
    @State private var gripCursorPushed = false
    /// The pointer is over the grip (the cursor also stays up mid-drag).
    @State private var gripHovered = false
    /// The list content's bottom edge in the viewport's coordinates, so
    /// the list can fade its bottom while more is below.
    @State private var contentMaxY: CGFloat = 0

    /// The notice counts the needs_you notes the query hides whether the
    /// Updates panel is open or closed: nothing else lists them.
    private var sections: PopoverSections {
        PopoverSections.make(from: store.snapshot.sessions, query: SessionSearch(store.popoverQuery))
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
        let mode = updatesMode
        let shown = updatesShown
        // The feed sorts every open note, so it is built once, and only
        // while the panel can show it (`panelVisible` is false when hidden).
        let updates = shown ? UpdatesFeed.rows(from: sessions, query: query) : []
        let feed = UpdatesState(
            hasUpdates: PopoverUpdatesSettings.hasOpenUpdates(sessions),
            matching: UpdatesFeed.total(from: sessions, query: query),
            needsYou: PopoverUpdatesSettings.openNeedsYouCount(sessions),
            shown: shown
        )
        ScrollViewReader { scroller in
            VStack(spacing: 0) {
                searchBar(sections, feed: feed, mode: mode) { kind in
                    if kind == .unconcluded { unconcludedExpanded = true }
                    withAnimation { scroller.scrollTo(kind, anchor: .top) }
                }
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { searchBarHeight = $0 }
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        // Above Needs you: what sessions said that is worth
                        // keeping, behind the Updates button unless Settings →
                        // Popover says otherwise. Compact (UpdatesSectionView),
                        // so it never pushes the sessions out of view. Always
                        // shown, only needs_you and error rows sit up here;
                        // the rest follow the last section (`belowUpdates`).
                        let panelVisible = PopoverUpdatesSettings.panelVisible(
                            mode: mode, shown: shown, hasUpdates: feed.hasUpdates, hasRows: !updates.isEmpty)
                        let split = mode == .alwaysShown
                            ? UpdatesFeed.partitionForAlwaysShown(updates) : (above: updates, below: [])
                        if panelVisible && mode == .alwaysShown {
                            if !split.above.isEmpty {
                                alwaysShownUpdates(split.above, otherPart: split.below, above: true,
                                                   sessions: sessions, query: query)
                                    .reportsContentBottom()
                            }
                        } else if panelVisible {
                            UpdatesSectionView(
                                rows: updates,
                                listed: updates.reduce(0) { $0 + $1.notes.count },
                                total: feed.matching,
                                clearPlan: UpdatesFeed.clearPlan(from: sessions, query: query),
                                store: store,
                                fontSize: updatesFont,
                                compactLimit: PopoverUpdatesSettings.compactLimit(updatesLimitRaw),
                                emptyText: "No updates match “\(store.popoverQuery)”",
                                onHide: { setUpdatesShown(false) }
                            )
                            .reportsContentBottom()
                        }
                        ForEach(sections.sections) { section in
                            if section.kind == .unconcluded {
                                unconcludedSection(section, in: sections)
                            } else {
                                sessionSection(section, in: sections)
                                    .reportsContentBottom(PopoverSizing.isActive(section.kind) || sections.isFiltering)
                            }
                        }
                        if sections.snapshotIsEmpty {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(StoreStatus.emptyBoardText(lastError: store.lastError, lastUpdated: store.lastUpdated))
                                    .foregroundStyle(.secondary)
                                if store.lastUpdated != nil {
                                    Text("Start a Claude Code or Codex session and it shows up here within a few seconds.")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            .padding()
                        } else if sections.hasNoMatches {
                            noMatches
                                .reportsContentBottom()
                        }
                        // Always shown: info notes after every section, so they
                        // never outrank a session. Auto-expand does not fit
                        // them (they are not active sessions).
                        if panelVisible && mode == .alwaysShown && !split.below.isEmpty {
                            alwaysShownUpdates(split.below, otherPart: split.above, above: false,
                                               sessions: sessions, query: query)
                        }
                    }
                    .onGeometryChange(for: CGFloat.self) { $0.frame(in: .named(Self.listSpace)).maxY } action: { bottom in
                        fullContentBottom = PopoverSizing.settledAutoHeight(previous: fullContentBottom, measured: bottom)
                    }
                    .padding(.vertical, PopoverSizing.listPadding)
                    .coordinateSpace(name: Self.listSpace)
                    .onPreferenceChange(ContentBottomKey.self) { bottom in
                        contentBottom = PopoverSizing.settledAutoHeight(previous: contentBottom, measured: bottom)
                    }
                    .onGeometryChange(for: CGFloat.self) { $0.frame(in: .named(Self.viewportSpace)).maxY } action: {
                        contentMaxY = $0
                    }
                }
                .coordinateSpace(name: Self.viewportSpace)
                .frame(height: listHeight)
                .mask { listMask }

                Divider()
                footer
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { footerHeight = $0 }
            }
        }
        .frame(width: popoverWidth)
        .overlay(alignment: .bottomTrailing) { resizeGrip }
        .onGeometryChange(for: CGSize.self) { $0.size } action: { placer.setContentSize($0) }
        .background(WindowReader { window in
            keyMonitor.window = window
            placer.window = window
            readScreen()
        })
        .onAppear {
            placer.onAnchorChange = { readScreen() }
            placer.opened()
            readScreen()
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
            // The view outlives the popover, and a drag cut short by closing
            // may never end, so drop its state or it would win next open.
            gripHovered = false
            setGripCursor(false)
            dragSize = nil
            dragOrigin = nil
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

    /// One part of the split Always-shown Updates panel: card-less, with
    /// its own header and count. The part above lists every row (needs_you
    /// and errors); the part below keeps the stored compact limit and its
    /// "Show all".
    private func alwaysShownUpdates(_ rows: [UpdatesFeed.Row], otherPart: [UpdatesFeed.Row], above: Bool,
                                    sessions: [Session], query: SessionSearch) -> some View {
        let counts = UpdatesFeed.counts(of: rows)
        let otherHasRows = !otherPart.isEmpty
        return UpdatesSectionView(
            rows: rows,
            title: UpdatesFeed.partTitle(above: above, otherPartHasRows: otherHasRows),
            listed: counts.listed,
            total: counts.total,
            clearPlan: UpdatesFeed.partClearPlan(rows, whole: UpdatesFeed.clearPlan(from: sessions, query: query),
                                                 otherPartHasRows: otherHasRows),
            store: store,
            fontSize: updatesFont,
            compactLimit: above ? Int.max : PopoverUpdatesSettings.compactLimit(updatesLimitRaw),
            emptyText: "No updates match “\(store.popoverQuery)”",
            onHide: nil
        )
    }

    // MARK: - Size

    fileprivate static let listSpace = "popoverList"
    private static let viewportSpace = "popoverViewport"

    /// Opaque, except a fade over the list's last points while more
    /// content is below (PopoverSizing.hasMoreBelow).
    @ViewBuilder
    private var listMask: some View {
        if PopoverSizing.hasMoreBelow(contentMaxY: contentMaxY, viewportHeight: listHeight) {
            VStack(spacing: 0) {
                Color.black
                LinearGradient(colors: [.black, .black.opacity(0.15)], startPoint: .top, endPoint: .bottom)
                    .frame(height: PopoverSizing.moreBelowFade)
            }
        } else {
            Color.black
        }
    }
    static let gripSize: CGFloat = 14

    private struct DragOrigin {
        var size: CGSize
        /// The pointer in screen coordinates (y up) when the drag began.
        var mouse: CGPoint
    }

    /// Reads the placer's anchor; never resolves one of its own, so the
    /// content width and the panel's origin come from one anchorX.
    private func readScreen() {
        screen = placer.screen
    }

    /// The tallest list that fits under the menu bar on this screen.
    private var maxListHeight: CGFloat {
        guard let screen else { return .greatestFiniteMagnitude }
        // Two one-point dividers sit between the list and the chrome.
        return PopoverSizing.maxHeight(screenVisibleHeight: screen.visibleFrame.height,
                                       chromeHeight: searchBarHeight + footerHeight + 2)
    }

    private var availableWidth: CGFloat {
        screen?.availableWidth ?? .greatestFiniteMagnitude
    }

    private var listHeight: CGFloat {
        if autoExpand {
            return PopoverSizing.autoHeight(contentBottom: contentBottom, max: maxListHeight)
        }
        if let dragSize { return dragSize.height }
        return PopoverSizing.fittedHeight(stored: PopoverSizing.storedHeight(storedHeight),
                                          contentBottom: fullContentBottom, max: maxListHeight)
    }

    private var popoverWidth: CGFloat {
        if let dragSize { return dragSize.width }
        return PopoverSizing.width(auto: autoWidth, stored: storedWidth, available: screen?.availableWidth)
    }

    private var gripHelp: String {
        autoExpand
            ? "Drag to change the popover's width; its height fits the active sessions (Settings → Popover size). Double-click to reset"
            : "Drag to resize the popover; double-click to reset"
    }

    /// The bottom-right corner grip: drag to resize (width only while
    /// auto-expand sets the height), double-click to reset.
    private var resizeGrip: some View {
        ResizeGripShape()
            .stroke(.secondary, style: StrokeStyle(lineWidth: 1, lineCap: .round))
            .frame(width: 8, height: 8)
            .frame(width: Self.gripSize, height: Self.gripSize)
            .padding(2)
            .contentShape(Rectangle())
            .onHover { inside in
                gripHovered = inside
                // A fast drag can outrun the panel; keep the cursor until it ends.
                setGripCursor(inside || dragOrigin != nil)
            }
            .gesture(
                DragGesture(minimumDistance: 1)
                    .onChanged { value in dragChanged(value) }
                    .onEnded { _ in dragEnded() }
            )
            .onTapGesture(count: 2) { PopoverSizing.resetSize() }
            .help(gripHelp)
            .accessibilityElement()
            .accessibilityAddTraits(.isButton)
            .accessibilityLabel("Resize popover")
            .accessibilityValue("\(Int(listHeight)) points tall, \(Int(popoverWidth)) wide")
            .accessibilityHint(autoExpand ? "Adjust to change the width" : "Adjust to change the height")
            .accessibilityAdjustableAction { direction in
                let sign = direction == .increment ? 1 : -1
                // A step that cannot move (at a bound) stores nothing, so a
                // larger size saved on another display survives.
                if autoExpand {
                    if let width = PopoverSizing.steppedWidthToStore(shown: popoverWidth, direction: sign,
                                                                     available: availableWidth) {
                        storedWidth = width
                        autoWidth = false
                    }
                } else if let height = PopoverSizing.steppedHeightToStore(shown: listHeight, direction: sign,
                                                                          screenMax: maxListHeight) {
                    storedHeight = height
                }
            }
            .accessibilityAction(named: "Reset size") { PopoverSizing.resetSize() }
    }

    private func setGripCursor(_ on: Bool) {
        guard on != gripCursorPushed else { return }
        gripCursorPushed = on
        if on { ResizeGripShape.cursor(widthOnly: autoExpand).push() } else { NSCursor.pop() }
    }

    /// Follows the pointer in screen coordinates, so the panel moving or
    /// growing under it never feeds back into the drag.
    private func dragChanged(_ value: DragGesture.Value) {
        let mouse = NSEvent.mouseLocation
        if dragOrigin == nil {
            // The gesture starts after a point of movement; back that out.
            dragOrigin = DragOrigin(size: CGSize(width: popoverWidth, height: listHeight),
                                    mouse: CGPoint(x: mouse.x - value.translation.width,
                                                   y: mouse.y + value.translation.height))
        }
        guard let origin = dragOrigin else { return }
        let translation = CGSize(width: mouse.x - origin.mouse.x,
                                 height: autoExpand ? 0 : origin.mouse.y - mouse.y)
        dragSize = PopoverSizing.dragged(start: origin.size, translation: translation,
                                         available: availableWidth, screenMax: maxListHeight)
    }

    private func dragEnded() {
        defer {
            dragSize = nil
            dragOrigin = nil
            setGripCursor(gripHovered)
        }
        guard let origin = dragOrigin, let end = dragSize else { return }
        let result = PopoverSizing.dragResult(start: origin.size, end: end, heightIsAuto: autoExpand)
        if let height = result.height { storedHeight = height }
        if let width = result.width {
            storedWidth = width
            autoWidth = false
        }
    }

    // MARK: - Search

    /// What the Updates button and the search hint need to know.
    private struct UpdatesState {
        /// Any open update at all, whatever the query.
        let hasUpdates: Bool
        /// Open updates the query keeps, uncapped (`UpdatesFeed.total`).
        let matching: Int
        /// Open needs_you notes, whatever the query: the button is red
        /// while there are any (`PopoverUpdatesSettings.buttonTint`).
        let needsYou: Int
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
    /// the word and the count, red while any session has an open needs_you
    /// update (with the panel closed nothing else lists them), else tinted
    /// while the panel is open.
    private func updatesButton(_ feed: UpdatesState, filtering: Bool) -> some View {
        let tint = PopoverUpdatesSettings.buttonTint(needsYou: feed.needsYou, shown: feed.shown)
        return Button {
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
            .foregroundStyle(Self.updatesButtonForeground(tint))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Self.updatesButtonBackground(tint)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .help(PopoverUpdatesSettings.toggleHelp(shown: feed.shown, count: feed.matching, filtering: filtering,
                                                needsYou: feed.needsYou))
        .accessibilityLabel(PopoverUpdatesSettings.toggleAccessibilityLabel(shown: feed.shown, count: feed.matching,
                                                                             filtering: filtering,
                                                                             needsYou: feed.needsYou))
    }

    /// Red text, accent, or secondary grey for `buttonTint`.
    private static func updatesButtonForeground(_ tint: PopoverUpdatesSettings.ButtonTint) -> AnyShapeStyle {
        switch tint {
        case .needsYou: return AnyShapeStyle(SessionColor.needsYouText)
        case .accent: return AnyShapeStyle(Color.accentColor)
        case .plain: return AnyShapeStyle(.secondary)
        }
    }

    /// The capsule behind the button: the tint at 18%, or a grey fill.
    private static func updatesButtonBackground(_ tint: PopoverUpdatesSettings.ButtonTint) -> AnyShapeStyle {
        switch tint {
        case .needsYou: return AnyShapeStyle(SessionColor.needsYou.opacity(0.18))
        case .accent: return AnyShapeStyle(Color.accentColor.opacity(0.18))
        case .plain: return AnyShapeStyle(.quaternary.opacity(0.8))
        }
    }

    private func searchBar(_ sections: PopoverSections, feed: UpdatesState, mode: UpdatesPanelMode,
                           jump: @escaping (PopoverSections.Kind) -> Void) -> some View {
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

            if !sections.summary.isEmpty {
                summaryLine(sections.summary, jump: jump)
            }

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
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// "2 need you · 1 in review · 4 running": one button per section,
    /// each scrolling the list to it, so the counts read at a glance even
    /// when a section is below the fold.
    private func summaryLine(_ items: [PopoverSections.SummaryItem],
                             jump: @escaping (PopoverSections.Kind) -> Void) -> some View {
        // The words when they fit; else dot and count only (the tooltip
        // names the section), so a narrow popover never runs past its edge.
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 4) {
                ForEach(items) { item in
                    SummaryButton(item: item, compact: false) { jump(item.kind) }
                }
            }
            HStack(spacing: 4) {
                ForEach(items) { item in
                    SummaryButton(item: item, compact: true) { jump(item.kind) }
                }
            }
        }
        .font(.caption.weight(.medium))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Summary")
    }

    /// A section's color in the summary line and its header: Needs you and
    /// Running carry their state color; mixed sections stay neutral.
    static func sectionTextStyle(_ kind: PopoverSections.Kind) -> AnyShapeStyle {
        switch kind {
        case .needsYou: return AnyShapeStyle(SessionColor.needsYouText)
        case .running: return AnyShapeStyle(SessionColor.runningText)
        case .review, .unconcluded, .snoozed, .done: return AnyShapeStyle(.secondary)
        }
    }

    /// The dot matches its rows' dots: Review is teal while any of its
    /// sessions still runs, else amber like its closed rows.
    static func summaryDotColor(_ item: PopoverSections.SummaryItem) -> Color {
        switch item.kind {
        case .needsYou: return SessionColor.needsYou
        case .running: return SessionColor.running
        case .review: return item.anyAlive ? SessionColor.reviewOpen : SessionColor.closed
        // Gray: the quiet, collapsed bucket, never mistaken for Review.
        case .unconcluded, .snoozed, .done: return .secondary
        }
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
                .foregroundStyle(Self.sectionTextStyle(section.kind))
                .padding(.horizontal, 10)
                .padding(.top, 4)
                .help(Self.sectionHelp(section.kind))
            ForEach(section.sessions) { session in
                SessionRowView(session: session, store: store)
                    .padding(.horizontal, 4)
            }
        }
        .id(section.kind)
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
        return DisclosureGroup(isExpanded: expanded) {
            ForEach(section.sessions) { session in
                SessionRowView(session: session, store: store)
                    .padding(.horizontal, 4)
            }
        } label: {
            // Auto-expand fits the header, so the section is never out of
            // sight, but not its rows: expanding it never grows the popover.
            Text(sections.header(for: section))
                .reportsContentBottom()
        }
        .font(.caption.bold())
        .foregroundStyle(.secondary)
        .padding(.horizontal, 10)
        .help("Sessions that ended or stopped without finishing; click to show or hide them")
        .id(section.kind)
        // Auto-expand fits it only while filtering, so matches are not hidden.
        .reportsContentBottom(sections.isFiltering)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 4) {
            // Plan usage, one line per source, above the button row.
            UsageLineView(usage: store.snapshot.usage)
            HStack {
                FooterButton(title: "Open Dashboard", help: "Open the dashboard window with every session (⌃⌥⌘A)") {
                    DashboardWindowController.show()
                }
                Spacer()
                FooterButton(title: "Refresh", help: "Reload every session's state now") {
                    Task { await store.refresh() }
                }
                GearMenu(store: store, closesHostWindow: true)
            }
            // Room for the corner grip, which overlays the bottom-right corner.
            .padding(.trailing, Self.gripSize)
        }
        .padding(8)
        .buttonStyle(.plain)
        .font(.caption)
    }
}

/// One count in the summary line: a dot in its rows' color and the text,
/// with a capsule and the pointing hand on hover so it reads as a control.
private struct SummaryButton: View {
    let item: PopoverSections.SummaryItem
    /// Dot and count only, for a popover too narrow for the words.
    let compact: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Circle()
                    .fill(MenuBarView.summaryDotColor(item))
                    .frame(width: 6, height: 6)
                Text(compact ? "\(item.count)" : item.text)
                    .monospacedDigit()
                    .foregroundStyle(MenuBarView.sectionTextStyle(item.kind))
            }
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(Capsule().fill(.quaternary.opacity(hovering ? 0.9 : 0)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { inside in
            guard inside != hovering else { return }
            hovering = inside
            if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() }
        }
        .onDisappear {
            if hovering { NSCursor.pop() }
            hovering = false
        }
        .help("Scroll to \(item.kind.title) (\(item.count))")
        .accessibilityLabel(item.text)
    }
}

/// A footer action (Open Dashboard, Refresh): plain text at rest, with
/// the same hover capsule and pointing hand as `SummaryButton`, so it reads
/// as a control and not as a label.
private struct FooterButton: View {
    let title: String
    let help: String
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Text(title)
                .padding(.horizontal, 5)
                .padding(.vertical, 2)
                .background(Capsule().fill(.quaternary.opacity(hovering ? 0.9 : 0)))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { inside in
            guard inside != hovering else { return }
            hovering = inside
            if inside { NSCursor.pointingHand.push() } else { NSCursor.pop() }
        }
        .onDisappear {
            if hovering { NSCursor.pop() }
            hovering = false
        }
        .help(help)
    }
}

/// The bottom of the content auto-expand must fit, in the list's
/// coordinate space: the largest bottom edge any reporting view sends.
private struct ContentBottomKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

private extension View {
    /// Reports this view's bottom edge to auto-expand when `active`.
    func reportsContentBottom(_ active: Bool = true) -> some View {
        background {
            if active {
                GeometryReader { proxy in
                    Color.clear.preference(key: ContentBottomKey.self,
                                           value: proxy.frame(in: .named(MenuBarView.listSpace)).maxY)
                }
            }
        }
    }
}

/// Three diagonal strokes in the bottom-right corner, the classic resize grip.
private struct ResizeGripShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        for fraction in [0.0, 0.4, 0.8] {
            path.move(to: CGPoint(x: rect.minX + rect.width * fraction, y: rect.maxY))
            path.addLine(to: CGPoint(x: rect.maxX, y: rect.minY + rect.height * fraction))
        }
        return path
    }

    /// The diagonal resize cursor (macOS 15 and later), or left-right for
    /// a width-only drag or on macOS 14.
    @MainActor
    static func cursor(widthOnly: Bool) -> NSCursor {
        if widthOnly { return .resizeLeftRight }
        if #available(macOS 15, *) {
            return .frameResize(position: .bottomRight, directions: .all)
        }
        return .resizeUpDown
    }
}
