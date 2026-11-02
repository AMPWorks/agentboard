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

/// Banners are silent: the sound comes from Settings → Sounds.
@Suite("NotificationManager content")
@MainActor
struct NotificationContentTests {
    @Test func theRequestContentHasNoSound() {
        let session = Fixtures.decoded("claude:a", "needs_you", extra: ["waiting_reason": "Approve Bash"])
        let content = NotificationManager.content(for: SessionNotificationPlan.needsYou(session))
        #expect(content.sound == nil)
        #expect(content.title == "AgentBoard: needs you")
        #expect(content.body == "Approve Bash")
        #expect(content.threadIdentifier == "claude:a")
        #expect(content.categoryIdentifier == SessionNotificationPlan.categoryIdentifier)
        #expect(content.userInfo["sessionKey"] as? String == "claude:a")
    }

    @Test func aTurnNotificationIsSilentToo() {
        let session = Fixtures.decoded("claude:a", "review", extra: ["last_assistant": "All done"])
        let planned = SessionNotificationPlan.turnRequests(
            finished: [session], context: .init(dashboardIsKey: false, dashboardSelection: nil)
        )
        let content = NotificationManager.content(for: try! #require(planned.first))
        #expect(content.sound == nil)
        #expect(content.body == "All done")
    }

    @Test func bannersPresentWithoutSound() {
        #expect(NotificationManager.presentationOptions == [.banner])
        #expect(!NotificationManager.presentationOptions.contains(.sound))
    }
}
