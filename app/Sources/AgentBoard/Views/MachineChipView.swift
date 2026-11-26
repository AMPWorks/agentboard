import SwiftUI

/// A Mac's chip: its short label in a gray rounded square, outlined only
/// (dashed) while that Mac is offline.
struct MachineChipView: View {
    let label: String
    var offline: Bool = false
    var size: CGFloat = 14

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: size * 0.25, style: .continuous)
        Text(label)
            .font(.system(size: size * (label.count > 1 ? 0.5 : 0.62), weight: .bold, design: .rounded))
            .lineLimit(1)
            .minimumScaleFactor(0.6)
            .foregroundStyle(.secondary)
            .frame(width: size, height: size)
            .background {
                if offline {
                    shape.strokeBorder(.secondary, style: StrokeStyle(lineWidth: 1, dash: [2, 1.5]))
                } else {
                    shape.fill(.secondary.opacity(0.2))
                }
            }
    }
}
