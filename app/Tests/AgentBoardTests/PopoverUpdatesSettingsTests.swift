import Testing
@testable import AgentBoard

private func note(_ id: Int, _ kind: String = "update", _ text: String, minute: Int) -> [String: Any] {
    let ts = String(format: "2026-10-15T10:%02d:00Z", minute)
    return ["id": id, "kind": kind, "text": text, "ts": ts, "last_ts": ts, "count": 1, "source": "stop"]
}

private func session(_ key: String, notes: [[String: Any]]) -> [String: Any] {
    Fixtures.session(key, "review", extra: [
        "cwd": "/tmp/\(key)", "project": key, "notes": notes, "notes_count": notes.count,
    ])
}

@Suite("Popover Updates settings")
struct PopoverUpdatesSettingsTests {
    @Test func defaultsAreHiddenDefaultSizeAndTodaysLimit() {
        #expect(UpdatesPanelMode.default == .hiddenUntilClicked)
        #expect(UpdatesFontSize.default == .standard)
        #expect(PopoverUpdatesSettings.compactLimit(0) == UpdatesFeed.compactLimit)
        #expect(UpdatesFeed.compactLimit == 3)
    }

    @Test func parsesStoredValuesAndFallsBackOnUnknownOnes() {
        #expect(UpdatesPanelMode.parse("rememberLast") == .rememberLast)
        #expect(UpdatesPanelMode.parse("alwaysShown") == .alwaysShown)
        #expect(UpdatesPanelMode.parse(nil) == .hiddenUntilClicked)
        #expect(UpdatesPanelMode.parse("sometimes") == .hiddenUntilClicked)
        #expect(UpdatesFontSize.parse("large") == .large)
        #expect(UpdatesFontSize.parse("default") == .standard)
        #expect(UpdatesFontSize.parse("huge") == .standard)
        #expect(PopoverUpdatesSettings.compactLimit(5) == 5)
        #expect(PopoverUpdatesSettings.compactLimit(10) == 10)
        #expect(PopoverUpdatesSettings.compactLimit(7) == 3)
    }

    @Test func rawValuesAreTheDocumentedOnes() {
        #expect(UpdatesPanelMode.allCases.map(\.rawValue) == ["hiddenUntilClicked", "rememberLast", "alwaysShown"])
        #expect(UpdatesFontSize.allCases.map(\.rawValue) == ["small", "default", "large"])
        #expect(PopoverUpdatesSettings.compactLimits == [3, 5, 10])
    }

    @Test func defaultFontMatchesTheCaptionStylesAndSizesGrow() {
        #expect(UpdatesFontSize.standard.textSize == 10)
        #expect(UpdatesFontSize.standard.minorSize == 10)
        #expect(UpdatesFontSize.standard.rowPadding == 4)
        let sizes = UpdatesFontSize.allCases
        #expect(sizes.map(\.textSize) == sizes.map(\.textSize).sorted())
        #expect(sizes.map(\.minorSize) == sizes.map(\.minorSize).sorted())
        #expect(sizes.map(\.rowPadding) == [2, 4, 5])
    }

    @Test func panelShownFollowsTheMode() {
        typealias S = PopoverUpdatesSettings
        #expect(S.panelShown(mode: .alwaysShown, remembered: false, toggledThisOpen: false))
        #expect(S.panelShown(mode: .rememberLast, remembered: true, toggledThisOpen: false))
        #expect(!S.panelShown(mode: .rememberLast, remembered: false, toggledThisOpen: true))
        #expect(S.panelShown(mode: .hiddenUntilClicked, remembered: false, toggledThisOpen: true))
        #expect(!S.panelShown(mode: .hiddenUntilClicked, remembered: true, toggledThisOpen: false))
    }

    @Test func toggleShowsOnlyWithUpdatesAndNeverWhenAlwaysShown() {
        typealias S = PopoverUpdatesSettings
        #expect(S.toggleVisible(mode: .hiddenUntilClicked, hasUpdates: true))
        #expect(S.toggleVisible(mode: .rememberLast, hasUpdates: true))
        #expect(!S.toggleVisible(mode: .hiddenUntilClicked, hasUpdates: false))
        #expect(!S.toggleVisible(mode: .alwaysShown, hasUpdates: true))
    }

    @Test func panelVisibility() {
        typealias S = PopoverUpdatesSettings
        // Always shown keeps the old layout: only when rows exist.
        #expect(S.panelVisible(mode: .alwaysShown, shown: true, hasUpdates: true, hasRows: true))
        #expect(!S.panelVisible(mode: .alwaysShown, shown: true, hasUpdates: true, hasRows: false))
        // Toggled open, a query matching nothing still shows the panel.
        #expect(S.panelVisible(mode: .hiddenUntilClicked, shown: true, hasUpdates: true, hasRows: false))
        #expect(!S.panelVisible(mode: .hiddenUntilClicked, shown: true, hasUpdates: false, hasRows: false))
        #expect(!S.panelVisible(mode: .rememberLast, shown: false, hasUpdates: true, hasRows: true))
    }

    @Test func badgeAndTooltip() {
        typealias S = PopoverUpdatesSettings
        #expect(S.badge(3) == "3")
        #expect(S.badge(120) == "99+")
        #expect(S.toggleHelp(shown: false, count: 1, filtering: false).hasPrefix("Show 1 open update:"))
        #expect(S.toggleHelp(shown: false, count: 4, filtering: true) == "Show 4 updates that match the filter")
        #expect(S.toggleHelp(shown: true, count: 4, filtering: false) == "Hide the Updates panel")
        // A query that keeps no update: say so rather than "Show 0 updates".
        #expect(S.toggleHelp(shown: false, count: 0, filtering: true)
            == "No updates match the filter; show the Updates panel anyway")
        #expect(S.toggleAccessibilityLabel(shown: false, count: 0, filtering: true)
            == "Show updates; none match the filter")
        #expect(S.toggleAccessibilityLabel(shown: false, count: 1, filtering: false) == "Show 1 update")
        #expect(S.toggleAccessibilityLabel(shown: false, count: 4, filtering: true) == "Show 4 updates")
        #expect(S.toggleAccessibilityLabel(shown: true, count: 4, filtering: false) == "Hide updates")
    }

    /// The button's visibility uses the same rule as its count.
    @Test func hasOpenUpdatesUsesTheCountingRule() {
        let none = Fixtures.snapshot([session("quiet", notes: [])]).sessions
        let some = Fixtures.snapshot([session("busy", notes: [note(1, "update", "PR #1 opened", minute: 1)])]).sessions
        #expect(!PopoverUpdatesSettings.hasOpenUpdates(none))
        #expect(PopoverUpdatesSettings.hasOpenUpdates(some))
        #expect(PopoverUpdatesSettings.hasOpenUpdates(some)
            == (UpdatesFeed.total(from: some, query: SessionSearch("")) > 0))
    }

    /// The button is red for any open needs_you note, on a session in any
    /// state (done and snoozed ones included), never for updates alone, and
    /// a popover query that hides the note does not turn it grey.
    @Test func redButtonCountsEveryNeedsYouNoteWhateverTheQuery() {
        func s(_ key: String, _ state: String, _ notes: [[String: Any]]) -> [String: Any] {
            Fixtures.session(key, state, extra: ["project": key, "notes": notes, "notes_count": notes.count])
        }
        let infoOnly = Fixtures.snapshot([
            s("busy", "running", [note(1, "update", "PR #1 opened", minute: 1),
                                  note(2, "error", "API error 529", minute: 2)]),
        ]).sessions
        #expect(Self.tint(infoOnly) == .plain)
        #expect(PopoverUpdatesSettings.openNeedsYouCount(infoOnly) == 0)

        let finished = Fixtures.snapshot([s("finished", "done", [note(1, "needs_you", "USER_ACTION: ship it?", minute: 1)])]).sessions
        #expect(Self.tint(finished) == .needsYou)
        let parked = Fixtures.snapshot([s("parked", "snoozed", [note(1, "needs_you", "USER_ACTION: pick one", minute: 1)])]).sessions
        #expect(Self.tint(parked) == .needsYou)

        let sessions = Self.redScenario()
        #expect(PopoverUpdatesSettings.openNeedsYouCount(sessions) == 5)
        // A query that hides every note: the feed is empty, the button red.
        #expect(UpdatesFeed.total(from: sessions, query: SessionSearch("zzz-no-such-thing")) == 0)
        #expect(Self.tint(sessions) == .needsYou)
    }

    /// The tint the button gets from these sessions with the panel closed.
    private static func tint(_ sessions: [Session]) -> PopoverUpdatesSettings.ButtonTint {
        PopoverUpdatesSettings.buttonTint(needsYou: PopoverUpdatesSettings.openNeedsYouCount(sessions), shown: false)
    }

    /// Red beats the accent tint of an open panel; grey only when closed
    /// with no needs_you note.
    @Test func buttonTintIsRedForNeedsYouWhetherThePanelIsOpenOrClosed() {
        typealias S = PopoverUpdatesSettings
        #expect(S.buttonTint(needsYou: 1, shown: false) == .needsYou)
        #expect(S.buttonTint(needsYou: 3, shown: true) == .needsYou)
        #expect(S.buttonTint(needsYou: 0, shown: true) == .accent)
        #expect(S.buttonTint(needsYou: 0, shown: false) == .plain)
    }

    @Test func redButtonTooltipSaysHowManyNeedYou() {
        typealias S = PopoverUpdatesSettings
        #expect(S.toggleHelp(shown: false, count: 4, filtering: false, needsYou: 1)
            == "Show 4 open updates: what sessions said that is worth keeping (1 needs you)")
        #expect(S.toggleHelp(shown: false, count: 0, filtering: true, needsYou: 2)
            == "No updates match the filter; show the Updates panel anyway (2 need you)")
        #expect(S.toggleHelp(shown: true, count: 4, filtering: false, needsYou: 3)
            == "Hide the Updates panel (3 need you)")
        #expect(S.toggleAccessibilityLabel(shown: false, count: 4, filtering: false, needsYou: 1)
            == "Show 4 updates, 1 needs you")
        #expect(S.toggleAccessibilityLabel(shown: true, count: 4, filtering: false, needsYou: 2)
            == "Hide updates, 2 need you")
        // Grey (no needs_you note): the wording is unchanged.
        #expect(S.toggleHelp(shown: false, count: 4, filtering: true, needsYou: 0)
            == "Show 4 updates that match the filter")
    }

    @Test func compactLimitIsThreadedThroughTheFeed() {
        let notes = (1...8).map { note($0, "update", "tick \($0)", minute: $0) }
        let sessions = Fixtures.snapshot(notes.enumerated().map { i, n in session("s\(i)", notes: [n]) }).sessions
        let rows = UpdatesFeed.rows(from: sessions, query: SessionSearch(""))
        #expect(UpdatesFeed.compact(rows, expanded: false, limit: 5).shown.count == 5)
        #expect(UpdatesFeed.needsToggle(rows, limit: 5))
        #expect(!UpdatesFeed.needsToggle(rows, limit: 10))
    }

    private static func redScenario() -> [Session] {
        func s(_ key: String, _ state: String, _ notes: [[String: Any]]) -> [String: Any] {
            Fixtures.session(key, state, extra: ["project": key, "notes": notes, "notes_count": notes.count])
        }
        return Fixtures.snapshot([
            s("red", "needs_you", [note(1, "needs_you", "USER_ACTION: approve", minute: 1)]),
            s("parked", "snoozed", [note(2, "needs_you", "USER_ACTION: pick one", minute: 2)]),
            s("answered", "running", [note(3, "needs_you", "USER_ACTION: merge?", minute: 3),
                                      note(4, "update", "PR #4 opened", minute: 4)]),
            s("finished", "done", [note(5, "needs_you", "USER_ACTION: ship it?", minute: 5)]),
            s("stopped", "interrupted", [note(6, "needs_you", "USER_ACTION: retry?", minute: 6)]),
        ]).sessions
    }

    /// A query that hides needs_you notes: the panel is closed and nothing
    /// above the sessions lists them, so the notice counts them (in red).
    /// The hidden Interrupted session's note is left to its "1 needs you".
    @Test func hiddenNoticeCountsHiddenNeedsYouNotesWithThePanelClosed() {
        let sessions = Self.redScenario()
        let query = SessionSearch("approve") // only the red session's note says it
        #expect(UpdatesFeed.hiddenNeedsYouNotes(from: sessions, query: query) == 3)
        let closed = PopoverUpdatesSettings.panelShown(mode: .hiddenUntilClicked, remembered: false,
                                                       toggledThisOpen: false)
        #expect(!closed)
        let sections = PopoverSections.make(from: sessions, query: query)
        #expect(sections.hiddenNeedsYouNotes == 3)
        #expect(sections.hiddenNotice == "Filtered: 4 sessions hidden, 1 needs you, 3 updates need you")
        #expect(sections.noticeIsEmphasized)
    }

    /// Only a snoozed session's request is hidden: the notice says so in
    /// red even though no hidden session is itself in Needs you.
    @Test func hiddenNoticeIsRedWhenTheOnlyHiddenRequestIsOnASnoozedSession() {
        let sessions = Fixtures.snapshot([
            Fixtures.session("parked", "snoozed", extra: [
                "project": "parked", "notes": [note(1, "needs_you", "USER_ACTION: pick one", minute: 1)],
                "notes_count": 1,
            ]),
            session("alpha", notes: []),
        ]).sessions
        let sections = PopoverSections.make(from: sessions, query: SessionSearch("alpha"))
        #expect(sections.hiddenNotice == "Filtered: 1 session hidden, 1 update needs you")
        #expect(sections.noticeIsEmphasized)
    }

    /// The badge counts the updates the query keeps, uncapped.
    @Test func badgeCountFollowsTheQuery() {
        let sessions = Fixtures.snapshot([
            session("alpha", notes: [note(1, "update", "PR #7 merged", minute: 1)]),
            session("beta", notes: [note(2, "update", "CI red on main", minute: 2),
                                    note(3, "update", "deployed", minute: 3)]),
        ]).sessions
        #expect(UpdatesFeed.total(from: sessions, query: SessionSearch("")) == 3)
        #expect(UpdatesFeed.total(from: sessions, query: SessionSearch("merged")) == 1)
    }

    /// A query that matches only update text: its session is listed (note
    /// text is searched) but the update itself sits in the closed panel, so
    /// the hint points at it, and computing it opens nothing.
    @Test func queryMatchingOnlyNotesShowsTheHintWithoutOpeningThePanel() {
        let sessions = Fixtures.snapshot([
            session("alpha", notes: [note(1, "update", "flaky-widget test fixed", minute: 1)]),
            session("beta", notes: []),
        ]).sessions
        let query = SessionSearch("flaky-widget")
        let sections = PopoverSections.make(from: sessions, query: query)
        #expect(sections.sections.flatMap(\.sessions).map(\.key) == ["alpha"])
        let matching = UpdatesFeed.total(from: sessions, query: query)
        #expect(matching == 1)
        let shown = PopoverUpdatesSettings.panelShown(mode: .hiddenUntilClicked, remembered: false,
                                                      toggledThisOpen: false)
        #expect(PopoverUpdatesSettings.matchHint(panelShown: shown, isFiltering: true, matching: matching)
            == "1 update matches")
        #expect(!shown)
        // No hint once the panel is open, with no query, or with no match.
        #expect(PopoverUpdatesSettings.matchHint(panelShown: true, isFiltering: true, matching: 1) == nil)
        #expect(PopoverUpdatesSettings.matchHint(panelShown: false, isFiltering: false, matching: 1) == nil)
        #expect(PopoverUpdatesSettings.matchHint(panelShown: false, isFiltering: true, matching: 0) == nil)
        #expect(PopoverUpdatesSettings.matchHint(panelShown: false, isFiltering: true, matching: 2) == "2 updates match")
    }
}
