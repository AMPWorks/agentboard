import AppKit
import Foundation

/// The Settings window's tabs, in tab order. Section names inside them are
/// the window's section headers (and the Settings help topic's headings).
enum SettingsTab: String, CaseIterable, Identifiable, Sendable {
    case general, menuBar, notifications, machines, advanced

    var id: String { rawValue }

    var title: String {
        switch self {
        case .general: return "General"
        case .menuBar: return "Menu bar"
        case .notifications: return "Notifications"
        case .machines: return "Machines"
        case .advanced: return "Advanced"
        }
    }

    /// SF Symbol of the tab item.
    var symbol: String {
        switch self {
        case .general: return "gearshape"
        case .menuBar: return "menubar.rectangle"
        case .notifications: return "bell.badge"
        case .machines: return "desktopcomputer"
        case .advanced: return "wrench.and.screwdriver"
        }
    }

    /// The tab stored under `SettingsKeys.settingsLastTab`; a missing or
    /// unknown value is General.
    static func stored(_ raw: String?) -> SettingsTab {
        raw.flatMap(SettingsTab.init(rawValue:)) ?? .general
    }
}

/// One searchable Settings control: where it lives, the label and tooltip
/// the control shows (the catalog is their single source), and the
/// defaults keys it writes.
struct SettingsItem: Identifiable, Sendable, Equatable {
    let id: String
    let tab: SettingsTab
    let section: String
    let label: String
    let help: String
    var keys: [String] = []
    /// The row a search result goes to while this item's own row is not
    /// on screen (a row shown only in some states).
    var fallback: String? = nil

    /// "Menu bar → Popover size".
    var path: String { "\(tab.title) → \(section)" }

    /// Everything a search term can match.
    var searchText: String {
        ([label, help, section, tab.title] + keys).joined(separator: "\n")
    }
}

/// Every searchable Settings control, in window order.
enum SettingsCatalog {
    // MARK: General

    static let appearance = SettingsItem(
        id: "appearance", tab: .general, section: "Appearance", label: "Appearance",
        help: "Use the system's light or dark mode for AgentBoard's windows, or always dark or light",
        keys: [SettingsKeys.appearance])
    static let launchAtLogin = SettingsItem(
        id: "launchAtLogin", tab: .general, section: "General", label: "Launch at login",
        help: "Start AgentBoard automatically when you log in to this Mac")
    static let showPlanUsage = SettingsItem(
        id: "showPlanUsage", tab: .general, section: "General", label: "Show plan usage",
        help: "Show each plan-usage source (how much of each limit window is used, and when it resets) in the popover footer and the dashboard sidebar, whenever a source has numbers",
        keys: [SettingsKeys.showPlanUsage])
    static let showClosedAndMergedPRs = SettingsItem(
        id: "showClosedAndMergedPRs", tab: .general, section: "General", label: "Show closed and merged PRs",
        help: "Also show buttons for merged and closed PRs on session rows; the detail view lists every link either way",
        keys: [SettingsKeys.showClosedAndMergedPRs])

    // MARK: Menu bar

    static let menuBarIconColored = SettingsItem(
        id: "menuBarIconColored", tab: .menuBar, section: "Menu bar icon",
        label: "Color the menu bar icon by session state",
        help: "Tint the menu bar icon with the most urgent session's color; off shows the plain monochrome icon",
        keys: [MenuBarIconSettings.coloredKey])
    static let menuBarIconFlash = SettingsItem(
        id: "menuBarIconFlash", tab: .menuBar, section: "Menu bar icon",
        label: "Flash when a session needs you",
        help: "Flash the menu bar icon when a session newly needs you (needs the colored icon)",
        keys: [MenuBarIconSettings.flashKey])
    static let menuBarIconFlashStop = SettingsItem(
        id: "menuBarIconFlashStop", tab: .menuBar, section: "Menu bar icon", label: "Stop flashing",
        help: "Choose when a flashing icon stops: when you open AgentBoard, after a delay, or only once every session is handled",
        keys: [MenuBarIconSettings.flashStopKey])
    static let updatesPanel = SettingsItem(
        id: "updatesPanel", tab: .menuBar, section: "Popover", label: "Updates panel",
        help: "Choose whether the popover's Updates panel starts hidden behind the Updates button, remembers how you left it, or is always shown",
        keys: [PopoverUpdatesSettings.modeKey])
    static let updatesTextSize = SettingsItem(
        id: "updatesTextSize", tab: .menuBar, section: "Popover", label: "Updates text size",
        help: "Text size of the rows and header in the popover's Updates panel",
        keys: [PopoverUpdatesSettings.fontSizeKey])
    static let updatesCompactLimit = SettingsItem(
        id: "updatesCompactLimit", tab: .menuBar, section: "Popover", label: "Updates listed before Show all",
        help: "How many updates besides the ones that need you the Updates panel lists before Show all",
        keys: [PopoverUpdatesSettings.compactLimitKey])
    static let popoverAutoExpand = SettingsItem(
        id: "popoverAutoExpand", tab: .menuBar, section: "Popover size", label: "Fit height to active sessions",
        help: "Grow or shrink the popover to show every Needs you, Review and Running session, up to the screen's height; while on, the corner grip changes only the width",
        keys: [PopoverSizing.autoExpandKey])
    static let popoverAutoWidth = SettingsItem(
        id: "popoverAutoWidth", tab: .menuBar, section: "Popover size", label: "Use the full width to the screen edge",
        help: "Make the popover as wide as the room from the menu bar icon to the right edge of the screen; turning this off keeps the width you last dragged to (or the current width if you never dragged), and dragging the corner grip sideways turns it off",
        keys: [PopoverSizing.autoWidthKey])
    static let resetSize = SettingsItem(
        id: "resetSize", tab: .menuBar, section: "Popover size", label: "Reset size",
        help: "Go back to the default height and the full width (the same as double-clicking the corner grip); Fit height stays as it is",
        keys: [PopoverSizing.heightKey, PopoverSizing.widthKey])

    // MARK: Notifications

    static let notifyOnNeedsYou = SettingsItem(
        id: "notifyOnNeedsYou", tab: .notifications, section: "Notifications",
        label: "Notify when a session needs you",
        help: "Post a macOS notification when a session newly needs you; click it to jump to that terminal",
        keys: [SettingsKeys.notifyOnNeedsYou])
    static let notifyOnTurnFinished = SettingsItem(
        id: "notifyOnTurnFinished", tab: .notifications, section: "Notifications",
        label: "Notify when a session finishes a turn",
        help: "Post a macOS notification when a session you are working with finishes its turn; click it to jump to that terminal",
        keys: [SettingsKeys.notifyOnTurnFinished])

    /// One item per sound row, in `AttentionEvent` order.
    static let sounds: [SettingsItem] = AttentionEvent.allCases.map(sound)

    static func sound(_ event: AttentionEvent) -> SettingsItem {
        SettingsItem(
            id: "sound.\(event.rawValue)", tab: .notifications, section: "Sounds", label: event.label,
            help: SoundSettingsRow.pickerHelp(event: event), keys: [soundKey(event)])
    }

    private static func soundKey(_ event: AttentionEvent) -> String {
        switch event {
        case .needsYou: return SettingsKeys.soundNeedsYou
        case .turnFinished: return SettingsKeys.soundTurnFinished
        case .denied: return SettingsKeys.soundDenied
        case .reviewReady: return SettingsKeys.soundReviewReady
        }
    }

    // MARK: Machines

    static let thisMacName = SettingsItem(
        id: "thisMacName", tab: .machines, section: "Machines", label: "This Mac's name",
        help: "The name your other Macs show for this one (unless one of them renames it there); press Return to save")
    static let thisMacAddress = SettingsItem(
        id: "thisMacAddress", tab: .machines, section: "Machines", label: "Address others use to reach this Mac",
        help: "The address your other Macs connect to, put in this Mac's pairing code: a VPN or DNS name or an IP address works from anywhere, a .local name only on the same network. Press Return to save")
    static let shareLevel = SettingsItem(
        id: "shareLevel", tab: .machines, section: "Machines", label: "Share with other machines",
        help: "How much of this Mac's sessions your other Macs receive: everything, titles without prompt, reply, update or folder text, or state and project name only")
    static let sharingSettings = SettingsItem(
        id: "sharingSettings", tab: .machines, section: "Machines", label: "Open Sharing Settings",
        help: "Open System Settings → General → Sharing, where Remote Login is turned on")
    static let copyPairingCode = SettingsItem(
        id: "copyPairingCode", tab: .machines, section: "Machines", label: "Copy pairing code",
        help: "Copy this Mac's pairing code to paste into Add machine… on your other Mac; it holds only public keys, names and this Mac's address. Press Return in the address field first to save an edit")
    /// Stands for the per-peer rows (Host, Allow opening, Remove), whose
    /// own tooltips name the peer.
    static let pairedMachines = SettingsItem(
        id: "pairedMachines", tab: .machines, section: "Machines", label: "Paired machines",
        help: "Each paired Mac's status, the host ssh reaches it by, whether it may open sessions on this Mac (shell access as you), and Remove to stop syncing with it",
        fallback: "addMachine")
    static let peerDisplayName = SettingsItem(
        id: "peerDisplayName", tab: .machines, section: "Machines", label: "Name shown here",
        help: "Name shown here for this Mac; only this Mac sees it; leave empty to use the Mac's own name. Press Return to save",
        fallback: "addMachine")
    static let addMachine = SettingsItem(
        id: "addMachine", tab: .machines, section: "Machines", label: "Add machine…",
        help: "Pair another of your Macs by pasting its pairing code")
    static let showRemoteSessions = SettingsItem(
        id: "showRemoteSessions", tab: .machines, section: "Machines", label: "Show sessions from other machines",
        help: "Show the sessions of your paired Macs on this board; off hides them and stops syncing",
        keys: [SettingsKeys.showRemoteSessions])
    static let notifyRemoteSessions = SettingsItem(
        id: "notifyRemoteSessions", tab: .machines, section: "Machines", label: "Notify for sessions on other machines",
        help: "Let sessions on your paired Macs notify, play sounds and flash the menu bar icon; their rows show either way",
        keys: [SettingsKeys.notifyRemoteSessions])
    static let groupPopoverByMachine = SettingsItem(
        id: "groupPopoverByMachine", tab: .machines, section: "Machines", label: "Group the popover by machine",
        help: "On: each popover section lists this Mac's sessions first, then each other Mac's, under a header naming the Mac. Off: one list per section, newest first, with the Mac's name on other Macs' rows",
        keys: [SettingsKeys.groupPopoverByMachine])
    static let remoteIconMode = SettingsItem(
        id: "remoteIconMode", tab: .machines, section: "Machines", label: "Other Macs in the menu bar icon",
        help: "Separate marker: the icon and its count are this Mac's, and a diamond at its corner shows another Mac's session needs you. Merged: other Macs' sessions color, count and flash the icon like this Mac's. Not shown: the icon ignores other Macs",
        keys: [MenuBarIconSettings.remoteModeKey])

    // MARK: Advanced

    static let cliPath = SettingsItem(
        id: "cliPath", tab: .advanced, section: "CLI & Data", label: "agentboard path override (optional)",
        help: "Full path to the agentboard CLI, used only when it is not in the app bundle or on $PATH",
        keys: [AgentBoardCLI.pathDefaultsKey])
    static let useFixture = SettingsItem(
        id: "useFixture", tab: .advanced, section: "CLI & Data", label: "Use fixture data (development)",
        help: "Show a saved sample snapshot instead of calling the CLI; actions are logged, not run",
        keys: [AgentBoardCLI.useFixtureDefaultsKey])
    static let fixturePath = SettingsItem(
        id: "fixturePath", tab: .advanced, section: "CLI & Data",
        label: "Fixture path (optional, uses bundled fixture if empty)",
        help: "Snapshot JSON file to show; empty uses the sample bundled in the app",
        keys: [AgentBoardCLI.fixturePathDefaultsKey], fallback: "useFixture")
    static let verboseLogging = SettingsItem(
        id: "verboseLogging", tab: .advanced, section: "Diagnostics", label: "Verbose logging",
        help: "Also log every background snapshot and the CLI's debug detail, in the app and in agentboard.log",
        keys: [CLIRunner.verboseLoggingKey])
    static let openLog = SettingsItem(
        id: "openLog", tab: .advanced, section: "Diagnostics", label: "Open log",
        help: "Reveal agentboard.log, the diagnostic log, in Finder")
    /// The button's own label is just "Reveal"; it shows only once the
    /// hooks have logged a failure.
    static let revealHookErrorsLog = SettingsItem(
        id: "revealHookErrorsLog", tab: .advanced, section: "Diagnostics", label: "Reveal",
        help: "Reveal hook-errors.log, where the hooks record their own failures, in Finder",
        fallback: "openLog")

    static let items: [SettingsItem] = [
        appearance, launchAtLogin, showPlanUsage, showClosedAndMergedPRs,
        menuBarIconColored, menuBarIconFlash, menuBarIconFlashStop,
        updatesPanel, updatesTextSize, updatesCompactLimit,
        popoverAutoExpand, popoverAutoWidth, resetSize,
        notifyOnNeedsYou, notifyOnTurnFinished,
    ] + sounds + [
        thisMacName, thisMacAddress, shareLevel, sharingSettings, copyPairingCode, pairedMachines, peerDisplayName, addMachine,
        showRemoteSessions, groupPopoverByMachine, notifyRemoteSessions, remoteIconMode,
        cliPath, useFixture, fixturePath,
        verboseLogging, openLog, revealHookErrorsLog,
    ]

    /// Settings keys no control writes directly, so no item lists them.
    static let keysWithoutItem: Set<String> = [SettingsKeys.settingsLastTab]

    static func items(in tab: SettingsTab) -> [SettingsItem] {
        items.filter { $0.tab == tab }
    }

    static func item(id: SettingsItem.ID) -> SettingsItem? {
        items.first { $0.id == id }
    }

    /// The tab's section names, in window order.
    static func sections(in tab: SettingsTab) -> [String] {
        var seen: [String] = []
        for item in items(in: tab) where !seen.contains(item.section) {
            seen.append(item.section)
        }
        return seen
    }
}

/// The Settings search: the Help window's matching (whitespace-separated
/// terms, all must match, case- and diacritic-insensitive) over each
/// item's label, help text, section, tab title and keys. An empty query
/// matches nothing, so no popup shows.
struct SettingsSearch: Sendable, Equatable {
    let terms: [String]

    init(_ query: String) {
        terms = HelpSearch(query).terms
    }

    var isEmpty: Bool { terms.isEmpty }

    func matches(_ item: SettingsItem) -> Bool {
        guard !terms.isEmpty else { return false }
        let haystack = HelpSearch.fold(item.searchText)
        return terms.allSatisfy { haystack.contains($0) }
    }

    /// The matching items grouped by tab, in tab order then window order;
    /// tabs with no match are left out.
    func results(in items: [SettingsItem] = SettingsCatalog.items) -> [(tab: SettingsTab, items: [SettingsItem])] {
        guard !isEmpty else { return [] }
        let matched = items.filter(matches)
        return SettingsTab.allCases.compactMap { tab in
            let inTab = matched.filter { $0.tab == tab }
            return inTab.isEmpty ? nil : (tab, inTab)
        }
    }
}

/// What a key press in the Settings window does. Pure, so ⌘F, Escape and
/// the arrow keys are testable without a window (the accessory app has no
/// Find menu).
enum SettingsKeyAction: Equatable {
    /// ⌘F: focus the search field.
    case focusSearch
    /// Escape with a query: clear it, which closes the results popup.
    case clearQuery
    /// Up or Down arrow with a query: move the popup's selection.
    case moveSelection(Int)
    /// Tab or Shift-Tab with a query: focus is leaving the search field,
    /// so clear the query (closing the popup) and let the key move focus.
    case clearQueryAndPass

    static let upArrowKeyCode: UInt16 = 126
    static let downArrowKeyCode: UInt16 = 125
    static let tabKeyCode: UInt16 = 48

    static func action(
        keyCode: UInt16, characters: String?, modifiers: NSEvent.ModifierFlags, queryEmpty: Bool
    ) -> SettingsKeyAction? {
        let mods = modifiers.intersection([.command, .option, .control, .shift])
        if keyCode == PopoverKeyAction.escapeKeyCode && mods.isEmpty {
            return queryEmpty ? nil : .clearQuery
        }
        if (keyCode == upArrowKeyCode || keyCode == downArrowKeyCode) && mods.isEmpty && !queryEmpty {
            return .moveSelection(keyCode == upArrowKeyCode ? -1 : 1)
        }
        if keyCode == tabKeyCode && mods.subtracting(.shift).isEmpty && !queryEmpty {
            return .clearQueryAndPass
        }
        if mods == .command && characters?.lowercased() == "f" {
            return .focusSearch
        }
        return nil
    }
}

/// Which tab Settings shows, the search query and the row to highlight.
/// The tab is remembered under `SettingsKeys.settingsLastTab`, so Settings
/// reopens where it was left; a deep link (`SettingsWindowController
/// .show(using:tab:item:)`) sets it before the window opens.
@MainActor
final class SettingsNavigator: ObservableObject {
    static let shared = SettingsNavigator()

    @Published var tab: SettingsTab {
        didSet { if tab != oldValue { defaults.set(tab.rawValue, forKey: SettingsKeys.settingsLastTab) } }
    }
    /// The search query; while it matches anything, the results popup
    /// shows under the search field with its first result selected.
    @Published var query = "" {
        didSet {
            guard query != oldValue else { return }
            selectedResult = results.first?.id
            keyedSelection()
        }
    }
    /// The popup row that Return goes to (arrow keys and the pointer move it).
    @Published private(set) var selectedResult: SettingsItem.ID?
    /// Whether the popup scrolls to `selectedResult`: yes when the keyboard
    /// moved it, no when the pointer did (that row is already under the
    /// pointer, and scrolling would slide another one under it).
    @Published private(set) var selectionScrolls = true
    /// The item whose row wears the highlight, until its pane clears it.
    @Published private(set) var highlighted: SettingsItem.ID?

    private let defaults: UserDefaults
    /// The pointer's screen location, read when the selection changes.
    private let pointerLocation: @MainActor () -> CGPoint
    /// Where the pointer was when the keyboard (a key or the query) last
    /// moved the selection. Hover from that same spot is the list moving
    /// under a still pointer, not the user pointing, and is ignored.
    private var pointerAtKeyedSelection: CGPoint?

    init(defaults: UserDefaults = .standard,
         pointerLocation: @escaping @MainActor () -> CGPoint = { NSEvent.mouseLocation }) {
        self.defaults = defaults
        self.pointerLocation = pointerLocation
        tab = SettingsTab.stored(defaults.string(forKey: SettingsKeys.settingsLastTab))
    }

    private func keyedSelection() {
        selectionScrolls = true
        pointerAtKeyedSelection = pointerLocation()
    }

    func select(_ tab: SettingsTab) {
        self.tab = tab
    }

    /// The popup's rows: the query's matches in tab order, then window order.
    var results: [SettingsItem] {
        SettingsSearch(query).results().flatMap(\.items)
    }

    /// Moves the popup selection by `delta` rows, stopping at either end.
    func moveSelection(by delta: Int) {
        let rows = results
        guard !rows.isEmpty else { selectedResult = nil; return }
        let current = rows.firstIndex { $0.id == selectedResult }
        let next = current.map { $0 + delta } ?? (delta > 0 ? 0 : rows.count - 1)
        selectedResult = rows[min(max(next, 0), rows.count - 1)].id
        keyedSelection()
    }

    /// Selects a popup row the pointer entered, unless the pointer has not
    /// moved since the keyboard last moved the selection (the row slid
    /// under it). Does not scroll the popup.
    func selectResult(_ id: SettingsItem.ID) {
        guard results.contains(where: { $0.id == id }),
              pointerLocation() != pointerAtKeyedSelection else { return }
        selectedResult = id
        selectionScrolls = false
    }

    /// Goes to the selected popup row; false when nothing is selected.
    @discardableResult
    func revealSelection() -> Bool {
        guard let id = selectedResult, let item = SettingsCatalog.item(id: id) else { return false }
        reveal(item)
        return true
    }

    /// Goes to `item`: clears the search, shows its tab, highlights its row.
    func reveal(_ item: SettingsItem) {
        query = ""
        tab = item.tab
        highlighted = item.id
    }

    /// Clears the highlight, or only `id`'s when given (a later reveal
    /// keeps its own).
    func clearHighlight(_ id: SettingsItem.ID? = nil) {
        if id == nil || highlighted == id { highlighted = nil }
    }
}
