import Foundation
import Testing
@testable import AgentBoard

/// Records what the player asks for instead of making noise.
@MainActor
final class FakeSoundBackend: SoundBackend {
    final class Sound {
        let url: URL
        init(url: URL) { self.url = url }
    }

    var loaded: [URL] = []
    var played: [URL] = []
    var stopped: [URL] = []
    var beeps = 0
    var unloadable: Set<URL> = []
    var startFails = false
    var finish: [ObjectIdentifier: @MainActor () -> Void] = [:]

    func load(_ url: URL) -> AnyObject? {
        loaded.append(url)
        return unloadable.contains(url) ? nil : Sound(url: url)
    }

    func play(_ sound: AnyObject, onFinish: @escaping @MainActor () -> Void) -> Bool {
        let sound = sound as! Sound
        guard !startFails else { return false }
        played.append(sound.url)
        finish[ObjectIdentifier(sound)] = onFinish
        return true
    }

    func stop(_ sound: AnyObject) {
        let sound = sound as! Sound
        finish[ObjectIdentifier(sound)] = nil
        stopped.append(sound.url)
    }

    func beep() { beeps += 1 }

    /// Every playing sound reaches its end.
    func finishAll() {
        let handlers = finish.values
        finish = [:]
        for handler in handlers { handler() }
    }
}

@Suite("SoundPlayer")
@MainActor
struct SoundPlayerTests {
    private let backend = FakeSoundBackend()
    private let suite = TestDefaults()

    /// A player whose choices resolve `file:` paths as given (whether or not
    /// they exist) and `system:` names under /sounds; `missing` paths
    /// resolve to nil. `.systemDefault` never reaches `resolve` (it beeps).
    private func player(missing: Set<String> = [], notice: @escaping (String) -> Void = { _ in }) -> SoundPlayer {
        SoundPlayer(
            backend: backend,
            defaults: suite.defaults,
            resolve: { choice in
                switch choice {
                case .none, .systemDefault: return nil
                case .system(let name): return missing.contains(name) ? nil : URL(fileURLWithPath: "/sounds/\(name).aiff")
                case .file(let path): return missing.contains(path) ? nil : URL(fileURLWithPath: path)
                }
            },
            notice: notice
        )
    }

    @Test func noneLoadsAndPlaysNothing() {
        let player = player()
        player.play(.none, for: .needsYou)
        player.play(.none, for: nil)
        #expect(backend.loaded.isEmpty)
        #expect(backend.played.isEmpty)
        #expect(backend.beeps == 0)
    }

    @Test func aConfiguredEventPlaysExactlyOnce() {
        suite.defaults.set("system:Glass", forKey: SettingsKeys.soundDenied)
        let player = player()
        player.play([.denied])
        #expect(backend.played == [URL(fileURLWithPath: "/sounds/Glass.aiff")])
        #expect(player.playing[.event(.denied)] != nil)
    }

    @Test func eventsWithoutASoundStaySilent() {
        // denied and reviewReady default to None.
        let player = player()
        player.play([.denied, .reviewReady])
        #expect(backend.played.isEmpty)
    }

    @Test func theDefaultsWithBothTogglesOnBeepOnceForTwoEvents() {
        suite.defaults.set(true, forKey: SettingsKeys.notifyOnNeedsYou)
        suite.defaults.set(true, forKey: SettingsKeys.notifyOnTurnFinished)
        let player = player()
        player.play([.needsYou, .turnFinished])
        #expect(backend.beeps == 1)
        #expect(backend.played.isEmpty)
    }

    @Test func theDefaultsWithTheTogglesOffAreSilent() {
        let player = player()
        player.play([.needsYou, .turnFinished, .denied, .reviewReady])
        #expect(backend.beeps == 0)
        #expect(backend.played.isEmpty)
        #expect(AttentionEvent.allCases.allSatisfy { !player.sounds($0) })
    }

    @Test func soundsReportsTheEffectiveChoice() {
        suite.defaults.set(true, forKey: SettingsKeys.notifyOnNeedsYou)
        suite.defaults.set("system:Glass", forKey: SettingsKeys.soundReviewReady)
        suite.defaults.set("none", forKey: SettingsKeys.soundTurnFinished)
        let player = player()
        #expect(player.sounds(.needsYou))
        #expect(!player.sounds(.turnFinished))
        #expect(!player.sounds(.denied))
        #expect(player.sounds(.reviewReady))
    }

    @Test func differentSoundsForTwoEventsBothPlay() {
        suite.defaults.set("system:Glass", forKey: SettingsKeys.soundNeedsYou)
        suite.defaults.set("system:Ping", forKey: SettingsKeys.soundDenied)
        let player = player()
        player.play([.needsYou, .denied])
        #expect(Set(backend.played) == [URL(fileURLWithPath: "/sounds/Glass.aiff"),
                                        URL(fileURLWithPath: "/sounds/Ping.aiff")])
    }

    @Test func aMissingFileLogsOnceAndPlaysNothing() {
        var notices: [String] = []
        let player = player(missing: ["/gone/ding.wav"], notice: { notices.append($0) })
        player.play(.file("/gone/ding.wav"), for: .needsYou)
        player.play(.file("/gone/ding.wav"), for: .needsYou)
        player.play(.file("/gone/ding.wav"), for: nil)
        #expect(backend.played.isEmpty)
        #expect(backend.beeps == 0)
        #expect(notices == ["sound file cannot be played: /gone/ding.wav"])
    }

    @Test func anUndecodableFileLogsOnce() {
        var notices: [String] = []
        let player = player(notice: { notices.append($0) })
        backend.unloadable = [URL(fileURLWithPath: "/bad.mp3")]
        player.play(.file("/bad.mp3"), for: .denied)
        player.play(.file("/bad.mp3"), for: .denied)
        #expect(backend.played.isEmpty)
        #expect(notices.count == 1)
    }

    @Test func aMissingSystemSoundLogsOnce() {
        var notices: [String] = []
        let player = player(missing: ["Gone"], notice: { notices.append($0) })
        player.play(.system("Gone"), for: nil)
        player.play(.system("Gone"), for: nil)
        #expect(notices == ["system sound not found: Gone"])
    }

    @Test func systemDefaultIsTheSystemBeep() {
        var notices: [String] = []
        let player = player(notice: { notices.append($0) })
        player.play(.systemDefault, for: .needsYou)
        player.play(.systemDefault, for: nil)
        #expect(backend.beeps == 2)
        #expect(backend.loaded.isEmpty)
        #expect(backend.played.isEmpty)
        #expect(player.playing.isEmpty)
        #expect(notices.isEmpty)
    }

    @Test func aReplayStopsThePriorSoundOfTheSameEvent() {
        let player = player()
        player.play(.system("Glass"), for: .needsYou)
        player.play(.system("Ping"), for: .needsYou)
        #expect(backend.stopped == [URL(fileURLWithPath: "/sounds/Glass.aiff")])
        #expect(backend.played.count == 2)
    }

    @Test func anotherEventDoesNotStopIt() {
        let player = player()
        player.play(.system("Glass"), for: .needsYou)
        player.play(.system("Ping"), for: .denied)
        player.play(.system("Pop"), for: nil)
        #expect(backend.stopped.isEmpty)
    }

    @Test func aPreviewStopsThePriorPreview() {
        let player = player()
        player.play(.system("Glass"), for: nil)
        player.play(.none, for: nil)
        #expect(backend.stopped == [URL(fileURLWithPath: "/sounds/Glass.aiff")])
        #expect(player.playing[.preview] == nil)
    }

    @Test func aFinishedSoundIsReleased() {
        let player = player()
        player.play(.system("Glass"), for: .needsYou)
        #expect(player.playing[.event(.needsYou)] != nil)
        backend.finishAll()
        #expect(player.playing.isEmpty)
        // Nothing left to stop.
        player.play(.system("Ping"), for: .needsYou)
        #expect(backend.stopped.isEmpty)
    }

    @Test func aSoundThatDoesNotStartIsReleasedAndLogged() {
        var notices: [String] = []
        let player = player(notice: { notices.append($0) })
        backend.startFails = true
        player.play(.file("/x.wav"), for: .denied)
        #expect(player.playing.isEmpty)
        #expect(notices.count == 1)
    }

    /// A choice that will only log a notice does not count as a sound, so
    /// it never covers (and silences) a review-ready sound.
    @Test func soundsIsFalseForAChoiceThatDoesNotResolve() {
        suite.defaults.set("file:/gone.wav", forKey: SettingsKeys.soundTurnFinished)
        suite.defaults.set("system:Gone", forKey: SettingsKeys.soundNeedsYou)
        suite.defaults.set("default", forKey: SettingsKeys.soundDenied)
        suite.defaults.set("file:/here.wav", forKey: SettingsKeys.soundReviewReady)
        let player = player(missing: ["/gone.wav", "Gone"])
        #expect(!player.sounds(.turnFinished))
        #expect(!player.sounds(.needsYou))
        #expect(player.sounds(.denied))
        #expect(player.sounds(.reviewReady))
    }
}
