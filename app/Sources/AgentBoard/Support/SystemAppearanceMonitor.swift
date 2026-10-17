import AppKit

/// Tracks whether the *system* (and so the menu bar) is in Dark Mode.
///
/// `NSApp.effectiveAppearance` is not usable for this: `Appearance.apply()`
/// overrides it from the in-app Appearance setting, and the menu bar ignores
/// that override. The system setting lives in the global `AppleInterfaceStyle`
/// default, announced by a distributed notification when it changes.
@MainActor
final class SystemAppearanceMonitor: ObservableObject {
    @Published private(set) var isDark: Bool

    private let observer: Observer

    init() {
        isDark = Self.systemIsDark()
        observer = Observer()
        observer.token = DistributedNotificationCenter.default().addObserver(
            forName: Notification.Name("AppleInterfaceThemeChangedNotification"), object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
    }

    nonisolated static func systemIsDark(defaults: UserDefaults = .standard) -> Bool {
        defaults.string(forKey: "AppleInterfaceStyle") == "Dark"
    }

    private func refresh() {
        isDark = Self.systemIsDark()
        // The notification can arrive a beat before the global default is
        // readable in this process; look again shortly after.
        Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(500))
            guard let self else { return }
            let dark = Self.systemIsDark()
            if dark != self.isDark { self.isDark = dark }
        }
    }

    /// Removes the distributed-notification observer when the monitor goes away.
    private final class Observer: @unchecked Sendable {
        var token: NSObjectProtocol?
        deinit {
            if let token { DistributedNotificationCenter.default().removeObserver(token) }
        }
    }
}
