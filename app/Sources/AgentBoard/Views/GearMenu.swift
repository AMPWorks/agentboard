import AppKit
import SwiftUI

/// The gear (settings) menu shared by the popover footer and the dashboard
/// toolbar: Settings…, AgentBoard Help, Open Log and Quit. The accessory app
/// has no menu bar of its own, so this is where those live.
struct GearMenu: View {
    @ObservedObject var store: SnapshotStore
    /// True in the popover: Settings… and AgentBoard Help close it, like a
    /// row click does. False in the dashboard, which stays open behind them.
    let closesHostWindow: Bool
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Menu {
            Button("Settings…") { showSettings() }
                .help("Open AgentBoard's settings: appearance, the menu bar icon, notifications and diagnostics")
            Button("AgentBoard Help") { showHelp() }
                .help("Open the guide to sessions, states, updates, search, settings and the CLI")
            Button("Open Log") { store.revealLog() }
                .disabled(!store.logFileExists)
                .help("Reveal agentboard.log, the diagnostic log, in Finder")
            Divider()
            Button("Quit AgentBoard") { NSApplication.shared.terminate(nil) }
                .help("Quit the menu bar app; hooks keep recording sessions while it is closed")
        } label: {
            Image(systemName: "gearshape")
                .accessibilityLabel("Settings and help")
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("Settings, help, the diagnostic log and Quit")
    }

    /// From the popover, close it first so Settings is the window left in
    /// front.
    private func showSettings() {
        let host = closesHostWindow ? NSApp.keyWindow : nil
        host?.close()
        SettingsWindowController.show(using: openSettings)
    }

    private func showHelp() {
        let host = closesHostWindow ? NSApp.keyWindow : nil
        HelpWindowController.show(topic: .gettingStarted)
        host?.close()
    }
}
