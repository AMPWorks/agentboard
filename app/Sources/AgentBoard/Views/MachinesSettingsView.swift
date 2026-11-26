import AppKit
import SwiftUI

/// Pure text for Settings → Machines, kept out of the views so it is testable.
enum MachinesDisplay {
    static let shareLevels: [(value: String, label: String)] = [
        ("full", "Everything"), ("titles", "Titles only"), ("state", "State only"),
    ]

    /// The System Settings pane with Remote Login (General → Sharing).
    static let sharingSettingsURL = URL(string: "x-apple.systempreferences:com.apple.Sharing-Settings.extension")!

    /// "Online", "Offline, last seen 2 h ago: <why>", "Not connected yet", ...
    static func statusText(_ peer: Peer, now: Date = Date()) -> String {
        var text: String
        switch peer.status {
        case "online": text = "Online"
        case "never": text = "Not connected yet"
        case "expired": text = "Not reached for a long time"
        case "incompatible": text = "Incompatible version"
        case "offline":
            if let seen = peer.lastSeenAt, !seen.isEmpty {
                text = "Offline, last seen \(RelativeTime.string(from: seen, relativeTo: now))"
            } else {
                text = "Offline"
            }
        default: text = peer.status.capitalized
        }
        if let error = peer.error, !error.isEmpty {
            text += ": \(error)"
        }
        if peer.status == "online", let skew = peer.clockSkewSecs, abs(skew) > 60 {
            text += " (its clock is \(Int(abs(skew)))s \(skew > 0 ? "ahead" : "behind"))"
        }
        return text
    }

    static func remoteLoginText(_ status: String) -> String {
        switch status {
        case "on": return "Remote Login is on"
        case "off": return "Remote Login is off: other Macs can't reach this one"
        default: return "Remote Login: couldn't tell"
        }
    }
}

/// Settings → Machines: this Mac's name and pairing code, Remote Login,
/// the paired Macs, the share level and the two display toggles.
struct MachinesSettingsSection: View {
    @ObservedObject var store: SnapshotStore
    @AppStorage(SettingsKeys.showRemoteSessions) private var showRemote: Bool = true
    @AppStorage(SettingsKeys.notifyRemoteSessions) private var notifyRemote: Bool = true
    @AppStorage(MenuBarIconSettings.remoteModeKey) private var remoteIconMode: String = RemoteIconMode.separate.rawValue
    @State private var name = ""
    @State private var showingAdd = false
    @State private var removing: Peer?
    @State private var copied = false

    var body: some View {
        Section {
            if let info = store.peersInfo {
                thisMac(info)
                let chips = MachineChip.labels(for: info.peers.map { Machine(id: $0.id, name: $0.name) })
                ForEach(info.peers) { peer in
                    PeerRow(peer: peer, chip: chips[peer.id] ?? MachineChip.initial(peer.name),
                            store: store, remove: { removing = peer })
                }
            } else if let error = store.peersError {
                Text(error).font(.caption).foregroundStyle(.red)
            } else {
                Text("Loading…").font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Add machine…") { showingAdd = true }
                    .help("Pair another of your Macs by pasting its pairing code")
                Spacer()
            }
            Toggle("Show sessions from other machines", isOn: $showRemote)
                .onChange(of: showRemote) { _, _ in store.applyRemoteFilter() }
                .help("Show the sessions of your paired Macs on this board; off hides them and stops syncing")
            Toggle("Notify for sessions on other machines", isOn: $notifyRemote)
                .help("Let sessions on your paired Macs notify, play sounds and flash the menu bar icon; their rows show either way")
            Picker("Other Macs in the menu bar icon", selection: $remoteIconMode) {
                ForEach(RemoteIconMode.allCases) { mode in
                    Text(mode.label).tag(mode.rawValue)
                }
            }
            .help("Separate marker: the icon and its count are this Mac's, and a diamond at its corner shows another Mac's session needs you. Merged: other Macs' sessions color, count and flash the icon like this Mac's. Not shown: the icon ignores other Macs")
            Text("Each Mac pulls the others' boards over ssh (Remote Login): pair in both directions. Being allowed to open sessions on a Mac is shell access as you there.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if let error = store.peersError, store.peersInfo != nil {
                Text(error).font(.caption).foregroundStyle(.red)
            }
        } header: {
            HStack {
                Text("Machines")
                Spacer()
                HelpButton(topic: .machines, tooltip: "Explain syncing with your other Macs in AgentBoard Help")
            }
        }
        .task {
            // Status changes (a Mac going offline) show while Settings is open.
            while !Task.isCancelled {
                await store.refreshPeers()
                if name.isEmpty, let info = store.peersInfo { name = info.machine.name }
                try? await Task.sleep(for: .seconds(5))
            }
        }
        .sheet(isPresented: $showingAdd) {
            AddMachineSheet(store: store, isPresented: $showingAdd)
        }
        .confirmationDialog(
            "Remove \(removing?.name ?? "this machine")?",
            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
            presenting: removing
        ) { peer in
            Button("Remove", role: .destructive) {
                Task { _ = await store.removePeer(peer.id) }
            }
            .help("Stop syncing with this Mac and remove its key from this Mac's authorized_keys")
            Button("Cancel", role: .cancel) { removing = nil }
                .help("Keep this machine")
        } message: { peer in
            Text("Its sessions leave this board, and \(peer.name) can no longer reach this Mac. Remove this Mac on \(peer.name) too.")
        }
    }

    @ViewBuilder
    private func thisMac(_ info: PeersInfo) -> some View {
        HStack {
            TextField("This Mac's name", text: $name)
                .onSubmit { Task { _ = await store.setThisMac(name: name) } }
                .help("How this Mac is named on your other Macs' boards; press Return to save")
        }
        Picker("Share with other machines", selection: Binding(
            get: { info.share },
            set: { level in Task { _ = await store.setThisMac(share: level) } }
        )) {
            ForEach(MachinesDisplay.shareLevels, id: \.value) { level in
                Text(level.label).tag(level.value)
            }
        }
        .help("How much of this Mac's sessions your other Macs receive: everything, titles without prompt, reply, update or folder text, or state and project name only")
        HStack {
            Text(MachinesDisplay.remoteLoginText(info.remoteLogin))
                .font(.caption)
                .foregroundStyle(info.remoteLogin == "off" ? .red : .secondary)
            Spacer()
            Button("Open Sharing Settings") { NSWorkspace.shared.open(MachinesDisplay.sharingSettingsURL) }
                .help("Open System Settings → General → Sharing, where Remote Login is turned on")
        }
        HStack {
            if let fingerprint = info.fingerprint {
                Text("Fingerprint \(fingerprint)")
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .help("The other Mac shows the same fingerprint when you paste this code")
            } else if let error = info.codeError {
                Text(error).font(.caption).foregroundStyle(.red)
            }
            Spacer()
            Button(copied ? "Copied" : "Copy pairing code") {
                if let code = info.code {
                    ClipboardHelper.copy(code)
                    copied = true
                    Task {
                        try? await Task.sleep(for: .seconds(2))
                        copied = false
                    }
                }
            }
            .disabled(info.code == nil)
            .help("Copy this Mac's pairing code to paste into Add machine… on your other Mac; it holds only public keys and names")
        }
    }
}

/// One paired Mac: status, editable host, the allow-opening switch, Remove.
private struct PeerRow: View {
    let peer: Peer
    /// The Mac's chip label, as its rows show it.
    let chip: String
    @ObservedObject var store: SnapshotStore
    let remove: () -> Void
    @State private var host = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                MachineChipView(label: chip, offline: !peer.isOnline, size: 18)
                    .help(peer.isOnline ? "Connected; its rows carry this chip" : "Not connected; its rows carry this chip")
                Circle()
                    .fill(peer.isOnline ? Color.green : Color.secondary)
                    .frame(width: 6, height: 6)
                    .help(peer.isOnline ? "Connected" : "Not connected")
                Text(peer.name).font(.body.weight(.medium))
                Spacer()
                Button("Remove", role: .destructive, action: remove)
                    .controlSize(.small)
                    .help("Stop syncing with \(peer.name) and remove its key from this Mac")
            }
            Text(MachinesDisplay.statusText(peer))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextField("Host", text: $host)
                .onSubmit { Task { _ = await store.setPeer(peer.id, host: host) } }
                .help("The name ssh reaches \(peer.name) by; a .local name works on the same network only, so use a VPN or DNS name to reach it from elsewhere. Press Return to save")
            Toggle("Allow opening sessions on this Mac", isOn: Binding(
                get: { peer.allowOpen },
                set: { allow in Task { _ = await store.setPeer(peer.id, allowOpen: allow) } }
            ))
            .help("Let \(peer.name) attach to or resume your sessions on this Mac over ssh; this is shell access as you. Off, it can only read the board and mark sessions")
            if peer.authorized == false {
                Text("This Mac's authorized_keys no longer has \(peer.name)'s key: remove it and pair again.")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
        .padding(.vertical, 2)
        .onAppear { host = peer.host }
        .onChange(of: peer.host) { _, newValue in host = newValue }
    }
}

/// Paste a pairing code, check the name and fingerprint, confirm.
private struct AddMachineSheet: View {
    @ObservedObject var store: SnapshotStore
    @Binding var isPresented: Bool
    @State private var code = ""
    @State private var preview: PeerPreview?
    @State private var error: String?
    @State private var allowOpen = true
    @State private var working = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Add machine").font(.headline)
            Text("On the other Mac, open Settings → Machines and click Copy pairing code, then paste it here.")
                .font(.caption)
                .foregroundStyle(.secondary)
            TextEditor(text: $code)
                .font(.caption.monospaced())
                .frame(height: 80)
                .border(.quaternary)
                .help("The other Mac's pairing code")
                .onChange(of: code) { _, _ in
                    preview = nil
                    error = nil
                }
            if let preview {
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(preview.name) (\(preview.user)@\(preview.host))").font(.body.weight(.medium))
                    Text("Fingerprint \(preview.fingerprint)")
                        .font(.caption.monospaced())
                    Text("Check that the other Mac shows the same name and fingerprint before adding it.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if let note = preview.replacementNote {
                        Text(note).font(.caption)
                    }
                }
                if preview.canAdd {
                    Toggle("Allow \(preview.name) to open sessions on this Mac", isOn: $allowOpen)
                        .help("Let that Mac attach to or resume your sessions here over ssh; this is shell access as you")
                }
            }
            if let error {
                Text(error).font(.caption).foregroundStyle(.red)
            }
            HStack {
                Spacer()
                Button("Cancel") { isPresented = false }
                    .keyboardShortcut(.cancelAction)
                    .help("Close without adding a machine")
                if let preview, preview.canAdd {
                    Button("Add") { add() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(working)
                        .help("Pair with \(preview.name): pin its host key, let its key in, and start syncing")
                } else {
                    Button("Check code") { check() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(working || code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .help("Read the pasted code and show which Mac it names, changing nothing yet")
                }
            }
        }
        .padding(20)
        .frame(width: 440)
    }

    private func check() {
        working = true
        Task {
            switch await store.previewPeer(code: code) {
            case .success(let parsed):
                preview = parsed
                // A re-pair starts from the replaced Mac's switch, not the default.
                allowOpen = parsed.replaces?.allowOpen ?? true
                error = parsed.blockingMessage
            case .failure(let failure):
                preview = nil
                error = MachinesSettingsMessages.clean(failure.localizedDescription)
            }
            working = false
        }
    }

    private func add() {
        working = true
        Task {
            if let failure = await store.addPeer(code: code, allowOpen: allowOpen) {
                error = MachinesSettingsMessages.clean(failure)
            } else {
                isPresented = false
            }
            working = false
        }
    }
}

enum MachinesSettingsMessages {
    /// "Exited 1: error: the pairing code is damaged" -> "The pairing code is damaged".
    static func clean(_ message: String) -> String {
        var text = message.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.hasPrefix("Exited "), let colon = text.firstIndex(of: ":") {
            text = String(text[text.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
        }
        if text.hasPrefix("error: ") { text = String(text.dropFirst("error: ".count)) }
        return text.prefix(1).uppercased() + text.dropFirst()
    }
}
