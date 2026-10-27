import Foundation
import UserNotifications
import OSLog

/// Posts AgentBoard's local notifications and handles clicks on them.
///
/// Two kinds, each behind its own Settings toggle (both off by default):
/// a session newly needing you, and a session finishing its turn. What to
/// post or remove is decided by `SessionNotificationPlan`; this class only
/// turns that into `UNNotificationRequest`s. Clicking either kind focuses
/// the session's terminal, or opens the dashboard with it selected.
///
/// `UNUserNotificationCenter.current()` raises in a process without a
/// bundle (the bare `.build/release/AgentBoard` used in fixture mode), so
/// every call into it is skipped there, with one notice in the log.
@MainActor
final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationManager()

    private var authorized = false
    private var loggedUnavailable = false
    private let log = Logger(subsystem: "io.github.agentboard", category: "notifications")

    /// Opens a session for a clicked notification; returns false when the
    /// app is not ready yet (no snapshot loaded), so the click waits in
    /// `pendingOpenKey`. Set by `MenuBarLabel` once its windows exist.
    var onOpenSession: ((String) -> Bool)?
    /// A clicked session not opened yet: the click arrived (or launched the
    /// app) before `onOpenSession` was set or the first snapshot loaded.
    private(set) var pendingOpenKey: String?

    /// The notification center, or nil (logged once) without a bundle.
    private func center() -> UNUserNotificationCenter? {
        guard Bundle.main.bundleIdentifier != nil else {
            if !loggedUnavailable {
                loggedUnavailable = true
                log.notice("notifications unavailable: not running from an app bundle")
            }
            return nil
        }
        return UNUserNotificationCenter.current()
    }

    /// Becomes the center's delegate and registers the "session" category.
    /// Runs from `applicationWillFinishLaunching`, so a click that launches
    /// the app already reaches `didReceive`.
    func configure() {
        guard let center = center() else { return }
        center.delegate = self
        let category = UNNotificationCategory(
            identifier: SessionNotificationPlan.categoryIdentifier,
            actions: [], intentIdentifiers: [], options: []
        )
        center.setNotificationCategories([category])
    }

    func requestAuthorizationIfNeeded() {
        center()?.requestAuthorization(options: [.alert, .sound]) { [weak self] granted, _ in
            Task { @MainActor in self?.authorized = granted }
        }
    }

    /// Posts one notification per session that newly entered needs_you.
    /// The diff itself lives in `AttentionController.note(snapshot:)`, which
    /// also drives the menu bar flash, so the two never disagree.
    func notify(newlyNeedingYou: Set<String>, in snapshot: Snapshot, enabled: Bool) {
        guard enabled, !newlyNeedingYou.isEmpty else { return }
        for session in snapshot.sessions where newlyNeedingYou.contains(session.key) {
            post(SessionNotificationPlan.needsYou(session))
        }
    }

    /// Removes the delivered notifications of answered turns, then posts
    /// one per finished turn the plan keeps (removals first, so a key that
    /// is both never loses its new notification). `enabled` gates only the
    /// posts (`SessionNotificationPlan.turnActions`): a notification
    /// delivered before the toggle was turned off still goes away when the
    /// user answers that session.
    func notify(turns changes: TurnFinishDetector.Changes, enabled: Bool,
                context: SessionNotificationPlan.Context) {
        let actions = SessionNotificationPlan.turnActions(changes, enabled: enabled, context: context)
        if !actions.removals.isEmpty {
            center()?.removeDeliveredNotifications(withIdentifiers: actions.removals)
        }
        for planned in actions.posts {
            post(planned)
        }
    }

    private func post(_ planned: SessionNotificationPlan.Planned) {
        guard let center = center() else { return }
        let content = UNMutableNotificationContent()
        content.title = planned.title
        if let subtitle = planned.subtitle { content.subtitle = subtitle }
        content.body = planned.body
        content.sound = .default
        content.threadIdentifier = planned.threadIdentifier
        content.categoryIdentifier = SessionNotificationPlan.categoryIdentifier
        content.userInfo = planned.userInfo

        let identifier = planned.identifier
        let request = UNNotificationRequest(identifier: identifier, content: content, trigger: nil)
        center.add(request) { [weak self] error in
            guard let error else { return }
            let message = error.localizedDescription
            Task { @MainActor in
                self?.log.error("notification \(identifier, privacy: .public) failed: \(message, privacy: .public)")
            }
        }
    }

    /// A notification for `key` was clicked.
    func open(sessionKey key: String) {
        log.notice("notification clicked key=\(key, privacy: .public)")
        if let onOpenSession, onOpenSession(key) {
            pendingOpenKey = nil
            return
        }
        pendingOpenKey = key
    }

    /// The pending click, cleared.
    func takePendingOpenKey() -> String? {
        defer { pendingOpenKey = nil }
        return pendingOpenKey
    }

    // MARK: - UNUserNotificationCenterDelegate

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        // Only the key (a String) crosses to the main actor; the response
        // and the handler are not Sendable.
        let key = response.notification.request.content.userInfo["sessionKey"] as? String
        completionHandler()
        guard let key else { return }
        Task { @MainActor in NotificationManager.shared.open(sessionKey: key) }
    }

    /// Show banners even while AgentBoard is the active app (the dashboard
    /// is key with another session selected); the selected session was
    /// already left out before posting.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}
