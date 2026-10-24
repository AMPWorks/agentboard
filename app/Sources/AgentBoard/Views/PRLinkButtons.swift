import SwiftUI
import AppKit

/// Colors and glyphs for a PR link's state, shared by the row buttons and
/// the detail view's Links section.
enum PRStyle {
    static func color(for link: Link) -> Color {
        switch link.state {
        case "OPEN": return .green
        case "MERGED": return .purple
        case "CLOSED": return .red
        default: return .secondary
        }
    }

    static func symbol(for link: Link) -> String {
        link.state == "MERGED" ? "arrow.triangle.merge" : "arrow.triangle.pull"
    }

    static func help(for link: Link) -> String {
        var parts = ["Open \(link.kindLabel) \(link.ref)"]
        if let title = link.title, !title.isEmpty { parts.append(title) }
        if let state = link.state { parts.append(state.lowercased()) }
        if link.awaitsYou == true { parts.append("awaits your review") }
        return parts.joined(separator: " — ")
    }
}

/// One compact button per PR link (glyph + `#N`, tinted by state) that opens
/// the PR in the browser. A separate hit target from the row: clicking one
/// never focuses the session's terminal. More than `PRButtons.defaultCap`
/// PRs show a "+N" that opens the detail view instead.
struct PRLinkButtons: View {
    let session: Session
    let onOverflow: () -> Void

    var body: some View {
        let layout = PRButtons.layout(for: session.links)
        if !layout.shown.isEmpty {
            HStack(spacing: 3) {
                ForEach(layout.shown) { link in
                    button(for: link)
                }
                if layout.overflow > 0 {
                    Button("+\(layout.overflow)", action: onOverflow)
                        .buttonStyle(.borderless)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .help("Show all \(layout.shown.count + layout.overflow) PRs")
                }
            }
        }
    }

    private func button(for link: Link) -> some View {
        let color = PRStyle.color(for: link)
        let awaits = link.awaitsYou == true
        return Button {
            if let url = link.openURL { NSWorkspace.shared.open(url) }
        } label: {
            HStack(spacing: 2) {
                Image(systemName: PRStyle.symbol(for: link))
                Text(link.ref)
            }
            .font(.caption2.monospacedDigit().weight(awaits ? .bold : .regular))
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(color.opacity(0.15), in: Capsule())
            .overlay(Capsule().stroke(color, lineWidth: awaits ? 1 : 0))
            .foregroundStyle(color)
        }
        .buttonStyle(.borderless)
        .help(PRStyle.help(for: link))
        .accessibilityLabel(PRStyle.help(for: link))
    }
}
