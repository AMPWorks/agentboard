import SwiftUI

/// A diamond inscribed in its frame.
struct DiamondShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.midX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
        path.addLine(to: CGPoint(x: rect.midX, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.midY))
        path.closeSubpath()
        return path
    }
}

/// A session's state marker in its state color: a dot for this Mac's
/// sessions, a slightly larger diamond (about the same filled area) for a
/// session on another Mac. Both take the dot's layout frame (the diamond
/// overhangs it by about a point), so rows line up and nothing shifts. An
/// offline Mac's session gets the diamond's outline.
struct StateMarker: View {
    let shape: SessionColor.MarkerShape
    let color: Color
    /// The dot's diameter; the diamond's frame is 1.3 times it.
    var size: CGFloat = 7

    var body: some View {
        Group {
            switch shape {
            case .dot:
                Circle().fill(color).frame(width: size, height: size)
            case .diamond:
                DiamondShape().fill(color).frame(width: size * 1.3, height: size * 1.3)
            case .hollowDiamond:
                DiamondShape().stroke(color, lineWidth: 1.3)
                    .frame(width: size * 1.3, height: size * 1.3)
            }
        }
        .frame(width: size, height: size)
    }
}
