import AppKit
import Testing
@testable import AgentBoard

@Suite("MenuBarIconState.tier")
struct MenuBarIconTierTests {
    typealias Tier = MenuBarIconState.Tier

    private func tier(_ snapshot: Snapshot) -> Tier {
        MenuBarIconState.tier(for: snapshot.counts, sessions: snapshot.sessions)
    }

    private func tier(_ sessions: [[String: Any]]) -> Tier {
        tier(Fixtures.snapshot(sessions))
    }

    @Test func needsYouWinsOverEverything() {
        #expect(tier([
            Fixtures.session("d", "done"),
            Fixtures.session("r", "running", alive: true),
            Fixtures.session("v", "review", alive: true),
            Fixtures.session("i", "interrupted"),
            Fixtures.session("n", "needs_you", alive: true),
        ]) == .needsYou)
    }

    @Test func interruptedBeatsReviewAndRunning() {
        #expect(tier([
            Fixtures.session("r", "running", alive: true),
            Fixtures.session("v", "review", alive: true),
            Fixtures.session("i", "interrupted"),
        ]) == .interrupted)
    }

    @Test func aliveReviewBeatsRunning() {
        #expect(tier([
            Fixtures.session("r", "running", alive: true),
            Fixtures.session("v", "review", alive: true),
        ]) == .review)
    }

    @Test func closedReviewDoesNotCountAsReview() {
        // A review session whose process is gone is "closed" (amber), so a
        // running session outranks it.
        #expect(tier([
            Fixtures.session("r", "running", alive: true),
            Fixtures.session("v", "review", alive: false),
        ]) == .running)
        #expect(tier([Fixtures.session("v", "review", alive: false)]) == .closed)
    }

    @Test func runningOnly() {
        #expect(tier([Fixtures.session("r", "running", alive: true)]) == .running)
    }

    @Test func unconcludedOnlyIsClosed() {
        #expect(tier([
            Fixtures.session("u", "unconcluded"),
            Fixtures.session("d", "done"),
        ]) == .closed)
    }

    @Test func emptyBoardIsIdle() {
        #expect(tier([]) == .idle)
    }

    @Test func doneAndSnoozedOnlyIsIdle() {
        #expect(tier([
            Fixtures.session("d", "done"),
            Fixtures.session("s", "snoozed"),
        ]) == .idle)
    }

    @Test func unknownOnlyIsIdle() {
        // An unrecognized state string decodes to .unknown.
        #expect(tier([Fixtures.session("x", "some_future_state")]) == .idle)
    }

    @Test func countsCoverSessionsMissingFromTheList() {
        let snapshot = Fixtures.snapshot([], counts: ["needs_you": 1])
        #expect(tier(snapshot) == .needsYou)
        #expect(tier(Fixtures.snapshot([], counts: ["running": 2])) == .running)
        #expect(tier(Fixtures.snapshot([], counts: ["unconcluded": 1])) == .closed)
    }

    @Test func tierColorsComeFromSessionColor() {
        #expect(Tier.needsYou.nsColor === SessionColor.needsYouNSColor)
        #expect(Tier.interrupted.nsColor === SessionColor.interruptedNSColor)
        #expect(Tier.review.nsColor === SessionColor.reviewOpenNSColor)
        #expect(Tier.running.nsColor === SessionColor.runningNSColor)
        #expect(Tier.closed.nsColor === SessionColor.closedNSColor)
        #expect(Tier.idle.nsColor == nil)
    }
}

@Suite("MenuBarIconRenderer")
@MainActor
struct MenuBarIconRendererTests {
    /// Most opaque pixel of the image, in sRGB.
    private func strongestPixel(_ image: NSImage) -> NSColor? {
        guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
        let rep = NSBitmapImageRep(cgImage: cg)
        var best: NSColor?
        for x in 0..<rep.pixelsWide {
            for y in 0..<rep.pixelsHigh {
                guard let color = rep.colorAt(x: x, y: y)?.usingColorSpace(.sRGB) else { continue }
                if color.alphaComponent > (best?.alphaComponent ?? 0) { best = color }
            }
        }
        return best
    }

    private func countPixels(_ image: NSImage, where match: (NSColor) -> Bool) -> Int {
        guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return 0 }
        let rep = NSBitmapImageRep(cgImage: cg)
        var count = 0
        for x in 0..<rep.pixelsWide {
            for y in 0..<rep.pixelsHigh {
                if let color = rep.colorAt(x: x, y: y)?.usingColorSpace(.sRGB), match(color) { count += 1 }
            }
        }
        return count
    }

    @Test(arguments: [false, true])
    func needsYouGlyphIsRedAndNotTemplate(dark: Bool) throws {
        let image = try #require(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .needsYou, darkMenuBar: dark)))
        #expect(!image.isTemplate)
        #expect(image.size.width > 0 && image.size.width <= 20)
        let pixel = try #require(strongestPixel(image))
        #expect(pixel.alphaComponent > 0.9)
        #expect(pixel.redComponent > 0.9)
        #expect(pixel.greenComponent < 0.5)
        #expect(pixel.blueComponent < 0.5)
    }

    @Test func idleHasNoColoredImage() {
        #expect(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .idle, darkMenuBar: false)) == nil)
    }

    @Test func dimmedPhaseIsFainterButNotEmpty() throws {
        let solid = try #require(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .needsYou, darkMenuBar: false)))
        let dimmed = try #require(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .needsYou, dimmed: true, darkMenuBar: false)))
        let solidAlpha = try #require(strongestPixel(solid)).alphaComponent
        let dimmedAlpha = try #require(strongestPixel(dimmed)).alphaComponent
        #expect(dimmedAlpha > 0.2)
        #expect(dimmedAlpha < solidAlpha - 0.3)
        #expect(dimmed.size == solid.size)
    }

    @Test func alertBadgeAddsWhitePixels() throws {
        let plain = try #require(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .needsYou, darkMenuBar: true)))
        let alert = try #require(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .needsYou, alert: true, darkMenuBar: true)))
        let isWhite: (NSColor) -> Bool = {
            $0.alphaComponent > 0.8 && $0.redComponent > 0.9 && $0.greenComponent > 0.9 && $0.blueComponent > 0.9
        }
        #expect(countPixels(plain, where: isWhite) == 0)
        #expect(countPixels(alert, where: isWhite) > 0)
        #expect(alert.size == plain.size)
    }

    @Test func closedAmberResolvesPerMenuBarAppearance() throws {
        // The dynamic amber is darker on a light menu bar than on a dark one.
        let light = try #require(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .closed, darkMenuBar: false)))
        let dark = try #require(MenuBarIconRenderer.image(for: MenuBarIconStyle(tier: .closed, darkMenuBar: true)))
        let lightPixel = try #require(strongestPixel(light))
        let darkPixel = try #require(strongestPixel(dark))
        #expect(lightPixel.greenComponent < darkPixel.greenComponent - 0.2)
    }

    @Test func resolveUsesTheGivenAppearanceNotTheApp() {
        let light = MenuBarIconRenderer.resolve(SessionColor.closedNSColor, dark: false)
        let dark = MenuBarIconRenderer.resolve(SessionColor.closedNSColor, dark: true)
        #expect(abs(light.redComponent - 0.58) < 0.02)
        #expect(dark.redComponent > 0.9)
    }
}

@Suite("Fixture snapshots")
struct FixtureSnapshotTests {
    /// app/Fixtures, found relative to this source file.
    private static let fixturesDir = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Fixtures")

    private func tier(_ name: String) throws -> MenuBarIconState.Tier {
        let data = try Data(contentsOf: Self.fixturesDir.appendingPathComponent(name))
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: data)
        return MenuBarIconState.tier(for: snapshot.counts, sessions: snapshot.sessions)
    }

    @Test func needsYouFixtureIsRed() throws {
        #expect(try tier("snapshot-needs-you.json") == .needsYou)
    }

    @Test func quietFixtureIsTemplate() throws {
        #expect(try tier("snapshot-quiet.json") == .idle)
    }

    @Test func bundledFixtureIsNeedsYou() throws {
        #expect(try tier("snapshot.json") == .needsYou)
    }
}
