import Foundation
import Testing
@testable import AgentBoard

@Suite("FocusRemedy")
struct FocusRemedyTests {
    @Test func exitCodesMapToRemedies() {
        #expect(FocusRemedy(exitCode: 0) == nil)
        #expect(FocusRemedy(exitCode: 1) == nil)  // tab not found, timeout, no terminal
        #expect(FocusRemedy(exitCode: 2) == nil)  // argparse
        #expect(FocusRemedy(exitCode: 3) == nil)  // reserved; nothing needs Accessibility
        #expect(FocusRemedy(exitCode: 4) == .automation)
        #expect(FocusRemedy(exitCode: 5) == .spacesSetting)
        #expect(FocusRemedy(exitCode: 99) == nil)
    }

    @Test func settingsURLs() {
        #expect(FocusRemedy.automation.settingsURL.absoluteString
                == "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")
        #expect(FocusRemedy.spacesSetting.settingsURL.absoluteString
                == "x-apple.systempreferences:com.apple.Desktop-Settings.extension")
        #expect(FocusRemedy.automation.buttonTitle == "Open Automation Settings")
        #expect(FocusRemedy.spacesSetting.buttonTitle == "Open Desktop & Dock Settings")
    }

    @Test func cliExitBecomesTheStderrLineWithoutExitedPrefix() {
        let stderr = "Found the session's tab, but macOS stayed on this desktop. Turn on ...\n"
        let error = FocusError.from(CLIRunner.CLIError.nonZeroExit(5, stderr))
        let focus = error as? FocusError
        #expect(focus?.message == "Found the session's tab, but macOS stayed on this desktop. Turn on ...")
        #expect(focus?.remedy == .spacesSetting)
        #expect(error.localizedDescription == focus?.message)
        #expect(!error.localizedDescription.contains("Exited"))
    }

    @Test func aFailureWithoutRemedyKeepsItsMessage() {
        let error = FocusError.from(CLIRunner.CLIError.nonZeroExit(1, "Couldn't find this session's tab in iTerm\n"))
        #expect((error as? FocusError)?.remedy == nil)
        #expect(error.localizedDescription == "Couldn't find this session's tab in iTerm")
        // Empty stderr still says something.
        #expect(FocusError.from(CLIRunner.CLIError.nonZeroExit(1, "")).localizedDescription == "Exited 1")
    }

    @Test func otherErrorsPassThrough() {
        let launch = CLIRunner.CLIError.launchFailed("nope")
        #expect(!(FocusError.from(launch) is FocusError))
    }

    @Test func bannerCarriesTheRemedy() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let error = ActionError(action: "Focus", message: "AgentBoard isn't allowed to control iTerm", at: now,
                                remedy: .automation)
        let banner = StoreStatus.banner(lastError: nil, lastActionError: error, lastUpdated: now,
                                        refreshStartedAt: nil, startedAt: now, now: now)
        #expect(banner?.isActionError == true)
        #expect(banner?.remedy == .automation)
        #expect(banner?.text == "Focus failed: AgentBoard isn't allowed to control iTerm")

        let plain = ActionError(action: "Mark done", message: "x", at: now)
        #expect(StoreStatus.banner(lastError: nil, lastActionError: plain, lastUpdated: now,
                                   refreshStartedAt: nil, startedAt: now, now: now)?.remedy == nil)
    }
}
