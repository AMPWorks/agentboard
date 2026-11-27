import AppKit
import os

/// Whether the Claude desktop app is installed on this Mac, for the titles
/// and tooltips of remote Remote Control rows (a click opens the session
/// there when it is). Looked up through Launch Services and cached for 60 s,
/// since tooltips are built per row. Never part of the snapshot; tests pass
/// `desktopAppInstalled:` explicitly instead of overriding this.
enum ClaudeDesktopApp {
    static let bundleIdentifier = "com.anthropic.claudefordesktop"
    static let cacheSeconds: TimeInterval = 60

    private struct Cached: Sendable {
        var value: Bool
        var at: Date
    }

    private static let cache = OSAllocatedUnfairLock<Cached?>(initialState: nil)

    nonisolated static var isInstalled: Bool {
        let now = Date()
        if let hit = cache.withLock({ $0 }), now.timeIntervalSince(hit.at) < cacheSeconds {
            return hit.value
        }
        let value = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleIdentifier) != nil
        cache.withLock { $0 = Cached(value: value, at: now) }
        return value
    }
}
