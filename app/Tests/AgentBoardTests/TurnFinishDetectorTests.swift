import AppKit
import Testing
@testable import AgentBoard

@Suite("Session turn fields")
struct SessionTurnFieldsTests {
    @Test func absentFieldsDefault() {
        let session = Fixtures.decoded("a", "review")
        #expect(session.turnFinishedAt == nil)
        #expect(session.turnFinishedDate == nil)
        #expect(session.turnPromptKind == nil)
        #expect(session.backgroundTasks == nil)
        #expect(session.turnEndsBackgroundWait == false)
    }

    @Test func presentFieldsDecode() {
        let session = Fixtures.decoded("a", "review", extra: [
            "turn_finished_at": "2026-10-24T09:00:00.123456Z",
            "turn_prompt_kind": "injected",
            "background_tasks": 2,
            "turn_ends_background_wait": true,
        ])
        #expect(session.turnFinishedAt == "2026-10-24T09:00:00.123456Z")
        #expect(session.turnFinishedDate != nil)
        #expect(session.turnPromptKind == "injected")
        #expect(session.backgroundTasks == 2)
        #expect(session.turnEndsBackgroundWait)
    }

    @Test func nullFieldsDecodeAsDefaults() {
        let session = Fixtures.decoded("a", "running", extra: [
            "turn_finished_at": NSNull(), "turn_prompt_kind": NSNull(),
            "background_tasks": NSNull(), "turn_ends_background_wait": NSNull(),
        ])
        #expect(session.turnFinishedAt == nil)
        #expect(session.backgroundTasks == nil)
        #expect(session.turnEndsBackgroundWait == false)
    }

    @Test func bundledFixtureCarriesTheFields() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/snapshot.json")
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
        let review = snapshot.sessions.filter { $0.state == .review }
        #expect(!review.isEmpty)
        #expect(review.allSatisfy { $0.turnFinishedDate != nil })
        #expect(snapshot.sessions.contains { $0.state == .running && $0.backgroundTasks == 1 })
    }
}

private let launch = ISO8601DateParsing.date(from: "2026-10-24T09:00:00Z")!
private let before = "2026-10-24T08:00:00.000000Z"
private let t1 = "2026-10-24T09:05:00.100000Z"
private let t2 = "2026-10-24T09:10:00.200000Z"

private func row(_ key: String, _ state: String = "review", finished: String? = nil,
                 kind: String? = "human", endsWait: Bool = false, background: Int? = nil,
                 interactive: Bool = true, updated: String = "2026-10-24T09:00:00Z") -> [String: Any] {
    Fixtures.session(key, state, alive: true, extra: [
        "turn_finished_at": finished ?? NSNull(),
        "turn_prompt_kind": kind ?? NSNull(),
        "turn_ends_background_wait": endsWait,
        "background_tasks": background ?? NSNull(),
        "interactive": interactive,
        "updated_at": updated,
    ])
}

private extension TurnFinishDetector {
    mutating func feed(_ rows: [String: Any]...) -> Changes {
        note(Fixtures.snapshot(rows))
    }
}

private extension TurnFinishDetector.Changes {
    var finishedKeys: [String] { finished.map(\.key) }
}

@Suite("TurnFinishDetector")
struct TurnFinishDetectorTests {
    private func primed(_ rows: [String: Any]...) -> TurnFinishDetector {
        var detector = TurnFinishDetector(startedAt: launch)
        #expect(detector.note(Fixtures.snapshot(rows)).isEmpty)
        return detector
    }

    @Test func primingReportsNothing() {
        var detector = TurnFinishDetector(startedAt: launch)
        let changes = detector.feed(row("a", finished: t1), row("b", "running"))
        #expect(changes.isEmpty)
        // The turn already in Review at launch stays silent afterwards too.
        #expect(detector.feed(row("a", finished: t1), row("b", "running")).isEmpty)
    }

    @Test func runningToReviewReportsOnce() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
        #expect(detector.feed(row("a", finished: t1)).isEmpty)
    }

    @Test func aFlapThroughRunningDoesNotRepeat() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
        #expect(detector.feed(row("a", "running", finished: t1)).isEmpty)
        #expect(detector.feed(row("a", finished: t1)).isEmpty)
    }

    @Test func aFirstObservationDuringAFlapReportsOnceItSettles() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", "running", finished: t1)).isEmpty)
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
    }

    @Test func updatedAtChangingWithTheSameTurnDoesNotRepeat() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
        #expect(detector.feed(row("a", finished: t1, updated: "2026-10-24T09:06:00Z")).isEmpty)
    }

    @Test func statesOtherThanReviewNeverReport() {
        var detector = primed(row("n", "running"), row("i", "running"), row("l", "running"),
                              row("u", "running"))
        let changes = detector.feed(
            row("n", "needs_you", finished: t1),
            row("i", "interrupted", finished: t1),
            row("l", "running", finished: t1, kind: "loop"),
            row("u", "unconcluded", finished: t1)
        )
        #expect(changes.finished.isEmpty)
        // needs_you/interrupted/unconcluded considered their turn: a later
        // Review for the same turn stays silent.
        #expect(detector.feed(row("n", finished: t1), row("i", finished: t1),
                              row("u", finished: t1)).isEmpty)
    }

    @Test func aLoopTurnInReviewIsSilent() {
        var detector = primed(row("a", "running", kind: "loop"))
        #expect(detector.feed(row("a", finished: t1, kind: "loop")).finished.isEmpty)
        #expect(detector.feed(row("a", finished: t2, kind: "cron")).finished.isEmpty)
    }

    @Test func aBackgroundWaitReportsOnlyTheReportBack() {
        var detector = primed(row("a", "running"))
        // The Stop listed a background task: running, then Review with a
        // null ts once the subagent finished (the column dropped to 0).
        #expect(detector.feed(row("a", "running", background: 1)).isEmpty)
        #expect(detector.feed(row("a", background: 0)).isEmpty)
        // The injected <task-notification> turn runs, then finishes.
        #expect(detector.feed(row("a", "running", kind: "injected")).isEmpty)
        let done = detector.feed(row("a", finished: t2, kind: "injected", endsWait: true))
        #expect(done.finishedKeys == ["a"])
        #expect(detector.feed(row("a", finished: t2, kind: "injected", endsWait: true)).isEmpty)
    }

    @Test func anInjectedFinishWithoutAWaitIsSilentButMarked() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1, kind: "injected")).isEmpty)
        // Marked: the same turn does not report if the flag shows up later.
        #expect(detector.feed(row("a", finished: t1, kind: "injected", endsWait: true)).isEmpty)
    }

    @Test func anUnobservedBackgroundRunStillReports() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
        // Straight from the human turn's Review to the report-back finish.
        let changes = detector.feed(row("a", finished: t2, kind: "injected", endsWait: true))
        #expect(changes.finishedKeys == ["a"])
        #expect(changes.answered.isEmpty)
    }

    @Test func aPeerMessageAnsweredInAnIdleSessionIsSilent() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
        #expect(detector.feed(row("a", "running", kind: "injected")).isEmpty)
        #expect(detector.feed(row("a", finished: t2, kind: "injected")).isEmpty)
        // The user's own reply later still clears the delivered one.
        #expect(detector.feed(row("a", "running", kind: "human")).answered == ["a"])
    }

    @Test func aKeyFirstSeenAfterLaunchReportsOnlyForANewTurn() {
        var detector = primed(row("a", "running"))
        let changes = detector.feed(row("a", "running"), row("old", finished: before),
                                    row("new", finished: t1))
        #expect(changes.finishedKeys == ["new"])
        #expect(changes.answered.isEmpty)
        // The baselined old turn stays silent; its next turn reports.
        #expect(detector.feed(row("old", finished: before)).isEmpty)
        #expect(detector.feed(row("old", finished: t2)).finishedKeys == ["old"])
    }

    @Test func nonInteractiveIsSilent() {
        var detector = primed(row("a", "running", interactive: false))
        #expect(detector.feed(row("a", finished: t1, interactive: false)).isEmpty)
    }

    @Test func aHumanPromptAnswersTheNotifiedTurn() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
        let changes = detector.feed(row("a", "running", kind: "human"))
        #expect(changes.answered == ["a"])
        #expect(changes.finished.isEmpty)
        // Once answered, the next polls are quiet until the next finish.
        #expect(detector.feed(row("a", "running", kind: "human")).isEmpty)
        #expect(detector.feed(row("a", finished: t2)).finishedKeys == ["a"])
    }

    @Test func endingTheSessionAnswersTheNotifiedTurn() {
        // SessionEnd nulls turn_finished_at while the prompt kind stays
        // human: quitting the session removes its delivered notification,
        // whatever state the ended row lands in.
        for state in ["review", "done", "unconcluded"] {
            var detector = primed(row("a", "running"))
            #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
            let ended = detector.feed(row("a", state, finished: nil, kind: "human"))
            #expect(ended.answered == ["a"], "state \(state)")
            #expect(ended.finished.isEmpty)
            #expect(detector.feed(row("a", state, finished: nil, kind: "human")).isEmpty)
        }
    }

    @Test func anInjectedPromptDoesNotAnswer() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
        #expect(detector.feed(row("a", "running", kind: "injected")).isEmpty)
    }

    @Test func aNewerTurnSeenDirectlyIsFinishedNotAnswered() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
        let changes = detector.feed(row("a", finished: t2))
        #expect(changes.finishedKeys == ["a"])
        #expect(changes.answered.isEmpty)
    }

    @Test func aFlapIsNeitherFinishedNorAnswered() {
        var detector = primed(row("a", "running"))
        #expect(detector.feed(row("a", finished: t1)).finishedKeys == ["a"])
        let flap = detector.feed(row("a", "running", finished: t1))
        #expect(flap.finished.isEmpty)
        #expect(flap.answered.isEmpty)
    }
}
