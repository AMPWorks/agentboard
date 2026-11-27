import Testing
@testable import AgentBoard

@Suite("PopoverBackdrop")
struct PopoverBackdropTests {
    @Test(arguments: [false, true], [(true, false), (false, true), (true, true)])
    func eitherAccessibilityFlagMakesTheLayerOpaque(dark: Bool, flags: (Bool, Bool)) {
        // flags = (reduceTransparency, increaseContrast), at least one on.
        #expect(PopoverBackdrop.opacity(dark: dark, reduceTransparency: flags.0,
                                        increaseContrast: flags.1) == 1.0)
    }

    @Test func allEightFlagCombinations() {
        // The rule written out, not recomputed: (dark, reduce, increase) -> opacity.
        let table: [(Bool, Bool, Bool, Double)] = [
            (false, false, false, 0), (false, true, false, 1), (false, false, true, 1), (false, true, true, 1),
            (true, false, false, PopoverBackdrop.darkOpacity), (true, true, false, 1), (true, false, true, 1),
            (true, true, true, 1),
        ]
        for (dark, reduce, increase, want) in table {
            let got = PopoverBackdrop.opacity(dark: dark, reduceTransparency: reduce,
                                              increaseContrast: increase)
            #expect(got == want, "dark=\(dark) reduce=\(reduce) increase=\(increase)")
        }
    }

    @Test func reduceTransparencyAloneIsOpaqueInBothAppearances() {
        #expect(PopoverBackdrop.opacity(dark: false, reduceTransparency: true, increaseContrast: false) == 1.0)
        #expect(PopoverBackdrop.opacity(dark: true, reduceTransparency: true, increaseContrast: false) == 1.0)
    }

    @Test func lightWithNoFlagsKeepsTheSystemMaterial() {
        #expect(PopoverBackdrop.opacity(dark: false, reduceTransparency: false, increaseContrast: false) == 0)
    }

    @Test func darkIsNearOpaque() {
        let o = PopoverBackdrop.opacity(dark: true, reduceTransparency: false, increaseContrast: false)
        #expect(o > 0.8 && o < 1.0)
    }

    @Test func secondaryTextOverAWhiteBackdropReachesAA() {
        let ratio = PopoverBackdrop.contrast(
            layerGray: PopoverBackdrop.darkGray, opacity: PopoverBackdrop.darkOpacity,
            backdropGray: 1.0, textWhiteAlpha: 0.55)
        #expect(ratio >= 4.5, "ratio \(ratio)")
    }

    @Test func contrastRisesWithOpacity() {
        var previous = 0.0
        for step in 0...10 {
            let ratio = PopoverBackdrop.contrast(
                layerGray: PopoverBackdrop.darkGray, opacity: Double(step) / 10,
                backdropGray: 1.0, textWhiteAlpha: 0.55)
            #expect(ratio >= previous)
            previous = ratio
        }
    }

    @Test func opaqueWhiteOnOpaqueBlackIsTwentyOne() {
        let ratio = PopoverBackdrop.contrast(layerGray: 0, opacity: 1, backdropGray: 1,
                                             textWhiteAlpha: 1)
        #expect(abs(ratio - 21) < 1e-9)
    }
}
