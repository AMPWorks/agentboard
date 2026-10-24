import AppKit
import SwiftUI

/// Bridges SwiftUI's openWindow/dismissWindow environment actions (captured
/// from an always-live view) to non-View callers like the global hotkey.
@MainActor
final class WindowOpener {
    static let shared = WindowOpener()
    var open: (() -> Void)?
    var dismiss: (() -> Void)?
    /// Opens the Help window scene (`HelpWindowController.windowID`).
    var openHelp: (() -> Void)?
}

@MainActor
enum DashboardWindowController {
    static let windowID = "dashboard"

    static func toggle() {
        if let window = find() {
            if window.isVisible {
                window.orderOut(nil)
            } else {
                show(window)
            }
        } else {
            WindowOpener.shared.open?()
        }
    }

    static func show() {
        if let window = find() {
            show(window)
        } else {
            WindowOpener.shared.open?()
        }
    }

    private static func show(_ window: NSWindow) {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    private static func find() -> NSWindow? {
        NSApp.windows.first { $0.identifier?.rawValue == windowID }
    }
}

/// Opens the Help window on a topic. `Window(id:)` scenes take no value, so
/// the topic goes through `HelpNavigator.shared`, which the Help sidebar's
/// selection is bound to.
@MainActor
enum HelpWindowController {
    static let windowID = "help"

    static func show(topic: HelpTopic.ID, navigator: HelpNavigator = .shared) {
        navigator.select(topic)
        NSApp.activate(ignoringOtherApps: true)
        if let window = find() {
            window.makeKeyAndOrderFront(nil)
        } else {
            WindowOpener.shared.openHelp?()
        }
    }

    private static func find() -> NSWindow? {
        NSApp.windows.first { $0.identifier?.rawValue == windowID }
    }
}

/// Brings SwiftUI's Settings window to the front. An accessory app's
/// windows open behind the frontmost app unless the app activates first,
/// and `openSettings` alone does not order an existing Settings window
/// front, so: activate, open, then front the window on the next runloop
/// turn (the same activate-then-front order as `DashboardWindowController`).
@MainActor
enum SettingsWindowController {
    static func show(using openSettings: OpenSettingsAction) {
        NSApp.activate(ignoringOtherApps: true)
        openSettings()
        DispatchQueue.main.async {
            find()?.makeKeyAndOrderFront(nil)
        }
    }

    /// SwiftUI names the Settings window `com_apple_SwiftUI_Settings_window`.
    private static func find() -> NSWindow? {
        NSApp.windows.first { $0.identifier?.rawValue.contains("Settings") == true }
    }
}
