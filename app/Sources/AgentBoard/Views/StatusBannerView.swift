import SwiftUI

/// One severity-coloured line under the popover's search bar and at the top
/// of the dashboard's session list, shown only while the CLI or the
/// database is failing (`StoreStatus.banner`). Ticks once a second so the
/// "Waiting on the CLI" count and an action error's expiry stay current
/// with nothing else changing. It never feeds the menu bar icon.
struct StatusBannerView: View {
    @ObservedObject var store: SnapshotStore
    /// Space around the banner, applied only while it shows, so a healthy
    /// store leaves no gap.
    var insets = EdgeInsets()

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            if let banner = store.banner(now: context.date) {
                content(banner).padding(insets)
            }
        }
    }

    private func content(_ banner: StoreStatus.Banner) -> some View {
        let color: Color = banner.severity == .error ? SessionColor.needsYou : SessionColor.interrupted
        return HStack(spacing: 6) {
            Image(systemName: banner.severity == .error ? "xmark.octagon.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(color)
            Text(banner.text)
                .font(.caption)
                .lineLimit(2)
                .truncationMode(.middle)
                .help(banner.detail ?? banner.text)
            Spacer(minLength: 0)
            Button("Open log") { store.revealLog() }
                .controlSize(.small)
                .disabled(!store.logFileExists)
                .help("Reveal agentboard.log, the diagnostic log, in Finder")
            if banner.isActionError {
                Button {
                    store.dismissActionError()
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Dismiss this message")
                .accessibilityLabel("Dismiss")
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
        .accessibilityElement(children: .contain)
    }
}
