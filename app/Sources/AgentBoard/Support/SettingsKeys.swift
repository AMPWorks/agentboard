import Foundation

/// Every UserDefaults key the Settings window writes, in one list. Each
/// must be documented in the Settings help topic (a Swift test checks), and
/// every `@AppStorage`/`forKey:` in the sources must name one of these or
/// carry a `// not-a-setting: <reason>` comment (a Python test checks).
enum SettingsKeys {
    /// `Appearance` raw value: System (default), Dark or Light.
    static let appearance = "appearance"
    /// Post a notification when a session newly needs you (default off).
    static let notifyOnNeedsYou = "notifyOnNeedsYou"
    /// Post a notification when an attended session finishes its turn
    /// (default off).
    static let notifyOnTurnFinished = "notifyOnTurnFinished"

    static let all: [String] = [
        SettingsKeys.appearance,
        SettingsKeys.notifyOnNeedsYou,
        SettingsKeys.notifyOnTurnFinished,
        MenuBarIconSettings.coloredKey,
        MenuBarIconSettings.flashKey,
        MenuBarIconSettings.flashStopKey,
        AgentBoardCLI.pathDefaultsKey,
        AgentBoardCLI.useFixtureDefaultsKey,
        AgentBoardCLI.fixturePathDefaultsKey,
        CLIRunner.verboseLoggingKey,
        SettingsKeys.showPlanUsage,
    ]

    /// Show the plan-usage lines (one per usage source) in the popover
    /// footer and the dashboard sidebar (default on).
    static let showPlanUsage = "showPlanUsage"
}
