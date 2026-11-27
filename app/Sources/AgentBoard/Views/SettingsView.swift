import AppKit
import SwiftUI

/// The Settings window, laid out like System Settings: a top bar with the
/// five tabs (`SettingsTab`) and, beside them, a search field. A query
/// opens a popup list of matching settings under the field, over the
/// selected tab; picking one goes to its tab and highlights its row
/// (`SettingsNavigator`).
struct SettingsView: View {
    @ObservedObject var store: SnapshotStore
    @ObservedObject private var navigator = SettingsNavigator.shared
    @FocusState private var searchFocused: Bool
    /// The Settings window, so opening it can leave nothing focused.
    @State private var window: NSWindow?
    /// Set on each open and spent the first time the window becomes key.
    @State private var clearFocusOnNextKey = false
    @State private var keyMonitor = SettingsKeyMonitor()

    /// Fixed so the five tabs and the search field fit side by side; the
    /// height follows the pane, up to most of the screen (the Machines pane
    /// scrolls past that).
    static let width: CGFloat = 580
    /// The top bar's height, which the results popup hangs below.
    static let topBarHeight: CGFloat = 56

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                SettingsTabBar(selection: navigator.tab) { tab in
                    navigator.query = ""
                    navigator.select(tab)
                }
                Spacer(minLength: 8)
                SettingsSearchField(
                    query: $navigator.query, focused: $searchFocused, onSubmit: revealSelection
                )
            }
            .padding(.horizontal, 12)
            .frame(height: Self.topBarHeight)
            // A click on the bar's empty part is outside the popup too
            // (the tabs and the field take their own clicks).
            .background {
                Color.black.opacity(0.001)
                    .contentShape(Rectangle())
                    .onTapGesture { navigator.query = "" }
                    .accessibilityHidden(true)
            }
            Divider()
            pane(for: navigator.tab)
                .overlay(alignment: .topTrailing) {
                    if !SettingsSearch(navigator.query).isEmpty {
                        SettingsSearchPopup(navigator: navigator, reveal: reveal)
                    }
                }
        }
        .navigationTitle(navigator.tab.title)
        // Picking a tab (or a deep link) ends a search.
        .onChange(of: navigator.tab) { _, _ in
            if !navigator.query.isEmpty { navigator.query = "" }
        }
        .frame(width: Self.width)
        .background(WindowReader { window = $0 })
        .onAppear {
            clearFocusOnNextKey = true
            clearInitialFocus()
            startKeyMonitor()
        }
        // AppKit picks the first key view (the search field, or with
        // keyboard navigation a tab button) when the window first becomes
        // key, which can be after onAppear; clear it then too.
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didBecomeKeyNotification)) { note in
            guard let window, note.object as? NSWindow === window else { return }
            startKeyMonitor()
            guard clearFocusOnNextKey else { return }
            clearFocusOnNextKey = false
            clearInitialFocus()
        }
        // Closing re-arms the clear for the next open, so it does not
        // depend on SwiftUI tearing this view down and running onAppear
        // again: the next time the window becomes key is that reopen.
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.willCloseNotification)) { note in
            guard let window, note.object as? NSWindow === window else { return }
            clearFocusOnNextKey = true
            keyMonitor.stop()
            // The next open shows no stale results popup.
            navigator.query = ""
        }
    }

    @ViewBuilder
    private func pane(for tab: SettingsTab) -> some View {
        switch tab {
        case .general: SettingsGeneralPane()
        case .menuBar: SettingsMenuBarPane()
        case .notifications: SettingsNotificationsPane()
        case .machines:
            SettingsPane(tab: .machines) {
                MachinesSettingsSection(store: store)
            }
        case .advanced: SettingsAdvancedPane(store: store)
        }
    }

    /// The tallest a pane grows before it scrolls: most of the screen,
    /// less the title bar and the top bar.
    static var maxPaneHeight: CGFloat {
        ((NSScreen.main?.visibleFrame.height) ?? 900) - 160
    }

    /// Opening Settings focuses nothing: the window itself takes first
    /// responder, so no control wears a focus ring it was never given (and
    /// that a click on the form's background would not clear). Tab or ⌘F
    /// still move into the controls from there. `defaultFocus` cannot do
    /// this: it needs a focusable target, which would wear the ring instead.
    private func clearInitialFocus() {
        DispatchQueue.main.async {
            searchFocused = false
            window?.makeFirstResponder(nil)
        }
    }

    private func reveal(_ item: SettingsItem) {
        navigator.reveal(item)
        searchFocused = false
    }

    private func revealSelection() {
        if navigator.revealSelection() { searchFocused = false }
    }

    private func startKeyMonitor() {
        keyMonitor.window = window
        keyMonitor.start(
            queryEmpty: { navigator.query.isEmpty },
            clearQuery: { navigator.query = "" },
            focusSearch: { searchFocused = true },
            moveSelection: { navigator.moveSelection(by: $0) }
        )
    }
}

/// The five tabs as toolbar-style buttons: symbol over title, the
/// selected one tinted.
private struct SettingsTabBar: View {
    let selection: SettingsTab
    let select: (SettingsTab) -> Void

    var body: some View {
        HStack(spacing: 2) {
            ForEach(SettingsTab.allCases) { tab in
                let selected = tab == selection
                Button { select(tab) } label: { // not-searchable: a tab, not a setting
                    VStack(spacing: 2) {
                        Image(systemName: tab.symbol)
                            .font(.system(size: 17))
                            .frame(height: 22)
                        Text(tab.title)
                            .font(.caption)
                            .lineLimit(1)
                            .fixedSize()
                    }
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 4)
                    .background(
                        RoundedRectangle(cornerRadius: 6)
                            .fill(Color.primary.opacity(selected ? 0.08 : 0))
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Show the \(tab.title) settings")
                .accessibilityLabel(tab.title)
                .accessibilityAddTraits(selected ? [.isSelected] : [])
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Settings tabs")
    }
}

/// A search field with the usual magnifying glass and, once something is
/// typed, a clear button.
private struct SettingsSearchField: View {
    @Binding var query: String
    var focused: FocusState<Bool>.Binding
    let onSubmit: () -> Void

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            TextField("Search", text: $query) // not-searchable: the search field itself
                .textFieldStyle(.plain)
                .focused(focused)
                .onSubmit(onSubmit)
                .help("Find a setting by its name, its description, its section or tab, or its defaults key; pick a match to go to it")
                .accessibilityLabel("Search settings")
            if !query.isEmpty {
                Button { query = "" } label: { // not-searchable: clears the search field
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Clear the search")
                .accessibilityLabel("Clear search")
            }
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .strokeBorder(focused.wrappedValue ? Color.accentColor.opacity(0.6) : Color(nsColor: .separatorColor),
                              lineWidth: focused.wrappedValue ? 2 : 1)
        )
        .frame(width: 180)
    }
}

/// The popup list of matching settings, hanging under the search field
/// over the selected tab: each row is the setting's name and its
/// `Tab → Section` path, the selected one (arrow keys, hover) tinted.
/// A click outside it closes it and clears the search.
private struct SettingsSearchPopup: View {
    @ObservedObject var navigator: SettingsNavigator
    let reveal: (SettingsItem) -> Void

    static let width: CGFloat = 300
    /// The rows' full height, so the popup is no taller than its rows.
    @State private var contentHeight: CGFloat = 0

    var body: some View {
        let rows = navigator.results
        GeometryReader { geometry in
            ZStack(alignment: .topTrailing) {
                Color.black.opacity(0.001)
                    .contentShape(Rectangle())
                    .onTapGesture { navigator.query = "" }
                    .accessibilityHidden(true)
                list(rows, maxHeight: max(geometry.size.height - 12, 80))
                    .frame(width: Self.width)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color(nsColor: .separatorColor)))
                    .shadow(color: .black.opacity(0.2), radius: 8, y: 3)
                    .padding(.top, 4)
                    .padding(.trailing, 12)
            }
        }
    }

    @ViewBuilder
    private func list(_ rows: [SettingsItem], maxHeight: CGFloat) -> some View {
        if rows.isEmpty {
            Text("No settings match")
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(rows) { item in
                            row(item)
                        }
                    }
                    .padding(5)
                    .background(GeometryReader { geometry in
                        Color.clear.preference(key: SettingsPopupHeightKey.self, value: geometry.size.height)
                    })
                }
                .frame(height: min(contentHeight > 0 ? contentHeight : maxHeight, maxHeight))
                .onPreferenceChange(SettingsPopupHeightKey.self) { contentHeight = $0 }
                // Only a keyboard move scrolls: scrolling to a row the
                // pointer picked would slide another under the pointer.
                .onChange(of: navigator.selectedResult) { _, id in
                    if let id, navigator.selectionScrolls { proxy.scrollTo(id) }
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Matching settings")
        }
    }

    private func row(_ item: SettingsItem) -> some View {
        let selected = navigator.selectedResult == item.id
        return Button { reveal(item) } label: { // not-searchable: a search result
            VStack(alignment: .leading, spacing: 1) {
                Text(item.label)
                    .lineLimit(1)
                Text(item.path)
                    .font(.caption)
                    .foregroundStyle(selected ? Color.white.opacity(0.85) : Color.secondary)
                    .lineLimit(1)
            }
            .foregroundStyle(selected ? Color.white : Color.primary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 5).fill(selected ? Color.accentColor : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(item.help)
        .onHover { inside in if inside { navigator.selectResult(item.id) } }
        .accessibilityLabel("\(item.label), \(item.path)")
        .accessibilityHint("Goes to this setting")
        .accessibilityAddTraits(selected ? [.isSelected] : [])
        .id(item.id)
    }
}

private struct SettingsPopupHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

/// A local key-down monitor scoped to the Settings window, for ⌘F, Escape,
/// Tab and the popup's arrow keys (`SettingsKeyAction`).
@MainActor
final class SettingsKeyMonitor {
    weak var window: NSWindow?
    private var monitor: Any?

    func start(
        queryEmpty: @escaping @MainActor () -> Bool,
        clearQuery: @escaping @MainActor () -> Void,
        focusSearch: @escaping @MainActor () -> Void,
        moveSelection: @escaping @MainActor (Int) -> Void
    ) {
        stop()
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let window = self?.window, event.window === window else { return event }
            switch SettingsKeyAction.action(
                keyCode: event.keyCode, characters: event.charactersIgnoringModifiers,
                modifiers: event.modifierFlags, queryEmpty: queryEmpty()
            ) {
            case .clearQuery:
                clearQuery()
                return nil
            case .focusSearch:
                focusSearch()
                return nil
            case .moveSelection(let delta):
                moveSelection(delta)
                return nil
            case .clearQueryAndPass:
                clearQuery()
                return event
            case nil:
                return event
            }
        }
    }

    func stop() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }
}
