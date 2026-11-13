import Foundation
import Testing
@testable import AgentBoard

@Suite("SoundChoice")
struct SoundChoiceTests {
    @Test(arguments: [
        SoundChoice.none,
        .systemDefault,
        .system("Glass"),
        .system("Name With Spaces"),
        .file("/tmp/a sound.wav"),
        .file("/Volumes/My Disk/sounds/ding:2.mp3"),
        .file("/tmp/file:with:colons.aiff"),
    ])
    func roundTrips(_ choice: SoundChoice) {
        #expect(SoundChoice(encoded: choice.encoded) == choice)
    }

    @Test func encodings() {
        #expect(SoundChoice.none.encoded == "none")
        #expect(SoundChoice.systemDefault.encoded == "default")
        #expect(SoundChoice.system("Glass").encoded == "system:Glass")
        #expect(SoundChoice.file("/abs/path.wav").encoded == "file:/abs/path.wav")
    }

    @Test(arguments: ["", "bogus", "system:", "file:", "System:Glass", "none "])
    func unknownStringsDecodeAsNone(_ raw: String) {
        #expect(SoundChoice(encoded: raw) == .none)
    }

    @Test func labels() {
        #expect(SoundChoice.none.label == "None")
        #expect(SoundChoice.systemDefault.label == "System default")
        #expect(SoundChoice.system("Glass").label == "Glass")
        #expect(SoundChoice.file("/a/b/ding.wav").label == "ding.wav")
    }

    @Test func aMissingFileResolvesToNil() {
        let path = NSTemporaryDirectory() + "agentboard-missing-\(UUID().uuidString).wav"
        #expect(SoundChoice.file(path).resolvedURL() == nil)
    }

    @Test func aDirectoryResolvesToNil() {
        #expect(SoundChoice.file(NSTemporaryDirectory()).resolvedURL() == nil)
    }

    @Test func noneResolvesToNil() {
        #expect(SoundChoice.none.resolvedURL() == nil)
    }

    @Test func anExistingFileResolves() throws {
        let dir = try TempSoundsDir()
        defer { dir.remove() }
        let file = try dir.add("ding.wav")
        #expect(SoundChoice.file(file.path).resolvedURL()?.path == file.path)
    }

    @Test func systemSoundsAreListedFromTheFolderWithoutExtensions() throws {
        let dir = try TempSoundsDir()
        defer { dir.remove() }
        _ = try dir.add("Zeta.aiff")
        _ = try dir.add("alpha.wav")
        _ = try dir.add("notes.txt")
        #expect(SoundChoice.systemSoundNames(in: dir.url) == ["alpha", "Zeta"])
    }

    @Test func anUnreadableSoundsFolderListsNothing() {
        let missing = URL(fileURLWithPath: NSTemporaryDirectory() + "agentboard-none-\(UUID().uuidString)")
        #expect(SoundChoice.systemSoundNames(in: missing).isEmpty)
    }

    @Test func aSystemSoundResolvesByName() throws {
        let dir = try TempSoundsDir()
        defer { dir.remove() }
        let file = try dir.add("Glass.aiff")
        // The listing may spell the temp folder through /private.
        let resolved = SoundChoice.system("Glass").resolvedURL(soundsDirectory: dir.url)
        #expect(resolved?.resolvingSymlinksInPath() == file.resolvingSymlinksInPath())
        #expect(SoundChoice.system("Gone").resolvedURL(soundsDirectory: dir.url) == nil)
    }

    @Test func systemDefaultHasNoFile() {
        // It plays as the system beep, never from a file.
        #expect(SoundChoice.systemDefault.resolvedURL() == nil)
    }
}

@Suite("AttentionEvent")
struct AttentionEventTests {
    @Test func defaultsFollowTheNotificationToggle() {
        #expect(AttentionEvent.needsYou.defaultChoice(notifyOn: true) == .systemDefault)
        #expect(AttentionEvent.turnFinished.defaultChoice(notifyOn: true) == .systemDefault)
        #expect(AttentionEvent.needsYou.defaultChoice(notifyOn: false) == .none)
        #expect(AttentionEvent.turnFinished.defaultChoice(notifyOn: false) == .none)
        for notifyOn in [true, false] {
            #expect(AttentionEvent.denied.defaultChoice(notifyOn: notifyOn) == .none)
            #expect(AttentionEvent.reviewReady.defaultChoice(notifyOn: notifyOn) == .none)
        }
    }

    @Test func emptyCountsAsNeverPicked() {
        #expect(AttentionEvent.needsYou.choice(stored: "", notifyOn: true) == .systemDefault)
        #expect(AttentionEvent.needsYou.choice(stored: nil, notifyOn: false) == .none)
        #expect(AttentionEvent.needsYou.choice(stored: "none", notifyOn: true) == .none)
    }

    @Test func unsetWithTheToggleOnPlaysTheAlertSound() {
        let suite = TestDefaults()
        suite.defaults.set(true, forKey: SettingsKeys.notifyOnNeedsYou)
        suite.defaults.set(true, forKey: SettingsKeys.notifyOnTurnFinished)
        #expect(AttentionEvent.needsYou.storedChoice(in: suite.defaults) == .systemDefault)
        #expect(AttentionEvent.turnFinished.storedChoice(in: suite.defaults) == .systemDefault)
    }

    @Test func unsetWithTheToggleOffIsSilent() {
        let suite = TestDefaults()
        #expect(AttentionEvent.needsYou.storedChoice(in: suite.defaults) == .none)
        suite.defaults.set(false, forKey: SettingsKeys.notifyOnTurnFinished)
        #expect(AttentionEvent.turnFinished.storedChoice(in: suite.defaults) == .none)
    }

    @Test func anExplicitNoneWinsOverTheToggle() {
        let suite = TestDefaults()
        suite.defaults.set(true, forKey: SettingsKeys.notifyOnNeedsYou)
        suite.defaults.set("none", forKey: SettingsKeys.soundNeedsYou)
        #expect(AttentionEvent.needsYou.storedChoice(in: suite.defaults) == .none)
    }

    @Test func anExplicitSoundPlaysWithTheToggleOff() {
        let suite = TestDefaults()
        suite.defaults.set(false, forKey: SettingsKeys.notifyOnTurnFinished)
        suite.defaults.set("system:Glass", forKey: SettingsKeys.soundTurnFinished)
        #expect(AttentionEvent.turnFinished.storedChoice(in: suite.defaults) == .system("Glass"))
    }

    @Test func storedChoiceReadsEachKeyAndFallsBackToTheDefault() {
        let suite = TestDefaults()
        for event in AttentionEvent.allCases {
            #expect(event.storedChoice(in: suite.defaults) == event.defaultChoice(notifyOn: false))
        }
        suite.defaults.set("system:Glass", forKey: SettingsKeys.soundNeedsYou)
        suite.defaults.set("none", forKey: SettingsKeys.soundTurnFinished)
        suite.defaults.set("file:/a b/c.wav", forKey: SettingsKeys.soundDenied)
        suite.defaults.set("default", forKey: SettingsKeys.soundReviewReady)
        #expect(AttentionEvent.needsYou.storedChoice(in: suite.defaults) == .system("Glass"))
        #expect(AttentionEvent.turnFinished.storedChoice(in: suite.defaults) == .none)
        #expect(AttentionEvent.denied.storedChoice(in: suite.defaults) == .file("/a b/c.wav"))
        #expect(AttentionEvent.reviewReady.storedChoice(in: suite.defaults) == .systemDefault)
    }

    @Test func theSoundKeysAreSettings() {
        for key in [SettingsKeys.soundNeedsYou, SettingsKeys.soundTurnFinished,
                    SettingsKeys.soundDenied, SettingsKeys.soundReviewReady] {
            #expect(SettingsKeys.all.contains(key))
        }
    }
}

/// A temporary folder of (empty) files standing in for sounds.
struct TempSoundsDir {
    let url: URL

    init() throws {
        url = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentboard-sounds-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    }

    func add(_ name: String) throws -> URL {
        let file = url.appendingPathComponent(name)
        try Data([0]).write(to: file)
        return file
    }

    func remove() {
        try? FileManager.default.removeItem(at: url)
    }
}
