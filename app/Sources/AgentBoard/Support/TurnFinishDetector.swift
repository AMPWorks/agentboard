import Foundation

/// Decides which sessions just finished a turn worth a notification, from
/// one snapshot to the next. Pure value type: `MenuBarLabel` feeds it every
/// snapshot (whether or not the toggle is on, so turning it on never
/// replays old turns) and `NotificationManager` posts what it reports.
///
/// A finished turn is identified by the CLI's `turn_finished_at` (the
/// newest parent Stop's time), which an idle reminder, a kept marker event
/// or a registry flap never changes, so each turn is considered once.
struct TurnFinishDetector {
    struct Changes: Equatable {
        /// Sessions whose turn just finished and should notify.
        var finished: [Session] = []
        /// Keys whose last considered turn the user has since answered (a
        /// new prompt that is not injected): their delivered notification
        /// can go. Never also in `finished`.
        var answered: [String] = []

        var isEmpty: Bool { finished.isEmpty && answered.isEmpty }
    }

    let startedAt: Date
    private var primed = false
    private var seen: Set<String> = []
    /// Per key: the `turn_finished_at` last considered (reported or not).
    private var notifiedAt: [String: String] = [:]

    init(startedAt: Date = Date()) {
        self.startedAt = startedAt
    }

    mutating func note(_ snapshot: Snapshot) -> Changes {
        var changes = Changes()
        guard primed else {
            // Sessions already in Review at launch never notify.
            for session in snapshot.sessions {
                seen.insert(session.key)
                if let finished = session.turnFinishedAt { notifiedAt[session.key] = finished }
            }
            primed = true
            return changes
        }
        for session in snapshot.sessions {
            let key = session.key
            let current = session.turnFinishedAt
            if !seen.contains(key) {
                seen.insert(key)
                // A session first seen after launch (a backfill, Show hidden)
                // notifies only for a turn that finished since launch.
                guard let date = session.turnFinishedDate, date >= startedAt else {
                    if let current { notifiedAt[key] = current }
                    continue
                }
            }
            let previous = notifiedAt[key]
            guard current != previous else { continue }

            if let current, session.state != .running {
                // Considered once, reported or not. A `running` row with a
                // finished ts is a registry flap (or a loop idle, whose
                // prompt kind keeps it silent anyway): wait for it to settle.
                notifiedAt[key] = current
                if Self.reportable(session) {
                    changes.finished.append(session)
                    continue
                }
            }
            // An injected prompt (a task notification, another agent's
            // message) is not the user replying.
            if previous != nil, session.turnPromptKind != "injected" {
                changes.answered.append(key)
                if notifiedAt[key] == previous { notifiedAt[key] = nil }
            }
        }
        return changes
    }

    /// An attended, interactive session's finished turn in Review. An
    /// injected turn counts only when it reports a background task back
    /// (the user's own request finishing); loop and cron turns never do.
    static func reportable(_ session: Session) -> Bool {
        guard session.interactive, session.state == .review, session.turnFinishedAt != nil else {
            return false
        }
        switch session.turnPromptKind {
        case "injected": return session.turnEndsBackgroundWait
        case "loop", "cron": return false
        default: return true
        }
    }
}
