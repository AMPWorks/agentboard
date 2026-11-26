import SwiftUI

/// Compact color key for the row state dots, shown in the dashboard sidebar.
struct SessionColorLegendView: View {
    /// Adds the shape key (this Mac, another Mac, offline) to the color key.
    var showsRemote: Bool = false

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
            if showsRemote {
                // The shape key: this Mac / another Mac / offline, side by side.
                HStack(spacing: 8) {
                    ForEach(SessionColor.markerKey, id: \.label) { item in
                        HStack(spacing: 3) {
                            StateMarker(shape: item.shape, color: .secondary, size: 6)
                            Text(item.shape == .hollowDiamond ? "offline" : item.label)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(.top, 2)
            }
            Text(SessionColor.menuBarIconCaption)
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .padding(.top, 2)
        }
        .help(showsRemote ? "Shape: \(SessionColor.markerKeyText). \(SessionColor.legendTooltip)" : SessionColor.legendTooltip)
    }
}
