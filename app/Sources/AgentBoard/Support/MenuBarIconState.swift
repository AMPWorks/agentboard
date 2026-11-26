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
    static func tier(for counts: Counts, sessions allSessions: [Session]) -> Tier {
        // A remote session whose Mac is offline shows its last-known state
        // but never colours the icon (the core already leaves it out of
        // `counts`).
        let sessions = allSessions.filter(\.alertsEligible)
        func any(_ state: SessionState) -> Bool { sessions.contains { $0.state == state } }

        if counts.needsYou > 0 || any(.needsYou) { return .needsYou }
        if counts.interrupted > 0 || any(.interrupted) { return .interrupted }
        if sessions.contains(where: { $0.state == .review && $0.alive }) { return .review }
        if counts.running > 0 || any(.running) { return .running }
        if counts.review > 0 || counts.unconcluded > 0 || any(.review) || any(.unconcluded) { return .closed }
        return .idle
    }

    /// The remote diamond badge's tier: the most urgent live session on
    /// another Mac, only when it needs you or was interrupted (a remote
    /// "running" is not worth menu bar pixels). Offline Macs never show it.
    static func remoteTier(sessions: [Session]) -> Tier? {
        let live = sessions.filter { $0.isRemote && $0.alertsEligible }
        if live.contains(where: { $0.state == .needsYou }) { return .needsYou }
        if live.contains(where: { $0.state == .interrupted }) { return .interrupted }
        return nil
    }

    struct MachineCount: Equatable, Sendable {
        var name: String
        var count: Int
    }

    /// What the menu bar shows for one snapshot under a `RemoteIconMode`.
    struct Summary: Equatable, Sendable {
        /// The glyph's tier.
        var tier: Tier
        /// The badge's tier (Separate marker only), else nil.
        var remoteTier: Tier?
        /// The counts next to the glyph.
        var urgentCount: Int
        var reviewCount: Int
        /// For the tooltip: every urgent session it speaks for (this Mac's
        /// plus, unless Not shown, the other Macs'), and the other Macs'
        /// share by name in first-seen order.
        var tooltipUrgent: Int
        var tooltipReview: Int
        var remoteUrgentByMachine: [MachineCount]

        /// "2 need you (1 on mini), 1 to review", or "nothing needs you".
        var sentence: String {
            var parts: [String] = []
            if tooltipUrgent > 0 {
                var urgent = "\(tooltipUrgent) need\(tooltipUrgent == 1 ? "s" : "") you"
                if !remoteUrgentByMachine.isEmpty {
                    urgent += " (" + remoteUrgentByMachine.map { "\($0.count) on \($0.name)" }
                        .joined(separator: ", ") + ")"
                }
                parts.append(urgent)
            }
            if tooltipReview > 0 { parts.append("\(tooltipReview) to review") }
            return parts.isEmpty ? "nothing needs you" : parts.joined(separator: ", ")
        }

        /// The image's spoken description.
        var accessibilityDescription: String { "AgentBoard: \(sentence)" }
    }

    /// Separate marker: the glyph, its counts and its flash describe this
    /// Mac only and the badge speaks for the others. Merged: one glyph for
    /// every Mac (offline ones excepted). Not shown: this Mac only, no badge.
    static func summary(_ snapshot: Snapshot, mode: RemoteIconMode) -> Summary {
        let local = snapshot.withoutRemote()
        let iconSource = mode == .merged ? snapshot : local
        let liveRemote = snapshot.sessions.filter { $0.isRemote && $0.alertsEligible }
        var byMachine: [MachineCount] = []
        if mode != .hidden {
            for session in liveRemote where session.state == .needsYou || session.state == .interrupted {
                let name = session.machine?.name ?? "another Mac"
                if let i = byMachine.firstIndex(where: { $0.name == name }) {
                    byMachine[i].count += 1
                } else {
                    byMachine.append(MachineCount(name: name, count: 1))
                }
            }
        }
        let remoteUrgent = byMachine.reduce(0) { $0 + $1.count }
        let remoteReview = mode == .hidden ? 0 : liveRemote.filter { $0.state == .review }.count
        let localUrgent = local.counts.needsYou + local.counts.interrupted
        let urgent = iconSource.counts.needsYou + iconSource.counts.interrupted
        return Summary(
            tier: tier(for: iconSource.counts, sessions: iconSource.sessions),
            remoteTier: mode == .separate ? remoteTier(sessions: snapshot.sessions) : nil,
            urgentCount: urgent,
            reviewCount: iconSource.counts.review,
            tooltipUrgent: localUrgent + remoteUrgent,
            tooltipReview: local.counts.review + remoteReview,
            remoteUrgentByMachine: byMachine
        )
    }
}

/// Settings → Machines → Other Macs in the menu bar icon.
enum RemoteIconMode: String, CaseIterable, Identifiable, Sendable {
    /// The glyph is this Mac's; a diamond badge stands for the other Macs.
    case separate
    /// One glyph, count and flash for every Mac (offline ones excepted).
    case merged
    /// The icon ignores the other Macs; their rows still show.
    case hidden

    var id: String { rawValue }

    var label: String {
        switch self {
        case .separate: return "Separate marker"
        case .merged: return "Merged with this Mac"
        case .hidden: return "Not shown"
        }
    }

    static func from(_ raw: String) -> RemoteIconMode { RemoteIconMode(rawValue: raw) ?? .separate }
}

/// `@AppStorage` keys for the Settings → Menu bar icon section.
enum MenuBarIconSettings {
    /// Color the icon by the most urgent session (default on; off = template icon).
    static let coloredKey = "menuBarIconColored"
    /// Flash when a session newly needs you (default on).
    static let flashKey = "menuBarIconFlash"
    /// `FlashStopPolicy` raw value (default `.onOpen`).
    static let flashStopKey = "menuBarIconFlashStop"
    /// `RemoteIconMode` raw value (default `.separate`).
    static let remoteModeKey = "menuBarIconRemote"
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
    /// The other-Mac diamond badge at the top-trailing corner, in this
    /// tier's color; nil for none.
    var remoteTier: MenuBarIconState.Tier?
    /// The "off" half of the badge's pulse.
    var remoteDimmed: Bool = false
    /// Color the menu bar icon is off: a template image (the glyph and the
    /// badge in the menu bar's own color).
    var monochrome: Bool = false

    init(tier: MenuBarIconState.Tier, dimmed: Bool = false, alert: Bool = false, darkMenuBar: Bool,
         remoteTier: MenuBarIconState.Tier? = nil, remoteDimmed: Bool = false, monochrome: Bool = false) {
        self.tier = tier
        self.dimmed = dimmed
        self.alert = alert
        self.darkMenuBar = darkMenuBar
        self.remoteTier = remoteTier
        self.remoteDimmed = remoteDimmed
        self.monochrome = monochrome
    }

    /// Whether the renderer draws anything; otherwise the caller shows the
    /// plain template glyph (coloring off, or idle, with no badge).
    var needsRendering: Bool {
        if remoteTier != nil { return true }
        return !monochrome && tier.nsColor != nil
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
    /// The remote badge: a diamond this wide, with a gap this wide cut out
    /// of the glyph around it (so it reads on the translucent menu bar
    /// without a fixed outline color).
    static let badgeSize: CGFloat = 7
    static let badgeGap: CGFloat = 1.5
    /// The badge's opacity in the "off" half of its pulse.
    static let badgeDimmedOpacity = 0.25

    private static var cache: [MenuBarIconStyle: NSImage] = [:]

    /// `nil` when there is nothing to render (`needsRendering` is false):
    /// the caller shows the template glyph instead.
    /// `description` is the image's spoken text (the menu bar's sentence);
    /// cached images take the latest one.
    static func image(for style: MenuBarIconStyle, scale: CGFloat = 2, description: String = "AgentBoard") -> NSImage? {
        if let cached = cache[style] {
            cached.accessibilityDescription = description
            return cached
        }
        guard style.needsRendering else { return nil }

        let dark = style.darkMenuBar
        // A template image takes only the alpha; idle with a badge uses the
        // menu bar's label color so the glyph looks like the template one.
        let glyphColor: Color = style.monochrome
            ? .black
            : Color(nsColor: resolve(style.tier.nsColor ?? .labelColor, dark: dark))
        let badgeColor: Color? = style.remoteTier.map { remote in
            style.monochrome ? .black : Color(nsColor: resolve(remote.nsColor ?? .labelColor, dark: dark))
        }
        let content = glyph(style: style, color: glyphColor, badgeColor: badgeColor)
            .environment(\.colorScheme, dark ? .dark : .light)
        let renderer = ImageRenderer(content: content)
        renderer.scale = scale
        guard let image = renderer.nsImage else { return nil }
        image.isTemplate = style.monochrome
        image.accessibilityDescription = description
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

    private static func glyph(style: MenuBarIconStyle, color: Color, badgeColor: Color?) -> some View {
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
            .overlay(alignment: .topTrailing) {
                if let badgeColor {
                    ZStack {
                        DiamondShape()
                            .fill(.black)
                            .frame(width: badgeSize + 2 * badgeGap, height: badgeSize + 2 * badgeGap)
                            .blendMode(.destinationOut)
                        DiamondShape()
                            .fill(badgeColor)
                            .frame(width: badgeSize, height: badgeSize)
                            .opacity(style.remoteDimmed ? badgeDimmedOpacity : 1)
                    }
                }
            }
            .compositingGroup()
    }
}
