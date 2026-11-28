import Foundation
import Testing
@testable import AgentBoard

@Suite("Peer sync model")
struct PeerSyncModelTests {
    @Test func decodesRemoteFieldsAndPeers() {
        let snapshot = Fixtures.snapshot(
            [Fixtures.remote("r1", "running", remoteOpen: "tmux"), Fixtures.session("l1", "review")],
            top: Fixtures.peers().merging(["machine": ["id": "me", "name": "Laptop"]]) { _, b in b })
        let remote = snapshot.sessions[0]
        #expect(remote.isRemote)
        #expect(remote.machine == Machine(id: Fixtures.peerID, name: "Mini"))
        #expect(remote.remoteOpen == "tmux")
        #expect(!remote.stale)
        #expect(snapshot.machine?.name == "Laptop")
        #expect(snapshot.peers.map(\.name) == ["Mini"])
        #expect(snapshot.peer(for: remote)?.host == "mini.local")
        #expect(!snapshot.sessions[1].isRemote)
    }

    @Test func decodesPeerOwnNameAndAlias() {
        var peers = Fixtures.peers()
        var peer = (peers["peers"] as! [[String: Any]])[0]
        peer["name"] = "Studio"
        peer["own_name"] = "Mac mini"
        peer["alias"] = "Studio"
        peers["peers"] = [peer, ["id": "x", "name": "Old", "own_name": 5]]
        // The CLI puts the alias in the session's machine name too.
        let snapshot = Fixtures.snapshot(
            [Fixtures.remote("r1", "running", extra: ["machine": ["id": Fixtures.peerID, "name": "Studio"]])],
            top: peers)
        #expect(snapshot.peers.map(\.name) == ["Studio", "Old"])
        #expect(snapshot.peers.map(\.ownName) == ["Mac mini", nil])
        #expect(snapshot.peers.map(\.alias) == ["Studio", nil])
        let remote = snapshot.sessions[0]
        let group = PopoverSections.make(from: snapshot.sessions, query: SessionSearch(""), groupByMachine: true)
            .sections[0].groups[0]
        #expect(PopoverSections.machineHeader(for: group, localName: nil) == "Studio · 1")
        #expect(SessionRowLayout.repoTags(for: remote).first == .machine)
        #expect(remote.machine?.name == "Studio")
        #expect(SessionSearch("machine:studio").matches(remote))
    }

    @Test func snapshotWithoutTheNewFieldsDecodes() {
        let snapshot = Fixtures.snapshot([Fixtures.session("l1", "running")])
        #expect(snapshot.machine == nil)
        #expect(snapshot.peers.isEmpty)
        #expect(snapshot.sessions[0].machine == nil)
        #expect(!snapshot.sessions[0].stale)
        #expect(snapshot.sessions[0].remoteOpen == nil)
    }

    @Test func malformedPeerFieldsDegrade() {
        let snapshot = Fixtures.snapshot(
            [Fixtures.session("l1", "running", extra: ["machine": "nope", "stale": "yes"])],
            top: ["peers": [["id": "x"], ["name": "no id"]], "machine": 5])
        #expect(snapshot.sessions[0].machine == nil)
        #expect(!snapshot.sessions[0].stale)
        #expect(snapshot.peers.map(\.id) == ["x"])
        #expect(snapshot.machine == nil)
    }

    @Test func remoteActionsFollowRemoteOpen() {
        let tmux = Fixtures.decoded("x", extra: Fixtures.remote("t", "running", remoteOpen: "tmux"))
        #expect(tmux.isFocusable && !tmux.resumeAvailable)
        let ended = Fixtures.decoded("x", extra: Fixtures.remote("e", "review", remoteOpen: "resume"))
        #expect(!ended.isFocusable && ended.resumeAvailable)
        let closed = Fixtures.decoded("x", extra: Fixtures.remote("c", "running", remoteOpen: nil))
        #expect(!closed.isFocusable && !closed.resumeAvailable)
    }

    @Test func withoutRemoteDropsRowsAndRecounts() {
        let snapshot = Fixtures.snapshot(
            [Fixtures.remote("r1", "needs_you"), Fixtures.session("l1", "running")],
            top: Fixtures.peers())
        let local = snapshot.withoutRemote()
        #expect(local.sessions.map(\.key) == ["l1"])
        #expect(local.counts.needsYou == 0 && local.counts.running == 1)
        #expect(local.peers.count == 1)
    }

    @Test func markArgumentsCarryTheMachineAndNeverANote() {
        #expect(AgentBoardCLI.markArguments(tool: "claude", sessionId: "s", action: "done", hours: nil,
                                            note: "n", machine: "m")
                == ["mark", "claude", "s", "done", "--machine", "m"])
        #expect(AgentBoardCLI.markArguments(tool: "claude", sessionId: "s", action: "snooze", hours: 24,
                                            note: "n", machine: nil)
                == ["mark", "claude", "s", "snooze", "--hours", "24", "--note", "n"])
    }

    @Test func focusArgumentsCarryViaOnlyWithAMachine() {
        #expect(AgentBoardCLI.focusArguments(tool: "claude", sessionId: "s", machine: nil)
                == ["focus", "claude", "s"])
        #expect(AgentBoardCLI.focusArguments(tool: "claude", sessionId: "s", machine: "m")
                == ["focus", "claude", "s", "--machine", "m"])
        #expect(AgentBoardCLI.focusArguments(tool: "claude", sessionId: "s", machine: "m", via: "screen")
                == ["focus", "claude", "s", "--machine", "m", "--via", "screen"])
        #expect(AgentBoardCLI.focusArguments(tool: "claude", sessionId: "s", machine: nil, via: "browser")
                == ["focus", "claude", "s"])
    }

    @Test func remoteControlTitleAndHelpFollowTheDesktopApp() {
        let rc = Fixtures.decoded("x", extra: Fixtures.remote("r", "running", remoteOpen: "remote-control"))
        #expect(SessionActionHelp.focusTitle(rc, desktopAppInstalled: true) == "Open in Claude App")
        #expect(SessionActionHelp.focusTitle(rc, desktopAppInstalled: false) == "Open in Remote Control")
        let withApp = SessionActionHelp.focusHelp(rc, desktopAppInstalled: true)
        #expect(withApp.hasPrefix("Brings this session forward on Mini when a Screen Sharing connection with it is open"))
        #expect(withApp.hasSuffix("in the Claude desktop app"))
        #expect(SessionActionHelp.focusHelp(rc, desktopAppInstalled: false).hasSuffix("in the browser"))
        #expect(SessionActionHelp.screenShareTitle(rc) == "Open Screen Sharing to Mini")
        #expect(SessionActionHelp.browserTitle(rc) == "Open Remote Control in Browser")
    }

    @Test func screenRowHelpMentionsAnOpenConnection() {
        let screen = Fixtures.decoded("x", extra: Fixtures.remote("s", "running", remoteOpen: "screen"))
        #expect(SessionActionHelp.focusTitle(screen, desktopAppInstalled: true) == "Screen Share to Mini")
        #expect(SessionActionHelp.focusHelp(screen, desktopAppInstalled: true)
                == "Bring this session forward on Mini; opens Screen Sharing to it unless a connection with Mini is already open")
        #expect(SessionActionHelp.screenShareTitle(screen) == "Open Screen Sharing to Mini")
        #expect(SessionActionHelp.browserTitle(screen) == nil)
        let tmux = Fixtures.decoded("x", extra: Fixtures.remote("t", "running", remoteOpen: "tmux"))
        #expect(SessionActionHelp.screenShareTitle(tmux) == nil)
        let stale = Fixtures.decoded("x", extra: Fixtures.remote("o", "running", stale: true, remoteOpen: "screen"))
        #expect(SessionActionHelp.screenShareTitle(stale) == nil)
        #expect(SessionActionHelp.screenShareTitle(Fixtures.decoded("l")) == nil)
    }

    @Test func staleRemoteSessionsDoNotColourTheIcon() {
        let stale = Fixtures.snapshot([Fixtures.remote("r1", "needs_you", stale: true)],
                                      counts: [:])
        #expect(MenuBarIconState.tier(for: stale.counts, sessions: stale.sessions) == .idle)
        let live = Fixtures.snapshot([Fixtures.remote("r1", "needs_you")])
        #expect(MenuBarIconState.tier(for: live.counts, sessions: live.sessions) == .needsYou)
    }

    @Test func supervisorPolicy() {
        #expect(PeerSyncSupervisor.shouldRun(peerCount: 1, fixtureMode: false, showRemote: true))
        #expect(!PeerSyncSupervisor.shouldRun(peerCount: 0, fixtureMode: false, showRemote: true))
        #expect(!PeerSyncSupervisor.shouldRun(peerCount: 1, fixtureMode: true, showRemote: true))
        #expect(!PeerSyncSupervisor.shouldRun(peerCount: 1, fixtureMode: false, showRemote: false))
        #expect((1...7).map { PeerSyncSupervisor.restartDelay(failures: $0) } == [2, 4, 8, 16, 32, 60, 60])
    }

    @Test func notificationsNameTheMachine() {
        let session = Fixtures.decoded("x", "needs_you", extra: Fixtures.remote("r", "needs_you"))
        #expect(SessionNotificationPlan.needsYou(session).title == "AgentBoard: needs you on Mini")
    }
}

@Suite("Peer sync attention")
@MainActor
struct PeerSyncAttentionTests {
    static let frozen: AttentionController.Sleeper = { _ in try await Task.sleep(for: .seconds(86_400)) }

    private func makeController() -> AttentionController {
        AttentionController(sleeper: Self.frozen, reduceMotion: false,
                            notificationCenter: NotificationCenter(), workspaceNotificationCenter: nil)
    }

    @Test func firstExportOfANewPeerFiresNothing() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.snapshot([]))
        let entered = controller.note(snapshot: Fixtures.snapshot(
            [Fixtures.remote("r1", "needs_you")], top: Fixtures.peers()))
        #expect(entered.isEmpty)
        #expect(!controller.isFlashing)
    }

    @Test func remoteRunningToNeedsYouFiresOnce() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.snapshot([Fixtures.remote("r1", "running")], top: Fixtures.peers()))
        let entered = controller.note(snapshot: Fixtures.snapshot(
            [Fixtures.remote("r1", "needs_you")], top: Fixtures.peers()))
        #expect(entered.count == 1)
        // Separate marker (the default): the badge pulses, the glyph stays.
        #expect(controller.isPulsingRemote)
        #expect(!controller.isFlashing)
        let again = controller.note(snapshot: Fixtures.snapshot(
            [Fixtures.remote("r1", "needs_you")], top: Fixtures.peers()))
        #expect(again.isEmpty)
    }

    @Test func newRemoteSessionAfterThePeersLastReportFires() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.snapshot([], top: Fixtures.peers(lastSeen: "2026-10-15T00:00:00Z")))
        let entered = controller.note(snapshot: Fixtures.snapshot(
            [Fixtures.remote("r2", "needs_you", updatedAt: "2026-10-15T00:00:05Z")],
            top: Fixtures.peers(lastSeen: "2026-10-15T00:00:06Z")))
        #expect(entered.count == 1)
    }

    @Test func peerClockSkewIsTakenOffUpdatedAt() {
        // The peer's clock runs 10 s behind: its new session's updated_at
        // reads before this Mac's previous last_seen_at but is after it.
        let behind = makeController()
        behind.note(snapshot: Fixtures.snapshot([], top: Fixtures.peers(lastSeen: "2026-10-15T00:00:00Z", clockSkew: -10)))
        let entered = behind.note(snapshot: Fixtures.snapshot(
            [Fixtures.remote("r2", "needs_you", updatedAt: "2026-10-14T23:59:55Z")],
            top: Fixtures.peers(lastSeen: "2026-10-15T00:00:06Z", clockSkew: -10)))
        #expect(entered.count == 1)
        // Running 10 s ahead: a session already waiting reads as after it.
        let ahead = makeController()
        ahead.note(snapshot: Fixtures.snapshot([], top: Fixtures.peers(lastSeen: "2026-10-15T00:00:00Z", clockSkew: 10)))
        let quiet = ahead.note(snapshot: Fixtures.snapshot(
            [Fixtures.remote("r3", "needs_you", updatedAt: "2026-10-15T00:00:05Z")],
            top: Fixtures.peers(lastSeen: "2026-10-15T00:00:06Z", clockSkew: 10)))
        #expect(quiet.isEmpty)
    }

    @Test func staleNeverFlashesAndStaleToLiveFiresNothing() {
        let controller = makeController()
        controller.note(snapshot: Fixtures.snapshot([Fixtures.remote("r1", "running")], top: Fixtures.peers()))
        let staleEntered = controller.note(snapshot: Fixtures.snapshot(
            [Fixtures.remote("r1", "needs_you", stale: true)], top: Fixtures.peers(status: "offline")))
        #expect(staleEntered.isEmpty)
        #expect(!controller.isFlashing)
        let live = controller.note(snapshot: Fixtures.snapshot(
            [Fixtures.remote("r1", "needs_you")], top: Fixtures.peers()))
        #expect(live.isEmpty)
        #expect(!controller.isFlashing)
    }

    @Test func remoteAlertsOffKeepsRemoteQuiet() {
        let controller = makeController()
        controller.remoteAlerts = false
        controller.note(snapshot: Fixtures.snapshot([Fixtures.remote("r1", "running")], top: Fixtures.peers()))
        let entered = controller.note(snapshot: Fixtures.snapshot(
            [Fixtures.remote("r1", "needs_you")], top: Fixtures.peers()))
        #expect(entered.isEmpty)
        #expect(!controller.isFlashing)
    }

    @Test func staleRemoteTurnsNeverReport() {
        var detector = TurnFinishDetector(startedAt: Date(timeIntervalSince1970: 0))
        let turn: [String: Any] = ["turn_finished_at": "2026-10-15T00:01:00Z", "interactive": true]
        _ = detector.note(Fixtures.snapshot([Fixtures.remote("r1", "running")]))
        let stale = detector.note(Fixtures.snapshot([Fixtures.remote("r1", "review", stale: true, extra: turn)]))
        #expect(stale.finished.isEmpty)
        let live = detector.note(Fixtures.snapshot([Fixtures.remote("r1", "review", extra: turn)]))
        #expect(live.finished.isEmpty)
        let firstSeen = detector.note(Fixtures.snapshot([Fixtures.remote("r9", "review", extra: turn)]))
        #expect(firstSeen.finished.isEmpty)
    }
}

@Suite("Peer sync rows")
struct PeerSyncRowTests {
    private func note(_ id: Int) -> [String: Any] {
        ["id": id, "kind": "update", "text": "note \(id)", "ts": "2026-10-15T00:00:00Z",
         "last_ts": "2026-10-15T00:00:0\(id)Z", "count": 1, "source": "stop"]
    }

    @Test func updateRowIdsAreUniqueAcrossMachinesWithEqualNoteIds() {
        let sessions = Fixtures.snapshot([
            Fixtures.session("claude:l1", "review", extra: ["notes": [note(7)], "notes_count": 1]),
            Fixtures.remote("l1", "review", extra: ["notes": [note(7)], "notes_count": 1]),
        ]).sessions
        let rows = UpdatesFeed.rows(from: sessions, query: SessionSearch(""))
        #expect(rows.count == 2)
        #expect(Set(rows.map(\.id)).count == 2)
        #expect(Set(UpdatesFeed.make(from: sessions, query: SessionSearch("")).map(\.id)).count == 2)
        // The remote row dismisses nothing here; the local one its own id.
        let remote = rows.first { $0.session.isRemote }!
        #expect(remote.dismissal == .ids([]))
        #expect(rows.first { !$0.session.isRemote }!.dismissal == .ids([7]))
    }

    @Test func clearPlansNeverTouchRemoteSessions() {
        let sessions = Fixtures.snapshot([
            Fixtures.remote("r1", "review", extra: ["notes": [note(3)], "notes_count": 1,
                                                    "denials": [["id": 4, "ts": "2026-10-15T00:00:00Z",
                                                                 "tool_name": "Bash", "reason": "x"]],
                                                    "denials_count": 1]),
        ]).sessions
        #expect(UpdatesFeed.clearPlan(from: sessions, query: SessionSearch("note")).dismissals.isEmpty)
        #expect(DenialFeed.clearPlan(from: sessions, query: SessionSearch("Bash")).dismissals.isEmpty)
    }

    @Test func remoteRowShowsTheMachineAndActionTitles() {
        let tmux = Fixtures.decoded("x", extra: Fixtures.remote("t", "running", remoteOpen: "tmux"))
        #expect(SessionRowLayout.iconColumn(for: tmux).contains(.machine))
        #expect(SessionRowLayout.repoTags(for: tmux).first == .machine)
        // Grouped by machine, the sub-header names the Mac: no name capsule,
        // the chip and the other tags stay, in order.
        let named = SessionRowLayout.repoTags(for: tmux)
        let unnamed = SessionRowLayout.repoTags(for: tmux, showsMachineName: false)
        #expect(!unnamed.contains(.machine))
        #expect(unnamed == named.filter { $0 != .machine })
        #expect(SessionRowLayout.iconColumn(for: tmux).contains(.machine))
        #expect(SessionActionHelp.focusTitle(tmux) == "Attach on Mini")
        let screen = Fixtures.decoded("x", extra: Fixtures.remote("s", "running", remoteOpen: "screen"))
        #expect(SessionActionHelp.focusTitle(screen) == "Screen Share to Mini")
        let ended = Fixtures.decoded("x", extra: Fixtures.remote("e", "review", remoteOpen: "resume"))
        #expect(SessionActionHelp.resumeTitle(ended) == "Resume on Mini")
        let local = Fixtures.decoded("l")
        #expect(!SessionRowLayout.iconColumn(for: local).contains(.machine))
        #expect(SessionActionHelp.focusTitle(local) == "Focus")
    }

    @Test func remoteStateMarkerIsADiamondInTheSameColor() {
        let remote = Fixtures.decoded("x", extra: Fixtures.remote("r", "needs_you"))
        let local = Fixtures.decoded("l")
        #expect(SessionColor.markerShape(for: remote) == .diamond)
        #expect(SessionColor.markerShape(for: local) == .dot)
        #expect(SessionColor.color(for: remote) == SessionColor.needsYou)
        #expect(SessionColor.markerTooltip(for: remote).hasPrefix("On Mini: Needs you."))
        #expect(SessionColor.markerTooltip(for: remote).contains(SessionColor.markerKeyText))
        #expect(SessionColor.markerTooltip(for: local) == SessionColor.legendTooltip)
        #expect(SessionColor.markerLabel(for: remote) == "Needs you, on Mini")
        #expect(SessionColor.markerLabel(for: local) == SessionColor.label(for: local))
    }

    @Test func machineTooltipSaysSinceWhenWhenStale() {
        let stale = Fixtures.decoded("x", extra: Fixtures.remote("s", "running", stale: true))
        let now = ISO8601DateParsing.date(from: "2026-10-15T02:00:00Z")!
        let help = SessionActionHelp.machineHelp(stale, lastSeen: "2026-10-15T00:00:00Z", now: now)
        #expect(help.hasPrefix("Runs on Mini, offline; as of "))
        #expect(help.contains("2"))
        let live = Fixtures.decoded("x", extra: Fixtures.remote("l", "running"))
        #expect(SessionActionHelp.machineHelp(live, lastSeen: nil) == "Runs on Mini")
    }

    @Test func searchMatchesTheMachineName() {
        let remote = Fixtures.decoded("x", extra: Fixtures.remote("r", "running"))
        #expect(SessionSearch("mini").matches(remote))
        #expect(SessionSearch("machine:mini").matches(remote))
        #expect(!SessionSearch("machine:mini").matches(Fixtures.decoded("local")))
    }
}

@Suite("Machines settings text")
struct MachinesDisplayTests {
    @Test func statusText() {
        let now = ISO8601DateParsing.date(from: "2026-10-15T02:00:00Z")!
        #expect(MachinesDisplay.statusText(Peer(id: "a", name: "Mini"), now: now) == "Online")
        let offline = Peer(id: "a", name: "Mini", status: "offline", lastSeenAt: "2026-10-15T00:00:00Z",
                           error: "Remote Login is off there")
        let text = MachinesDisplay.statusText(offline, now: now)
        #expect(text.hasPrefix("Offline, last seen "))
        #expect(text.hasSuffix(": Remote Login is off there"))
        #expect(MachinesDisplay.statusText(Peer(id: "a", name: "Mini", status: "never"), now: now)
                == "Not connected yet")
        let rejecting = Peer(id: "a", name: "Mini", error: "its board is too large to sync")
        #expect(MachinesDisplay.statusText(rejecting, now: now) == "Online: its board is too large to sync")
    }

    @Test func cliErrorsReadAsSentences() {
        #expect(MachinesSettingsMessages.clean("Exited 1: error: the pairing code is damaged (copy all of it)\n")
                == "The pairing code is damaged (copy all of it)")
    }
}

@Suite("Remote text links")
struct RemoteTextLinkTests {
    private func links(_ value: AttributedString?) -> [String] {
        (value?.runs.compactMap { $0.link?.absoluteString }) ?? []
    }

    @Test func remoteTextKeepsOnlyWebLinks() {
        let text = "[a](file:///Applications/Calculator.app) [b](vnc://host) [c](https://example.com) [d](HTTP://x.org)"
        let remote = InlineMarkdown.attributed(text, remote: true)
        #expect(links(remote) == ["https://example.com", "HTTP://x.org"])
        #expect(remote.map { String($0.characters) } == "a b c d")
    }

    @Test func localTextIsUnchanged() {
        let text = "[a](file:///tmp/x) and [c](https://example.com)"
        #expect(links(InlineMarkdown.attributed(text, remote: false)) == ["file:///tmp/x", "https://example.com"])
    }
}

@Suite("Add machine preview")
struct PeerPreviewTests {
    private func decode(_ json: String) throws -> PeerPreview {
        try JSONDecoder().decode(PeerPreview.self, from: Data(json.utf8))
    }

    private let base = #""id":"a","name":"Mini","user":"u","host":"mini.local","fingerprint":"f""#

    @Test func aCopiedIdentityCannotBeAddedAndSaysWhy() throws {
        let preview = try decode("{\(base),\"own\":false,\"known\":false,\"copied_identity\":true,"
                                 + "\"error\":\"this code comes from another Mac that has this Mac's identity\"}")
        #expect(!preview.canAdd)
        #expect(preview.blockingMessage == "This code comes from another Mac that has this Mac's identity")
    }

    @Test func anOwnCodeCannotBeAdded() throws {
        let preview = try decode("{\(base),\"own\":true,\"known\":false}")
        #expect(!preview.canAdd)
        #expect(preview.blockingMessage?.contains("own code") == true)
    }

    @Test func aReplacementNamesTheMacAndItsSwitch() throws {
        let preview = try decode("{\(base),\"own\":false,\"known\":true,"
                                 + "\"replaces\":{\"id\":\"b\",\"name\":\"Old Mini\",\"host\":\"vpn\",\"allow_open\":false}}")
        #expect(preview.canAdd)
        #expect(preview.replaces == PeerPreview.Replaced(id: "b", name: "Old Mini", host: "vpn", allowOpen: false))
        let note = try #require(preview.replacementNote)
        #expect(note.contains("Old Mini (paired under another id)"))
        #expect(note.contains("(off)"))
    }

    @Test func anOlderPreviewWithoutTheNewFieldsDecodes() throws {
        let preview = try decode("{\(base),\"own\":false,\"known\":false}")
        #expect(preview.canAdd)
        #expect(preview.replaces == nil)
        #expect(preview.replacementNote == nil)
        #expect(preview.blockingMessage == nil)
    }
}

@Suite("Advertised address")
struct AdvertisedAddressTests {
    private func decodeInfo(_ json: String) throws -> PeersInfo {
        try JSONDecoder().decode(PeersInfo.self, from: Data(json.utf8))
    }

    private let machine = #""machine":{"id":"m","name":"Laptop"}"#

    @Test func peersInfoDecodesTheAddressAndCandidates() throws {
        let info = try decodeInfo("{\(machine),\"host\":\"mac.vpn\","
            + "\"address\":{\"address\":\"mac.vpn\",\"source\":\"config\",\"kind\":\"custom\"},"
            + "\"address_automatic\":{\"address\":\"Laptop.local\",\"kind\":\"local\",\"source\":\"auto\"},"
            + "\"address_candidates\":[{\"address\":\"Laptop.local\",\"kind\":\"local\",\"label\":\"Local name\",\"via\":\"local\"},"
            + "{\"kind\":\"vpn\"},{\"address\":\"10.8.0.2\"}]}")
        #expect(info.address == PeerAddress(address: "mac.vpn", source: "config", kind: "custom"))
        #expect(info.address?.isChosen == true)
        #expect(info.addressAutomatic?.address == "Laptop.local")
        // A malformed entry is dropped; a missing label falls back to the address.
        #expect(info.addressCandidates.map(\.address) == ["Laptop.local", "10.8.0.2"])
        #expect(info.addressCandidates.last?.label == "10.8.0.2")
    }

    @Test func peersInfoWithoutTheAddressFieldsDecodes() throws {
        let info = try decodeInfo("{\(machine),\"host\":\"mac.local\"}")
        #expect(info.address == nil)
        #expect(info.addressAutomatic == nil)
        #expect(info.addressCandidates.isEmpty)
        #expect(info.host == "mac.local")
    }

    @Test func peerDecodesErrorKindAndSuggestion() throws {
        let peer = try JSONDecoder().decode(Peer.self, from: Data(
            #"{"id":"a","name":"Mini","host":"mini.local","error_kind":"address","suggested_address":"mini.vpn"}"#.utf8))
        #expect(peer.errorKind == "address")
        #expect(peer.suggestedAddress == "mini.vpn")
        let old = try JSONDecoder().decode(Peer.self, from: Data(#"{"id":"a","name":"Mini"}"#.utf8))
        #expect(old.errorKind == nil)
        #expect(old.suggestedAddress == nil)
    }

    @Test func previewDecodesCurrentHost() throws {
        let base = #""id":"a","name":"Mini","user":"u","host":"mini.vpn","fingerprint":"f","own":false,"known":true"#
        let preview = try JSONDecoder().decode(PeerPreview.self, from: Data("{\(base),\"current_host\":\"mini.local\"}".utf8))
        #expect(preview.currentHost == "mini.local")
        #expect(MachinesDisplay.currentHostNote(preview) == "Currently reached at mini.local; the code says mini.vpn.")
        let older = try JSONDecoder().decode(PeerPreview.self, from: Data("{\(base)}".utf8))
        #expect(older.currentHost == nil)
        #expect(MachinesDisplay.currentHostNote(older) == nil)
    }

    @Test func addressAdvice() {
        let addressError = Peer(id: "a", name: "Mini", host: "mini.vpn", status: "offline", errorKind: "address")
        #expect(MachinesDisplay.addressAdvice(addressError)?.hasPrefix("Check the address") == true)
        let unreachableLocal = Peer(id: "a", name: "Mini", host: "mini.local", status: "offline", errorKind: "unreachable")
        let advice = MachinesDisplay.addressAdvice(unreachableLocal)
        #expect(advice?.hasPrefix("A .local name only works on the same network. On Mini,") == true)
        let offlineLocal = Peer(id: "a", name: "Mini", host: "Mini.LOCAL", status: "offline")
        #expect(MachinesDisplay.addressAdvice(offlineLocal) != nil)
        #expect(MachinesDisplay.addressAdvice(Peer(id: "a", name: "Mini", host: "mini.local", status: "never")) != nil)
        #expect(MachinesDisplay.addressAdvice(Peer(id: "a", name: "Mini", host: "mini.local")) == nil)
        #expect(MachinesDisplay.addressAdvice(Peer(id: "a", name: "Mini", host: "192.0.2.5", status: "offline")) == nil)
        let authError = Peer(id: "a", name: "Mini", host: "192.0.2.5", status: "offline", errorKind: "auth")
        #expect(MachinesDisplay.addressAdvice(authError) == nil)
    }

    @Test func captionAndMenuTitles() {
        let auto = PeerAddress(address: "Laptop.local", source: "auto", kind: "local")
        let chosen = PeerAddress(address: "mac.vpn", source: "config", kind: "custom")
        #expect(MachinesDisplay.addressCaption(auto).hasPrefix("Chosen automatically."))
        #expect(MachinesDisplay.addressCaption(chosen).hasPrefix("Set by you."))
        #expect(MachinesDisplay.addressCaption(nil).hasPrefix("Chosen automatically."))
        #expect(MachinesDisplay.addressCaption(chosen).contains("keep the address they saved"))
        #expect(MachinesDisplay.automaticTitle(auto) == "Automatic (Laptop.local)")
        #expect(MachinesDisplay.automaticTitle(nil) == "Automatic")
        #expect(MachinesDisplay.candidateTitle(AddressCandidate(address: "100.64.0.9", kind: "vpn", label: "VPN address"))
                == "VPN address: 100.64.0.9")
    }

    @Test func suggestionText() {
        let peer = Peer(id: "a", name: "Mini", host: "mini.local", suggestedAddress: "mini.vpn")
        #expect(MachinesDisplay.suggestionText(peer)?.hasPrefix("Mini now advertises mini.vpn (this Mac uses mini.local).") == true)
        #expect(MachinesDisplay.suggestionText(Peer(id: "a", name: "Mini", host: "mini.vpn", suggestedAddress: "mini.vpn")) == nil)
        #expect(MachinesDisplay.suggestionText(Peer(id: "a", name: "Mini", host: "mini.local")) == nil)
        #expect(MachinesDisplay.suggestionText(peer)?.contains("enter mini.local under Address again") == true)
        #expect(MachinesDisplay.suggestionText(Peer(id: "a", name: "Mini", host: "mini.local", suggestedAddress: "Mini.local.")) == nil)
    }

    @Test func cliArgumentLists() {
        #expect(AgentBoardCLI.peersSetSelfArguments(address: "mac.vpn") == ["set-self", "--address=mac.vpn"])
        #expect(AgentBoardCLI.peersSetSelfArguments(address: "-bad") == ["set-self", "--address=-bad"])
        #expect(AgentBoardCLI.peersSetSelfArguments(address: "x", autoAddress: true) == ["set-self", "--auto-address"])
        #expect(AgentBoardCLI.peersSetSelfArguments(name: "Laptop", share: "full")
                == ["set-self", "--name", "Laptop", "--share", "full"])
        #expect(AgentBoardCLI.peersAddArguments(code: "C", host: "mini.vpn", allowOpen: false)
                == ["add", "--code", "C", "--host=mini.vpn", "--allow-open", "off"])
    }

    @Test func localNameHint() {
        #expect(MachinesDisplay.isLocalName("mini.local"))
        #expect(MachinesDisplay.isLocalName(" Mini.Local "))
        #expect(!MachinesDisplay.isLocalName("mini.vpn"))
        #expect(!MachinesDisplay.isLocalName("local"))
    }
}

@Suite("AgentBoard CLI peers arguments")
struct AgentBoardCLITests {
    @Test func peersSetArguments() {
        #expect(AgentBoardCLI.peersSetArguments(id: "i", alias: "Studio") == ["set", "--id", "i", "--alias=Studio"])
        #expect(AgentBoardCLI.peersSetArguments(id: "i", alias: "-x") == ["set", "--id", "i", "--alias=-x"])
        #expect(AgentBoardCLI.peersSetArguments(id: "i", clearAlias: true) == ["set", "--id", "i", "--clear-alias"])
        #expect(AgentBoardCLI.peersSetArguments(id: "i", alias: "x", clearAlias: true)
                == ["set", "--id", "i", "--clear-alias"])
        #expect(AgentBoardCLI.peersSetArguments(id: "i", host: "mini.vpn", name: "Mini", allowOpen: false,
                                                dismissAddress: "old.vpn")
                == ["set", "--id", "i", "--host=mini.vpn", "--name", "Mini", "--allow-open", "off",
                    "--dismiss-address=old.vpn"])
    }
}
