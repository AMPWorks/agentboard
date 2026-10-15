import Foundation
import UserNotifications
import OSLog

/// Posts a local notification when a session transitions into needs_you.
/// Off by default; gated by the "notifyOnNeedsYou" Settings toggle.
@MainActor
final class NotificationManager {
    static let shared = NotificationManager()

    private var authorized = false
    private var previousNeedsYouKeys: Set<String> = []
    private let log = Logger(subsystem: "io.github.agentboard", category: "notifications")

    func requestAuthorizationIfNeeded() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { [weak self] granted, _ in
            Task { @MainActor in self?.authorized = granted }
        }
    }

    func noteTransitions(in snapshot: Snapshot, enabled: Bool) {
        let currentNeedsYou = Set(snapshot.sessions.filter { $0.state == .needsYou }.map(\.key))
        defer { previousNeedsYouKeys = currentNeedsYou }
        guard enabled else { return }

        let newlyNeedingYou = currentNeedsYou.subtracting(previousNeedsYouKeys)
        guard !newlyNeedingYou.isEmpty else { return }

        for session in snapshot.sessions where newlyNeedingYou.contains(session.key) {
            post(for: session)
        }
    }

    private func post(for session: Session) {
        let content = UNMutableNotificationContent()
        content.title = "AgentBoard: needs you"
        content.body = session.waitingReason ?? session.title
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
