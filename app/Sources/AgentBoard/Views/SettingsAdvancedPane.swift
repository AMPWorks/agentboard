import SwiftUI

/// Settings → Advanced: CLI & Data, and Diagnostics.
struct SettingsAdvancedPane: View {
    @ObservedObject var store: SnapshotStore
    @AppStorage(AgentBoardCLI.pathDefaultsKey) private var cliPath: String = ""
    @AppStorage(AgentBoardCLI.useFixtureDefaultsKey) private var useFixture: Bool = false
    @AppStorage(AgentBoardCLI.fixturePathDefaultsKey) private var fixturePath: String = ""
    @AppStorage(CLIRunner.verboseLoggingKey) private var verboseLogging: Bool = false

    var body: some View {
        SettingsPane(tab: .advanced) {
            Section {
                TextField(SettingsCatalog.cliPath.label, text: $cliPath)
                    .help(SettingsCatalog.cliPath.help)
                    .settingsAnchor(SettingsCatalog.cliPath)
                Text("AgentBoard looks for the CLI in its own bundle, then $PATH, then this override.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Toggle(SettingsCatalog.useFixture.label, isOn: $useFixture)
                    .help(SettingsCatalog.useFixture.help)
                    .settingsAnchor(SettingsCatalog.useFixture, standingIn: !useFixture)
                if useFixture {
                    TextField(SettingsCatalog.fixturePath.label, text: $fixturePath)
                        .help(SettingsCatalog.fixturePath.help)
                        .settingsAnchor(SettingsCatalog.fixturePath)
                }
            } header: {
                SettingsSectionHeader("CLI & Data")
            }

            Section {
                Toggle(SettingsCatalog.verboseLogging.label, isOn: $verboseLogging)
                    .help(SettingsCatalog.verboseLogging.help)
                    .settingsAnchor(SettingsCatalog.verboseLogging)
                Text("Also logs every background snapshot and the CLI's debug detail (hook events, each focus step). Read it back with `agentboard logs`.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button(SettingsCatalog.openLog.label) { store.revealLog() }
                    .disabled(!store.logFileExists)
                    .help(SettingsCatalog.openLog.help)
                    .settingsAnchor(SettingsCatalog.openLog, standingIn: store.hookErrorsLogInfo == nil)
                // Only when the hook has logged a failure.
                if let info = store.hookErrorsLogInfo {
                    HStack {
                        Text("hook-errors.log: \(Self.byteCount(info.size)), last written \(Self.relative(info.modified))")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button(SettingsCatalog.revealHookErrorsLog.label) { store.revealHookErrorsLog() }
                            .help(SettingsCatalog.revealHookErrorsLog.help)
                    }
                    .settingsAnchor(SettingsCatalog.revealHookErrorsLog)
                }
            } header: {
                SettingsSectionHeader("Diagnostics", topic: .diagnostics)
            }
        }
    }

    private static func byteCount(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    private static func relative(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }
}
