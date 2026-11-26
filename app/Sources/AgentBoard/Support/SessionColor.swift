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

    /// Text colors: the same hues, darker in light appearance so a title
    /// or tag reads at about 4.5:1 or better on the light popover (the
    /// system colors alone fall to about 2:1 for green and orange there).
    /// Dots, the menu bar icon and capsule fills keep the colors above.
    static func adaptive(light: NSColor, dark: NSColor) -> NSColor {
        NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
        }
    }

    private static func srgb(_ hex: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }

    /// The light-appearance text colors as sRGB hex, one per text color
    /// below; SessionColorTests checks each reaches 4.5:1 on the light
    /// popover background. Darkened orange and amber both drift to brown,
    /// so closed is an olive gold (hue about 50°) well clear of
    /// interrupted's burnt orange (about 26°); the test keeps them apart.
    static let lightTextHex: [String: UInt32] = [
        "needsYou": 0xC4161C, "interrupted": 0xA84A00, "running": 0x1A7430,
        "reviewOpen": 0x00737A, "closed": 0x766300,
    ]

    private static func lightText(_ name: String) -> NSColor { srgb(lightTextHex[name]!) }

    static let needsYouText = Color(nsColor: adaptive(light: lightText("needsYou"), dark: .systemRed))
    static let interruptedText = Color(nsColor: adaptive(light: lightText("interrupted"), dark: .systemOrange))
    static let runningText = Color(nsColor: adaptive(light: lightText("running"), dark: .systemGreen))
    static let reviewOpenText = Color(nsColor: adaptive(light: lightText("reviewOpen"), dark: .systemTeal))
    // Dark: a softer amber than systemYellow, so a block of closed Review
    // rows never outshines the red Needs you rows above it.
    static let closedText = Color(nsColor: adaptive(light: lightText("closed"), dark: srgb(0xD9A92E)))

    /// The title color for a session's row: its state color, as text.
    static func textColor(for session: Session) -> Color {
        switch session.state {
        case .needsYou: return needsYouText
        case .interrupted: return interruptedText
        case .running: return runningText
        case .review: return session.alive ? reviewOpenText : closedText
        case .unconcluded: return closedText
        case .snoozed, .done, .unknown: return .secondary
        }
    }

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

    /// The state marker's shape: a round dot for this Mac's sessions, a
    /// diamond for a session on another Mac, so the two read apart at a
    /// glance in the same state color, and the diamond's outline when that
    /// Mac is offline (its last-known state; outline means "not active"
    /// elsewhere in the app too).
    enum MarkerShape: Equatable {
        case dot
        case diamond
        case hollowDiamond
    }

    static func markerShape(for session: Session) -> MarkerShape {
        guard session.isRemote else { return .dot }
        return session.stale ? .hollowDiamond : .diamond
    }

    /// The marker key, shown while the board has remote sessions (the
    /// dashboard legend, and the popover marker's tooltip).
    static let markerKey: [(shape: MarkerShape, label: String)] = [
        (.dot, "this Mac"), (.diamond, "another Mac"), (.hollowDiamond, "offline (last known state)"),
    ]
    static let markerKeyText = "● this Mac, ◆ another Mac, ◇ offline (last known state)"

    /// The marker's tooltip: the row's own fact first, then the color key
    /// (and the shape key for a remote row).
    static func markerTooltip(for session: Session) -> String {
        guard let machine = session.machine else { return legendTooltip }
        let fact = session.stale
            ? "On \(machine.name), offline: last known state \(label(for: session))."
            : "On \(machine.name): \(label(for: session))."
        return "\(fact) Shape: \(markerKeyText). \(legendTooltip)"
    }

    /// The marker's spoken label: the state, and the Mac for a remote one.
    static func markerLabel(for session: Session) -> String {
        guard let machine = session.machine else { return label(for: session) }
        if session.stale { return "Last known: \(label(for: session)), on \(machine.name), offline" }
        return "\(label(for: session)), on \(machine.name)"
    }
}
