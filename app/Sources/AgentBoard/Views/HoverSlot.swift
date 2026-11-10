import SwiftUI

/// A fixed-width trailing slot for a row's hover buttons. It always takes
/// `width`, shown or not, so hovering a row never reflows its text; only
/// whether the buttons can be seen and used changes.
///
/// Hidden, the content is transparent, disabled (so Tab and Space never
/// reach an invisible button), passes clicks through to the row beneath
/// (the row's own tap still fires) and is left out of accessibility.
struct HoverSlot<Content: View>: View {
    let width: CGFloat
    let visible: Bool
    @ViewBuilder let content: Content

    var body: some View {
        content
            .frame(width: width, alignment: .trailing)
            .opacity(visible ? 1 : 0)
            .disabled(!visible)
            .allowsHitTesting(visible)
            .accessibilityHidden(!visible)
    }
}
