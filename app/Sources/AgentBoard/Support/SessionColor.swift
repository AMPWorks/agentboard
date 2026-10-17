import SwiftUI
import AppKit

/// Maps a session's state (and liveness) to a color, shared by every row and
/// legend in the app so the mapping only lives in one place.
enum SessionColor {
    /// AppKit sources for the colors below. The menu bar icon resolves these
    /// under the menu bar's appearance before rendering, so they are kept as
    /// (dynamic) NSColors rather than only as SwiftUI Colors.
    static let needsYouNSColor: NSColor = .systemRed
    static let interruptedNSColor: NSColor = .systemOrange
    static let runningNSColor: NSColor = .systemGreen
    static let reviewOpenNSColor: NSColor = .systemTeal

    /// Review/unconcluded once the process is gone. systemYellow reads fine
    /// on a dark background but is too pale as text on light backgrounds, so
    /// resolve to a darker amber there.
    static let closedNSColor = NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark ? NSColor.systemYellow : NSColor(red: 0.58, green: 0.44, blue: 0.0, alpha: 1.0)
    }

    static let needsYou = Color(nsColor: needsYouNSColor)
    static let interrupted = Color(nsColor: interruptedNSColor)
    static let running = Color(nsColor: runningNSColor)
    static let reviewOpen = Color(nsColor: reviewOpenNSColor)
    static let closed = Color(nsColor: closedNSColor)

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

    /// Explains the colored menu bar icon, which reuses these colors.
    static let menuBarIconCaption = "Menu bar icon takes the most urgent color"

    /// Plain-text form of the legend for tooltips, which can't render colored views.
    static let legendTooltip = legend.map { "\($0.label) (\($0.colorName))" }.joined(separator: " · ")
        + ". " + menuBarIconCaption + "."
}
