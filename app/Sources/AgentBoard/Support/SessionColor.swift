import SwiftUI
import AppKit

/// Maps a session's state (and liveness) to a color, shared by every row and
/// legend in the app so the mapping only lives in one place.
enum SessionColor {
    static let needsYou = Color(nsColor: .systemRed)
    static let interrupted = Color(nsColor: .systemOrange)
    static let running = Color(nsColor: .systemGreen)
    static let reviewOpen = Color(nsColor: .systemTeal)

    /// Review/unconcluded once the process is gone. systemYellow reads fine
    /// on a dark background but is too pale as text on light backgrounds, so
    /// resolve to a darker amber there.
    static let closed: Color = {
        let dynamic = NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return isDark ? NSColor.systemYellow : NSColor(red: 0.58, green: 0.44, blue: 0.0, alpha: 1.0)
        }
        return Color(nsColor: dynamic)
    }()

    static func color(for session: Session) -> Color {
        switch session.state {
        case .needsYou: return needsYou
        case .interrupted: return interrupted
        case .running: return running
        case .review: return session.alive ? reviewOpen : closed
        case .unconcluded: return closed
        case .snoozed, .done, .unknown: return .secondary
        }
    }

    static func label(for session: Session) -> String {
        switch session.state {
        case .needsYou: return "Needs you"
        case .interrupted: return "Interrupted"
        case .running: return "Running"
        case .review: return session.alive ? "Review (open)" : "Closed"
        case .unconcluded: return "Closed"
        case .snoozed: return "Snoozed"
        case .done: return "Done"
        case .unknown: return "Unknown"
        }
    }

    struct LegendItem: Identifiable {
        let id: String
        let label: String
        let colorName: String
        let color: Color
    }

    static let legend: [LegendItem] = [
        LegendItem(id: "needs_you", label: "Needs you", colorName: "red", color: needsYou),
        LegendItem(id: "interrupted", label: "Interrupted", colorName: "orange", color: interrupted),
        LegendItem(id: "running", label: "Running", colorName: "green", color: running),
        LegendItem(id: "review_open", label: "Review (open)", colorName: "teal", color: reviewOpen),
        LegendItem(id: "closed", label: "Closed", colorName: "yellow", color: closed),
        LegendItem(id: "snoozed_done", label: "Snoozed / Done", colorName: "gray", color: .secondary),
    ]

    /// Plain-text form of the legend for tooltips, which can't render colored views.
    static let legendTooltip = legend.map { "\($0.label) (\($0.colorName))" }.joined(separator: " · ")
}
