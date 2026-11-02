import AppKit
import OSLog

/// Loads and plays sound files. `NSSoundBackend` in the app; tests use a
/// fake so they never make noise. A loaded sound is opaque to the player.
@MainActor
protocol SoundBackend: AnyObject {
    /// A playable sound for the file, or nil when it cannot be decoded.
    func load(_ url: URL) -> AnyObject?
    /// Starts `sound`. `onFinish` runs on the main actor once it has
    /// played to the end (not after `stop`). False when it did not start.
    func play(_ sound: AnyObject, onFinish: @escaping @MainActor () -> Void) -> Bool
    func stop(_ sound: AnyObject)
    /// The system beep (`NSSound.beep()`), for `.systemDefault`: the
    /// user's alert sound at the alert volume.
    func beep()
}

/// Plays the sound configured for an `AttentionEvent` (Settings →
/// Sounds), or a chosen sound as a Settings preview. Independent of the
/// notification toggles and of notification permission.
///
/// Keeps each playing sound until it finishes (an `NSSound` nobody holds
/// stops), and a new sound for the same event, or a new preview, stops the
/// one still playing. A file that cannot be loaded logs one notice per
/// path and plays nothing.
@MainActor
final class SoundPlayer {
    static let shared = SoundPlayer()

    /// What a playing sound belongs to; a newer sound in the same slot
    /// replaces it.
    enum Slot: Hashable {
        case event(AttentionEvent)
        case preview
    }

    private let backend: SoundBackend
    private let choiceFor: (AttentionEvent) -> SoundChoice
    private let resolve: (SoundChoice) -> URL?
    private let notice: (String) -> Void
    private(set) var playing: [Slot: AnyObject] = [:]
    /// Choices (by encoded string) already reported as unloadable.
    private var reported: Set<String> = []

    /// `defaults` holds the per-event choices (the app's own domain;
    /// tests pass an in-memory suite). `resolve` maps a choice to its file.
    init(
        backend: SoundBackend? = nil,
        defaults: UserDefaults = .standard,
        resolve: ((SoundChoice) -> URL?)? = nil,
        notice: ((String) -> Void)? = nil
    ) {
        self.backend = backend ?? NSSoundBackend()
        self.choiceFor = { $0.storedChoice(in: defaults) }
        self.resolve = resolve ?? { $0.resolvedURL() }
        let log = Logger(subsystem: "io.github.agentboard", category: "sounds")
        self.notice = notice ?? { message in log.notice("\(message, privacy: .public)") }
    }

    /// Whether `event` currently has a sound that will play: System
    /// default, or a system sound or file that resolves (not None, not a
    /// file that is gone, not a system sound this Mac lacks).
    /// `SoundEventDetector` uses it to drop a review-ready sound only when
    /// the event that covers it will actually play. Resolving stats the
    /// file, at most once per event per snapshot change.
    func sounds(_ event: AttentionEvent) -> Bool {
        let choice = choiceFor(event)
        return choice == .systemDefault || resolve(choice) != nil
    }

    /// Plays each event's configured sound, each distinct sound once (two
    /// events set to the same sound in one snapshot ding once).
    func play(_ events: Set<AttentionEvent>) {
        var played: Set<SoundChoice> = []
        for event in AttentionEvent.allCases where events.contains(event) {
            let choice = choiceFor(event)
            guard choice != .none, played.insert(choice).inserted else { continue }
            play(choice, for: event)
        }
    }

    /// Plays `choice` for `event`, or as a Settings preview when `event`
    /// is nil. Stops the sound still playing in the same slot first.
    func play(_ choice: SoundChoice, for event: AttentionEvent?) {
        let slot: Slot = event.map(Slot.event) ?? .preview
        if let previous = playing[slot] {
            playing[slot] = nil
            backend.stop(previous)
        }
        guard choice != .none else { return }
        if choice == .systemDefault {
            // The beep plays the user's alert sound at the alert volume,
            // like a notification's default sound did. It cannot be held
            // or stopped, so it never occupies the slot.
            backend.beep()
            return
        }

        guard let url = resolve(choice), let sound = backend.load(url) else {
            report(choice)
            return
        }
        playing[slot] = sound
        let started = backend.play(sound) { [weak self] in
            guard let self, self.playing[slot] === sound else { return }
            self.playing[slot] = nil
        }
        if !started {
            playing[slot] = nil
            report(choice)
        }
    }

    private func report(_ choice: SoundChoice) {
        guard reported.insert(choice.encoded).inserted else { return }
        switch choice {
        case .file(let path):
            notice("sound file cannot be played: \(path)")
        case .system(let name):
            notice("system sound not found: \(name)")
        case .none, .systemDefault:
            notice("sound cannot be played: \(choice.encoded)")
        }
    }
}

/// Plays through `NSSound`, which needs no permission and never activates
/// the app.
@MainActor
final class NSSoundBackend: NSObject, SoundBackend, NSSoundDelegate {
    private var finishHandlers: [ObjectIdentifier: @MainActor () -> Void] = [:]

    func load(_ url: URL) -> AnyObject? {
        NSSound(contentsOf: url, byReference: true)
    }

    func play(_ sound: AnyObject, onFinish: @escaping @MainActor () -> Void) -> Bool {
        guard let sound = sound as? NSSound else { return false }
        sound.delegate = self
        let id = ObjectIdentifier(sound)
        finishHandlers[id] = onFinish
        if sound.play() { return true }
        finishHandlers[id] = nil
        return false
    }

    func stop(_ sound: AnyObject) {
        guard let sound = sound as? NSSound else { return }
        finishHandlers[ObjectIdentifier(sound)] = nil
        sound.stop()
    }

    func beep() {
        NSSound.beep()
    }

    nonisolated func sound(_ sound: NSSound, didFinishPlaying flag: Bool) {
        // Only the identifier crosses to the main actor; NSSound is not Sendable.
        let id = ObjectIdentifier(sound)
        Task { @MainActor in
            let handler = self.finishHandlers[id]
            self.finishHandlers[id] = nil
            handler?()
        }
    }
}
