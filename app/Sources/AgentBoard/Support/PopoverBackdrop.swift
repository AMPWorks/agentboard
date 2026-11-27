import AppKit
import SwiftUI

/// The layer drawn behind the popover (and the Dashboard and Help sidebars)
/// so a bright window behind them cannot wash out their text. The system
/// material those surfaces use is translucent: in dark appearance a white
/// window behind it shows through and dims secondary text. A near-opaque
/// dark layer keeps a hint of what is behind; Reduce transparency or
/// Increase contrast make it fully opaque in either appearance. Light
/// appearance otherwise keeps the system material as it is.
enum PopoverBackdrop {
    /// The dark layer's sRGB grey (#1E1E1E).
    static let darkGray: Double = 0.118
    /// The dark layer's opacity with no accessibility setting on.
    static let darkOpacity: Double = 0.9

    /// How opaque the layer is: fully opaque under either accessibility
    /// setting, `darkOpacity` in dark, and absent in light.
    static func opacity(dark: Bool, reduceTransparency: Bool, increaseContrast: Bool) -> Double {
        if reduceTransparency || increaseContrast { return 1.0 }
        return dark ? darkOpacity : 0
    }

    /// WCAG contrast ratio of white text at `textWhiteAlpha` drawn over the
    /// layer (`layerGray` at `opacity`) composited on a `backdropGray`
    /// window. Greys are sRGB-encoded and blended in that encoding, as
    /// AppKit composites. Tests use it to pin the constants above.
    static func contrast(layerGray: Double, opacity: Double, backdropGray: Double,
                         textWhiteAlpha: Double) -> Double {
        let ground = opacity * layerGray + (1 - opacity) * backdropGray
        let text = textWhiteAlpha + (1 - textWhiteAlpha) * ground
        let lighter = max(luminance(text), luminance(ground))
        let darker = min(luminance(text), luminance(ground))
        return (lighter + 0.05) / (darker + 0.05)
    }

    /// Relative luminance of an sRGB-encoded grey.
    static func luminance(_ gray: Double) -> Double {
        gray <= 0.04045 ? gray / 12.92 : pow((gray + 0.055) / 1.055, 2.4)
    }
}

/// Draws `PopoverBackdrop`'s layer behind the content it modifies, following
/// the appearance and accessibility settings live.
private struct PopoverBackdropModifier: ViewModifier {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast

    func body(content: Content) -> some View {
        let dark = colorScheme == .dark
        let opacity = PopoverBackdrop.opacity(
            dark: dark, reduceTransparency: reduceTransparency,
            increaseContrast: contrast == .increased)
        let layer = dark
            ? Color(.sRGB, white: PopoverBackdrop.darkGray)
            : Color(nsColor: .windowBackgroundColor)
        content.background(layer.opacity(opacity), ignoresSafeAreaEdges: .all)
    }
}

extension View {
    /// Puts the readable backdrop layer behind this view (see `PopoverBackdrop`).
    func popoverBackdrop() -> some View { modifier(PopoverBackdropModifier()) }
}
