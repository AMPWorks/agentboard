import AppKit
import Testing
@testable import AgentBoard

@Suite("SettingsCatalog grouping")
struct SettingsCatalogGroupingTests {
    @Test func itemIdsAreUnique() {
        let ids = SettingsCatalog.items.map(\.id)
        #expect(Set(ids).count == ids.count)
    }

    @Test func everyTabHasAnItem() {
        for tab in SettingsTab.allCases {
            #expect(!SettingsCatalog.items(in: tab).isEmpty, "\(tab) has no settings")
        }
    }

    @Test func sectionsSitInTheirTabs() {
        #expect(SettingsCatalog.sections(in: .general) == ["Appearance", "General"])
        #expect(SettingsCatalog.sections(in: .menuBar) == ["Menu bar icon", "Popover", "Popover size"])
        #expect(SettingsCatalog.sections(in: .notifications) == ["Notifications", "Sounds"])
        #expect(SettingsCatalog.sections(in: .machines) == ["Machines"])
        #expect(SettingsCatalog.sections(in: .advanced) == ["CLI & Data", "Diagnostics"])
    }

    @Test func everySettingsKeyBelongsToExactlyOneItem() {
        for key in SettingsKeys.all where !SettingsCatalog.keysWithoutItem.contains(key) {
            let owners = SettingsCatalog.items.filter { $0.keys.contains(key) }
            #expect(owners.count == 1, "\(key) is in \(owners.map(\.id))")
        }
        for key in SettingsCatalog.keysWithoutItem {
            #expect(SettingsKeys.all.contains(key))
            #expect(!SettingsCatalog.items.contains { $0.keys.contains(key) })
        }
    }

    @Test func everyItemKeyIsASettingsKey() {
        for item in SettingsCatalog.items {
            for key in item.keys {
                #expect(SettingsKeys.all.contains(key), "\(item.id) lists \(key)")
            }
        }
    }

    @Test func everyItemHasALabelAndHelp() {
        for item in SettingsCatalog.items {
            #expect(!item.label.isEmpty, "\(item.id)")
            #expect(!item.help.isEmpty, "\(item.id)")
        }
    }

    @Test func itemsAreInTabOrder() {
        let tabs = SettingsCatalog.items.map(\.tab)
        let order = SettingsTab.allCases
        #expect(tabs == tabs.sorted { order.firstIndex(of: $0)! < order.firstIndex(of: $1)! })
    }

    @Test func lookupByIdAndOneSoundItemPerEvent() {
        #expect(SettingsCatalog.item(id: "menuBarIconFlashStop") == SettingsCatalog.menuBarIconFlashStop)
        #expect(SettingsCatalog.item(id: "nope") == nil)
        #expect(SettingsCatalog.sounds.map(\.label) == AttentionEvent.allCases.map(\.label))
        #expect(SettingsCatalog.sound(.denied).help == SoundSettingsRow.pickerHelp(event: .denied))
    }

    @Test func fallbacksAreAlwaysShownRowsOnTheSameTab() {
        let withFallback = SettingsCatalog.items.filter { $0.fallback != nil }
        #expect(withFallback.map(\.id) == ["pairedMachines", "peerDisplayName", "fixturePath", "revealHookErrorsLog"])
        for item in withFallback {
            let fallback = SettingsCatalog.item(id: item.fallback!)
            #expect(fallback?.tab == item.tab, "\(item.id)")
            #expect(fallback?.fallback == nil, "\(item.id)")
        }
    }

    @Test func lastOpenTabRoundTripsThroughItsRawValue() {
        for tab in SettingsTab.allCases {
            #expect(SettingsTab.stored(tab.rawValue) == tab)
        }
        #expect(SettingsTab.stored("integrations") == .general)
        #expect(SettingsTab.stored("") == .general)
        #expect(SettingsTab.stored(nil) == .general)
    }
}

@Suite("SettingsSearch")
struct SettingsSearchTests {
    private func ids(_ query: String) -> [String] {
        SettingsSearch(query).results().flatMap { $0.items.map(\.id) }
    }

    @Test func matchesByLabel() {
        let found = ids("flash")
        #expect(found.contains("menuBarIconFlash"))
        #expect(found.contains("menuBarIconFlashStop"))
    }

    @Test func matchesByHelpTextAlone() {
        // "monochrome" is only in the colored-icon toggle's tooltip.
        #expect(!SettingsCatalog.menuBarIconColored.label.lowercased().contains("monochrome"))
        #expect(ids("monochrome") == ["menuBarIconColored"])
    }

    @Test func matchesByDefaultsKey() {
        #expect(ids("menuBarIconFlashStop") == ["menuBarIconFlashStop"])
        #expect(ids("popoverHeight") == ["resetSize"])
    }

    @Test func matchesBySectionOrTabTitle() {
        let groups = SettingsSearch("sounds").results()
        let notifications = groups.first { $0.tab == .notifications }?.items.map(\.id)
        #expect(notifications == SettingsCatalog.sounds.map(\.id))
        #expect(ids("advanced").contains("verboseLogging"))
    }

    @Test func everyTermMustMatch() {
        #expect(ids("flash stop") == ["menuBarIconFlashStop"])
        #expect(ids("flash xyzzy").isEmpty)
    }

    @Test func caseAndAccentsAreIgnored() {
        #expect(ids("FLASH STOP") == ["menuBarIconFlashStop"])
        #expect(ids("flásh stöp") == ["menuBarIconFlashStop"])
    }

    @Test func anEmptyOrBlankQueryFindsNothing() {
        #expect(SettingsSearch("").isEmpty)
        #expect(SettingsSearch("  \t ").isEmpty)
        #expect(SettingsSearch("").results().isEmpty)
        #expect(SettingsSearch("   ").results().isEmpty)
        #expect(!SettingsSearch("").matches(SettingsCatalog.appearance))
    }

    @Test func resultsAreGroupedInTabOrder() {
        let tabs = SettingsSearch("a").results().map(\.tab)
        #expect(tabs == SettingsTab.allCases)
        for group in SettingsSearch("a").results() {
            #expect(group.items.allSatisfy { $0.tab == group.tab })
        }
    }

    @Test func remoteIconSettingIsFoundUnderMachines() {
        let groups = SettingsSearch("menu bar").results()
        let machines = groups.first { $0.tab == .machines }?.items.map(\.id) ?? []
        #expect(machines.contains("remoteIconMode"))
        #expect(SettingsCatalog.remoteIconMode.path == "Machines → Machines")
    }
}

@Suite("SettingsNavigator")
@MainActor
struct SettingsNavigatorTests {
    @Test func revealGoesToTheItemAndClearsTheSearch() {
        let navigator = SettingsNavigator(defaults: TestDefaults().defaults)
        navigator.query = "pairing"
        navigator.reveal(SettingsCatalog.copyPairingCode)
        #expect(navigator.tab == .machines)
        #expect(navigator.highlighted == "copyPairingCode")
        #expect(navigator.query.isEmpty)
    }

    @Test func selectChangesOnlyTheTab() {
        let navigator = SettingsNavigator(defaults: TestDefaults().defaults)
        navigator.query = "x"
        navigator.select(.advanced)
        #expect(navigator.tab == .advanced)
        #expect(navigator.query == "x")
        #expect(navigator.highlighted == nil)
    }

    @Test func clearHighlightKeepsANewerOne() {
        let navigator = SettingsNavigator(defaults: TestDefaults().defaults)
        navigator.reveal(SettingsCatalog.appearance)
        navigator.reveal(SettingsCatalog.verboseLogging)
        navigator.clearHighlight(SettingsCatalog.appearance.id)
        #expect(navigator.highlighted == "verboseLogging")
        navigator.clearHighlight(SettingsCatalog.verboseLogging.id)
        #expect(navigator.highlighted == nil)
        navigator.reveal(SettingsCatalog.appearance)
        navigator.clearHighlight()
        #expect(navigator.highlighted == nil)
    }

    @Test func aQuerySelectsItsFirstResult() {
        let navigator = SettingsNavigator(defaults: TestDefaults().defaults)
        #expect(navigator.selectedResult == nil)
        navigator.query = "flash"
        #expect(navigator.results.map(\.id) == SettingsSearch("flash").results().flatMap { $0.items.map(\.id) })
        #expect(navigator.selectedResult == navigator.results.first?.id)
        navigator.query = "zzz-no-such-setting"
        #expect(navigator.results.isEmpty)
        #expect(navigator.selectedResult == nil)
        navigator.query = ""
        #expect(navigator.selectedResult == nil)
    }

    @Test func arrowsMoveTheSelectionAndStopAtTheEnds() throws {
        let pointer = PointerStub()
        let navigator = SettingsNavigator(defaults: TestDefaults().defaults, pointerLocation: { pointer.at })
        navigator.query = "sounds"
        let rows = navigator.results.map(\.id)
        try #require(rows.count >= 3)
        navigator.moveSelection(by: -1)
        #expect(navigator.selectedResult == rows[0])
        navigator.moveSelection(by: 1)
        navigator.moveSelection(by: 1)
        #expect(navigator.selectedResult == rows[2])
        #expect(navigator.selectionScrolls)
        navigator.moveSelection(by: 100)
        #expect(navigator.selectedResult == rows.last)
        pointer.at = CGPoint(x: 5, y: 5)
        navigator.selectResult(rows[1])
        #expect(navigator.selectedResult == rows[1])
        #expect(!navigator.selectionScrolls)
        navigator.selectResult("notAResultOfThisQuery")
        #expect(navigator.selectedResult == rows[1])
    }

    @Test func hoverUnderAStillPointerDoesNotTakeTheSelection() throws {
        let pointer = PointerStub(at: CGPoint(x: 10, y: 10))
        let navigator = SettingsNavigator(defaults: TestDefaults().defaults, pointerLocation: { pointer.at })
        navigator.query = "sounds"
        let rows = navigator.results.map(\.id)
        try #require(rows.count >= 3)
        // A new query reflows the list under the pointer: not a pick.
        navigator.selectResult(rows[2])
        #expect(navigator.selectedResult == rows[0])
        // Down scrolls the list and a row slides under the still pointer.
        navigator.moveSelection(by: 1)
        navigator.selectResult(rows[2])
        #expect(navigator.selectedResult == rows[1])
        #expect(navigator.selectionScrolls)
        // Once the pointer moves, hover selects again, without scrolling.
        pointer.at.y += 1
        navigator.selectResult(rows[2])
        #expect(navigator.selectedResult == rows[2])
        #expect(!navigator.selectionScrolls)
        navigator.moveSelection(by: -1)
        #expect(navigator.selectedResult == rows[1])
        #expect(navigator.selectionScrolls)
    }

    @Test func returnRevealsTheSelectedResult() throws {
        let navigator = SettingsNavigator(defaults: TestDefaults().defaults)
        #expect(navigator.revealSelection() == false)
        navigator.query = "verbose"
        let id = try #require(navigator.selectedResult)
        #expect(navigator.revealSelection())
        #expect(navigator.highlighted == id)
        #expect(navigator.tab == SettingsCatalog.item(id: id)?.tab)
        #expect(navigator.query.isEmpty)
        #expect(navigator.selectedResult == nil)
    }

    @Test func theTabIsRememberedAcrossLaunches() {
        let defaults = TestDefaults().defaults
        #expect(SettingsNavigator(defaults: defaults).tab == .general)
        SettingsNavigator(defaults: defaults).select(.notifications)
        #expect(defaults.string(forKey: SettingsKeys.settingsLastTab) == "notifications")
        #expect(SettingsNavigator(defaults: defaults).tab == .notifications)
        SettingsNavigator(defaults: defaults).reveal(SettingsCatalog.cliPath)
        #expect(SettingsNavigator(defaults: defaults).tab == .advanced)
        defaults.set("bogus", forKey: SettingsKeys.settingsLastTab)
        #expect(SettingsNavigator(defaults: defaults).tab == .general)
    }
}

@Suite("SettingsKeyAction")
struct SettingsKeyActionTests {
    private func action(_ keyCode: UInt16, _ characters: String?, _ modifiers: NSEvent.ModifierFlags = [],
                        queryEmpty: Bool) -> SettingsKeyAction? {
        SettingsKeyAction.action(keyCode: keyCode, characters: characters, modifiers: modifiers,
                                 queryEmpty: queryEmpty)
    }

    @Test func commandFFocusesSearch() {
        #expect(action(3, "f", .command, queryEmpty: true) == .focusSearch)
        #expect(action(3, "F", [.command, .capsLock], queryEmpty: false) == .focusSearch)
    }

    @Test func escapeClearsOnlyANonEmptyQuery() {
        #expect(action(53, "\u{1B}", queryEmpty: false) == .clearQuery)
        #expect(action(53, "\u{1B}", queryEmpty: true) == nil)
        #expect(action(53, "\u{1B}", .command, queryEmpty: false) == nil)
    }

    @Test func arrowsMoveThePopupSelectionOnlyWhileSearching() {
        #expect(action(125, nil, [.numericPad, .function], queryEmpty: false) == .moveSelection(1))
        #expect(action(126, nil, [.numericPad, .function], queryEmpty: false) == .moveSelection(-1))
        #expect(action(125, nil, queryEmpty: true) == nil)
        #expect(action(126, nil, .shift, queryEmpty: false) == nil)
    }

    @Test func tabClosesThePopupAndStillMovesFocus() {
        #expect(action(48, "\t", queryEmpty: false) == .clearQueryAndPass)
        #expect(action(48, "\u{19}", .shift, queryEmpty: false) == .clearQueryAndPass)
        #expect(action(48, "\t", queryEmpty: true) == nil)
        #expect(action(48, "\t", .control, queryEmpty: false) == nil)
    }

    @Test func otherKeysPassThrough() {
        #expect(action(3, "f", queryEmpty: true) == nil)
        #expect(action(3, "f", [.command, .shift], queryEmpty: true) == nil)
    }
}

@Suite("Settings help")
struct SettingsHelpTests {
    @Test func everyTabIsNamedInBoldInTheSettingsTopic() throws {
        let page = try #require(HelpContent.topic(.settings))
        for tab in SettingsTab.allCases {
            #expect(page.searchText.contains("**\(tab.title)**"), "Settings help does not name the \(tab.title) tab")
        }
    }
}

/// A pointer location a test can move, for `SettingsNavigator`'s hover gate.
@MainActor
final class PointerStub {
    var at: CGPoint
    init(at: CGPoint = .zero) { self.at = at }
}
