import AppKit
import Testing
@testable import AgentBoard

@Suite("StoreStatus")
struct StoreStatusTests {
    let now = Date(timeIntervalSince1970: 1_800_000_000)

    private func banner(
        lastError: String? = nil,
        lastActionError: ActionError? = nil,
        lastUpdated: Date? = nil,
        refreshStartedAt: Date? = nil,
        startedAt: Date? = nil
    ) -> StoreStatus.Banner? {
        StoreStatus.banner(lastError: lastError, lastActionError: lastActionError,
                           lastUpdated: lastUpdated, refreshStartedAt: refreshStartedAt,
                           startedAt: startedAt, now: now)
    }

    @Test func nothingWhenHealthy() {
        #expect(banner(lastUpdated: now.addingTimeInterval(-2), startedAt: now.addingTimeInterval(-600)) == nil)
        // Just launched, first snapshot still on its way.
        #expect(banner(refreshStartedAt: now.addingTimeInterval(-3), startedAt: now.addingTimeInterval(-3)) == nil)
    }

    @Test func snapshotErrorIsAnErrorBanner() {
        let shown = banner(lastError: "Exited 1: disk I/O error", lastUpdated: now)
        #expect(shown?.text == "Snapshot failed: Exited 1: disk I/O error")
        #expect(shown?.severity == .error)
        #expect(shown?.isActionError == false)
    }

    @Test func errorWinsOverStale() {
        let shown = banner(lastError: "unable to open database file",
                           refreshStartedAt: now.addingTimeInterval(-120),
                           startedAt: now.addingTimeInterval(-600))
        #expect(shown?.severity == .error)
        #expect(shown?.text.hasPrefix("Snapshot failed:") == true)
    }

    @Test func staleWhenARefreshHasBeenInFlightOver30Seconds() {
        let started = now.addingTimeInterval(-600)
        #expect(banner(lastUpdated: now.addingTimeInterval(-40), refreshStartedAt: now.addingTimeInterval(-30),
                       startedAt: started) == nil)
        let shown = banner(lastUpdated: now.addingTimeInterval(-40), refreshStartedAt: now.addingTimeInterval(-31),
                           startedAt: started)
        #expect(shown?.text == "Waiting on the CLI for 31 s")
        #expect(shown?.severity == .warning)
    }

    @Test func staleWhenNothingSucceededWithin45SecondsOfStart() {
        #expect(banner(startedAt: now.addingTimeInterval(-45)) == nil)
        let shown = banner(startedAt: now.addingTimeInterval(-46))
        #expect(shown?.text == "No snapshot yet; the CLI is not answering")
        #expect(shown?.severity == .warning)
    }

    @Test func noStaleBannerAfterALongGapWithNoRefreshInFlight() {
        // Sleep/wake or a long popover-closed stretch: lastUpdated is old,
        // but nothing is hanging.
        #expect(banner(lastUpdated: now.addingTimeInterval(-3600), startedAt: now.addingTimeInterval(-86_400)) == nil)
    }

    @Test func actionErrorShowsThenExpires() {
        let fresh = ActionError(action: "Mark done", message: "Exited 1: no such session", at: now.addingTimeInterval(-14))
        let shown = banner(lastActionError: fresh, lastUpdated: now)
        #expect(shown?.text == "Mark done failed: Exited 1: no such session")
        #expect(shown?.severity == .warning)
        #expect(shown?.isActionError == true)

        let expired = ActionError(action: "Mark done", message: "x", at: now.addingTimeInterval(-15))
        #expect(banner(lastActionError: expired, lastUpdated: now) == nil)
    }

    @Test func staleWinsOverActionError() {
        let error = ActionError(action: "Focus", message: "x", at: now)
        let shown = banner(lastActionError: error, refreshStartedAt: now.addingTimeInterval(-60),
                           startedAt: now.addingTimeInterval(-600))
        #expect(shown?.isActionError == false)
    }

    @Test func multiLineErrorsBecomeOneLine() {
        let traceback = "Exited 1: Traceback (most recent call last):\n  File \"x\", line 1\nsqlite3.OperationalError: disk I/O error\n"
        #expect(StoreStatus.oneLine(traceback)
                == "Exited 1: Traceback (most recent call last): … sqlite3.OperationalError: disk I/O error")
        #expect(StoreStatus.oneLine("plain") == "plain")
        let shown = banner(lastError: traceback)
        #expect(shown?.detail == traceback)
    }

    @MainActor @Test func actionTitleCapitalizesTheFirstLetter() {
        #expect(SnapshotStore.actionTitle("mark done") == "Mark done")
        #expect(SnapshotStore.actionTitle("") == "")
    }
}
