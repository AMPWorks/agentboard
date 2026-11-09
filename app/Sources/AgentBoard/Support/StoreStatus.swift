import Foundation

/// A failed user action (mark, focus, dismiss, ...) or background chore
/// (prune), shown in the status banner for `StoreStatus.actionErrorLifetime`.
struct ActionError: Equatable, Sendable {
    /// What failed, e.g. "mark done" or "Prune".
    let action: String
    let message: String
    let at: Date
    /// The settings pane that fixes it (a failed focus only).
    var remedy: FocusRemedy? = nil
}

/// The one-line banner the popover and the dashboard show when the CLI or
/// the database is failing, as a pure rule so it is unit-tested. It never
/// feeds the menu bar icon or attention state.
enum StoreStatus {
    struct Banner: Equatable, Sendable {
        enum Severity: Equatable, Sendable {
            case error
            case warning
        }

        let text: String
        let severity: Severity
        /// The full message, for the tooltip (the text is cut to one line).
        let detail: String?
        /// True for an action error, which the user can dismiss.
        let isActionError: Bool
        /// A settings pane the banner offers a button for.
        var remedy: FocusRemedy? = nil
    }

    /// A `refresh()` still in flight after this long means the CLI hangs.
    static let refreshStaleAfter: TimeInterval = 30
    /// No successful snapshot this long after `start()` means the CLI is
    /// not answering at all.
    static let firstSnapshotGrace: TimeInterval = 45
    /// How long a failed action stays in the banner.
    static let actionErrorLifetime: TimeInterval = 15

    /// The banner to show, or nil when everything is healthy. Order: a
    /// failed snapshot, then a hanging CLI, then a recent action error.
    ///
    /// Staleness is deliberately not "`lastUpdated` is old": the poll loop
    /// sleeps, so after sleep/wake or a long stretch with the popover closed
    /// `lastUpdated` is old with nothing wrong. Only a refresh that is
    /// actually in flight, or no success at all since launch, counts.
    static func banner(
        lastError: String?,
        lastActionError: ActionError?,
        lastUpdated: Date?,
        refreshStartedAt: Date?,
        startedAt: Date?,
        now: Date
    ) -> Banner? {
        if let lastError {
            return Banner(text: "Snapshot failed: \(oneLine(lastError))", severity: .error,
                          detail: lastError, isActionError: false)
        }
        if let refreshStartedAt, now.timeIntervalSince(refreshStartedAt) > refreshStaleAfter {
            let seconds = Int(now.timeIntervalSince(refreshStartedAt))
            return Banner(text: "Waiting on the CLI for \(seconds) s", severity: .warning,
                          detail: nil, isActionError: false)
        }
        if lastUpdated == nil, let startedAt, now.timeIntervalSince(startedAt) > firstSnapshotGrace {
            return Banner(text: "No snapshot yet; the CLI is not answering", severity: .warning,
                          detail: nil, isActionError: false)
        }
        if let error = lastActionError, isLive(error, now: now) {
            return Banner(text: "\(error.action) failed: \(oneLine(error.message))", severity: .warning,
                          detail: error.message, isActionError: true, remedy: error.remedy)
        }
        return nil
    }

    /// The banner text for a failed snapshot. A JSON decoding error's own
    /// description ("The data couldn't be read because it isn't in the
    /// correct format.") says nothing about the cause, so it is named, with
    /// the likely fix; the full error goes to the app log.
    static func snapshotErrorMessage(_ error: Error) -> String {
        if error is DecodingError {
            return "the CLI's snapshot is in a format this app can't read. The app and the CLI may be different versions; reinstall AgentBoard to update both."
        }
        return error.localizedDescription
    }

    /// The popover's text for a board with no sessions. Before the first
    /// snapshot it says so, rather than "No sessions yet", which would read
    /// as an empty board while the banner reports a failure.
    static func emptyBoardText(lastError: String?, lastUpdated: Date?) -> String {
        guard lastUpdated == nil else { return "No sessions yet" }
        return lastError == nil ? "Loading sessions…" : "No sessions to show until a snapshot succeeds"
    }

    /// Whether an action error is still within its lifetime.
    static func isLive(_ error: ActionError, now: Date) -> Bool {
        let age = now.timeIntervalSince(error.at)
        return age >= 0 && age < actionErrorLifetime
    }

    /// A multi-line CLI error (a Python traceback in stderr) as one line:
    /// its first line, then its last, which names the exception.
    static func oneLine(_ message: String) -> String {
        let lines = message
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        guard let first = lines.first else { return message.trimmingCharacters(in: .whitespacesAndNewlines) }
        guard lines.count > 1, let last = lines.last else { return first }
        return "\(first) … \(last)"
    }
}
