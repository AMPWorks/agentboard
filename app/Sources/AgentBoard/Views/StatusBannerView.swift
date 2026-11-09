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
            // Wraps (up to three lines) instead of cutting the message in
            // the middle; the tooltip keeps the full text.
            Text(banner.text)
                .font(.caption)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
                .help(banner.detail ?? banner.text)
            Spacer(minLength: 0)
            if let remedy = banner.remedy {
                Button(remedy.buttonTitle) { store.openSettings(for: remedy) }
                    .controlSize(.small)
                    .help(remedy.help)
            }
            if store.logFileExists {
                Button("Open log") { store.revealLog() }
                    .controlSize(.small)
                    .help("Reveal agentboard.log, the diagnostic log, in Finder")
            } else {
                // No log to open yet: offer the message itself instead of a
                // disabled button.
                Button("Copy error") { ClipboardHelper.copy(banner.detail ?? banner.text) }
                    .controlSize(.small)
                    .help("Copy the full error message to the clipboard")
            }
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
