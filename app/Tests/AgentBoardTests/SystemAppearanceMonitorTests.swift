import AppKit
import Testing
@testable import AgentBoard

@Suite("SystemAppearanceMonitor")
struct SystemAppearanceMonitorTests {
    /// Sets the key explicitly each time: a suite's search list falls back to
    /// the global domain, so a removed key would read the real system setting.
    @Test func readsAppleInterfaceStyle() throws {
        let suite = "agentboard.tests.appearance.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }

        defaults.set("Dark", forKey: "AppleInterfaceStyle")
        #expect(SystemAppearanceMonitor.systemIsDark(defaults: defaults))
        defaults.set("Light", forKey: "AppleInterfaceStyle")
        #expect(!SystemAppearanceMonitor.systemIsDark(defaults: defaults))
        defaults.set("", forKey: "AppleInterfaceStyle")
        #expect(!SystemAppearanceMonitor.systemIsDark(defaults: defaults))
    }
}
