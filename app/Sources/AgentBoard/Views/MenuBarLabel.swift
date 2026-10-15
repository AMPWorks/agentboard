import SwiftUI

struct MenuBarLabel: View {
    @ObservedObject var store: SnapshotStore
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismissWindow) private var dismissWindow
    @AppStorage("notifyOnNeedsYou") private var notifyOnNeedsYou: Bool = false

    private var urgentCount: Int { store.snapshot.counts.needsYou + store.snapshot.counts.interrupted }
    private var reviewCount: Int { store.snapshot.counts.review }

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: "square.stack.3d.up")
                .symbolVariant(urgentCount > 0 ? .fill : .none)
            if urgentCount > 0 {
                Text("\(urgentCount)")
            }
            if reviewCount > 0 {
                Text("\(reviewCount)")
                    .foregroundStyle(.secondary)
                    .font(.caption2)
            }
        }
        .onAppear {
            WindowOpener.shared.open = { openWindow(id: DashboardWindowController.windowID) }
            WindowOpener.shared.dismiss = { dismissWindow(id: DashboardWindowController.windowID) }
            store.start()
        }
        .onChange(of: store.snapshot) { _, newValue in
            NotificationManager.shared.noteTransitions(in: newValue, enabled: notifyOnNeedsYou)
        }
    }
}
