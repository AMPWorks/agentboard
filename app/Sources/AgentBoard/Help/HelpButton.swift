import AppKit
import SwiftUI

/// A small ? button that opens the Help window at `topic`. In the popover
/// (`closesHostWindow`) it closes the popover once Help is showing, like a
/// row click does.
struct HelpButton: View {
    let topic: HelpTopic.ID
    /// What the button explains, for its tooltip ("Explain the Updates feed").
    let tooltip: String
    var closesHostWindow = false

    var body: some View {
        Button {
            let host = closesHostWindow ? NSApp.keyWindow : nil
            HelpWindowController.show(topic: topic)
            host?.close()
        } label: {
            Image(systemName: "questionmark.circle")
        }
        .buttonStyle(.borderless)
        .foregroundStyle(.secondary)
        .help(tooltip)
        .accessibilityLabel(tooltip)
    }
}
