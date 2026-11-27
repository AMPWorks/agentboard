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
    /// Settings → Notifications → Sounds; plays even with the notification toggles off.
    @State private var sounds = SoundEventDetector()
    @AppStorage(MenuBarIconSettings.coloredKey) private var colored: Bool = true
    @AppStorage(MenuBarIconSettings.flashKey) private var flash: Bool = true
    @AppStorage(MenuBarIconSettings.flashStopKey) private var flashStop: String = FlashStopPolicy.onOpen.rawValue
    @AppStorage(SettingsKeys.notifyRemoteSessions) private var notifyRemote: Bool = true
    @AppStorage(MenuBarIconSettings.remoteModeKey) private var remoteMode: String = RemoteIconMode.separate.rawValue

    /// Glyph, counts and badge for the current snapshot and Settings →
    /// Machines → Other Macs in the menu bar icon.
    private var summary: MenuBarIconState.Summary {
        MenuBarIconState.summary(store.snapshot, mode: RemoteIconMode.from(remoteMode))
    }

    /// "AgentBoard: 2 need you (1 on mini), 1 to review", or "AgentBoard: nothing needs you".
    private func tooltip(_ summary: MenuBarIconState.Summary) -> String {
        "\(summary.accessibilityDescription). Click to show sessions; ⌃⌥⌘A opens the dashboard."
    }

    /// Flashing (or, under Reduce motion, the alert badge) only applies to
    /// the needs_you tier; otherwise the glyph is a solid color.
    private func style(_ summary: MenuBarIconState.Summary) -> MenuBarIconStyle {
        let tier = summary.tier
        let attending = tier == .needsYou && attention.isFlashing
        return MenuBarIconStyle(
            tier: tier,
            dimmed: colored && attending && !attention.reduceMotion && !attention.flashPhase,
            alert: colored && attending && attention.reduceMotion,
            darkMenuBar: systemAppearance.isDark,
            remoteTier: summary.remoteTier,
            remoteDimmed: !attention.remotePulsePhase,
            monochrome: !colored
        )
    }

    var body: some View {
        let summary = self.summary
        HStack(spacing: 3) {
            icon(summary)
            if summary.urgentCount > 0 {
                Text("\(summary.urgentCount)")
            }
            if summary.reviewCount > 0 {
                Text("\(summary.reviewCount)")
                    .foregroundStyle(.secondary)
                    .font(.caption2)
            }
        }
        .help(tooltip(summary))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(summary.accessibilityDescription)
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
            // Both diffs are stateful (a second call returns nothing), so
            // each runs once and feeds the notifications and the sounds.
            let entered = attention.note(snapshot: newValue)
            var turnChanges = turns.note(newValue)
            if !notifyRemote { turnChanges.finished.removeAll(where: \.isRemote) }
            let context = SessionNotificationPlan.Context(
                dashboardIsKey: attention.dashboardIsKey,
                dashboardSelection: store.dashboardSelection
            )
            notifications.notify(newlyNeedingYou: entered, in: newValue, enabled: notifyOnNeedsYou)
            notifications.notify(turns: turnChanges, enabled: notifyOnTurnFinished, context: context)
            // `player.sounds` resolves a sound only for an event that fired.
            let player = SoundPlayer.shared
            let events = sounds.note(
                newValue, needsYouEntered: entered, turns: turnChanges, context: context,
                remoteAlerts: notifyRemote, sounds: player.sounds
            )
            if !events.isEmpty { player.play(events) }
            if let key = notifications.takePendingOpenKey() {
                Self.openSession(key, store: store)
            }
        }
        .onChange(of: colored) { applySettings() }
        .onChange(of: flash) { applySettings() }
        .onChange(of: flashStop) { applySettings() }
        .onChange(of: notifyRemote) { applySettings() }
        .onChange(of: remoteMode) { applySettings() }
    }

    /// The rendered glyph (colored, or a template image carrying the remote
    /// badge when coloring is off), or today's template glyph when there is
    /// nothing to render.
    @ViewBuilder
    private func icon(_ summary: MenuBarIconState.Summary) -> some View {
        if let image = MenuBarIconRenderer.image(for: style(summary), description: summary.accessibilityDescription) {
            Image(nsImage: image)
        } else {
            Image(systemName: MenuBarIconRenderer.symbolName)
                .symbolVariant(summary.urgentCount > 0 ? .fill : .none)
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
        attention.remoteAlerts = notifyRemote
        attention.remoteMode = RemoteIconMode.from(remoteMode)
    }
}
