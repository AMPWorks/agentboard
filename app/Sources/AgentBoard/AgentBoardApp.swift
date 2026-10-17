import SwiftUI

@main
struct AgentBoardApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = SnapshotStore()
    @StateObject private var attention = AttentionController()

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(store: store, attention: attention)
        } label: {
            MenuBarLabel(store: store, attention: attention)
        }
        .menuBarExtraStyle(.window)

        Window("AgentBoard Dashboard", id: DashboardWindowController.windowID) {
            DashboardView(store: store)
                .onAppear { store.setVisible(true) }
                .onDisappear { store.setVisible(false) }
        }
        .windowResizability(.contentSize)
        .defaultSize(width: 900, height: 560)

        Settings {
            SettingsView()
        }
    }
}

/// Owns process-level setup that SwiftUI's App has no hook for: the global
/// hotkey and the appearance applied before any window is shown.
final class AppDelegate: NSObject, NSApplicationDelegate {
    @MainActor
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Without this, macOS treats a backgrounded LSUIElement app as idle
        // and can automatically terminate it even though its menu bar item
        // is still present. Disabled for the app's entire lifetime (never
        // re-enabled) since AgentBoard should keep polling/watching while
        // backgrounded.
        ProcessInfo.processInfo.disableAutomaticTermination("AgentBoard menu bar")

        let stored = UserDefaults.standard.string(forKey: "appearance") ?? Appearance.system.rawValue
        (Appearance(rawValue: stored) ?? .system).apply()

        HotKeyManager.shared.onHotKey = { DashboardWindowController.toggle() }
        HotKeyManager.shared.register()
    }

    @MainActor
    func applicationWillTerminate(_ notification: Notification) {
        HotKeyManager.shared.unregister()
    }
}
