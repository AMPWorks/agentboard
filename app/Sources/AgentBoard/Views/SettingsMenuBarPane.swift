import SwiftUI

/// Settings → Menu bar: Menu bar icon, Popover and Popover size.
struct SettingsMenuBarPane: View {
    @AppStorage(MenuBarIconSettings.coloredKey) private var menuBarIconColored: Bool = true
    @AppStorage(MenuBarIconSettings.flashKey) private var menuBarIconFlash: Bool = true
    @AppStorage(MenuBarIconSettings.flashStopKey) private var menuBarIconFlashStop: String = FlashStopPolicy.onOpen.rawValue
    @AppStorage(PopoverUpdatesSettings.modeKey) private var updatesMode: String = UpdatesPanelMode.default.rawValue
    @AppStorage(PopoverUpdatesSettings.fontSizeKey) private var updatesFontSize: String = UpdatesFontSize.default.rawValue
    @AppStorage(PopoverUpdatesSettings.compactLimitKey) private var updatesCompactLimit: Int = UpdatesFeed.compactLimit
    @AppStorage(PopoverSizing.autoExpandKey) private var popoverAutoExpand: Bool = false
    @AppStorage(PopoverSizing.autoWidthKey) private var popoverAutoWidth: Bool = true
    @AppStorage(PopoverSizing.widthKey) private var popoverWidth: Double = 0

    var body: some View {
        SettingsPane(tab: .menuBar) {
            Section {
                Toggle(SettingsCatalog.menuBarIconColored.label, isOn: $menuBarIconColored)
                    .help(SettingsCatalog.menuBarIconColored.help)
                    .settingsAnchor(SettingsCatalog.menuBarIconColored)
                Toggle(SettingsCatalog.menuBarIconFlash.label, isOn: $menuBarIconFlash)
                    .disabled(!menuBarIconColored)
                    .help(SettingsCatalog.menuBarIconFlash.help)
                    .settingsAnchor(SettingsCatalog.menuBarIconFlash)
                Picker(SettingsCatalog.menuBarIconFlashStop.label, selection: $menuBarIconFlashStop) {
                    ForEach(FlashStopPolicy.allCases) { policy in
                        Text(policy.label).tag(policy.rawValue)
                    }
                }
                .disabled(!menuBarIconColored || !menuBarIconFlash)
                .help(SettingsCatalog.menuBarIconFlashStop.help)
                .settingsAnchor(SettingsCatalog.menuBarIconFlashStop)
                Text("The icon takes the color of the most urgent session. With Reduce motion on, it shows an exclamation badge instead of flashing.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                SettingsSectionHeader("Menu bar icon")
            }

            Section {
                Picker(SettingsCatalog.updatesPanel.label, selection: $updatesMode) {
                    ForEach(UpdatesPanelMode.allCases) { mode in
                        Text(mode.label).tag(mode.rawValue)
                    }
                }
                .help(SettingsCatalog.updatesPanel.help)
                .settingsAnchor(SettingsCatalog.updatesPanel)
                Picker(SettingsCatalog.updatesTextSize.label, selection: $updatesFontSize) {
                    ForEach(UpdatesFontSize.allCases) { size in
                        Text(size.label).tag(size.rawValue)
                    }
                }
                .pickerStyle(.segmented)
                .help(SettingsCatalog.updatesTextSize.help)
                .settingsAnchor(SettingsCatalog.updatesTextSize)
                Picker(SettingsCatalog.updatesCompactLimit.label, selection: $updatesCompactLimit) {
                    ForEach(PopoverUpdatesSettings.compactLimits, id: \.self) { limit in
                        Text("\(limit)").tag(limit)
                    }
                }
                .pickerStyle(.segmented)
                .help(SettingsCatalog.updatesCompactLimit.help)
                .settingsAnchor(SettingsCatalog.updatesCompactLimit)
                Text(Self.popoverFootnote(UpdatesPanelMode.parse(updatesMode)))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                SettingsSectionHeader("Popover")
            }

            Section {
                Toggle(SettingsCatalog.popoverAutoExpand.label, isOn: $popoverAutoExpand)
                    .help(SettingsCatalog.popoverAutoExpand.help)
                    .settingsAnchor(SettingsCatalog.popoverAutoExpand)
                Toggle(SettingsCatalog.popoverAutoWidth.label, isOn: autoWidthBinding)
                    .help(SettingsCatalog.popoverAutoWidth.help)
                    .settingsAnchor(SettingsCatalog.popoverAutoWidth)
                HStack {
                    Text(Self.sizeFootnote(autoExpand: popoverAutoExpand))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button(SettingsCatalog.resetSize.label) { PopoverSizing.resetSize() }
                        .help(SettingsCatalog.resetSize.help)
                        .settingsAnchor(SettingsCatalog.resetSize)
                }
            } header: {
                SettingsSectionHeader("Popover size")
            }
        }
    }

    static func popoverFootnote(_ mode: UpdatesPanelMode) -> String {
        let red = "The Updates button turns red while any update needs you."
        switch mode {
        case .hiddenUntilClicked:
            return "The popover opens on the sessions. Click Updates next to the search field to show the panel; it hides again the next time the popover opens. " + red
        case .rememberLast:
            return "The panel stays open or closed the way you last left it with the Updates button. " + red
        case .alwaysShown:
            return "The panel sits above the sessions whenever there are updates, and there is no Updates button."
        }
    }

    /// Turning auto width off gives the popover a width of its own (the
    /// current full width unless one was dragged before), so the toggle has
    /// a visible meaning instead of waiting for a drag.
    private var autoWidthBinding: Binding<Bool> {
        Binding(
            get: { popoverAutoWidth },
            set: { on in
                if !on, let width = PopoverSizing.widthWhenAutoTurnsOff(
                    stored: popoverWidth, available: PopoverWindowPlacer.iconScreen()?.availableWidth) {
                    popoverWidth = width
                }
                popoverAutoWidth = on
            }
        )
    }

    static func sizeFootnote(autoExpand: Bool) -> String {
        autoExpand
            ? "Drag the grip in the popover's bottom-right corner to change its width; the height follows the active sessions."
            : "Drag the grip in the popover's bottom-right corner to resize it; AgentBoard remembers the size."
    }
}
