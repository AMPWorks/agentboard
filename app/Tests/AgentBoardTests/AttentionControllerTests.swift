import AppKit
import Testing
@testable import AgentBoard

@Suite("AttentionController")
@MainActor
struct AttentionControllerTests {
    /// A sleeper that never returns until cancelled, so the phase loop never
    /// advances on its own; tests drive it with `tick()`.
    static let frozen: AttentionController.Sleeper = { _ in try await Task.sleep(for: .seconds(86_400)) }

    let center = NotificationCenter()

    private func makeController(reduceMotion: Bool = false, policy: FlashStopPolicy = .onOpen) -> AttentionController {
        let controller = AttentionController(
            sleeper: Self.frozen,
            reduceMotion: reduceMotion,
            notificationCenter: center,
            workspaceNotificationCenter: nil
        )
        controller.policy = policy
        return controller
    }

    private func dashboardWindow() -> NSWindow {
        let window = NSWindow(contentRect: .init(x: 0, y: 0, width: 10, height: 10), styleMask: [.titled], backing: .buffered, defer: true)
        window.identifier = NSUserInterfaceItemIdentifier(DashboardWindowController.windowID)
        window.isReleasedWhenClosed = false
        return window
    }

    /// Lets main-queue notification blocks run.
    private func settle(until condition: () -> Bool) async {
        for _ in 0..<100 where !condition() {
            try? await Task.sleep(for: .milliseconds(10))
        }
    }

    @Test func newKeyStartsFlash() {
        let controller = makeController()
        let entered = controller.note(snapshot: Fixtures.needsYou("a"))
        #expect(entered == ["a"])
        #expect(controller.isFlashing)
        #expect(controller.hasFlashTask)
    }

    @Test func needsYouAtLaunchFlashesOnce() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a", "b"))
        #expect(controller.isFlashing)
        controller.acknowledge()
        // Later polls with the same sessions do not restart it.
        #expect(controller.note(snapshot: Fixtures.needsYou("a", "b")).isEmpty)
        #expect(!controller.isFlashing)
    }

    @Test func acknowledgeStopsFlashAndTimer() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a"))
        controller.tick()
        #expect(!controller.flashPhase)
        controller.popoverDidAppear()
        #expect(!controller.isFlashing)
        #expect(!controller.hasFlashTask)
        // Solid glyph after it stops, never stuck on the dimmed half.
        #expect(controller.flashPhase)
    }

    @Test func secondKeyRestartsFlashAfterAcknowledge() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a"))
        controller.acknowledge()
        #expect(!controller.isFlashing)
        let entered = controller.note(snapshot: Fixtures.needsYou("a", "b"))
        #expect(entered == ["b"])
        #expect(controller.isFlashing)
    }

    @Test func keyLeavingNeedsYouStopsFlash() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a"))
        controller.note(snapshot: Fixtures.snapshot([Fixtures.session("a", "running", alive: true)]))
        #expect(!controller.isFlashing)
        #expect(!controller.hasFlashTask)
    }

    @Test func reentryOfSameKeyFlashesAgain() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a"))
        controller.acknowledge()
        controller.note(snapshot: Fixtures.snapshot([Fixtures.session("a", "running", alive: true)]))
        #expect(!controller.acknowledgedKeys.contains("a"))
        let entered = controller.note(snapshot: Fixtures.needsYou("a"))
        #expect(entered == ["a"])
        #expect(controller.isFlashing)
    }

    @Test func timeoutStopsFlash() {
        let controller = makeController(policy: .after30Seconds)
        controller.note(snapshot: Fixtures.needsYou("a"))
        // 0.5 s per tick: 59 ticks is 29.5 s.
        for _ in 0..<59 { controller.tick() }
        #expect(controller.isFlashing)
        controller.tick()
        #expect(!controller.isFlashing)
        #expect(!controller.hasFlashTask)
        #expect(controller.flashPhase)
    }

    @Test func newKeyRestartsTimeout() {
        let controller = makeController(policy: .after30Seconds)
        controller.note(snapshot: Fixtures.needsYou("a"))
        for _ in 0..<50 { controller.tick() }
        controller.note(snapshot: Fixtures.needsYou("a", "b"))
        for _ in 0..<50 { controller.tick() }
        #expect(controller.isFlashing)
        for _ in 0..<10 { controller.tick() }
        #expect(!controller.isFlashing)
    }

    @Test func policyChangeMidFlashResetsElapsed() {
        let controller = makeController(policy: .after30Seconds)
        controller.note(snapshot: Fixtures.needsYou("a"))
        for _ in 0..<50 { controller.tick() }
        // 25 s in; switching to 2 minutes restarts the count from zero.
        controller.policy = .after2Minutes
        for _ in 0..<239 { controller.tick() }
        #expect(controller.isFlashing)
        controller.tick()
        #expect(!controller.isFlashing)
        #expect(!controller.hasFlashTask)
    }

    @Test func leavingNeverWhilePopoverOpenAcknowledges() {
        let controller = makeController(policy: .never)
        controller.note(snapshot: Fixtures.needsYou("a"))
        controller.popoverDidAppear()
        #expect(controller.isFlashing)
        controller.policy = .onOpen
        #expect(!controller.isFlashing)
        #expect(!controller.hasFlashTask)
    }

    @Test func leavingNeverWhileUnattendedKeepsFlashing() {
        let controller = makeController(policy: .never)
        controller.note(snapshot: Fixtures.needsYou("a"))
        controller.policy = .onOpen
        #expect(controller.isFlashing)
    }

    @Test func ticksAlternatePhase() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a"))
        #expect(controller.flashPhase)
        controller.tick()
        #expect(!controller.flashPhase)
        controller.tick()
        #expect(controller.flashPhase)
    }

    @Test func noTimerWhenFlashDisabled() {
        let controller = makeController()
        controller.flashEnabled = false
        let entered = controller.note(snapshot: Fixtures.needsYou("a"))
        // The diff still reaches notifications.
        #expect(entered == ["a"])
        #expect(!controller.isFlashing)
        #expect(!controller.hasFlashTask)
        // Re-enabling does not flash for sessions that arrived while it was off.
        controller.flashEnabled = true
        #expect(!controller.isFlashing)
    }

    @Test func disablingFlashStopsIt() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a"))
        controller.flashEnabled = false
        #expect(!controller.isFlashing)
        #expect(!controller.hasFlashTask)
    }

    @Test func newKeyWhilePopoverOpenDoesNotFlash() {
        let controller = makeController()
        controller.popoverDidAppear()
        controller.note(snapshot: Fixtures.needsYou("a"))
        #expect(!controller.isFlashing)
        controller.popoverDidDisappear()
        controller.note(snapshot: Fixtures.needsYou("a", "b"))
        #expect(controller.isFlashing)
    }

    @Test func newKeyWhileDashboardIsKeyDoesNotFlash() async {
        let controller = makeController()
        let window = dashboardWindow()
        center.post(name: NSWindow.didBecomeKeyNotification, object: window)
        await settle { controller.dashboardIsKey }
        #expect(controller.dashboardIsKey)
        controller.note(snapshot: Fixtures.needsYou("a"))
        #expect(!controller.isFlashing)
        #expect(controller.acknowledgedKeys == ["a"])

        center.post(name: NSWindow.didResignKeyNotification, object: window)
        await settle { !controller.dashboardIsKey }
        controller.note(snapshot: Fixtures.needsYou("a", "b"))
        #expect(controller.isFlashing)
    }

    @Test func dashboardBecomingKeyAcknowledges() async {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a"))
        #expect(controller.isFlashing)
        center.post(name: NSWindow.didBecomeKeyNotification, object: dashboardWindow())
        await settle { !controller.isFlashing }
        #expect(!controller.isFlashing)
        #expect(!controller.hasFlashTask)
    }

    @Test func otherWindowsBecomingKeyDoNotAcknowledge() async {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a"))
        let other = NSWindow(contentRect: .init(x: 0, y: 0, width: 10, height: 10), styleMask: [.titled], backing: .buffered, defer: true)
        other.isReleasedWhenClosed = false
        center.post(name: NSWindow.didBecomeKeyNotification, object: other)
        try? await Task.sleep(for: .milliseconds(50))
        #expect(controller.isFlashing)
        #expect(!controller.dashboardIsKey)
    }

    @Test func neverPolicyIgnoresAcknowledgeUntilHandled() {
        let controller = makeController(policy: .never)
        controller.note(snapshot: Fixtures.needsYou("a"))
        controller.acknowledge()
        controller.popoverDidAppear()
        #expect(controller.isFlashing)
        for _ in 0..<1_000 { controller.tick() }
        #expect(controller.isFlashing)
        controller.note(snapshot: Fixtures.snapshot([Fixtures.session("a", "done")]))
        #expect(!controller.isFlashing)
        #expect(!controller.hasFlashTask)
    }

    @Test func neverPolicyFlashesEvenWhilePopoverOpen() {
        let controller = makeController(policy: .never)
        controller.popoverDidAppear()
        controller.note(snapshot: Fixtures.needsYou("a"))
        #expect(controller.isFlashing)
    }

    @Test func reduceMotionKeepsAlertWithoutToggling() {
        let controller = makeController(reduceMotion: true)
        controller.note(snapshot: Fixtures.needsYou("a"))
        #expect(controller.isFlashing)
        // No phase loop is needed without a timeout.
        #expect(!controller.hasFlashTask)
        controller.tick()
        #expect(controller.flashPhase)
        controller.acknowledge()
        #expect(!controller.isFlashing)
    }

    @Test func reduceMotionStillHonorsTimeout() {
        let controller = makeController(reduceMotion: true, policy: .after30Seconds)
        controller.note(snapshot: Fixtures.needsYou("a"))
        #expect(controller.hasFlashTask)
        for _ in 0..<59 { controller.tick() }
        #expect(controller.isFlashing)
        #expect(controller.flashPhase)
        controller.tick()
        #expect(!controller.isFlashing)
    }

    @Test func turningOnReduceMotionMidFlashSettlesOnSolid() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.needsYou("a"))
        controller.tick()
        #expect(!controller.flashPhase)
        controller.setReduceMotion(true)
        #expect(controller.flashPhase)
        #expect(!controller.hasFlashTask)
        #expect(controller.isFlashing)
    }

    @Test func realLoopTogglesPhase() async {
        let controller = AttentionController(
            sleeper: { _ in try await Task.sleep(for: .milliseconds(5)) },
            reduceMotion: false,
            notificationCenter: center,
            workspaceNotificationCenter: nil
        )
        controller.note(snapshot: Fixtures.needsYou("a"))
        var sawDimmed = false
        for _ in 0..<200 where !sawDimmed {
            try? await Task.sleep(for: .milliseconds(5))
            if !controller.flashPhase { sawDimmed = true }
        }
        #expect(sawDimmed)
        controller.acknowledge()
        #expect(!controller.hasFlashTask)
        #expect(controller.flashPhase)
    }

    @Test func policyMetadata() {
        #expect(FlashStopPolicy.onOpen.timeout == nil)
        #expect(FlashStopPolicy.after30Seconds.timeout == .seconds(30))
        #expect(FlashStopPolicy.after2Minutes.timeout == .seconds(120))
        #expect(FlashStopPolicy.never.timeout == nil)
        #expect(!FlashStopPolicy.never.honorsAcknowledgement)
        #expect(FlashStopPolicy(rawValue: FlashStopPolicy.onOpen.rawValue) == .onOpen)
    }
}
