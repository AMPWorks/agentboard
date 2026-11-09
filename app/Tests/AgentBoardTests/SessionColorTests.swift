import AppKit
import Testing
@testable import AgentBoard

@Suite("Session colors")
struct SessionColorTests {
    /// WCAG relative luminance of an sRGB color.
    static func luminance(_ r: Double, _ g: Double, _ b: Double) -> Double {
        func linear(_ c: Double) -> Double { c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
        return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
    }

    static func luminance(hex: UInt32) -> Double {
        luminance(Double((hex >> 16) & 0xFF) / 255, Double((hex >> 8) & 0xFF) / 255, Double(hex & 0xFF) / 255)
    }

    static func contrast(_ a: Double, _ b: Double) -> Double {
        (max(a, b) + 0.05) / (min(a, b) + 0.05)
    }

    /// The light popover's backgrounds: white and the window background
    /// resolved under the light (aqua) appearance.
    static func lightBackgrounds() -> [(String, Double)] {
        var window = 1.0
        NSAppearance(named: .aqua)!.performAsCurrentDrawingAppearance {
            if let c = NSColor.windowBackgroundColor.usingColorSpace(.sRGB) {
                window = luminance(Double(c.redComponent), Double(c.greenComponent), Double(c.blueComponent))
            }
        }
        return [("white", 1.0), ("windowBackgroundColor", window)]
    }

    @Test func lightTextColorsReachFourPointFiveToOne() {
        #expect(Set(SessionColor.lightTextHex.keys) == ["needsYou", "interrupted", "running", "reviewOpen", "closed"])
        // Each text color looks its hex up by name (force-unwrapped), so a
        // misspelt key would crash at the first row drawn: touch all five.
        _ = [SessionColor.needsYouText, SessionColor.interruptedText, SessionColor.runningText,
             SessionColor.reviewOpenText, SessionColor.closedText]
        for (background, bg) in Self.lightBackgrounds() {
            for (name, hex) in SessionColor.lightTextHex {
                let ratio = Self.contrast(Self.luminance(hex: hex), bg)
                #expect(ratio >= 4.5, "\(name) on \(background): \(ratio)")
            }
        }
    }
}
