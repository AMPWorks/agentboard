import SwiftUI

/// Settings → Notifications: the two notification toggles and Sounds. One
/// pane owns both, since a sound row's default follows its toggle.
struct SettingsNotificationsPane: View {
    @AppStorage(SettingsKeys.notifyOnNeedsYou) private var notifyOnNeedsYou: Bool = false
    @AppStorage(SettingsKeys.notifyOnTurnFinished) private var notifyOnTurnFinished: Bool = false
    // Empty means never picked: the row shows `AttentionEvent.choice`'s
    // default, which follows the matching notification toggle.
    @AppStorage(SettingsKeys.soundNeedsYou) private var soundNeedsYou: String = ""
    @AppStorage(SettingsKeys.soundTurnFinished) private var soundTurnFinished: String = ""
    @AppStorage(SettingsKeys.soundDenied) private var soundDenied: String = ""
    @AppStorage(SettingsKeys.soundReviewReady) private var soundReviewReady: String = ""
    @State private var systemSounds: [String] = SoundChoice.systemSoundNames()

    var body: some View {
        SettingsPane(tab: .notifications) {
            Section {
                Toggle(SettingsCatalog.notifyOnNeedsYou.label, isOn: $notifyOnNeedsYou)
                    .onChange(of: notifyOnNeedsYou) { _, newValue in
                        if newValue { NotificationManager.shared.requestAuthorizationIfNeeded() }
                    }
                    .help(SettingsCatalog.notifyOnNeedsYou.help)
                    .settingsAnchor(SettingsCatalog.notifyOnNeedsYou)
                Toggle(SettingsCatalog.notifyOnTurnFinished.label, isOn: $notifyOnTurnFinished)
                    .onChange(of: notifyOnTurnFinished) { _, newValue in
                        if newValue { NotificationManager.shared.requestAuthorizationIfNeeded() }
                    }
                    .help(SettingsCatalog.notifyOnTurnFinished.help)
                    .settingsAnchor(SettingsCatalog.notifyOnTurnFinished)
                Text("Clicking a notification focuses the session's terminal. Loop sessions idling between ticks and turns still waiting on background tasks never notify. A turn that ends needing you notifies only through the needs-you toggle.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                SettingsSectionHeader("Notifications")
            }

            Section {
                ForEach(AttentionEvent.allCases) { event in
                    SoundSettingsRow(event: event, stored: stored(event),
                                     notifyOn: notifyOn(event), systemSounds: systemSounds)
                        .settingsAnchor(SettingsCatalog.sound(event))
                }
                Text("Sounds play even when notifications are off or not allowed, and Focus does not silence them; notification banners themselves are silent. Until you pick one, Needs you and Turn finished play the alert sound only while their notification toggle is on. Nothing plays for sessions already waiting when AgentBoard starts.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                SettingsSectionHeader("Sounds")
            }
        }
    }

    private func stored(_ event: AttentionEvent) -> Binding<String> {
        switch event {
        case .needsYou: return $soundNeedsYou
        case .turnFinished: return $soundTurnFinished
        case .denied: return $soundDenied
        case .reviewReady: return $soundReviewReady
        }
    }

    /// The toggle that sets the event's default; none for the others.
    private func notifyOn(_ event: AttentionEvent) -> Bool {
        switch event {
        case .needsYou: return notifyOnNeedsYou
        case .turnFinished: return notifyOnTurnFinished
        case .denied, .reviewReady: return false
        }
    }
}
