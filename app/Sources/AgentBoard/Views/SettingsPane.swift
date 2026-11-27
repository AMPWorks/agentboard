import SwiftUI

/// One Settings tab's grouped form. The form does not scroll itself: it
/// reports its full height, so the window fits the selected tab, and an
/// outer scroll view takes over past `SettingsView.maxPaneHeight` (the
/// Machines tab with several Macs). It scrolls to the navigator's
/// highlighted row when that row is on this tab, and clears the highlight
/// about 3 s later.
struct SettingsPane<Content: View>: View {
    let tab: SettingsTab
    @ViewBuilder let content: () -> Content
    @ObservedObject private var navigator = SettingsNavigator.shared
    @State private var height: CGFloat = 200

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Form {
                    content()
                }
                .formStyle(.grouped)
                .scrollDisabled(true)
                .fixedSize(horizontal: false, vertical: true)
                .background(GeometryReader { geometry in
                    Color.clear.preference(key: SettingsPaneHeightKey.self, value: geometry.size.height)
                })
            }
            .frame(height: min(height, SettingsView.maxPaneHeight))
            .onPreferenceChange(SettingsPaneHeightKey.self) { value in
                if value > 0 { height = value }
            }
            .onAppear { reveal(navigator.highlighted, proxy: proxy) }
            .onChange(of: navigator.highlighted) { _, id in reveal(id, proxy: proxy) }
        }
    }

    private func reveal(_ id: SettingsItem.ID?, proxy: ScrollViewProxy) {
        guard let id, let item = SettingsCatalog.item(id: id), item.tab == tab else { return }
        // After the tab switch has laid the pane out. The fallback row
        // first, then the item's own row when it is there (scrolling to an
        // id not on screen does nothing).
        DispatchQueue.main.async {
            if let fallback = item.fallback { proxy.scrollTo(fallback, anchor: .center) }
            proxy.scrollTo(id, anchor: .center)
        }
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(3))
            navigator.clearHighlight(id)
        }
    }
}

private struct SettingsPaneHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

/// A section title with a ? button that opens Help at `topic`.
struct SettingsSectionHeader: View {
    let title: String
    let topic: HelpTopic.ID

    init(_ title: String, topic: HelpTopic.ID = .settings) {
        self.title = title
        self.topic = topic
    }

    var body: some View {
        HStack {
            Text(title)
            Spacer()
            HelpButton(topic: topic, tooltip: "Explain the \(title) settings in AgentBoard Help")
        }
    }
}

/// Gives a catalogued row its scroll id and, while the navigator
/// highlights it, a fading accent background (shown and removed without
/// the fade under Reduce motion). `standingIn`: the row also lights up for
/// an item whose `fallback` it is, while that item's row is not on screen.
/// `scrolls: false` leaves out the scroll id, for an item several rows
/// stand for (the paired Macs), so every row keeps one identity.
private struct SettingsAnchor: ViewModifier {
    let item: SettingsItem
    let standingIn: Bool
    let scrolls: Bool
    @ObservedObject private var navigator = SettingsNavigator.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @ViewBuilder
    func body(content: Content) -> some View {
        let highlighted = navigator.highlighted
        let on = highlighted == item.id
            || (standingIn && highlighted.flatMap(SettingsCatalog.item(id:))?.fallback == item.id)
        let marked = content
            .background(
                RoundedRectangle(cornerRadius: 5)
                    .fill(Color.accentColor.opacity(on ? 0.22 : 0))
                    .padding(-4)
                    .animation(reduceMotion ? nil : .easeOut(duration: 0.6), value: on)
            )
        if scrolls {
            marked.id(item.id)
        } else {
            marked
        }
    }
}

extension View {
    /// See `SettingsAnchor`.
    func settingsAnchor(_ item: SettingsItem, standingIn: Bool = false, scrolls: Bool = true) -> some View {
        modifier(SettingsAnchor(item: item, standingIn: standingIn, scrolls: scrolls))
    }
}
