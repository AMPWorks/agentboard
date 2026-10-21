import AppKit
import SwiftUI

struct SettingsView: View {
    @ObservedObject var store: SnapshotStore
    @AppStorage("appearance") private var appearance: String = Appearance.system.rawValue
    @AppStorage("notifyOnNeedsYou") private var notifyOnNeedsYou: Bool = false
    @AppStorage(MenuBarIconSettings.coloredKey) private var menuBarIconColored: Bool = true
    @AppStorage(MenuBarIconSettings.flashKey) private var menuBarIconFlash: Bool = true
    @AppStorage(MenuBarIconSettings.flashStopKey) private var menuBarIconFlashStop: String = FlashStopPolicy.onOpen.rawValue
    @AppStorage(AgentBoardCLI.pathDefaultsKey) private var cliPath: String = ""
    @AppStorage(AgentBoardCLI.useFixtureDefaultsKey) private var useFixture: Bool = false
    @AppStorage(AgentBoardCLI.fixturePathDefaultsKey) private var fixturePath: String = ""
    @AppStorage(CLIRunner.verboseLoggingKey) private var verboseLogging: Bool = false
    @State private var launchAtLogin: Bool = false

    /// `<data dir>/agentboard.log`, once the store has resolved the dir.
    private var logURL: URL? {
        store.dataDirectory.map { URL(fileURLWithPath: $0).appendingPathComponent("agentboard.log") }
    }

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

            Section("Menu bar icon") {
                Toggle("Color the menu bar icon by session state", isOn: $menuBarIconColored)
                Toggle("Flash when a session needs you", isOn: $menuBarIconFlash)
                    .disabled(!menuBarIconColored)
                Picker("Stop flashing", selection: $menuBarIconFlashStop) {
                    ForEach(FlashStopPolicy.allCases) { policy in
                        Text(policy.label).tag(policy.rawValue)
                    }
                }
                .disabled(!menuBarIconColored || !menuBarIconFlash)
                Text("The icon takes the color of the most urgent session. With Reduce motion on, it shows an exclamation badge instead of flashing.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
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

            Section("Diagnostics") {
                Toggle("Verbose logging", isOn: $verboseLogging)
                Text("Also logs every background snapshot and the CLI's debug detail (hook events, each focus step). Read it back with `agentboard logs`.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Open log") {
                    if let logURL { NSWorkspace.shared.activateFileViewerSelecting([logURL]) }
                }
                .disabled(!(logURL.map { FileManager.default.fileExists(atPath: $0.path) } ?? false))
            }
        }
        .formStyle(.grouped)
        .frame(width: 420)
        .padding()
        .onAppear { launchAtLogin = LaunchAtLogin.isEnabled }
    }
}
