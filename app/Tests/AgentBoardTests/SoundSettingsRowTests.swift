import Testing
@testable import AgentBoard

@Suite("SoundSettingsRow")
@MainActor
struct SoundSettingsRowTests {
    /// While unset, an event with a notification toggle selects its own
    /// Follow entry, so None and System default are real selection changes.
    @Test func unsetWithAToggleSelectsTheFollowEntry() {
        for event in [AttentionEvent.needsYou, .turnFinished] {
            for notifyOn in [true, false] {
                let tag = SoundSettingsRow.selectionTag(event: event, stored: "", notifyOn: notifyOn)
                #expect(tag == SoundSettingsRow.followToggleTag)
                #expect(tag != SoundChoice.systemDefault.encoded)
                #expect(tag != SoundChoice.none.encoded)
            }
        }
    }

    @Test func theFollowLabelNamesWhatTheToggleGives() {
        #expect(SoundSettingsRow.followToggleLabel(event: .needsYou, notifyOn: true)
                == "Follow notification toggle (System default)")
        #expect(SoundSettingsRow.followToggleLabel(event: .turnFinished, notifyOn: false)
                == "Follow notification toggle (None)")
        #expect(SoundSettingsRow.followToggleLabel(event: .denied, notifyOn: true) == nil)
        #expect(SoundSettingsRow.followToggleLabel(event: .reviewReady, notifyOn: false) == nil)
    }

    /// Picking System default with the toggle on stores it, and the row
    /// still shows System default (and it still plays) once the toggle is off.
    @Test func aPickEqualToTheToggleDefaultStaysPinned() {
        let stored = SoundChoice.systemDefault.encoded
        #expect(SoundSettingsRow.selectionTag(event: .needsYou, stored: stored, notifyOn: true) == stored)
        #expect(SoundSettingsRow.selectionTag(event: .needsYou, stored: stored, notifyOn: false) == stored)
        #expect(AttentionEvent.needsYou.choice(stored: stored, notifyOn: false) == .systemDefault)

        let none = SoundChoice.none.encoded
        #expect(SoundSettingsRow.selectionTag(event: .turnFinished, stored: none, notifyOn: true) == none)
        #expect(AttentionEvent.turnFinished.choice(stored: none, notifyOn: true) == .none)
    }

    /// Events without a toggle have no Follow entry and show None unset.
    @Test func unsetWithoutAToggleSelectsNone() {
        for event in [AttentionEvent.denied, .reviewReady] {
            #expect(SoundSettingsRow.selectionTag(event: event, stored: "", notifyOn: false)
                    == SoundChoice.none.encoded)
            #expect(SoundSettingsRow.selectionTag(event: event, stored: "system:Glass", notifyOn: false)
                    == "system:Glass")
        }
    }
}
