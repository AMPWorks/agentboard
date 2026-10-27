import Foundation

/// The plan-usage gauge lines in the popover footer and the dashboard
/// sidebar, one per usage source, as pure rules so they are unit-tested.
/// The numbers and every label come from the snapshot's `usage` list
/// (Claude's local cache, Codex, any tool's drop file); nothing here knows
/// an agent, a plan, a limit or a window name.
enum UsageLine {
    enum Tint: Equatable, Sendable {
        case normal
        case high
        case critical
    }

    /// Display thresholds on the used percentage, not plan knowledge.
    static let highThreshold = 80.0
    static let criticalThreshold = 95.0

    /// Numbers older than this say so in the line itself: a source is only
    /// as fresh as its last report on this Mac, so use elsewhere since then
    /// is missing from it.
    static let staleAfter: TimeInterval = 60 * 60

    /// The sources that have something to show, in snapshot order.
    static func shownSources(_ usage: [UsageSource], now: Date) -> [UsageSource] {
        usage.filter { !shownWindows($0, now: now).isEmpty }
    }

    /// The windows to show, in the source's order, without any whose reset
    /// time has passed (its percentage belongs to a window that already
    /// reset).
    static func shownWindows(_ source: UsageSource, now: Date) -> [UsageWindow] {
        source.windows.filter { window in
            guard let resets = window.resetsDate else { return true }
            return resets > now
        }
    }

    /// `57%`, `70.5%`.
    static func percent(_ value: Double) -> String {
        let rounded = (value * 10).rounded() / 10
        if rounded == rounded.rounded() { return "\(Int(rounded))%" }
        return String(format: "%.1f%%", rounded)
    }

    /// `$314`, rounded to whole dollars.
    static func dollars(_ value: Double) -> String { "$\(Int(value.rounded()))" }

    static func part(_ window: UsageWindow) -> String {
        if let used = window.usedUsd, let limit = window.limitUsd {
            return "\(window.label) \(dollars(used)) of \(dollars(limit))"
        }
        return "\(window.label) \(percent(window.usedPercentage))"
    }

    /// `1h 55m`, `42m`, `2d 5h`; at least `1m`.
    static func duration(_ interval: TimeInterval) -> String {
        let minutes = max(1, Int(interval / 60))
        if minutes < 60 { return "\(minutes)m" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours)h \(minutes % 60)m" }
        return "\(hours / 24)d \(hours % 24)h"
    }

    /// How old the source's numbers are (`3h`, `2d`) once they are older
    /// than `staleAfter`, else nil.
    static func staleAge(_ source: UsageSource, now: Date) -> String? {
        guard let updated = source.updatedDate else { return nil }
        let age = now.timeIntervalSince(updated)
        guard age > staleAfter else { return nil }
        let hours = Int(age / 3_600)
        return hours < 24 ? "\(hours)h" : "\(hours / 24)d"
    }

    /// `Claude · 5h 57% · 7d 70% · resets in 1h 55m`, or nil when no window
    /// is left to show. Stale numbers read `Claude as of 3h ago · ...`, up
    /// front so a truncated line never loses it.
    static func text(_ source: UsageSource, now: Date) -> String? {
        let shown = shownWindows(source, now: now)
        guard !shown.isEmpty else { return nil }
        let head = staleAge(source, now: now).map { "\(source.label) as of \($0) ago" }
            ?? source.label
        var parts = [head] + shown.map(part)
        let soonest = shown.compactMap(\.resetsDate).filter { $0 > now }.min()
        if let soonest {
            parts.append("resets in \(duration(soonest.timeIntervalSince(now)))")
        }
        return parts.joined(separator: " · ")
    }

    /// The tooltip: one sentence per window with its absolute reset time,
    /// then where the numbers come from and how old they are.
    static func detail(
        _ source: UsageSource, now: Date, timeZone: TimeZone = .current,
        locale: Locale = .current
    ) -> String {
        let formatter = DateFormatter()
        formatter.dateStyle = .short
        formatter.timeStyle = .short
        formatter.timeZone = timeZone
        formatter.locale = locale
        var lines: [String] = []
        for window in source.windows {
            let name = "\(source.label) \(window.label)"
            var sentence = "\(name): \(percent(window.usedPercentage)) used"
            if let used = window.usedUsd, let limit = window.limitUsd {
                sentence += " (\(dollars(used)) of \(dollars(limit))"
                    + (window.period.map { ", \($0)" } ?? "") + ")"
            }
            if let resets = window.resetsDate {
                if resets <= now {
                    sentence = "\(name): reset at \(formatter.string(from: resets)); "
                        + "no newer numbers reported yet"
                } else {
                    sentence += ", resets \(formatter.string(from: resets))"
                }
            }
            lines.append(sentence + ".")
        }
        var from = source.origin.map { "Read from \($0)" } ?? "Reported by \(source.label)"
        if let updated = source.updatedDate {
            let relative = RelativeDateTimeFormatter()
            relative.unitsStyle = .full
            relative.locale = locale
            from += ", updated \(relative.localizedString(for: updated, relativeTo: now))"
        }
        lines.append(from + ".")
        if staleAge(source, now: now) != nil {
            lines.append("These numbers are only as fresh as \(source.label)'s last report on "
                + "this Mac, so use since then elsewhere (another app or machine) is not counted.")
        }
        return lines.joined(separator: "\n")
    }

    /// `.high` at >= 80% on any shown window, `.critical` at >= 95%.
    static func tint(_ source: UsageSource, now: Date) -> Tint {
        let highest = shownWindows(source, now: now).map(\.usedPercentage).max() ?? 0
        if highest >= criticalThreshold { return .critical }
        if highest >= highThreshold { return .high }
        return .normal
    }
}
