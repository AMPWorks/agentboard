import AppKit
import SwiftUI

struct SettingsView: View {
    @ObservedObject var store: SnapshotStore
    @AppStorage(SettingsKeys.appearance) private var appearance: String = Appearance.system.rawValue
    @AppStorage(SettingsKeys.notifyOnNeedsYou) private var notifyOnNeedsYou: Bool = false
    @AppStorage(MenuBarIconSettings.coloredKey) private var menuBarIconColored: Bool = true
    @AppStorage(MenuBarIconSettings.flashKey) private var menuBarIconFlash: Bool = true
    @AppStorage(MenuBarIconSettings.flashStopKey) private var menuBarIconFlashStop: String = FlashStopPolicy.onOpen.rawValue
    @AppStorage(AgentBoardCLI.pathDefaultsKey) private var cliPath: String = ""
    @AppStorage(AgentBoardCLI.useFixtureDefaultsKey) private var useFixture: Bool = false
    @AppStorage(AgentBoardCLI.fixturePathDefaultsKey) private var fixturePath: String = ""
    @AppStorage(CLIRunner.verboseLoggingKey) private var verboseLogging: Bool = false
    @State private var launchAtLogin: Bool = false

    var body: some View {
        Form {
            Section {
                Picker("Appearance", selection: $appearance) {
                    ForEach(Appearance.allCases) { option in
                        Text(option.rawValue).tag(option.rawValue)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: appearance) { _, newValue in
                    (Appearance(rawValue: newValue) ?? .system).apply()
                }
                .help("Use the system's light or dark mode for AgentBoard's windows, or always dark or light")
            } header: {
                header("Appearance", topic: .settings)
            }

            Section {
                Toggle("Color the menu bar icon by session state", isOn: $menuBarIconColored)
                    .help("Tint the menu bar icon with the most urgent session's color; off shows the plain monochrome icon")
                Toggle("Flash when a session needs you", isOn: $menuBarIconFlash)
                    .disabled(!menuBarIconColored)
                    .help("Flash the menu bar icon when a session newly needs you (needs the colored icon)")
                Picker("Stop flashing", selection: $menuBarIconFlashStop) {
                    ForEach(FlashStopPolicy.allCases) { policy in
                        Text(policy.label).tag(policy.rawValue)
                    }
                }
                .disabled(!menuBarIconColored || !menuBarIconFlash)
                .help("Choose when a flashing icon stops: when you open AgentBoard, after a delay, or only once every session is handled")
                Text("The icon takes the color of the most urgent session. With Reduce motion on, it shows an exclamation badge instead of flashing.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                header("Menu bar icon", topic: .settings)
            }

            Section {
                Toggle("Notify when a session needs you", isOn: $notifyOnNeedsYou)
                    .onChange(of: notifyOnNeedsYou) { _, newValue in
                        if newValue { NotificationManager.shared.requestAuthorizationIfNeeded() }
                    }
                    .help("Post a macOS notification when a session newly needs you")
            } header: {
                header("Notifications", topic: .settings)
            }

            Section {
                Toggle("Launch at login", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, newValue in
                        LaunchAtLogin.setEnabled(newValue)
                    }
                    .help("Start AgentBoard automatically when you log in to this Mac")
            } header: {
                header("General", topic: .settings)
            }

            Section {
                TextField("agentboard path override (optional)", text: $cliPath)
                    .help("Full path to the agentboard CLI, used only when it is not in the app bundle or on $PATH")
                Text("AgentBoard looks for the CLI in its own bundle, then $PATH, then this override.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Toggle("Use fixture data (development)", isOn: $useFixture)
                    .help("Show a saved sample snapshot instead of calling the CLI; actions are logged, not run")
                if useFixture {
                    TextField("Fixture path (optional, uses bundled fixture if empty)", text: $fixturePath)
                        .help("Snapshot JSON file to show; empty uses the sample bundled in the app")
                }
            } header: {
                header("CLI & Data", topic: .settings)
            }

            Section {
                Toggle("Verbose logging", isOn: $verboseLogging)
                    .help("Also log every background snapshot and the CLI's debug detail, in the app and in agentboard.log")
                Text("Also logs every background snapshot and the CLI's debug detail (hook events, each focus step). Read it back with `agentboard logs`.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Open log") { store.revealLog() }
                    .disabled(!store.logFileExists)
                    .help("Reveal agentboard.log, the diagnostic log, in Finder")
            } header: {
                header("Diagnostics", topic: .diagnostics)
            }
        }
        .formStyle(.grouped)
        .frame(width: 420)
        .padding()
        .onAppear { launchAtLogin = LaunchAtLogin.isEnabled }
    }

    /// A section title with a ? button that opens Help at `topic`.
    private func header(_ title: String, topic: HelpTopic.ID) -> some View {
        HStack {
            Text(title)
            Spacer()
            HelpButton(topic: topic, tooltip: "Explain the \(title) settings in AgentBoard Help")
        }
    }
}
