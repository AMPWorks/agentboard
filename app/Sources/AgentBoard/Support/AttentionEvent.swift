import Foundation

/// An event that can play a sound (Settings → Sounds). Detected by
/// `SoundEventDetector`, played by `SoundPlayer`.
enum AttentionEvent: String, CaseIterable, Identifiable, Hashable, Sendable {
    /// A session newly needs you (the same diff that flashes the icon).
    case needsYou
    /// An attended session finished its turn (the same diff as the
    /// turn-finished notification).
    case turnFinished
    /// A session recorded a new permission denial.
    case denied
    /// A session newly entered Interrupted, or Review because a PR it
    /// opened awaits you or it raised an update note.
    case reviewReady

    var id: String { rawValue }

    /// The Settings row label.
    var label: String {
        switch self {
        case .needsYou: return "Needs you"
        case .turnFinished: return "Turn finished"
        case .denied: return "Permission denied"
        case .reviewReady: return "Interrupted or awaiting review"
        }
    }

    /// Whether this event has a notification toggle (Settings →
    /// Notifications) that sets its default sound.
    var hasNotificationToggle: Bool {
        switch self {
        case .needsYou, .turnFinished: return true
        case .denied, .reviewReady: return false
        }
    }

    /// What plays until the user picks a sound for this event. Needs you
    /// and turn finished play the alert sound while their notification
    /// toggle is on (the sound the banner played before sounds were
    /// configurable) and nothing while it is off, so an install that never
    /// had a notification sound stays silent. The others default to silence.
    func defaultChoice(notifyOn: Bool) -> SoundChoice {
        hasNotificationToggle && notifyOn ? .systemDefault : .none
    }

    /// The effective choice for a stored value: `raw` when the user picked
    /// something (an explicit `none` included), else `defaultChoice`. A
    /// missing or empty value counts as never picked.
    func choice(stored raw: String?, notifyOn: Bool) -> SoundChoice {
        guard let raw, !raw.isEmpty else { return defaultChoice(notifyOn: notifyOn) }
        return SoundChoice(encoded: raw)
    }

    /// The effective choice in `defaults`, the one place the player, the
    /// detector and Settings resolve it.
    func storedChoice(in defaults: UserDefaults) -> SoundChoice {
        let raw: String?
        let notifyOn: Bool
        switch self {
        case .needsYou:
            raw = defaults.string(forKey: SettingsKeys.soundNeedsYou)
            notifyOn = defaults.bool(forKey: SettingsKeys.notifyOnNeedsYou)
        case .turnFinished:
            raw = defaults.string(forKey: SettingsKeys.soundTurnFinished)
            notifyOn = defaults.bool(forKey: SettingsKeys.notifyOnTurnFinished)
        case .denied:
            raw = defaults.string(forKey: SettingsKeys.soundDenied)
            notifyOn = false
        case .reviewReady:
            raw = defaults.string(forKey: SettingsKeys.soundReviewReady)
            notifyOn = false
        }
        return choice(stored: raw, notifyOn: notifyOn)
    }
}
