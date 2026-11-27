import CoreGraphics
import Testing
@testable import AgentBoard

@Suite("PopoverHold")
struct PopoverHoldTests {
    private func session(_ key: String, _ state: String = "running", updated: String = "2026-10-15T00:00:00Z",
                         alive: Bool = false, extra: [String: Any] = [:]) -> [String: Any] {
        Fixtures.session(key, state, alive: alive, extra: ["updated_at": updated].merging(extra) { _, given in given })
    }

    private func sessions(_ items: [[String: Any]]) -> [Session] {
        Fixtures.snapshot(items).sessions
    }

    private func note(_ id: Int, _ kind: String = "update", text: String = "note") -> [String: Any] {
        ["id": id, "kind": kind, "text": text, "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]
    }

    private func layout(_ sessions: [Session], _ query: String = "") -> [String] {
        PopoverSections.make(from: sessions, query: SessionSearch(query)).sections.map {
            "\($0.kind.rawValue):\($0.sessions.map(\.key).joined(separator: ","))"
        }
    }

    /// Running a (newest), b, c.
    private var start: [Session] {
        sessions([
            session("a", updated: "2026-10-15T00:03:00Z"),
            session("b", updated: "2026-10-15T00:02:00Z"),
            session("c", updated: "2026-10-15T00:01:00Z"),
        ])
    }

    /// c is now newest, b is gone and d is new.
    private var changed: [Session] {
        sessions([
            session("a", updated: "2026-10-15T00:03:00Z"),
            session("c", updated: "2026-10-15T00:09:00Z"),
            session("d", updated: "2026-10-15T00:08:00Z"),
        ])
    }

    private func holding(_ live: [Session], query: String = "") -> PopoverHold {
        var hold = PopoverHold()
        hold.update(live: live, query: query, holding: true)
        return hold
    }

    // MARK: - Hold rules

    @Test func notHoldingShowsLive() {
        var hold = PopoverHold()
        hold.update(live: start, query: "", holding: false)
        #expect(!hold.isHolding)
        #expect(hold.displayed(live: changed) == changed)
    }

    @Test func holdingKeepsTheCapturedSessionsAndLayout() {
        var hold = holding(start)
        let before = layout(hold.displayed(live: start))
        hold.update(live: changed, query: "", holding: true)
        #expect(hold.displayed(live: changed).map(\.key) == ["a", "b", "c"])
        #expect(layout(hold.displayed(live: changed)) == before)
        #expect(before == ["running:a,b,c"])
        #expect(layout(changed) == ["running:c,d,a"])
    }

    @Test func aHoldBegunOnASnapshotChangeKeepsTheListShownBeforeIt() {
        // Pointer inside, but `onHover` never fired: no hold until the
        // snapshot goes start -> changed, which `store.snapshot` already holds.
        var hold = PopoverHold()
        hold.update(live: start, query: "", holding: false)
        hold.update(live: changed, query: "", holding: true, previous: start)
        #expect(hold.displayed(live: changed) == start)
        #expect(hold.pending(live: changed, query: "").total > 0)
        // Already holding: `previous` changes nothing.
        hold.update(live: changed, query: "", holding: true, previous: changed)
        #expect(hold.displayed(live: changed) == start)
        // A new query still recaptures from live, not `previous`.
        hold.update(live: changed, query: "d", holding: true, previous: start)
        #expect(hold.displayed(live: changed) == changed)
    }

    @Test func stoppingTheHoldShowsLive() {
        var hold = holding(start)
        hold.update(live: changed, query: "", holding: false)
        #expect(hold.displayed(live: changed) == changed)
    }

    @Test func aQueryChangeRecapturesFromLive() {
        var hold = holding(start)
        // Before update runs, a new query already shows live.
        #expect(hold.displayed(live: changed, query: "c") == changed)
        hold.update(live: changed, query: "c", holding: true)
        #expect(hold.displayed(live: changed, query: "c") == changed)
        #expect(hold.query == "c")
        // The same query keeps the newly held sessions.
        hold.update(live: start, query: "c", holding: true)
        #expect(hold.displayed(live: start, query: "c") == changed)
    }

    @Test(arguments: [
        (false, false, false, false), (false, true, false, false), (false, false, true, false),
        (false, true, true, false), (true, false, false, false), (true, true, false, true),
        (true, false, true, true), (true, true, true, true),
    ])
    func shouldHoldTable(visible: Bool, inside: Bool, menu: Bool, expected: Bool) {
        #expect(PopoverHold.shouldHold(popoverVisible: visible, pointerInside: inside, menuOpen: menu) == expected)
    }

    @Test func pointerInside() {
        let frame = CGRect(x: 100, y: 200, width: 300, height: 400)
        #expect(!PopoverHold.pointerInside(mouse: CGPoint(x: 150, y: 250), windowFrame: nil))
        #expect(PopoverHold.pointerInside(mouse: CGPoint(x: 150, y: 250), windowFrame: frame))
        #expect(!PopoverHold.pointerInside(mouse: CGPoint(x: 99, y: 250), windowFrame: frame))
        #expect(!PopoverHold.pointerInside(mouse: CGPoint(x: 150, y: 601), windowFrame: frame))
        #expect(PopoverHold.pointerInside(mouse: CGPoint(x: 100, y: 200), windowFrame: frame))
        #expect(PopoverHold.pointerInside(mouse: CGPoint(x: 400, y: 600), windowFrame: frame))
    }

    // MARK: - Status and pending

    @Test func movedAndGoneStatus() {
        let hold = holding(start)
        let live = sessions([session("a", "needs_you", alive: true), session("c")])
        let held = hold.displayed(live: live)
        #expect(hold.status(of: held[0], live: live) == .moved(to: .needsYou))
        #expect(hold.status(of: held[1], live: live) == .gone)
        #expect(hold.status(of: held[2], live: live) == .current)
        #expect(PopoverHold.RowStatus.gone.isDimmed)
        #expect(!PopoverHold.RowStatus.current.isDimmed)
        #expect(PopoverHold.RowStatus.moved(to: .needsYou).label == "Now in Needs you")
        #expect(PopoverHold.RowStatus.gone.label == "No longer on the board")
        #expect(PopoverHold.RowStatus.current.help == nil)
    }

    @Test func statusIsCurrentWhileLive() {
        let hold = PopoverHold()
        #expect(hold.status(of: start[0], live: []) == .current)
    }

    @Test func pendingCountsAddedMovedGoneAndNeedsYou() {
        let hold = holding(start)
        let live = sessions([session("a", "needs_you", alive: true), session("c"), session("d")])
        let pending = hold.pending(live: live, query: "")
        #expect(pending.added == 1)
        #expect(pending.moved == 1)
        #expect(pending.gone == 1)
        #expect(pending.needsYou == 1)
        #expect(pending.total == 3)
        #expect(pending.label == "1 needs you · Update list")
    }

    @Test func pendingSkipsWhatTheQueryOrTheListLeavesOut() {
        let alpha = ["title": "alpha"]
        let held = sessions([session("a", extra: alpha), session("b"), session("c")])
        let hold = holding(held, query: "alpha")
        let live = sessions([
            session("a", extra: alpha), session("b"), session("c"),
            session("zz", "needs_you"),                    // not matched by the query
            session("n", "unknown", extra: alpha),         // a state no section lists
            session("d", "done", extra: alpha),            // matched, listed while filtering
        ])
        let pending = hold.pending(live: live, query: "alpha")
        #expect(pending.added == 1)
        #expect(pending.needsYou == 0)
        #expect(pending.label == "1 change · Update list")
    }

    @Test func pendingCountsNewNotes() {
        let hold = holding(sessions([session("a", extra: ["notes": [note(1)]])]))
        let live = sessions([session("a", extra: ["notes": [note(1), note(2, "needs_you")]])])
        let pending = hold.pending(live: live, query: "")
        #expect(pending.notes == 1)
        #expect(pending.needsYou == 1)
        #expect(pending.label == "1 needs you · Update list")
    }

    @Test func pendingCountsASessionOnceInNeedsYou() {
        let hold = holding(sessions([session("a", extra: ["notes": [note(1)]])]))
        // a moved to needs_you and gained a needs_you note; b is new with one.
        let live = sessions([
            session("a", "needs_you", extra: ["notes": [note(1), note(2, "needs_you")]]),
            session("b", "needs_you", extra: ["notes": [note(3, "needs_you")]]),
        ])
        let pending = hold.pending(live: live, query: "")
        #expect(pending.moved == 1)
        #expect(pending.added == 1)
        #expect(pending.notes == 2)
        #expect(pending.needsYou == 2)
        let one = hold.pending(live: [live[0]], query: "")
        #expect(one.needsYou == 1)
        #expect(one.label == "1 needs you · Update list")
    }

    @Test func pendingLabels() {
        #expect(PopoverHold.Pending().label == nil)
        #expect(PopoverHold.Pending(added: 1).label == "1 change · Update list")
        #expect(PopoverHold.Pending(added: 2, moved: 1).label == "3 changes · Update list")
        #expect(PopoverHold.Pending(added: 2, needsYou: 2).label == "2 need you · Update list")
    }

    @Test func releaseZeroesPending() {
        var hold = holding(start)
        #expect(hold.pending(live: changed, query: "").total > 0)
        hold.release(live: changed)
        #expect(hold.isHolding)
        #expect(hold.pending(live: changed, query: "").total == 0)
        #expect(hold.displayed(live: changed) == changed)
    }

    @Test func releaseWhileLiveDoesNotHold() {
        var hold = PopoverHold()
        hold.release(live: changed)
        #expect(!hold.isHolding)
    }

    // MARK: - Click binding

    @Test func aClickResolvesByKeyNotByIndex() {
        let held = sessions([session("a"), session("b", extra: ["title": "held b"]), session("c")])
        let live = sessions([session("c"), session("a"), session("b", extra: ["title": "live b"])])
        let result = SessionRowView.popoverTap(held: held[1], live: live)
        #expect(result.session.key == "b")
        #expect(result.session.title == "live b")
        #expect(result.session != live[1])
        #expect(result.tap == .showDashboard)
        #expect(SessionRowView.allowedActions(held: held[1], live: live) == .all)
    }

    @Test func aGoneSessionOnlyCopiesAndShowsTheDashboard() {
        let held = sessions([session("gone", alive: true, extra: ["can_focus": true])])[0]
        let result = SessionRowView.popoverTap(held: held, live: start)
        #expect(result.session == held)
        #expect(result.tap == .showDashboard)
        #expect(SessionRowView.allowedActions(held: held, live: start) == .copyAndDashboard)
    }

    @Test func aSessionThatExitedWhileHeldOpensTheDashboard() {
        let held = sessions([session("a", alive: true, extra: ["can_focus": true])])[0]
        #expect(SessionRowView.popoverTap(for: held) == .focus)
        let live = sessions([session("a", "review", alive: false, extra: ["can_focus": true])])
        let result = SessionRowView.popoverTap(held: held, live: live)
        #expect(result.session.state == .review)
        #expect(result.tap == .showDashboard)
    }

    @Test func twoMacsSessionsWithOneIdNeverResolveToEachOther() {
        let local = Fixtures.session("claude:s1", "running", extra: ["session_id": "s1"])
        let remote = Fixtures.remote("s1", "running")
        let live = sessions([remote])
        let heldLocal = sessions([local])[0]
        #expect(PopoverHold.resolve(heldLocal, live: live) == nil)
        #expect(SessionRowView.popoverTap(held: heldLocal, live: live).session == heldLocal)
        let both = sessions([local, remote])
        #expect(PopoverHold.resolve(both[1], live: both.reversed())?.isRemote == true)
    }

    // MARK: - Own actions

    @Test func anOwnActionAppliesOnlyToTheActedSession() {
        var hold = holding(start)
        let live = sessions([
            session("a", "done"), session("b", "needs_you"), session("c"), session("d"),
        ])
        hold.applyOwnAction(sessionKey: "a", live: live)
        let shown = hold.displayed(live: live)
        #expect(shown.map(\.key) == ["a", "b", "c"])
        #expect(shown[0].state == .done)
        #expect(shown[1].state == .running)
        // b moved and d is new: still pending.
        let pending = hold.pending(live: live, query: "")
        #expect(pending.moved == 1)
        #expect(pending.added == 1)
        #expect(layout(shown) == ["running:b,c"])
    }

    @Test func anOwnActionOnAGoneSessionRemovesIt() {
        var hold = holding(start)
        hold.applyOwnAction(sessionKey: "b", live: changed)
        #expect(hold.displayed(live: changed).map(\.key) == ["a", "c"])
        hold.applyOwnAction(sessionKey: "d", live: changed)
        #expect(hold.displayed(live: changed).map(\.key) == ["a", "c"])
    }

    @Test func dismissalsDropOnlyClosedNotes() {
        let before = sessions([
            session("a", extra: ["notes": [note(1), note(2, "needs_you")], "notes_count": 2]),
            session("b", extra: ["notes": [note(3)], "notes_count": 1]),
        ])
        var hold = holding(before)
        let live = sessions([
            session("a", extra: ["notes": [note(2, "needs_you"), note(9)], "notes_count": 2]),
            session("b", extra: ["notes": [note(3)], "notes_count": 1]),
            session("c", extra: ["notes": [note(5)], "notes_count": 1]),
        ])
        hold.apply(.notes([.ids([1])]), live: live)
        let shown = hold.displayed(live: live)
        #expect(shown.map(\.key) == ["a", "b"])
        #expect(shown[0].notes.map(\.id) == [2])
        #expect(shown[0].notesCount == 1)
        #expect(shown[1].notes.map(\.id) == [3])
        let rowsBefore = UpdatesFeed.rows(from: before, query: SessionSearch("")).map(\.id)
        let rowsAfter = UpdatesFeed.rows(from: shown, query: SessionSearch("")).map(\.id)
        #expect(Set(rowsBefore).subtracting(rowsAfter) == ["note:1"])
        #expect(Set(rowsAfter).isSubset(of: Set(rowsBefore)))
    }

    @Test func dismissingOneUpdateLeavesTheSessionAsHeld() {
        var hold = holding(sessions([
            session("a", extra: ["notes": [note(1), note(2)], "notes_count": 2]),
        ]))
        // The owner dismissed note 1; meanwhile a needs you and gained note 3.
        let live = sessions([
            session("a", "needs_you", extra: ["notes": [note(2), note(3)], "notes_count": 2]),
        ])
        hold.apply(.notes([.ids([1])]), live: live)
        let shown = hold.displayed(live: live)
        #expect(shown[0].state == .running)
        #expect(shown[0].notes.map(\.id) == [2])
        #expect(shown[0].notesCount == 1)
        #expect(layout(shown) == ["running:a"])
    }

    @Test func dismissalsLeaveNotesClosedByOthersAndRemoteNotes() {
        let before = sessions([
            session("a", extra: ["notes": [note(1), note(2, "needs_you")], "notes_count": 2]),
            session("b", extra: ["notes": [note(3)], "notes_count": 1]),
            Fixtures.remote("r", "running", extra: ["notes": [note(1)], "notes_count": 1]),
        ])
        // Every note closed in live, but the owner dismissed only a's updates.
        let live = sessions([
            session("a"), session("b"), Fixtures.remote("r", "running"),
        ])
        var hold = holding(before)
        hold.apply(.notes([.session(tool: "claude", sessionId: "a", kinds: [.update])]), live: live)
        var shown = hold.displayed(live: live)
        #expect(shown.map { $0.notes.map(\.id) } == [[2], [3], [1]])
        // Clear all covers every local note, never a remote one.
        hold.apply(.notes([.all]), live: live)
        shown = hold.displayed(live: live)
        #expect(shown.map { $0.notes.map(\.id) } == [[], [], [1]])
    }

    @Test func aSessionActionLeavesOtherSessionsNotes() {
        var hold = holding(sessions([
            session("a"),
            session("b", extra: ["notes": [note(3)], "notes_count": 1]),
        ]))
        let live = sessions([session("a", "done"), session("b")])
        hold.apply(.session(key: "a"), live: live)
        let shown = hold.displayed(live: live)
        #expect(shown[0].state == .done)
        #expect(shown[1].notes.map(\.id) == [3])
    }

    // MARK: - Dismissing while held

    @Test func aHeldClearPlanNamesOnlyTheNotesShown() {
        var hold = holding(sessions([session("a", extra: ["notes": [note(1)], "notes_count": 1])]))
        let live = sessions([
            session("a", extra: ["notes": [note(2, "needs_you"), note(1)], "notes_count": 2]),
        ])
        let shown = hold.displayed(live: live)
        let rows = hold.scoped(UpdatesFeed.rows(from: shown, query: SessionSearch("")), live: live)
        let whole = UpdatesFeed.clearPlan(from: shown, query: SessionSearch(""))
        let plan = hold.clearPlan(whole, rows: rows, live: live, query: "")
        #expect(plan.title == "Clear these")
        #expect(!plan.dismissals.contains(.all))
        #expect(plan.dismissals == [.ids([1])])
        // Nothing pending, or live: the panel's own plan.
        hold.release(live: live)
        #expect(hold.clearPlan(whole, rows: rows, live: live, query: "") == whole)
        #expect(PopoverHold().clearPlan(whole, rows: rows, live: live, query: "") == whole)
    }

    @Test func aHeldGroupRowDismissesOnlyItsShownNotesWhenItsSessionGainedOne() {
        let held = sessions([session("a", extra: ["notes": [note(1), note(2)], "notes_count": 2])])
        let hold = holding(held)
        let rows = UpdatesFeed.rows(from: held, query: SessionSearch(""))
        #expect(rows.map(\.dismissal) == [.session(tool: "claude", sessionId: "a", kinds: UpdatesFeed.groupedKinds)])
        // Nothing new: the row keeps dismissing its whole session.
        #expect(hold.scoped(rows, live: held) == rows)
        let live = sessions([session("a", extra: ["notes": [note(3), note(1), note(2)], "notes_count": 3])])
        let scoped = hold.scoped(rows, live: live)
        guard case .ids(let ids) = scoped.first?.dismissal else {
            Issue.record("expected the row to dismiss its listed ids")
            return
        }
        #expect(Set(ids) == [1, 2])
        #expect(scoped.map(\.id) == rows.map(\.id))
        #expect(PopoverHold().scoped(rows, live: live) == rows)
    }

    @Test(arguments: ["", "alpha"])
    func aHeldClearPlanOverAGroupRowNamesOnlyItsShownNotes(query: String) {
        // "" covers Clear all (`.all`), "alpha" Clear matching (`.session(kinds: nil)`).
        let held = sessions([session("a", extra: ["title": "alpha", "notes": [note(1), note(2)], "notes_count": 2])])
        let hold = holding(held, query: query)
        let live = sessions([session("a", extra: ["title": "alpha", "notes": [note(3), note(1), note(2)],
                                                  "notes_count": 3])])
        let shown = hold.displayed(live: live, query: query)
        let rows = hold.scoped(UpdatesFeed.rows(from: shown, query: SessionSearch(query)), live: live)
        let whole = UpdatesFeed.clearPlan(from: shown, query: SessionSearch(query))
        #expect(whole.dismissals != [.ids([1, 2])])
        let plan = hold.clearPlan(whole, rows: rows, live: live, query: query)
        #expect(plan.title == "Clear these")
        #expect(plan.dismissals.count == 1)
        guard case .ids(let ids) = plan.dismissals.first else {
            Issue.record("expected the plan to dismiss the listed ids, got \(plan.dismissals)")
            return
        }
        #expect(Set(ids) == [1, 2])
    }

    @Test func aDismissalOnASessionThatLeftTheBoardDropsTheNote() {
        var hold = holding(sessions([session("a", extra: ["notes": [note(1), note(2)], "notes_count": 2])]))
        hold.apply(.notes([.ids([1])]), live: [])
        let shown = hold.displayed(live: [])
        #expect(shown.map(\.key) == ["a"])
        #expect(shown[0].notes.map(\.id) == [2])
        #expect(shown[0].notesCount == 1)
    }
}
