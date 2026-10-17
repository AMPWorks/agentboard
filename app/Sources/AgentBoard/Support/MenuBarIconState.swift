import SwiftUI
import AppKit

/// Pure model behind the colored menu bar icon: which single color the whole
/// board collapses to. The most urgent session wins, in the same order the
/// popover lists sections.
enum MenuBarIconState {
    enum Tier: String, CaseIterable, Sendable {
        /// Any session is blocked on an answer from the user.
        case needsYou
        /// Any session was interrupted (also counted in the urgent badge).
        case interrupted
        /// Any review session whose process is still alive.
        case review
        /// Any session is running.
        case running
        /// Only unconcluded sessions or review sessions whose process is gone.
        case closed
        /// Nothing worth coloring: no sessions, or only done/snoozed/unknown.
        case idle

        /// The tier's color, from `SessionColor` so the icon, the row dots
        /// and the legend never disagree. `nil` means "use the monochrome
        /// template glyph".
        var nsColor: NSColor? {
            switch self {
            case .needsYou: return SessionColor.needsYouNSColor
            case .interrupted: return SessionColor.interruptedNSColor
            case .review: return SessionColor.reviewOpenNSColor
            case .running: return SessionColor.runningNSColor
            case .closed: return SessionColor.closedNSColor
            case .idle: return nil
            }
        }

        /// The glyph is filled for the tiers the urgent badge counts, as the
        /// template icon was before coloring.
        var isUrgent: Bool { self == .needsYou || self == .interrupted }
    }

    /// First match wins. Today `cli.build_snapshot` computes `counts` over
    /// exactly the sessions it emits, so `counts` and `sessions` agree; the
    /// `counts.* > 0` checks only guard against a future snapshot that counts
    /// sessions its list leaves out. The alive/closed review split can only
    /// come from the sessions themselves.
    static func tier(for counts: Counts, sessions: [Session]) -> Tier {
        func any(_ state: SessionState) -> Bool { sessions.contains { $0.state == state } }

        if counts.needsYou > 0 || any(.needsYou) { return .needsYou }
        if counts.interrupted > 0 || any(.interrupted) { return .interrupted }
        if sessions.contains(where: { $0.state == .review && $0.alive }) { return .review }
        if counts.running > 0 || any(.running) { return .running }
        if counts.review > 0 || counts.unconcluded > 0 || any(.review) || any(.unconcluded) { return .closed }
        return .idle
    }
}

/// `@AppStorage` keys for the Settings → Menu bar icon section.
enum MenuBarIconSettings {
    /// Color the icon by the most urgent session (default on; off = template icon).
    static let coloredKey = "menuBarIconColored"
    /// Flash when a session newly needs you (default on).
    static let flashKey = "menuBarIconFlash"
    /// `FlashStopPolicy` raw value (default `.onOpen`).
    static let flashStopKey = "menuBarIconFlashStop"
}

/// Everything that changes the rendered glyph. Hashable so rendered images can
/// be cached; there are at most a few dozen combinations.
struct MenuBarIconStyle: Hashable, Sendable {
    var tier: MenuBarIconState.Tier
    /// The "off" half of a flash cycle: outline glyph at reduced opacity.
    /// Never an empty slot, which would read as a crash.
    var dimmed: Bool = false
    /// Reduce-motion stand-in for flashing: a static glyph with an
    /// exclamation badge, so "needs you, not yet seen" stays distinguishable
    /// from "needs you, acknowledged".
    var alert: Bool = false
    /// Whether the menu bar is dark. Comes from the system setting, not from
    /// `NSApp.effectiveAppearance`, which the in-app Appearance setting
    /// overrides while the menu bar ignores that override.
    var darkMenuBar: Bool

    init(tier: MenuBarIconState.Tier, dimmed: Bool = false, alert: Bool = false, darkMenuBar: Bool) {
        self.tier = tier
        self.dimmed = dimmed
        self.alert = alert
        self.darkMenuBar = darkMenuBar
    }
}

/// Renders the colored glyph into a non-template `NSImage`. SwiftUI hands a
/// `MenuBarExtra` label to the status item as a template image (which is why
/// `.foregroundStyle` on it does nothing); a non-template `NSImage` keeps its
/// color.
@MainActor
enum MenuBarIconRenderer {
    static let symbolName = "square.stack.3d.up"
    /// 13 pt matches the 16x18 pt template symbol the status item draws today.
    static let pointSize: CGFloat = 13
    static let frameSize = CGSize(width: 18, height: 18)
    static let dimmedOpacity = 0.45

    private static var cache: [MenuBarIconStyle: NSImage] = [:]

    /// `nil` for the idle tier: the caller shows the template glyph instead.
    static func image(for style: MenuBarIconStyle, scale: CGFloat = 2) -> NSImage? {
        if let cached = cache[style] { return cached }
        guard let source = style.tier.nsColor else { return nil }

        let color = Color(nsColor: resolve(source, dark: style.darkMenuBar))
        let content = glyph(style: style, color: color)
            .environment(\.colorScheme, style.darkMenuBar ? .dark : .light)
        let renderer = ImageRenderer(content: content)
        renderer.scale = scale
        guard let image = renderer.nsImage else { return nil }
        image.isTemplate = false
        image.accessibilityDescription = "AgentBoard"
        cache[style] = image
        return image
    }

    /// Resolves a (possibly dynamic) color to concrete sRGB components under
    /// the given menu bar appearance.
    nonisolated static func resolve(_ color: NSColor, dark: Bool) -> NSColor {
        guard let appearance = NSAppearance(named: dark ? .darkAqua : .aqua) else { return color }
        var resolved = color
        appearance.performAsCurrentDrawingAppearance {
            resolved = color.usingColorSpace(.sRGB) ?? color
        }
        return resolved
    }

    private static func glyph(style: MenuBarIconStyle, color: Color) -> some View {
        let filled = style.tier.isUrgent && !style.dimmed
        return Image(systemName: symbolName)
            .symbolVariant(filled ? .fill : .none)
            .font(.system(size: pointSize))
            .foregroundStyle(color)
            .opacity(style.dimmed ? dimmedOpacity : 1)
            .frame(width: frameSize.width, height: frameSize.height)
            .overlay(alignment: .bottomTrailing) {
                if style.alert {
                    Image(systemName: "exclamationmark.circle.fill")
                        .symbolRenderingMode(.palette)
                        .foregroundStyle(.white, color)
                        .font(.system(size: 8, weight: .bold))
                }
            }
    }
}
