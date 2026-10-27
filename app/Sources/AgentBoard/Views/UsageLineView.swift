import SwiftUI

/// The plan-usage gauge (popover footer, dashboard sidebar): one line per
/// usage source, each led by the source's label. Renders nothing when no
/// source has anything to show; each line's tooltip carries the reset
/// times and how old the numbers are. No timer: the snapshot redraws it
/// every few seconds while visible, finer than the minutes shown.
struct UsageLineView: View {
    let usage: [UsageSource]
    /// One line per source in the popover footer; the narrow dashboard
    /// sidebar wraps.
    var lineLimit = 1
    /// Applied only when something shows, so no source leaves no gap.
    var insets = EdgeInsets()
    @AppStorage(SettingsKeys.showPlanUsage) private var showPlanUsage: Bool = true

    var body: some View {
        let now = Date()
        let sources = showPlanUsage ? UsageLine.shownSources(usage, now: now) : []
        if !sources.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(sources, id: \.source) { source in
                    let text = UsageLine.text(source, now: now) ?? source.label
                    Text(text)
                        .font(.caption)
                        .foregroundStyle(Self.style(UsageLine.tint(source, now: now)))
                        .lineLimit(lineLimit)
                        .fixedSize(horizontal: false, vertical: lineLimit > 1)
                        .truncationMode(.tail)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .help(UsageLine.detail(source, now: now))
                        .accessibilityLabel(text)
                }
            }
            .padding(insets)
        }
    }

    static func style(_ tint: UsageLine.Tint) -> AnyShapeStyle {
        switch tint {
        case .normal: return AnyShapeStyle(.secondary)
        case .high: return AnyShapeStyle(SessionColor.interrupted)
        case .critical: return AnyShapeStyle(SessionColor.needsYou)
        }
    }
}
