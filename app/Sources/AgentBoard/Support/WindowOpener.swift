import AppKit

/// Bridges SwiftUI's openWindow/dismissWindow environment actions (captured
/// from an always-live view) to non-View callers like the global hotkey.
@MainActor
final class WindowOpener {
    static let shared = WindowOpener()
    var open: (() -> Void)?
    var dismiss: (() -> Void)?
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
