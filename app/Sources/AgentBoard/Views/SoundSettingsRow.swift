import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// One Settings → Sounds row: the event's sound picker and a preview
/// button. `stored` is the event's `SoundChoice` string in UserDefaults
/// (empty until the user picks one); `notifyOn` is the event's
/// notification toggle, which sets the default while it is empty.
///
/// An event with a notification toggle gets its own entry for the unset
/// state, "Follow notification toggle (<effective>)", so picking None or
/// System default is always a selection change and stores that value
/// even when it matches what the toggle gives today.
struct SoundSettingsRow: View {
    let event: AttentionEvent
    @Binding var stored: String
    let notifyOn: Bool
    /// Installed system sound names (`SoundChoice.systemSoundNames()`).
    let systemSounds: [String]

    /// The picker tag of Choose file…, never stored.
    static let chooseFileTag = "choose-file"
    /// The picker tag of Follow notification toggle: the unset value.
    static let followToggleTag = ""

    /// The picker's selected tag: `followToggleTag` while an event with a
    /// notification toggle has nothing stored, else the effective choice.
    /// An event without a toggle shows None while unset (nothing can
    /// change its default, so it needs no separate entry).
    static func selectionTag(event: AttentionEvent, stored: String, notifyOn: Bool) -> String {
        if stored.isEmpty && event.hasNotificationToggle { return followToggleTag }
        return event.choice(stored: stored, notifyOn: notifyOn).encoded
    }

    /// The label of the unset entry, naming what the toggle gives now, or
    /// nil for an event without a notification toggle.
    static func followToggleLabel(event: AttentionEvent, notifyOn: Bool) -> String? {
        guard event.hasNotificationToggle else { return nil }
        return "Follow notification toggle (\(event.defaultChoice(notifyOn: notifyOn).label))"
    }

    /// Whether the chosen file is gone or unreadable. Checked when the row
    /// appears and when the choice changes, not on every redraw.
    @State private var fileMissing = false

    /// The effective choice, the same one `SoundPlayer` plays.
    private var choice: SoundChoice { event.choice(stored: stored, notifyOn: notifyOn) }

    /// The stored choice when the picker has no fixed entry for it: a
    /// chosen file, or a system sound this Mac does not have.
    private var extraEntry: (tag: String, label: String)? {
        switch choice {
        case .file(let path):
            return (choice.encoded, fileMissing ? "\((path as NSString).lastPathComponent) (missing)" : choice.label)
        case .system(let name) where !systemSounds.contains(name):
            return (choice.encoded, "\(name) (missing)")
        default:
            return nil
        }
    }

    var body: some View {
        HStack {
            Picker(event.label, selection: selection) {
                if let follow = Self.followToggleLabel(event: event, notifyOn: notifyOn) {
                    Text(follow).tag(Self.followToggleTag)
                    Divider()
                }
                Text(SoundChoice.none.label).tag(SoundChoice.none.encoded)
                Text(SoundChoice.systemDefault.label).tag(SoundChoice.systemDefault.encoded)
                if let extraEntry {
                    Divider()
                    Text(extraEntry.label).tag(extraEntry.tag)
                }
                Divider()
                ForEach(systemSounds, id: \.self) { name in
                    Text(name).tag(SoundChoice.system(name).encoded)
                }
                Divider()
                Text("Choose file…").tag(Self.chooseFileTag)
            }
            .help("The sound for \"\(event.label)\": none, the macOS alert sound, a system sound, or an audio file you choose. Plays even with notifications off\(event.hasNotificationToggle ? ". Follow notification toggle plays the alert sound only while that notification is on" : "")")
            Button {
                SoundPlayer.shared.play(choice, for: nil)
            } label: {
                Image(systemName: "play.circle")
            }
            .buttonStyle(.borderless)
            .disabled(choice == .none)
            .help("Play this sound now")
        }
        .onAppear(perform: checkFile)
        .onChange(of: stored) { _, _ in checkFile() }
    }

    private func checkFile() {
        if case .file = choice {
            fileMissing = choice.resolvedURL() == nil
        } else {
            fileMissing = false
        }
    }

    /// Choosing Choose file… opens the panel and leaves the stored choice
    /// alone until a file is picked, so cancelling keeps the old one.
    /// Follow notification toggle stores the empty (unset) value.
    private var selection: Binding<String> {
        Binding(
            get: { Self.selectionTag(event: event, stored: stored, notifyOn: notifyOn) },
            set: { newValue in
                if newValue == Self.chooseFileTag {
                    // After the menu closes, not while it is tracking.
                    Task { @MainActor in chooseFile() }
                } else {
                    stored = newValue
                }
            }
        )
    }

    private func chooseFile() {
        let panel = NSOpenPanel()
        panel.title = "Choose a sound for \(event.label)"
        panel.prompt = "Choose"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = SoundChoice.fileExtensions.compactMap { UTType(filenameExtension: $0) }
        if case .file(let path) = choice {
            panel.directoryURL = URL(fileURLWithPath: path).deletingLastPathComponent()
        }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        stored = SoundChoice.file(url.path).encoded
    }
}
