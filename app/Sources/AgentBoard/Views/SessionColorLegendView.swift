import SwiftUI

/// Compact color key for the row state dots, shown in the dashboard sidebar.
struct SessionColorLegendView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(SessionColor.legend) { item in
                HStack(spacing: 6) {
                    Circle()
                        .fill(item.color)
                        .frame(width: 6, height: 6)
                    Text(item.label)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            Text(SessionColor.menuBarIconCaption)
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .padding(.top, 2)
        }
        .help(SessionColor.legendTooltip)
    }
}
