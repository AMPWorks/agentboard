import SwiftUI

/// When the popover's Updates panel shows (Settings → Popover).
enum UpdatesPanelMode: String, CaseIterable, Identifiable, Sendable {
    /// Closed every time the popover opens; the Updates button opens it.
    case hiddenUntilClicked
    /// Opens or stays closed the way the Updates button last left it.
    case rememberLast
    /// Always shown above the sessions, with no Updates button.
    case alwaysShown

    static let `default`: UpdatesPanelMode = .hiddenUntilClicked

    var id: String { rawValue }

    var label: String {
        switch self {
        case .hiddenUntilClicked: return "Hidden until clicked"
        case .rememberLast: return "Remember last state"
        case .alwaysShown: return "Always shown"
        }
    }

    /// A stored raw value, or the default for a missing or unknown one.
    static func parse(_ raw: String?) -> UpdatesPanelMode {
        raw.flatMap(UpdatesPanelMode.init(rawValue:)) ?? .default
    }
}

/// Text size of the popover's Updates panel (Settings → Popover). macOS
/// ignores Dynamic Type for text styles, so each choice maps to explicit
/// point sizes; `.standard` matches the caption styles the panel used before.
enum UpdatesFontSize: String, CaseIterable, Identifiable, Sendable {
    case small
    case standard = "default"
    case large

    static let `default`: UpdatesFontSize = .standard

    var id: String { rawValue }

    var label: String {
        switch self {
        case .small: return "Small"
        case .standard: return "Default"
        case .large: return "Large"
        }
    }

    static func parse(_ raw: String?) -> UpdatesFontSize {
        raw.flatMap(UpdatesFontSize.init(rawValue:)) ?? .default
    }

    /// Point size of the main text: the update's line, the project, the
    /// panel header and its buttons.
    var textSize: CGFloat {
        switch self {
        case .small: return 9
        case .standard: return 10
        case .large: return 12
        }
    }

    /// Point size of the secondary text: the time, ×N and the tool badge.
    var minorSize: CGFloat {
        switch self {
        case .small: return 9
        case .standard: return 10
        case .large: return 11
        }
    }

    /// Vertical padding of each row, so density follows the text size.
    var rowPadding: CGFloat {
        switch self {
        case .small: return 2
        case .standard: return 4
        case .large: return 5
        }
    }

    var text: Font { .system(size: textSize) }
    var minor: Font { .system(size: minorSize) }
}

/// The popover's Updates panel settings and the pure decisions behind the
/// Updates button, testable without SwiftUI state.
enum PopoverUpdatesSettings {
    /// `UpdatesPanelMode` raw value (default `hiddenUntilClicked`).
    static let modeKey = "updatesPanelMode"
    /// `UpdatesFontSize` raw value (default `default`).
    static let fontSizeKey = "updatesFontSize"
    /// Rows other than needs_you ones the panel lists before "Show all"
    /// (one of `compactLimits`, default `UpdatesFeed.compactLimit`).
    static let compactLimitKey = "updatesCompactLimit"
    /// Whether the panel was last left open; read only in
    /// `.rememberLast` mode. Remembered UI state, not a Settings control.
    static let lastShownKey = "updatesPanelLastShown"

    static let compactLimits = [3, 5, 10]

    /// A stored limit, or the default when it is not one of the choices
    /// (0 is what an unset `@AppStorage` Int would read).
    static func compactLimit(_ raw: Int) -> Int {
        compactLimits.contains(raw) ? raw : UpdatesFeed.compactLimit
    }

    /// Whether the panel is open: always in `.alwaysShown`; as last left in
    /// `.rememberLast`; otherwise as toggled since this popover opened (the
    /// view resets that to false on every open).
    static func panelShown(mode: UpdatesPanelMode, remembered: Bool, toggledThisOpen: Bool) -> Bool {
        switch mode {
        case .alwaysShown: return true
        case .rememberLast: return remembered
        case .hiddenUntilClicked: return toggledThisOpen
        }
    }

    /// Whether any session has an open update, whatever the popover query:
    /// the same counting rule as the button's count (`UpdatesFeed.total`),
    /// so the button never reads "Updates 0" with no query.
    static func hasOpenUpdates(_ sessions: [Session]) -> Bool {
        UpdatesFeed.total(from: sessions, query: SessionSearch("")) > 0
    }

    /// Open needs_you notes across every session, whatever the popover
    /// query (`SessionSearch("")`, the rule `hasOpenUpdates` uses), so a
    /// query kept from an earlier open never hides a request. The snapshot
    /// never caps out a needs_you note, so the listed notes are all of them.
    static func openNeedsYouCount(_ sessions: [Session]) -> Int {
        let all = SessionSearch("")
        return sessions.reduce(0) { sum, session in
            sum + session.notes.filter { $0.kind == .needsYou && all.matches($0, in: session) }.count
        }
    }

    /// How the Updates button is tinted.
    enum ButtonTint: Equatable, Sendable {
        /// Red: some session has an open needs_you note.
        case needsYou
        /// The accent colour: the panel is open.
        case accent
        /// Grey.
        case plain
    }

    /// The Updates button's tint from `needsYou` (`openNeedsYouCount`,
    /// unfiltered) and whether the panel is open: red whenever any session
    /// has an open needs_you note (on a session in any state, done and
    /// snoozed ones included), which beats the accent tint of an open panel;
    /// with the panel closed nothing else in the popover lists these notes,
    /// so the button is where they show.
    static func buttonTint(needsYou: Int, shown: Bool) -> ButtonTint {
        if needsYou > 0 { return .needsYou }
        return shown ? .accent : .plain
    }

    /// The Updates button shows whenever there is any open update (with or
    /// without a query, so the search row does not shift while typing), and
    /// never in `.alwaysShown`, which has nothing to toggle.
    static func toggleVisible(mode: UpdatesPanelMode, hasUpdates: Bool) -> Bool {
        mode != .alwaysShown && hasUpdates
    }

    /// The panel renders when it is open and there is something to show:
    /// in `.alwaysShown` only rows (as before this setting existed); when
    /// toggled open, any open update, so a query that matches none shows
    /// "No updates match" instead of silently closing the panel.
    static func panelVisible(mode: UpdatesPanelMode, shown: Bool, hasUpdates: Bool, hasRows: Bool) -> Bool {
        guard shown else { return false }
        return mode == .alwaysShown ? hasRows : hasUpdates
    }

    /// The button's count, capped so it stays narrow.
    static func badge(_ count: Int) -> String {
        count > 99 ? "99+" : "\(count)"
    }

    /// "1 needs you" / "3 need you": what the red button adds to its words.
    static func needsYouPhrase(_ needsYou: Int) -> String {
        needsYou == 1 ? "1 needs you" : "\(needsYou) need you"
    }

    /// The Updates button's tooltip. `needsYou` (open needs_you notes,
    /// unfiltered; the button is red when above zero) is appended.
    static func toggleHelp(shown: Bool, count: Int, filtering: Bool, needsYou: Int = 0) -> String {
        let base: String
        if shown {
            base = "Hide the Updates panel"
        } else if filtering && count == 0 {
            base = "No updates match the filter; show the Updates panel anyway"
        } else {
            let noun = count == 1 ? "update" : "updates"
            base = filtering
                ? "Show \(count) \(noun) that match the filter"
                : "Show \(count) open \(noun): what sessions said that is worth keeping"
        }
        return needsYou > 0 ? "\(base) (\(needsYouPhrase(needsYou)))" : base
    }

    /// The Updates button's VoiceOver label, worded like its tooltip.
    static func toggleAccessibilityLabel(shown: Bool, count: Int, filtering: Bool, needsYou: Int = 0) -> String {
        let base: String
        if shown {
            base = "Hide updates"
        } else if filtering && count == 0 {
            base = "Show updates; none match the filter"
        } else {
            base = count == 1 ? "Show 1 update" : "Show \(count) updates"
        }
        return needsYou > 0 ? "\(base), \(needsYouPhrase(needsYou))" : base
    }

    /// Under the search field while a query is active and the panel is
    /// closed: "2 updates match", so matches only in update text are not
    /// missed. Nil when there is nothing to point at.
    static func matchHint(panelShown: Bool, isFiltering: Bool, matching: Int) -> String? {
        guard isFiltering, !panelShown, matching > 0 else { return nil }
        return matching == 1 ? "1 update matches" : "\(matching) updates match"
    }
}
