import Foundation
import UserNotifications
import OSLog

/// Posts a local notification when a session transitions into needs_you.
/// Off by default; gated by the "notifyOnNeedsYou" Settings toggle.
@MainActor
final class NotificationManager {
    static let shared = NotificationManager()

    private var authorized = false
    private let log = Logger(subsystem: "io.github.agentboard", category: "notifications")

    func requestAuthorizationIfNeeded() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { [weak self] granted, _ in
            Task { @MainActor in self?.authorized = granted }
        }
    }

    /// Posts one notification per session that newly entered needs_you.
    /// The diff itself lives in `AttentionController.note(snapshot:)`, which
    /// also drives the menu bar flash, so the two never disagree.
    func notify(newlyNeedingYou: Set<String>, in snapshot: Snapshot, enabled: Bool) {
        guard enabled, !newlyNeedingYou.isEmpty else { return }
        for session in snapshot.sessions where newlyNeedingYou.contains(session.key) {
            post(for: session)
        }
    }

    private func post(for session: Session) {
        let content = UNMutableNotificationContent()
        content.title = "AgentBoard: needs you"
        content.body = session.waitingReason ?? session.displayTitle
        content.sound = .default

        let request = UNNotificationRequest(identifier: session.key, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { [weak self] error in
            if let error {
                Task { @MainActor in
                    self?.log.error("notification failed: \(error.localizedDescription, privacy: .public)")
                }
            }
        }
    }
}
