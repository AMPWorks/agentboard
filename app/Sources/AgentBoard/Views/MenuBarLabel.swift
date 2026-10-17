import SwiftUI

struct MenuBarLabel: View {
    @ObservedObject var store: SnapshotStore
    @ObservedObject var attention: AttentionController
    @StateObject private var systemAppearance = SystemAppearanceMonitor()
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismissWindow) private var dismissWindow
    @AppStorage("notifyOnNeedsYou") private var notifyOnNeedsYou: Bool = false
    @AppStorage(MenuBarIconSettings.coloredKey) private var colored: Bool = true
    @AppStorage(MenuBarIconSettings.flashKey) private var flash: Bool = true
    @AppStorage(MenuBarIconSettings.flashStopKey) private var flashStop: String = FlashStopPolicy.onOpen.rawValue

    private var urgentCount: Int { store.snapshot.counts.needsYou + store.snapshot.counts.interrupted }
    private var reviewCount: Int { store.snapshot.counts.review }

    private var tier: MenuBarIconState.Tier {
        MenuBarIconState.tier(for: store.snapshot.counts, sessions: store.snapshot.sessions)
    }

    /// Flashing (or, under Reduce motion, the alert badge) only applies to
    /// the needs_you tier; otherwise the glyph is a solid color.
    private var style: MenuBarIconStyle {
        let tier = self.tier
        let attending = tier == .needsYou && attention.isFlashing
        return MenuBarIconStyle(
            tier: tier,
            dimmed: attending && !attention.reduceMotion && !attention.flashPhase,
            alert: attending && attention.reduceMotion,
            darkMenuBar: systemAppearance.isDark
        )
    }

    var body: some View {
        HStack(spacing: 3) {
            icon
            if urgentCount > 0 {
                Text("\(urgentCount)")
            }
            if reviewCount > 0 {
                Text("\(reviewCount)")
                    .foregroundStyle(.secondary)
                    .font(.caption2)
            }
        }
        .onAppear {
            WindowOpener.shared.open = { openWindow(id: DashboardWindowController.windowID) }
            WindowOpener.shared.dismiss = { dismissWindow(id: DashboardWindowController.windowID) }
            applySettings()
            store.start()
        }
        .onChange(of: store.snapshot) { _, newValue in
            let entered = attention.note(snapshot: newValue)
            NotificationManager.shared.notify(newlyNeedingYou: entered, in: newValue, enabled: notifyOnNeedsYou)
        }
        .onChange(of: colored) { applySettings() }
        .onChange(of: flash) { applySettings() }
        .onChange(of: flashStop) { applySettings() }
    }

    /// The colored, non-template glyph, or today's template glyph when
    /// coloring is off or nothing is worth coloring (idle tier).
    @ViewBuilder
    private var icon: some View {
        if colored, let image = MenuBarIconRenderer.image(for: style) {
            Image(nsImage: image)
        } else {
            Image(systemName: MenuBarIconRenderer.symbolName)
                .symbolVariant(urgentCount > 0 ? .fill : .none)
        }
    }

    private func applySettings() {
        // Without color there is nothing to flash.
        attention.flashEnabled = colored && flash
        attention.policy = FlashStopPolicy(rawValue: flashStop) ?? .onOpen
    }
}
