import AppKit
import Testing
@testable import AgentBoard

@Suite("Display title")
struct SessionDisplayTests {
    @Test func statusTextLeadsAndTheTitleBecomesTheSubtitle() {
        let session = Fixtures.decoded("s1", extra: ["title": "PR #4 author investigation",
                                                     "status_text": "  Handler: reviewing PR #9 "])
        #expect(session.displayTitle == "Handler: reviewing PR #9")
        #expect(SessionDisplay.primaryTitle(session) == "Handler: reviewing PR #9")
        #expect(SessionDisplay.secondaryTitle(session) == "PR #4 author investigation")
    }

    @Test func withoutStatusTextTheTitleIsTheOnlyLine() {
        for status: Any in [NSNull(), "", "   \n"] {
            let session = Fixtures.decoded("s1", extra: ["title": "Fix the hook", "status_text": status])
            #expect(session.displayTitle == "Fix the hook")
            #expect(SessionDisplay.secondaryTitle(session) == nil)
        }
    }

    @Test func aStatusEqualToTheTitleIsNotRepeated() {
        let session = Fixtures.decoded("s1", extra: ["title": "Same", "status_text": "Same"])
        #expect(SessionDisplay.secondaryTitle(session) == nil)
    }

    @Test func searchMatchesBothTheStatusAndTheTitle() {
        let session = Fixtures.decoded("s1", extra: ["title": "Old summary", "status_text": "Current work"])
        #expect(SessionSearch("current").filter([session]).count == 1)
        #expect(SessionSearch("summary").filter([session]).count == 1)
    }
}

@Suite("PR buttons")
struct PRButtonsTests {
    private func pr(_ number: Int, _ state: String?, awaits: Bool? = nil, url: Bool = true) -> Link {
        Link(kind: "pr", ref: "#\(number)", url: url ? "https://github.com/a/b/pull/\(number)" : nil,
             state: state, awaitsYou: awaits)
    }

    @Test func ordersAwaitingThenOpenMergedClosedNewestFirst() {
        let links = [pr(1, "MERGED"), pr(2, "CLOSED"), pr(3, "OPEN"), pr(4, "OPEN", awaits: true),
                     pr(5, "OPEN"), pr(6, "MERGED")]
        let layout = PRButtons.layout(for: links, cap: 10)
        #expect(layout.shown.map(\.ref) == ["#4", "#5", "#3", "#6", "#1", "#2"])
        #expect(layout.overflow == 0)
    }

    @Test func capsAtThreeWithAPlusN() {
        let links = (1...5).map { pr($0, "OPEN") }
        let layout = PRButtons.layout(for: links)
        #expect(layout.shown.map(\.ref) == ["#5", "#4", "#3"])
        #expect(layout.overflow == 2)
        #expect(PRButtons.layout(for: Array(links.prefix(3))).overflow == 0)
    }

    @Test func skipsNonPRLinksAndPRsWithoutAURL() {
        let links = [Link(kind: "ticket", ref: "ABC-1", url: "https://t/ABC-1"),
                     pr(7, "OPEN", url: false), pr(8, "OPEN")]
        let layout = PRButtons.layout(for: links)
        #expect(layout.shown.map(\.ref) == ["#8"])
        #expect(layout.overflow == 0)
    }

    @Test func prShortLabelDropsTheRepo() {
        #expect(Link(kind: "pr", ref: "#42").shortLabel == "#42")
        #expect(Link(kind: "pr", ref: "acme/widgets#7").shortLabel == "#7")
        #expect(Link(kind: "pr", ref: "acme/widgets").shortLabel == "acme/widgets")
    }

    @Test func prNumberParsing() {
        #expect(Link(kind: "pr", ref: "#42").number == 42)
        #expect(Link(kind: "pr", ref: "acme/widgets#7").number == 7)
        #expect(Link(kind: "pr", ref: "12").number == 12)
        #expect(Link(kind: "pr", ref: "#x").number == nil)
    }

    @Test func linksFromAnOlderCLIDecodeWithoutTheNewFields() {
        let session = Fixtures.decoded("s1", extra: ["links": [
            ["kind": "pr", "ref": "#1", "url": "https://github.com/a/b/pull/1", "state": "OPEN", "title": "t"],
        ]])
        let link = session.links[0]
        #expect(link.branch == nil)
        #expect(link.owned == nil)
        #expect(link.awaitsYou == nil)
        #expect(PRButtons.layout(for: session.links).shown.count == 1)
    }

    @Test func newLinkFieldsDecode() {
        let session = Fixtures.decoded("s1", extra: ["links": [
            ["kind": "pr", "ref": "#2", "url": "https://github.com/a/b/pull/2", "state": "OPEN",
             "branch": "feature-x", "owned": true, "awaits_you": true],
        ]])
        let link = session.links[0]
        #expect(link.branch == "feature-x")
        #expect(link.owned == true)
        #expect(link.awaitsYou == true)
    }

    @Test func fixtureSessionWithSeveralPRs() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/snapshot.json")
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
        let review = try #require(snapshot.sessions.first { $0.key == "claude:sess-review-2" })
        let layout = PRButtons.layout(for: review.links)
        #expect(layout.shown.first?.ref == "#231")
        #expect(layout.shown.first?.awaitsYou == true)
        #expect(layout.shown.count == 3)
        #expect(layout.overflow == 1)
        let running = try #require(snapshot.sessions.first { $0.key == "claude:sess-running-1" })
        #expect(SessionDisplay.secondaryTitle(running) == running.title)
        #expect(snapshot.sessions.filter { $0.state == .review }.allSatisfy { !($0.waitingReason ?? "").isEmpty })
    }
}
