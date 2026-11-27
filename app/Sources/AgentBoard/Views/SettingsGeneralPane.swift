import SwiftUI

/// Settings → General: Appearance, and the General section (Launch at
/// login, Show plan usage, Show closed and merged PRs).
struct SettingsGeneralPane: View {
    @AppStorage(SettingsKeys.appearance) private var appearance: String = Appearance.system.rawValue
    @AppStorage(SettingsKeys.showPlanUsage) private var showPlanUsage: Bool = true
    @AppStorage(SettingsKeys.showClosedAndMergedPRs) private var showClosedAndMergedPRs: Bool = false
    @State private var launchAtLogin: Bool = false

    var body: some View {
        SettingsPane(tab: .general) {
            Section {
                Picker(SettingsCatalog.appearance.label, selection: $appearance) {
                    ForEach(Appearance.allCases) { option in
                        Text(option.rawValue).tag(option.rawValue)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: appearance) { _, newValue in
                    (Appearance(rawValue: newValue) ?? .system).apply()
                }
                .help(SettingsCatalog.appearance.help)
                .settingsAnchor(SettingsCatalog.appearance)
            } header: {
                SettingsSectionHeader("Appearance")
            }

            Section {
                Toggle(SettingsCatalog.launchAtLogin.label, isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, newValue in
                        LaunchAtLogin.setEnabled(newValue)
                    }
                    .help(SettingsCatalog.launchAtLogin.help)
                    .settingsAnchor(SettingsCatalog.launchAtLogin)
                Toggle(SettingsCatalog.showPlanUsage.label, isOn: $showPlanUsage)
                    .help(SettingsCatalog.showPlanUsage.help)
                    .settingsAnchor(SettingsCatalog.showPlanUsage)
                Toggle(SettingsCatalog.showClosedAndMergedPRs.label, isOn: $showClosedAndMergedPRs)
                    .help(SettingsCatalog.showClosedAndMergedPRs.help)
                    .settingsAnchor(SettingsCatalog.showClosedAndMergedPRs)
            } header: {
                SettingsSectionHeader("General")
            }
        }
        .onAppear { launchAtLogin = LaunchAtLogin.isEnabled }
    }
}
