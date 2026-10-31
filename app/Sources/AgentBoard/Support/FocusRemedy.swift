import Foundation

/// The System Settings pane that fixes a failed `agentboard focus`, from the
/// CLI's exit code: 4 `needs_automation` (AgentBoard may not control the
/// terminal) and 5 `space_not_reached` (macOS stayed on this desktop). Any
/// other failure has no setting to open.
enum FocusRemedy: Equatable, Sendable {
    case automation
    case spacesSetting

    init?(exitCode: Int32) {
        switch exitCode {
        case 4: self = .automation
        case 5: self = .spacesSetting
        default: return nil
        }
    }

    var settingsURL: URL {
        switch self {
        case .automation:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")!
        case .spacesSetting:
            return URL(string: "x-apple.systempreferences:com.apple.Desktop-Settings.extension")!
        }
    }

    /// The banner button's title.
    var buttonTitle: String {
        switch self {
        case .automation: return "Open Automation Settings"
        case .spacesSetting: return "Open Desktop & Dock Settings"
        }
    }

    /// The banner button's tooltip.
    var help: String {
        switch self {
        case .automation:
            return "Open System Settings > Privacy & Security > Automation to let AgentBoard control your terminal"
        case .spacesSetting:
            return "Open System Settings > Desktop & Dock, where 'When switching to an application, switch to a Space with open windows' lives"
        }
    }
}

/// A failed `agentboard focus`: the CLI's own one-line message (its stderr,
/// without `Exited N:`) and the settings pane that fixes it, if any.
struct FocusError: LocalizedError, Equatable, Sendable {
    let message: String
    let remedy: FocusRemedy?

    var errorDescription: String? { message }

    /// `error` as a FocusError when it is the CLI exiting non-zero;
    /// anything else (the CLI not found, a launch failure) passes through.
    static func from(_ error: Error) -> Error {
        guard case CLIRunner.CLIError.nonZeroExit(let code, let stderr) = error else { return error }
        let text = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        return FocusError(message: text.isEmpty ? "Exited \(code)" : text,
                          remedy: FocusRemedy(exitCode: code))
    }
}
