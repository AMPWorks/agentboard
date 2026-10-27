import Testing
@testable import AgentBoard

/// The click path: `open(sessionKey:)` and the pending key a cold-launch
/// click leaves for the first snapshot. None of it touches
/// `UNUserNotificationCenter`, so a fresh manager runs unbundled.
@Suite("NotificationManager clicks")
@MainActor
struct NotificationManagerTests {
    @Test func aClickWithNoHandlerIsParked() {
        let manager = NotificationManager()
        manager.open(sessionKey: "claude:a")
        #expect(manager.pendingOpenKey == "claude:a")
    }

    @Test func aClickTheHandlerIsNotReadyForIsParked() {
        let manager = NotificationManager()
        var asked: [String] = []
        manager.onOpenSession = { key in asked.append(key); return false }
        manager.open(sessionKey: "claude:a")
        #expect(asked == ["claude:a"])
        #expect(manager.pendingOpenKey == "claude:a")
    }

    @Test func anOpenedClickClearsThePendingKey() {
        let manager = NotificationManager()
        manager.open(sessionKey: "claude:old")
        var asked: [String] = []
        manager.onOpenSession = { key in asked.append(key); return true }
        manager.open(sessionKey: "claude:a")
        #expect(asked == ["claude:a"])
        #expect(manager.pendingOpenKey == nil)
    }

    @Test func thePendingKeyIsTakenOnce() {
        let manager = NotificationManager()
        manager.open(sessionKey: "claude:a")
        #expect(manager.takePendingOpenKey() == "claude:a")
        #expect(manager.takePendingOpenKey() == nil)
        #expect(manager.pendingOpenKey == nil)
    }

    @Test func aLaterClickReplacesAnEarlierPendingOne() {
        let manager = NotificationManager()
        manager.open(sessionKey: "claude:a")
        manager.open(sessionKey: "claude:b")
        #expect(manager.takePendingOpenKey() == "claude:b")
    }
}
