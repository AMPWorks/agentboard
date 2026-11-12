import AppKit
import Foundation
import Testing
@testable import AgentBoard

private let launch = ISO8601DateParsing.date(from: "2026-10-24T09:00:00Z")!
private let before = "2026-10-24T08:00:00Z"
private let after = "2026-10-24T09:30:00Z"
private let t1 = "2026-10-24T09:05:00.100000Z"
private let t2 = "2026-10-24T09:10:00.200000Z"

/// One session row. `denials` are (id, ts) pairs, newest first like the CLI.
private func row(
    _ key: String, _ state: String = "running",
    denials: [(Int, String)] = [], updated: String = after,
    finished: String? = nil, reason: String? = nil,
    awaitsPR: Bool = false, updateNote: Bool = false,
    interactive: Bool = true, kind: String? = "human"
) -> [String: Any] {
    var extra: [String: Any] = [
        "updated_at": updated,
        "interactive": interactive,
        "turn_prompt_kind": kind ?? NSNull(),
        "turn_finished_at": finished ?? NSNull(),
        "waiting_reason": reason ?? NSNull(),
        "denials": denials.map { ["id": $0.0, "ts": $0.1, "tool_name": "Bash", "reason": "[x]"] },
        "denials_count": denials.count,
    ]
    if awaitsPR {
        extra["links"] = [["kind": "pr", "ref": "o/r#7", "awaits_you": true]]
    }
    if updateNote {
        extra["notes"] = [["id": 1, "kind": "update", "text": reason ?? "Update", "ts": t1, "last_ts": t1]]
    }
    return Fixtures.session(key, state, alive: true, extra: extra)
}

private let noDashboard = SessionNotificationPlan.Context(dashboardIsKey: false, dashboardSelection: nil)

private extension SoundEventDetector {
    /// Feeds one snapshot; `needsYou` and `finished` stand in for what
    /// `AttentionController` and `TurnFinishDetector` reported for it.
    mutating func feed(
        _ rows: [String: Any]...,
        needsYou: Set<String> = [], finished: Set<String> = [],
        context: SessionNotificationPlan.Context = noDashboard,
        sounds: (AttentionEvent) -> Bool = { _ in true }
    ) -> Set<AttentionEvent> {
        let snapshot = Fixtures.snapshot(rows)
        let turns = TurnFinishDetector.Changes(
            finished: snapshot.sessions.filter { finished.contains($0.key) }
        )
        return note(snapshot, needsYouEntered: needsYou, turns: turns, context: context, sounds: sounds)
    }
}

@Suite("SoundEventDetector")
struct SoundEventDetectorTests {
    private func primed(_ rows: [String: Any]...) -> SoundEventDetector {
        var detector = SoundEventDetector(startedAt: launch)
        let snapshot = Fixtures.snapshot(rows)
        _ = detector.note(snapshot, needsYouEntered: Set(snapshot.sessions.map(\.key)),
                          turns: .init(), context: noDashboard)
        return detector
    }

    // MARK: Launch

    @Test func theFirstSnapshotPlaysNothing() {
        var detector = SoundEventDetector(startedAt: launch)
        let events = detector.feed(
            row("a", "needs_you"),
            row("b", "review", finished: t1, reason: "Your turn"),
            row("c", "interrupted"),
            row("d", "review", reason: "PR o/r#7 awaits your review", awaitsPR: true),
            row("e", denials: [(9, after)]),
            needsYou: ["a"], finished: ["b"]
        )
        #expect(events.isEmpty)
    }

    @Test func theFirstSnapshotPrimesSoNothingReplaysLater() {
        var detector = SoundEventDetector(startedAt: launch)
        _ = detector.feed(row("c", "interrupted"),
                          row("d", "review", reason: "PR o/r#7 awaits your review", awaitsPR: true),
                          row("e", denials: [(9, after)]))
        #expect(detector.feed(row("c", "interrupted"),
                              row("d", "review", reason: "PR o/r#7 awaits your review", awaitsPR: true),
                              row("e", denials: [(9, after)])).isEmpty)
    }

    // MARK: Needs you and turn finished

    @Test @MainActor func needsYouPlaysOnceOnEntryAndNotWhileItStays() {
        let attention = AttentionController(
            sleeper: { _ in try await Task.sleep(for: .seconds(86_400)) },
            reduceMotion: false, notificationCenter: NotificationCenter(),
            workspaceNotificationCenter: nil
        )
        var detector = SoundEventDetector(startedAt: launch)
        func step(_ rows: [String: Any]...) -> Set<AttentionEvent> {
            let snapshot = Fixtures.snapshot(rows)
            let entered = attention.note(snapshot: snapshot)
            return detector.note(snapshot, needsYouEntered: entered, turns: .init(), context: noDashboard)
        }
        #expect(step(row("a", "needs_you")).isEmpty)  // launch
        #expect(step(row("a", "needs_you"), row("b", "needs_you")) == [.needsYou])
        #expect(step(row("a", "needs_you"), row("b", "needs_you")).isEmpty)
        #expect(step(row("a", "running"), row("b", "needs_you")).isEmpty)
        #expect(step(row("a", "needs_you"), row("b", "needs_you")) == [.needsYou])
    }

    @Test func aFinishedTurnPlaysTurnFinished() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", "review", finished: t1, reason: "Your turn: done"),
                              finished: ["a"]) == [.turnFinished])
    }

    // MARK: Denied

    @Test func aDenialPlaysOnlyWhenTheNewestIdRises() {
        var detector = primed(row("a", denials: [(5, before)]), row("b", denials: [(4, before), (3, before)]))
        // b dismisses its newest: its count changes, nothing rises.
        #expect(detector.feed(row("a", denials: [(5, before)]), row("b", denials: [(3, before)])).isEmpty)
        // b dismisses everything.
        #expect(detector.feed(row("a", denials: [(5, before)]), row("b")).isEmpty)
        // a gets a new one.
        #expect(detector.feed(row("a", denials: [(8, t1), (5, before)]), row("b")) == [.denied])
        #expect(detector.feed(row("a", denials: [(8, t1), (5, before)]), row("b")).isEmpty)
        // a dismisses all, then a newer one arrives.
        #expect(detector.feed(row("a"), row("b")).isEmpty)
        #expect(detector.feed(row("a", denials: [(11, t2)]), row("b")) == [.denied])
    }

    @Test func aFirstDenialOnAKnownSessionPlays() {
        var detector = primed(row("a"))
        #expect(detector.feed(row("a", denials: [(2, t1)])) == [.denied])
    }

    @Test func aSessionThatDisappearsAndReturnsWithTheSameDenialsIsSilent() {
        var detector = primed(row("a", denials: [(5, before)]))
        #expect(detector.feed(row("a", denials: [(6, t1), (5, before)])) == [.denied])
        #expect(detector.feed().isEmpty)  // hidden
        #expect(detector.feed(row("a", denials: [(6, t1), (5, before)])).isEmpty)
    }

    // MARK: Review ready

    @Test func enteringInterruptedPlaysOncePerEntry() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", "interrupted")) == [.reviewReady])
        #expect(detector.feed(row("a", "interrupted")).isEmpty)
        #expect(detector.feed(row("a", "running")).isEmpty)
        #expect(detector.feed(row("a", "interrupted")) == [.reviewReady])
    }

    @Test func aPRAwaitingReviewPlaysOnEntryOnly() {
        var detector = primed(row("a", "running"))
        let review = row("a", "review", finished: t1, reason: "PR o/r#7 awaits your review", awaitsPR: true)
        #expect(detector.feed(review) == [.reviewReady])
        #expect(detector.feed(review).isEmpty)
    }

    @Test func anUpdateNotePlays() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", "review", finished: t1, reason: "Deployed to staging",
                                  updateNote: true)) == [.reviewReady])
    }

    @Test func aPlainFinishedTurnNeverPlaysReviewReady() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", "review", finished: t1, reason: "Your turn: done")).isEmpty)
        #expect(detector.feed(row("a", "review", finished: t2, reason: "Interrupted: your turn")).isEmpty)
        // An update note older than the newest prompt leaves a plain turn reason.
        #expect(detector.feed(row("a", "review", finished: t2, reason: "Your turn",
                                  updateNote: true)).isEmpty)
    }

    @Test func aTurnFinishingIsTurnFinishedNotAlsoReviewReady() {
        var detector = primed(row("a", "running"))
        let review = row("a", "review", finished: t1, reason: "PR o/r#7 awaits your review", awaitsPR: true)
        #expect(detector.feed(review, finished: ["a"]) == [.turnFinished])
        // Remembered: the same turn and reason later stays silent.
        #expect(detector.feed(review).isEmpty)
    }

    @Test func aSilentTurnFinishedDoesNotSwallowReviewReady() {
        var detector = primed(row("a", "running"))
        let review = row("a", "review", finished: t1, reason: "PR o/r#7 awaits your review", awaitsPR: true)
        let events = detector.feed(review, finished: ["a"], sounds: { $0 != .turnFinished })
        #expect(events.contains(.reviewReady))
        #expect(detector.feed(review, sounds: { $0 != .turnFinished }).isEmpty)
    }

    @Test func aSilentNeedsYouDoesNotSwallowInterrupted() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", "interrupted"), needsYou: ["a"]) == [.needsYou])
        var other = primed(row("a", "running"))
        #expect(other.feed(row("a", "interrupted"), needsYou: ["a"],
                           sounds: { $0 != .needsYou }).contains(.reviewReady))
    }

    /// The detector and the player together, as `MenuBarLabel` wires them:
    /// the configured sounds decide whether turn-finished covers review-ready.
    @Test @MainActor func aTurnIntoAPRReviewPlaysWhicheverEventHasASound() {
        let review = row("a", "review", finished: t1, reason: "PR o/r#7 awaits your review", awaitsPR: true)
        func run(turnFinished: String) -> FakeSoundBackend {
            let suite = TestDefaults()
            suite.defaults.set(turnFinished, forKey: SettingsKeys.soundTurnFinished)
            suite.defaults.set("system:Glass", forKey: SettingsKeys.soundReviewReady)
            let backend = FakeSoundBackend()
            let player = SoundPlayer(
                backend: backend, defaults: suite.defaults,
                resolve: { choice in
                    if case .system(let name) = choice { return URL(fileURLWithPath: "/sounds/\(name).aiff") }
                    return nil
                }
            )
            var detector = primed(row("a", "running"))
            let sounding = Set(AttentionEvent.allCases.filter { player.sounds($0) })
            player.play(detector.feed(review, finished: ["a"], sounds: sounding.contains))
            return backend
        }

        let silentTurn = run(turnFinished: "none")
        #expect(silentTurn.played == [URL(fileURLWithPath: "/sounds/Glass.aiff")])
        #expect(silentTurn.beeps == 0)

        let alertTurn = run(turnFinished: "default")
        #expect(alertTurn.played.isEmpty)
        #expect(alertTurn.beeps == 1)
    }

    /// A quiet snapshot (nothing finished, nobody newly needs you) never
    /// asks for a sound, which resolves a file; the bookkeeping still runs,
    /// so a later rising denial plays exactly once.
    @Test func aQuietSnapshotNeverAsksWhichEventsHaveASound() {
        var detector = primed(row("a", denials: [(5, before)]))
        var asked: [AttentionEvent] = []
        let quiet = detector.feed(row("a", denials: [(5, before)]), sounds: { asked.append($0); return true })
        #expect(quiet.isEmpty)
        #expect(asked.isEmpty)
        // The denial high-water mark kept up: a new denial plays once.
        let rose = detector.feed(row("a", denials: [(6, t1), (5, before)]), sounds: { asked.append($0); return true })
        #expect(rose == [.denied])
        #expect(asked.isEmpty)
        #expect(detector.feed(row("a", denials: [(6, t1), (5, before)])).isEmpty)
        // A finished turn asks about turn-finished only.
        _ = detector.feed(row("a", "review", denials: [(6, t1), (5, before)], finished: t2), finished: ["a"],
                          sounds: { asked.append($0); return true })
        #expect(asked == [.turnFinished])
    }

    @Test func aRunningReviewFlapWithAnUnchangedTurnIsSilent() {
        var detector = primed(row("a", "running"))
        let review = row("a", "review", finished: t1, reason: "PR o/r#7 awaits your review", awaitsPR: true)
        #expect(detector.feed(review) == [.reviewReady])
        #expect(detector.feed(row("a", "running", finished: t1)).isEmpty)
        #expect(detector.feed(review).isEmpty)
        #expect(detector.feed(row("a", "running", finished: t1)).isEmpty)
        #expect(detector.feed(review).isEmpty)
    }

    @Test func aNewReasonPlaysAgain() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", "review", finished: t1, reason: "PR o/r#7 awaits your review",
                                  awaitsPR: true)) == [.reviewReady])
        #expect(detector.feed(row("a", "review", finished: t1, reason: "PRs o/r#7, o/r#8 await your review",
                                  awaitsPR: true)) == [.reviewReady])
    }

    @Test func unattendedSessionsNeverPlayReviewReady() {
        var detector = primed(row("a", "running", interactive: false), row("b", "running", kind: "loop"),
                              row("c", "running", kind: "cron"))
        #expect(detector.feed(row("a", "interrupted", interactive: false),
                              row("b", "interrupted", kind: "loop"),
                              row("c", "review", finished: t1, reason: "PR o/r#7 awaits your review",
                                  awaitsPR: true, kind: "cron")).isEmpty)
    }

    // MARK: Sessions first seen after launch

    @Test func aLateSessionWithOldStateFiresNothing() {
        var detector = primed(row("x"))
        #expect(detector.feed(row("x"),
                              row("a", denials: [(3, before)], updated: before),
                              row("b", "interrupted", updated: before),
                              row("c", "review", updated: before, finished: before, reason: "PR o/r#7 awaits your review",
                                  awaitsPR: true)).isEmpty)
        // And recorded, so the next snapshot stays quiet too.
        #expect(detector.feed(row("x"),
                              row("a", denials: [(3, before)], updated: before),
                              row("b", "interrupted", updated: before),
                              row("c", "review", updated: before, finished: before, reason: "PR o/r#7 awaits your review",
                                  awaitsPR: true)).isEmpty)
    }

    @Test func aLateSessionWithADenialSinceLaunchFires() {
        var detector = primed(row("x"))
        #expect(detector.feed(row("x"), row("a", denials: [(3, t1)])) == [.denied])
    }

    @Test func aLateSessionThatChangedSinceLaunchFires() {
        var detector = primed(row("x"))
        #expect(detector.feed(row("x"), row("b", "interrupted", updated: t1)) == [.reviewReady])
    }

    // MARK: Collapsing and the dashboard

    @Test func severalSessionsMakeOneEventPerKind() {
        var detector = primed(row("a"), row("b"), row("c"), row("d"))
        let events = detector.feed(
            row("a", "interrupted", denials: [(4, t1)]),
            row("b", "interrupted", denials: [(5, t1)]),
            row("c", "needs_you"), row("d", "needs_you"),
            needsYou: ["c", "d"]
        )
        #expect(events == [.needsYou, .denied, .reviewReady])
    }

    @Test func theSelectedSessionInTheKeyDashboardIsSuppressed() {
        let watching = SessionNotificationPlan.Context(dashboardIsKey: true, dashboardSelection: "a")
        var detector = primed(row("a"))
        #expect(detector.feed(row("a", "needs_you"), needsYou: ["a"], context: watching).isEmpty)
        #expect(detector.feed(row("a", "review", finished: t1, reason: "Your turn"),
                              finished: ["a"], context: watching).isEmpty)
        #expect(detector.feed(row("a", "interrupted", denials: [(4, t1)]), context: watching).isEmpty)
        // Recorded while suppressed: not replayed once the user looks away.
        #expect(detector.feed(row("a", "interrupted", denials: [(4, t1)])).isEmpty)
    }

    @Test func anotherSessionStillPlaysWhileTheDashboardIsKey() {
        let watching = SessionNotificationPlan.Context(dashboardIsKey: true, dashboardSelection: "a")
        var detector = primed(row("a"), row("b"))
        #expect(detector.feed(row("a"), row("b", denials: [(4, t1)]), context: watching) == [.denied])
    }

    @Test func aSelectionWithoutTheKeyDashboardStillPlays() {
        let notKey = SessionNotificationPlan.Context(dashboardIsKey: false, dashboardSelection: "a")
        var detector = primed(row("a"))
        #expect(detector.feed(row("a", "needs_you"), needsYou: ["a"], context: notKey) == [.needsYou])
    }
}
