import SwiftUI

struct MenuBarLabel: View {
    @ObservedObject var store: SnapshotStore
    @ObservedObject var attention: AttentionController
    @StateObject private var systemAppearance = SystemAppearanceMonitor()
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismissWindow) private var dismissWindow
    @AppStorage(SettingsKeys.notifyOnNeedsYou) private var notifyOnNeedsYou: Bool = false
    @AppStorage(SettingsKeys.notifyOnTurnFinished) private var notifyOnTurnFinished: Bool = false
    /// Runs on every snapshot even with the toggle off, so turning it on
    /// later never replays old turns.
    @State private var turns = TurnFinishDetector()
    @AppStorage(MenuBarIconSettings.coloredKey) private var colored: Bool = true
    @AppStorage(MenuBarIconSettings.flashKey) private var flash: Bool = true
    @AppStorage(MenuBarIconSettings.flashStopKey) private var flashStop: String = FlashStopPolicy.onOpen.rawValue

    private var urgentCount: Int { store.snapshot.counts.needsYou + store.snapshot.counts.interrupted }
    private var reviewCount: Int { store.snapshot.counts.review }

    /// "AgentBoard: 2 need you, 1 to review", or "AgentBoard: nothing needs you".
    private var tooltip: String {
        var parts: [String] = []
        if urgentCount > 0 { parts.append("\(urgentCount) need\(urgentCount == 1 ? "s" : "") you") }
        if reviewCount > 0 { parts.append("\(reviewCount) to review") }
        let summary = parts.isEmpty ? "nothing needs you" : parts.joined(separator: ", ")
        return "AgentBoard: \(summary). Click to show sessions; ⌃⌥⌘A opens the dashboard."
    }

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
        .help(tooltip)
        .onAppear {
            WindowOpener.shared.open = { openWindow(id: DashboardWindowController.windowID) }
            WindowOpener.shared.dismiss = { dismissWindow(id: DashboardWindowController.windowID) }
            WindowOpener.shared.openHelp = { openWindow(id: HelpWindowController.windowID) }
            let store = self.store
            NotificationManager.shared.onOpenSession = { key in
                // Not ready until the first snapshot: the click waits in
                // pendingOpenKey and the snapshot handler below opens it.
                guard store.lastUpdated != nil else { return false }
                Self.openSession(key, store: store)
                return true
            }
            if notifyOnNeedsYou || notifyOnTurnFinished {
                NotificationManager.shared.requestAuthorizationIfNeeded()
            }
            applySettings()
            store.start()
        }
        .onChange(of: store.snapshot) { _, newValue in
            let notifications = NotificationManager.shared
            let entered = attention.note(snapshot: newValue)
            notifications.notify(newlyNeedingYou: entered, in: newValue, enabled: notifyOnNeedsYou)
            notifications.notify(
                turns: turns.note(newValue), enabled: notifyOnTurnFinished,
                context: .init(dashboardIsKey: attention.dashboardIsKey,
                               dashboardSelection: store.dashboardSelection)
            )
            if let key = notifications.takePendingOpenKey() {
                Self.openSession(key, store: store)
            }
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

    /// A clicked notification: focus the session's terminal when it has
    /// one, otherwise open the dashboard with the session selected (it
    /// selects a key a later snapshot brings in, too). A focus that fails
    /// at runtime (the tab was closed, the terminal quit, macOS stayed on
    /// this desktop) opens the dashboard too, with the reason in its banner,
    /// so the click never looks dead.
    private static func openSession(_ key: String, store: SnapshotStore) {
        if let session = store.snapshot.sessions.first(where: { $0.key == key }), session.isFocusable {
            Task { await store.focusOrShowDashboard(session) }
        } else {
            store.requestSelection(key)
            DashboardWindowController.show()
        }
    }

    private func applySettings() {
        // Without color there is nothing to flash.
        attention.flashEnabled = colored && flash
        attention.policy = FlashStopPolicy(rawValue: flashStop) ?? .onOpen
    }
}
