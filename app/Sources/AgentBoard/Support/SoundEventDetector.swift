import Foundation

/// Decides which `AttentionEvent`s one snapshot raises, for Settings →
/// Sounds. Pure value type, fed every snapshot by `MenuBarLabel`.
///
/// Needs-you and turn-finished reuse the diffs that already drive the
/// flash and the notifications (`AttentionController.note`,
/// `TurnFinishDetector.note`), passed in so the sound, the banner and the
/// flash never disagree. Denied and review-ready are detected here:
///
/// - **denied**: a session's newest open denial id rose. Dismissing
///   denials never plays, and the high-water mark survives a session
///   disappearing (hidden, filtered), so it never replays.
/// - **reviewReady**: an attended session entered Interrupted, or entered
///   Review because a PR it opened awaits you or it raised an update note
///   (never for a plain finished turn: that is turn-finished). A review
///   plays at most once per distinct turn and reason, so a running/review
///   flap with an unchanged turn stays silent. A session that also
///   finished its turn or newly needs you in the same snapshot plays that
///   event instead, when that event has a sound.
///
/// Nothing plays for the first snapshot after launch. A session first seen
/// later (a backfill, Show hidden) plays only for a denial or a state
/// change since launch. Several sessions in one snapshot make one event per
/// kind, and the session selected in the key dashboard never plays.
struct SoundEventDetector {
    let startedAt: Date
    private var primed = false
    private var seen: Set<String> = []
    /// Per key: the highest open denial id seen.
    private var maxDenialID: [String: Int] = [:]
    private var previousState: [String: SessionState] = [:]
    /// Per key: the turn and reason of the last review-ready Review seen.
    private var reviewToken: [String: String] = [:]

    init(startedAt: Date = Date()) {
        self.startedAt = startedAt
    }

    /// `sounds` says whether an event currently has a sound
    /// (`SoundPlayer.sounds`): a review-ready session that also finished
    /// its turn or newly needs you is dropped only when that other event
    /// will play, so a review with Turn finished set to None still sounds.
    mutating func note(
        _ snapshot: Snapshot,
        needsYouEntered: Set<String>,
        turns: TurnFinishDetector.Changes,
        context: SessionNotificationPlan.Context,
        sounds: (AttentionEvent) -> Bool = { _ in true }
    ) -> Set<AttentionEvent> {
        let firing = primed
        primed = true
        func watched(_ key: String) -> Bool {
            !(context.dashboardIsKey && context.dashboardSelection == key)
        }

        var events: Set<AttentionEvent> = []
        if firing {
            if needsYouEntered.contains(where: watched) { events.insert(.needsYou) }
            if turns.finished.contains(where: { watched($0.key) }) { events.insert(.turnFinished) }
        }
        // Sessions whose review-ready sound another event already covers.
        var covered: Set<String> = []
        if sounds(.turnFinished) { covered.formUnion(turns.finished.map(\.key)) }
        if sounds(.needsYou) { covered.formUnion(needsYouEntered) }

        for session in snapshot.sessions {
            let key = session.key
            let firstSeen = seen.insert(key).inserted
            // A session first seen after launch counts only what happened since.
            let fresh = !firstSeen || (session.updatedDate.map { $0 >= startedAt } ?? false)
            let canPlay = firing && watched(key)

            if let newest = session.denials.map(\.id).max() {
                let previous = maxDenialID[key]
                let rose: Bool
                if firstSeen {
                    rose = session.denials.contains { ($0.date ?? .distantPast) >= startedAt }
                } else {
                    rose = previous.map { newest > $0 } ?? true
                }
                if rose && canPlay { events.insert(.denied) }
                maxDenialID[key] = max(newest, previous ?? newest)
            }

            let before = previousState[key]
            previousState[key] = session.state
            guard Self.attended(session) else { continue }
            let alsoOther = covered.contains(key)
            switch session.state {
            case .interrupted:
                let entered = firstSeen ? fresh : before != .interrupted
                if entered && canPlay && !alsoOther { events.insert(.reviewReady) }
            case .review where Self.awaitsReview(session):
                let token = "\(session.turnFinishedAt ?? "")\u{1F}\(session.waitingReason ?? "")"
                guard reviewToken[key] != token else { continue }
                reviewToken[key] = token
                if fresh && canPlay && !alsoOther { events.insert(.reviewReady) }
            default:
                break
            }
        }
        return events
    }

    /// The sessions `TurnFinishDetector.reportable` would consider:
    /// interactive, and not a loop or cron turn.
    static func attended(_ session: Session) -> Bool {
        guard session.interactive else { return false }
        switch session.turnPromptKind {
        case "loop", "cron": return false
        default: return true
        }
    }

    /// Review because a PR the session opened awaits you, or because it
    /// raised an update note: not a plain "Your turn" (turn-finished's job).
    static func awaitsReview(_ session: Session) -> Bool {
        guard session.state == .review else { return false }
        if session.links.contains(where: { $0.isPR && $0.awaitsYou == true }) { return true }
        return session.notes.contains { $0.kind == .update } && !isPlainTurnReason(session.waitingReason)
    }

    /// The reasons the CLI gives a Review that is just a finished turn
    /// (`reconcile.your_turn_reason`).
    static func isPlainTurnReason(_ reason: String?) -> Bool {
        guard let reason else { return true }
        return reason == "Your turn" || reason.hasPrefix("Your turn:") || reason == "Interrupted: your turn"
    }
}
