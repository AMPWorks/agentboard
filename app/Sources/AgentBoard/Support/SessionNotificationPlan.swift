import Foundation

/// What AgentBoard posts (and removes) in Notification Center, decided
/// without touching `UserNotifications` so it can be tested.
/// `NotificationManager` only converts each `Planned` into a request.
enum SessionNotificationPlan {
    /// The `UNNotificationCategory` every session notification carries.
    static let categoryIdentifier = "session"
    static let fallbackBody = "Finished its turn"

    enum Kind: String {
        case turn
        case needsYou = "needs_you"
    }

    struct Context: Equatable {
        /// The dashboard window is key (the user is looking at it).
        var dashboardIsKey: Bool
        /// The session selected in the dashboard, if any.
        var dashboardSelection: String?
    }

    struct Planned: Equatable {
        var identifier: String
        var title: String
        var subtitle: String?
        var body: String
        var threadIdentifier: String
        var userInfo: [String: String]
    }

    static func identifier(_ kind: Kind, key: String) -> String { "\(kind.rawValue):\(key)" }

    /// One request per finished turn, except for the session the user is
    /// looking at (the dashboard is key with it selected). A newer turn of
    /// the same session reuses its identifier, replacing the delivered one.
    static func turnRequests(finished: [Session], context: Context) -> [Planned] {
        finished.compactMap { session in
            if context.dashboardIsKey && context.dashboardSelection == session.key { return nil }
            let title = session.project.isEmpty ? session.displayTitle : session.project
            let subtitle = session.displayTitle == title ? nil : session.displayTitle
            return Planned(
                identifier: identifier(.turn, key: session.key),
                title: title,
                subtitle: subtitle,
                body: firstLine(session.lastAssistant) ?? fallbackBody,
                threadIdentifier: session.key,
                userInfo: userInfo(.turn, key: session.key)
            )
        }
    }

    static func needsYou(_ session: Session) -> Planned {
        Planned(
            identifier: identifier(.needsYou, key: session.key),
            title: "AgentBoard: needs you",
            subtitle: nil,
            body: session.waitingReason ?? session.displayTitle,
            threadIdentifier: session.key,
            userInfo: userInfo(.needsYou, key: session.key)
        )
    }

    /// Delivered turn notifications to remove: the user has replied.
    static func removals(answered: [String]) -> [String] {
        answered.map { identifier(.turn, key: $0) }
    }

    /// What one detector step does in Notification Center.
    struct TurnActions: Equatable {
        /// Delivered notification identifiers to remove (applied first).
        var removals: [String]
        var posts: [Planned]
    }

    /// The toggle gates only the posts: removing an answered turn's
    /// notification is harmless when none was delivered, and it clears one
    /// delivered before the toggle was turned off.
    static func turnActions(_ changes: TurnFinishDetector.Changes, enabled: Bool,
                            context: Context) -> TurnActions {
        TurnActions(
            removals: removals(answered: changes.answered),
            posts: enabled ? turnRequests(finished: changes.finished, context: context) : []
        )
    }

    static func userInfo(_ kind: Kind, key: String) -> [String: String] {
        ["sessionKey": key, "kind": kind.rawValue]
    }

    /// The first non-empty line without markdown bold, as the CLI's "Your
    /// turn: ..." reason shows it; nil when there is none.
    static func firstLine(_ text: String?) -> String? {
        guard let text else { return nil }
        for line in text.split(whereSeparator: \.isNewline) {
            let trimmed = line.replacingOccurrences(of: "**", with: "")
                .trimmingCharacters(in: .whitespaces)
            if !trimmed.isEmpty { return trimmed }
        }
        return nil
    }
}
