import SwiftUI

struct SettingsView: View {
    @AppStorage("appearance") private var appearance: String = Appearance.system.rawValue
    @AppStorage("notifyOnNeedsYou") private var notifyOnNeedsYou: Bool = false
    @AppStorage(AgentBoardCLI.pathDefaultsKey) private var cliPath: String = ""
    @AppStorage(AgentBoardCLI.useFixtureDefaultsKey) private var useFixture: Bool = false
    @AppStorage(AgentBoardCLI.fixturePathDefaultsKey) private var fixturePath: String = ""
    @State private var launchAtLogin: Bool = false

    var body: some View {
        Form {
            Section("Appearance") {
                Picker("Appearance", selection: $appearance) {
                    ForEach(Appearance.allCases) { option in
                        Text(option.rawValue).tag(option.rawValue)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: appearance) { _, newValue in
                    (Appearance(rawValue: newValue) ?? .system).apply()
                }
            }

            Section("Notifications") {
                Toggle("Notify when a session needs you", isOn: $notifyOnNeedsYou)
                    .onChange(of: notifyOnNeedsYou) { _, newValue in
                        if newValue { NotificationManager.shared.requestAuthorizationIfNeeded() }
                    }
            }

            Section("General") {
                Toggle("Launch at login", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, newValue in
                        LaunchAtLogin.setEnabled(newValue)
                    }
            }

            Section("CLI & Data") {
                TextField("agentboard path override (optional)", text: $cliPath)
                Text("AgentBoard looks for the CLI in its own bundle, then $PATH, then this override.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Toggle("Use fixture data (development)", isOn: $useFixture)
                if useFixture {
                    TextField("Fixture path (optional, uses bundled fixture if empty)", text: $fixturePath)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 420)
        .padding()
        .onAppear { launchAtLogin = LaunchAtLogin.isEnabled }
    }
}
