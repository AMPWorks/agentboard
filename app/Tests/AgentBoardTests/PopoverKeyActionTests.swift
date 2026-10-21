import AppKit
import Testing
@testable import AgentBoard

@Suite("PopoverKeyAction")
struct PopoverKeyActionTests {
    private func action(_ keyCode: UInt16, _ characters: String?, _ modifiers: NSEvent.ModifierFlags = [],
                        queryIsEmpty: Bool) -> PopoverKeyAction {
        PopoverKeyAction.action(keyCode: keyCode, characters: characters, modifiers: modifiers,
                                queryIsEmpty: queryIsEmpty)
    }

    @Test func escapeClearsANonEmptyQuery() {
        #expect(action(53, "\u{1B}", queryIsEmpty: false) == .clearQuery)
    }

    @Test func escapeWithNoQueryKeepsItsUsualBehavior() {
        #expect(action(53, "\u{1B}", queryIsEmpty: true) == .releaseFocus)
    }

    @Test func modifiedEscapeIsPassedThrough() {
        #expect(action(53, "\u{1B}", .command, queryIsEmpty: false) == .pass)
        #expect(action(53, "\u{1B}", .option, queryIsEmpty: false) == .pass)
    }

    @Test func commandFFocusesSearch() {
        #expect(action(3, "f", .command, queryIsEmpty: true) == .focusSearch)
        #expect(action(3, "f", .command, queryIsEmpty: false) == .focusSearch)
        // Caps lock and device-dependent bits don't matter.
        #expect(action(3, "F", [.command, .capsLock], queryIsEmpty: true) == .focusSearch)
    }

    @Test func otherKeysPassThrough() {
        #expect(action(3, "f", queryIsEmpty: true) == .pass)               // plain typing
        #expect(action(3, "f", [.command, .shift], queryIsEmpty: true) == .pass)
        #expect(action(36, "\r", queryIsEmpty: false) == .pass)            // Return
        #expect(action(126, nil, queryIsEmpty: false) == .pass)            // arrow
    }
}
