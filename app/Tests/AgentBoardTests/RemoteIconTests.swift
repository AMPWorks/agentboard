import AppKit
import Testing
@testable import AgentBoard

@Suite("Menu bar icon: other Macs")
struct RemoteIconSummaryTests {
    private let mixed = Fixtures.snapshot([
        Fixtures.session("l1", "running", alive: true),
        Fixtures.remote("r1", "needs_you"),
    ], top: Fixtures.peers())

    @Test func separateMarkerKeepsTheGlyphForThisMac() {
        let summary = MenuBarIconState.summary(mixed, mode: .separate)
        #expect(summary.tier == .running)
        #expect(summary.remoteTier == .needsYou)
        #expect(summary.urgentCount == 0)
        #expect(summary.sentence == "1 needs you (1 on Mini)")
        #expect(summary.accessibilityDescription == "AgentBoard: 1 needs you (1 on Mini)")
    }

    @Test func mergedIsTodaysIcon() {
        let summary = MenuBarIconState.summary(mixed, mode: .merged)
        #expect(summary.tier == .needsYou)
        #expect(summary.remoteTier == nil)
        #expect(summary.urgentCount == 1)
        #expect(summary.sentence == "1 needs you (1 on Mini)")
    }

    @Test func notShownIgnoresOtherMacs() {
        let summary = MenuBarIconState.summary(mixed, mode: .hidden)
        #expect(summary.tier == .running)
        #expect(summary.remoteTier == nil)
        #expect(summary.urgentCount == 0)
        #expect(summary.sentence == "nothing needs you")
    }

    @Test func localAndRemoteBothUrgent() {
        let snapshot = Fixtures.snapshot([
            Fixtures.session("l1", "needs_you", alive: true),
            Fixtures.session("l2", "review", alive: true),
            Fixtures.remote("r1", "interrupted"),
        ], top: Fixtures.peers())
        let summary = MenuBarIconState.summary(snapshot, mode: .separate)
        #expect(summary.tier == .needsYou)
        #expect(summary.remoteTier == .interrupted)
        #expect(summary.urgentCount == 1)
        #expect(summary.reviewCount == 1)
        #expect(summary.sentence == "2 need you (1 on Mini), 1 to review")
    }

    @Test func badgeOnlyForLiveUrgentRemoteSessions() {
        let running = Fixtures.snapshot([Fixtures.remote("r1", "running")], top: Fixtures.peers())
        #expect(MenuBarIconState.summary(running, mode: .separate).remoteTier == nil)
        let stale = Fixtures.snapshot([Fixtures.remote("r1", "needs_you", stale: true)], counts: [:],
                                      top: Fixtures.peers(status: "offline"))
        let summary = MenuBarIconState.summary(stale, mode: .separate)
        #expect(summary.remoteTier == nil)
        #expect(summary.tier == .idle)
        #expect(summary.sentence == "nothing needs you")
    }

    @Test func settingDefaultsToSeparateAndIsASettingsKey() {
        #expect(RemoteIconMode.from("bogus") == .separate)
        #expect(RemoteIconMode.allCases.map(\.label) == ["Separate marker", "Merged with this Mac", "Not shown"])
        #expect(SettingsKeys.all.contains(MenuBarIconSettings.remoteModeKey))
    }
}

@Suite("Menu bar icon: remote badge rendering")
@MainActor
struct RemoteBadgeRendererTests {
    private func rep(_ image: NSImage) throws -> NSBitmapImageRep {
        let cg = try #require(image.cgImage(forProposedRect: nil, context: nil, hints: nil))
        return NSBitmapImageRep(cgImage: cg)
    }

    /// Pixels matching `match` in the image's top-right quarter.
    private func corner(_ image: NSImage, _ match: (NSColor) -> Bool) throws -> Int {
        let rep = try rep(image)
        var count = 0
        for x in (rep.pixelsWide / 2)..<rep.pixelsWide {
            for y in 0..<(rep.pixelsHigh / 2) {
                if let c = rep.colorAt(x: x, y: y)?.usingColorSpace(.sRGB), match(c) { count += 1 }
            }
        }
        return count
    }

    private let red: (NSColor) -> Bool = { $0.alphaComponent > 0.8 && $0.redComponent > 0.8 && $0.greenComponent < 0.5 }

    @Test func idleWithABadgeRendersARedDiamondInTheCorner() throws {
        let image = try #require(MenuBarIconRenderer.image(
            for: MenuBarIconStyle(tier: .idle, darkMenuBar: false, remoteTier: .needsYou)))
        #expect(!image.isTemplate)
        #expect(try corner(image, red) > 4)
    }

    @Test func badgeTakesTheCornerOfAGreenGlyph() throws {
        let plain = try #require(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .running, darkMenuBar: true)))
        let badged = try #require(MenuBarIconRenderer.image(
            for: MenuBarIconStyle(tier: .running, darkMenuBar: true, remoteTier: .needsYou)))
        #expect(try corner(plain, red) == 0)
        #expect(try corner(badged, red) > 4)
        #expect(badged.size == plain.size)
    }

    @Test func dimmedPulseHalfIsFainter() throws {
        let solid = try #require(MenuBarIconRenderer.image(
            for: MenuBarIconStyle(tier: .idle, darkMenuBar: false, remoteTier: .needsYou)))
        let dimmed = try #require(MenuBarIconRenderer.image(
            for: MenuBarIconStyle(tier: .idle, darkMenuBar: false, remoteTier: .needsYou, remoteDimmed: true)))
        #expect(try corner(solid, red) > 0)
        #expect(try corner(dimmed, red) == 0)
    }

    @Test func coloringOffDrawsAMonochromeTemplateBadge() throws {
        #expect(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .needsYou, darkMenuBar: false, monochrome: true)) == nil)
        let badged = try #require(MenuBarIconRenderer.image(
            for: MenuBarIconStyle(tier: .needsYou, darkMenuBar: false, remoteTier: .needsYou, monochrome: true)))
        #expect(badged.isTemplate)
        #expect(try corner(badged, red) == 0)
        #expect(try corner(badged) { $0.alphaComponent > 0.8 } > 4)
    }

    @Test func descriptionIsTheSentence() throws {
        let image = try #require(MenuBarIconRenderer.image(
            for: MenuBarIconStyle(tier: .running, darkMenuBar: false), description: "AgentBoard: nothing needs you"))
        #expect(image.accessibilityDescription == "AgentBoard: nothing needs you")
    }
}

@Suite("Remote badge pulse")
@MainActor
struct RemoteBadgePulseTests {
    static let frozen: AttentionController.Sleeper = { _ in try await Task.sleep(for: .seconds(86_400)) }

    private func makeController(reduceMotion: Bool = false, mode: RemoteIconMode = .separate) -> AttentionController {
        let controller = AttentionController(sleeper: Self.frozen, reduceMotion: reduceMotion,
                                             notificationCenter: NotificationCenter(), workspaceNotificationCenter: nil)
        controller.remoteMode = mode
        return controller
    }

    /// Running, then needs you: a real remote entry.
    private func enterRemote(_ controller: AttentionController) -> Set<String> {
        controller.note(snapshot: Fixtures.snapshot([Fixtures.remote("r1", "running")], top: Fixtures.peers()))
        return controller.note(snapshot: Fixtures.snapshot([Fixtures.remote("r1", "needs_you")], top: Fixtures.peers()))
    }

    @Test func separatePulsesTheBadgeThenHolds() {
        let controller = makeController()
        #expect(enterRemote(controller).count == 1)
        #expect(!controller.isFlashing)
        #expect(controller.isPulsingRemote)
        #expect(controller.hasFlashTask)
        var phases: [Bool] = []
        for _ in 0..<AttentionController.remotePulseHalfCycles {
            controller.tick()
            phases.append(controller.remotePulsePhase)
        }
        #expect(phases == [false, true, false, true, false, true])
        #expect(!controller.isPulsingRemote)
        #expect(controller.remotePulsePhase)
        #expect(!controller.hasFlashTask)
    }

    @Test func reduceMotionJustShowsTheBadge() {
        let controller = makeController(reduceMotion: true)
        #expect(enterRemote(controller).count == 1)
        #expect(!controller.isPulsingRemote)
        #expect(!controller.isFlashing)
        #expect(controller.remotePulsePhase)
    }

    @Test func mergedFlashesTheGlyph() {
        let controller = makeController(mode: .merged)
        #expect(enterRemote(controller).count == 1)
        #expect(controller.isFlashing)
        #expect(!controller.isPulsingRemote)
    }

    @Test func notShownNeitherFlashesNorPulsesButStillNotifies() {
        let controller = makeController(mode: .hidden)
        #expect(enterRemote(controller).count == 1)
        #expect(!controller.isFlashing)
        #expect(!controller.isPulsingRemote)
    }

    @Test func openingThePopoverEndsThePulse() {
        let controller = makeController()
        _ = enterRemote(controller)
        controller.tick()
        controller.popoverDidAppear()
        #expect(!controller.isPulsingRemote)
        #expect(controller.remotePulsePhase)
    }

    @Test func localEntryStillFlashesInSeparateMode() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.snapshot([]))
        #expect(controller.note(snapshot: Fixtures.needsYou("a")) == ["a"])
        #expect(controller.isFlashing)
        #expect(!controller.isPulsingRemote)
    }

    @Test func leavingMergedStopsARemoteFlash() {
        let controller = makeController(mode: .merged)
        _ = enterRemote(controller)
        #expect(controller.isFlashing)
        controller.remoteMode = .separate
        #expect(!controller.isFlashing)
    }
}

@Suite("Machine chip and offline marker")
struct MachineChipTests {
    private func m(_ id: String, _ name: String) -> Machine { Machine(id: id, name: name) }

    @Test func initialsTwoLettersThenDigits() {
        let labels = MachineChip.labels(for: [m("b", "studio"), m("a", "mini"), m("c", "Laptop")])
        #expect(labels == ["a": "M", "b": "S", "c": "L"])
        let two = MachineChip.labels(for: [m("a", "mini"), m("b", "mac-pro")])
        #expect(two == ["a": "Mi", "b": "Ma"])
        let digits = MachineChip.labels(for: [m("b", "mini-2"), m("a", "mini")])
        #expect(digits == ["a": "M1", "b": "M2"])
        #expect(MachineChip.initial("--") == "?")
    }

    @Test func labelsDoNotDependOnInputOrder() {
        let machines = [m("z", "mini"), m("y", "mini"), m("x", "studio")]
        #expect(MachineChip.labels(for: machines) == MachineChip.labels(for: machines.reversed()))
    }

    @Test func snapshotChipComesFromItsPeers() {
        let snapshot = Fixtures.snapshot([Fixtures.remote("r1", "running")], top: Fixtures.peers())
        #expect(snapshot.chipLabel(for: snapshot.sessions[0].machine!) == "M")
    }

    @Test func offlineRowGetsAHollowDiamondAndSaysSo() {
        let stale = Fixtures.decoded("x", extra: Fixtures.remote("s", "needs_you", stale: true))
        #expect(SessionColor.markerShape(for: stale) == .hollowDiamond)
        #expect(SessionColor.markerLabel(for: stale) == "Last known: Needs you, on Mini, offline")
        #expect(SessionColor.markerTooltip(for: stale).hasPrefix("On Mini, offline: last known state Needs you."))
    }
}
