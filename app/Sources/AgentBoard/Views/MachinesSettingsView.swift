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

    /// A Bonjour name, which resolves only on the same network.
    static func isLocalName(_ host: String) -> Bool {
        host.trimmingCharacters(in: .whitespaces).lowercased().hasSuffix(".local")
    }

    static let sameNetworkHint = "A .local name only works while both Macs are on the same network."

    /// The caption under this Mac's address: how it was chosen, and that
    /// Macs already paired keep the address they saved.
    static func addressCaption(_ address: PeerAddress?) -> String {
        let mode = address?.isChosen == true ? "Set by you." : "Chosen automatically."
        return "\(mode) Macs already paired keep the address they saved: change it there under this Mac's Address, or paste the new pairing code again."
    }

    /// The Choose… menu's first item.
    static func automaticTitle(_ automatic: PeerAddress?) -> String {
        guard let automatic, !automatic.address.isEmpty else { return "Automatic" }
        return "Automatic (\(automatic.address))"
    }

    /// One detected address in the Choose… menu: "<label>: <address>".
    static func candidateTitle(_ candidate: AddressCandidate) -> String {
        "\(candidate.label): \(candidate.address)"
    }

    /// What to do about a paired Mac that can't be reached at its address,
    /// else nil (it is online, or the failure is not about the address).
    static func addressAdvice(_ peer: Peer) -> String? {
        guard peer.status != "online" else { return nil }
        let local = isLocalName(peer.host)
        let aboutAddress = peer.errorKind == "address" || peer.errorKind == "unreachable"
        let offlineLocal = local && (peer.status == "never" || peer.status == "offline")
        guard aboutAddress || offlineLocal else { return nil }
        if local {
            return "A .local name only works on the same network. On \(peer.name), open Settings → Machines, pick an address that works from anywhere, then enter it here."
        }
        return "Check the address: enter the one \(peer.name) shows under Settings → Machines."
    }

    /// The row offering an address the peer now advertises, else nil.
    /// Names the address being replaced, so it can be typed back if the new
    /// one doesn't work from here.
    static func suggestionText(_ peer: Peer) -> String? {
        guard let address = peer.suggestedAddress, !address.isEmpty, !sameHost(address, peer.host) else { return nil }
        return "\(peer.name) now advertises \(address) (this Mac uses \(peer.host)). Switching works only while this Mac can still reach it; once the old address fails, use Edit address or paste its new pairing code. If the new address doesn't work from here, enter \(peer.host) under Address again."
    }

    /// Mirrors `peers.same_host`: DNS names ignore case and a trailing dot.
    static func sameHost(_ a: String, _ b: String) -> Bool {
        func norm(_ s: String) -> String {
            var s = s.lowercased()
            while s.hasSuffix(".") { s.removeLast() }
            return s
        }
        return norm(a) == norm(b)
    }

    /// The sheet's note when the code's address differs from the one this
    /// Mac already reaches that Mac at, else nil.
    static func currentHostNote(_ preview: PeerPreview) -> String? {
        guard let current = preview.currentHost, !current.isEmpty, !sameHost(current, preview.host) else { return nil }
        return "Currently reached at \(current); the code says \(preview.host)."
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
    @AppStorage(SettingsKeys.groupPopoverByMachine) private var groupByMachine: Bool = true
    @AppStorage(MenuBarIconSettings.remoteModeKey) private var remoteIconMode: String = RemoteIconMode.separate.rawValue
    @State private var name = ""
    /// The address field's unsaved edit; nil shows the saved address.
    @State private var addressDraft: String?
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
                        .settingsAnchor(SettingsCatalog.pairedMachines, scrolls: false)
                }
            } else if let error = store.peersError {
                Text(error).font(.caption).foregroundStyle(.red)
            } else {
                Text("Loading…").font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button(SettingsCatalog.addMachine.label) { showingAdd = true }
                    .help(SettingsCatalog.addMachine.help)
                    .settingsAnchor(SettingsCatalog.addMachine, standingIn: store.peersInfo?.peers.isEmpty ?? true)
                Spacer()
            }
            Toggle(SettingsCatalog.showRemoteSessions.label, isOn: $showRemote)
                .onChange(of: showRemote) { _, _ in store.applyRemoteFilter() }
                .help(SettingsCatalog.showRemoteSessions.help)
                .settingsAnchor(SettingsCatalog.showRemoteSessions)
            Toggle(SettingsCatalog.groupPopoverByMachine.label, isOn: $groupByMachine)
                .help(SettingsCatalog.groupPopoverByMachine.help)
                .settingsAnchor(SettingsCatalog.groupPopoverByMachine)
            Toggle(SettingsCatalog.notifyRemoteSessions.label, isOn: $notifyRemote)
                .help(SettingsCatalog.notifyRemoteSessions.help)
                .settingsAnchor(SettingsCatalog.notifyRemoteSessions)
            Picker(SettingsCatalog.remoteIconMode.label, selection: $remoteIconMode) {
                ForEach(RemoteIconMode.allCases) { mode in
                    Text(mode.label).tag(mode.rawValue)
                }
            }
            .help(SettingsCatalog.remoteIconMode.help)
            .settingsAnchor(SettingsCatalog.remoteIconMode)
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
            Button("Remove", role: .destructive) { // not-searchable: confirms a per-peer Remove
                Task { _ = await store.removePeer(peer.id) }
            }
            .help("Stop syncing with this Mac and remove its key from this Mac's authorized_keys")
            Button("Cancel", role: .cancel) { removing = nil } // not-searchable: dismisses the confirmation
                .help("Keep this machine")
        } message: { peer in
            Text("Its sessions leave this board, and \(peer.name) can no longer reach this Mac. Remove this Mac on \(peer.name) too.")
        }
    }

    @ViewBuilder
    private func thisMac(_ info: PeersInfo) -> some View {
        HStack {
            TextField(SettingsCatalog.thisMacName.label, text: $name)
                .onSubmit { Task { _ = await store.setThisMac(name: name) } }
                .help(SettingsCatalog.thisMacName.help)
                .settingsAnchor(SettingsCatalog.thisMacName)
        }
        let savedAddress = info.address?.address ?? info.host ?? ""
        HStack {
            TextField(SettingsCatalog.thisMacAddress.label, text: Binding(
                get: { addressDraft ?? savedAddress },
                set: { addressDraft = $0 }
            ))
            .onSubmit { saveAddress(savedAddress) }
            .help(SettingsCatalog.thisMacAddress.help)
            .settingsAnchor(SettingsCatalog.thisMacAddress)
            Menu("Choose…") { // not-searchable: part of the address row, catalogued as thisMacAddress
                Button(MachinesDisplay.automaticTitle(info.addressAutomatic)) { // not-searchable: an address menu entry
                    addressDraft = nil
                    Task { _ = await store.setThisMac(autoAddress: true) }
                }
                .help("Let AgentBoard pick the address: a detected VPN name or address, else the .local name")
                ForEach(info.addressCandidates) { candidate in
                    Button(MachinesDisplay.candidateTitle(candidate)) { // not-searchable: an address menu entry
                        addressDraft = nil
                        Task { _ = await store.setThisMac(address: candidate.address) }
                    }
                    .help("Use this address in this Mac's pairing code")
                }
            }
            .fixedSize()
            .help("Pick Automatic or one of the addresses detected on this Mac")
        }
        Text(MachinesDisplay.addressCaption(info.address))
            .font(.caption)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
        Picker(SettingsCatalog.shareLevel.label, selection: Binding(
            get: { info.share },
            set: { level in Task { _ = await store.setThisMac(share: level) } }
        )) {
            ForEach(MachinesDisplay.shareLevels, id: \.value) { level in
                Text(level.label).tag(level.value)
            }
        }
        .help(SettingsCatalog.shareLevel.help)
        .settingsAnchor(SettingsCatalog.shareLevel)
        HStack {
            Text(MachinesDisplay.remoteLoginText(info.remoteLogin))
                .font(.caption)
                .foregroundStyle(info.remoteLogin == "off" ? .red : .secondary)
            Spacer()
            Button(SettingsCatalog.sharingSettings.label) { NSWorkspace.shared.open(MachinesDisplay.sharingSettingsURL) }
                .help(SettingsCatalog.sharingSettings.help)
                .settingsAnchor(SettingsCatalog.sharingSettings)
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
            Button(copied ? "Copied" : SettingsCatalog.copyPairingCode.label) {
                if let code = info.code {
                    ClipboardHelper.copy(code)
                    copied = true
                    Task {
                        try? await Task.sleep(for: .seconds(2))
                        copied = false
                    }
                }
            }
            .disabled(info.code == nil || hasUnsavedAddress(info))
            .help(SettingsCatalog.copyPairingCode.help)
            .settingsAnchor(SettingsCatalog.copyPairingCode)
        }
    }

    private func hasUnsavedAddress(_ info: PeersInfo) -> Bool {
        guard let draft = addressDraft else { return false }
        return draft != (info.address?.address ?? info.host ?? "")
    }

    private func saveAddress(_ saved: String) {
        guard let draft = addressDraft else { return }
        let value = draft.trimmingCharacters(in: .whitespaces)
        guard value != saved else {
            addressDraft = nil
            return
        }
        Task {
            // On failure the error shows in the red line and the field goes
            // back to the saved address.
            _ = await store.setThisMac(address: value)
            addressDraft = nil
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
    @FocusState private var addressFocused: Bool

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
            if let advice = MachinesDisplay.addressAdvice(peer) {
                HStack(alignment: .firstTextBaseline) {
                    Text(advice)
                        .font(.caption)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer()
                    Button("Edit address") { addressFocused = true }
                        .controlSize(.small)
                        .help("Put the cursor in \(peer.name)'s Address field to change it")
                }
            }
            if let suggestion = MachinesDisplay.suggestionText(peer), let suggested = peer.suggestedAddress {
                VStack(alignment: .leading, spacing: 4) {
                    Text(suggestion)
                        .font(.caption)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack {
                        Button("Use new address") { Task { _ = await store.setPeer(peer.id, host: suggested) } }
                            .controlSize(.small)
                            .help("Reach \(peer.name) at \(suggested) from now on; its pinned host key still has to match")
                        Button("Dismiss") { Task { _ = await store.setPeer(peer.id, dismissAddress: suggested) } }
                            .controlSize(.small)
                            .help("Keep the current address and stop suggesting \(suggested) until \(peer.name) advertises another one")
                    }
                }
            }
            TextField("Address", text: $host)
                .focused($addressFocused)
                .onSubmit { Task { _ = await store.setPeer(peer.id, host: host.trimmingCharacters(in: .whitespaces)) } }
                .help("The address this Mac reaches \(peer.name) at over ssh: the one \(peer.name) shows under Settings → Machines. A .local name works on the same network only; a VPN or DNS name or an IP address works from elsewhere. Press Return to save")
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
    /// The address to reach the Mac at, prefilled from the code.
    @State private var address = ""

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
                    address = ""
                }
            if let preview {
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(preview.name) (login \(preview.user))").font(.body.weight(.medium))
                    if preview.canAdd {
                        TextField("Address", text: $address)
                            .help("The address this Mac reaches \(preview.name) at; it starts as the one in the code. Change it if \(preview.name) is reachable at another name or IP address")
                        if let note = MachinesDisplay.currentHostNote(preview) {
                            Text(note).font(.caption)
                        }
                        if MachinesDisplay.isLocalName(address) {
                            Text(MachinesDisplay.sameNetworkHint)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
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
                address = parsed.host
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
            let typed = address.trimmingCharacters(in: .whitespaces)
            let host = typed.isEmpty ? (preview?.host ?? "") : typed
            if let failure = await store.addPeer(code: code, host: host, allowOpen: allowOpen) {
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
