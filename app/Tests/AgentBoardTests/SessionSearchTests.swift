import AppKit
import Testing
@testable import AgentBoard

@Suite("SessionSearch")
struct SessionSearchTests {
    /// A session with every searchable field set to a distinct word.
    private static let rich = Fixtures.decoded("rich", "needs_you", extra: [
        "tool": "codex",
        "session_id": "sess-7f3a",
        "title": "Tidy the reconcile loop",
        "first_prompt": "please investigate the flaky retry",
        "last_prompt": "now add a regression test",
        "last_assistant": "I rewrote the backoff helper",
        "cwd": "/Users/me/src/widgetfactory",
        "project": "widgetfactory",
        "branch": "fix-retry-flake",
        "waiting_reason": "permission prompt for Bash",
        "status_text": "awaiting approval",
        "links": [
            ["kind": "pr", "ref": "#42", "url": "https://example.test/pulls/42",
             "state": "open", "title": "Schema pragma migration"],
            ["kind": "ticket", "ref": "ABC-123", "title": "Retry storm", "state": "triaged"],
        ],
        "triage": ["status": "open", "note": "ask Dana about quotas"],
    ])

    private func matches(_ query: String, _ session: Session = rich) -> Bool {
        SessionSearch(query).matches(session)
    }

    // MARK: Plain terms reach every field

    @Test(arguments: [
        "reconcile",          // title
        "flaky",              // first prompt
        "regression",         // last prompt
        "backoff",            // last assistant
        "widgetfactory",      // project
        "/Users/me/src",      // cwd
        "retry-flake",        // branch
        "codex",              // tool
        "needs_you",          // state raw value
        "\"needs you\"",      // state display name, as a phrase
        "permission",         // waiting reason
        "approval",           // status text
        "pr",                 // link kind
        "#42",                // link ref
        "abc-123",            // ticket ref
        "storm",              // link title
        "triaged",            // link state
        "dana",               // triage note
        "sess-7f3a",          // session id
    ])
    func plainTermMatchesField(_ query: String) {
        #expect(matches(query))
    }

    @Test func plainTermDoesNotSearchLinkURLs() {
        #expect(!matches("example.test"))
        #expect(matches("pr:example.test"))
    }

    @Test func unrelatedTermDoesNotMatch() {
        #expect(!matches("zzzz"))
    }

    // MARK: Semantics

    @Test func emptyAndWhitespaceQueriesMatchEverything() {
        for query in ["", "   ", "\t\n", "\"\""] {
            let search = SessionSearch(query)
            #expect(search.isEmpty)
            #expect(search.matches(Self.rich))
        }
    }

    @Test func everyTermMustMatch() {
        #expect(matches("flaky dana"))
        #expect(!matches("flaky zzzz"))
    }

    @Test func termsMayMatchDifferentFields() {
        #expect(matches("codex widgetfactory #42"))
    }

    @Test func quotedPhraseStaysTogether() {
        #expect(SessionSearch("\"reconcile loop\"").terms.count == 1)
        #expect(matches("\"reconcile loop\""))
        // Both words appear, but never as this phrase.
        #expect(matches("loop reconcile"))
        #expect(!matches("\"loop reconcile\""))
    }

    @Test func unclosedQuoteRunsToTheEnd() {
        let search = SessionSearch("\"reconcile loop")
        #expect(search.terms == [.init(field: nil, text: "reconcile loop")])
    }

    @Test func phraseNeverSpansTwoFields() {
        // title ends "...loop", first prompt starts "please..."
        #expect(!matches("\"loop please\""))
    }

    @Test func caseAndDiacriticsAreFolded() {
        let cafe = Fixtures.decoded("c", extra: ["title": "Café Résumé parser"])
        #expect(matches("cafe", cafe))
        #expect(matches("CAFÉ", cafe))
        #expect(matches("resume", cafe))
        #expect(matches("RECONCILE"))
        #expect(matches("Ŕeconcile"))
    }

    // MARK: Qualifiers

    @Test func repoAndProjectQualifiers() {
        #expect(matches("repo:widget"))
        #expect(matches("project:widget"))
        #expect(matches("repo:/users/me"))      // cwd
        #expect(!matches("repo:reconcile"))     // only in the title
    }

    @Test func pathQualifierSearchesCwdOnly() {
        #expect(matches("path:src/widget"))
        let other = Fixtures.decoded("o", extra: ["cwd": "/tmp/x", "project": "widgetfactory"])
        #expect(!matches("path:widget", other))
    }

    @Test func branchQualifier() {
        #expect(matches("branch:fix-retry"))
        #expect(!matches("branch:flaky"))   // in a prompt, not the branch
        let noBranch = Fixtures.decoded("n")
        #expect(!matches("branch:fix", noBranch))
    }

    @Test func quotedQualifierValue() {
        let spaced = Fixtures.decoded("s", extra: ["branch": "feat x"])
        #expect(SessionSearch("branch:\"feat x\"").terms == [.init(field: .branch, text: "feat x")])
        #expect(matches("branch:\"feat x\"", spaced))
        #expect(!matches("branch:\"feat y\"", spaced))
    }

    @Test func toolQualifier() {
        #expect(matches("tool:codex"))
        #expect(!matches("tool:claude"))
    }

    @Test func stateQualifierMatchesRawAndDisplayName() {
        #expect(matches("state:needs"))
        #expect(matches("state:needs_you"))
        #expect(matches("state:\"needs you\""))
        #expect(!matches("state:review"))
        let review = Fixtures.decoded("r", "review")
        #expect(matches("state:review", review))
    }

    @Test func prQualifierSearchesOnlyPRLinks() {
        #expect(matches("pr:42"))
        #expect(matches("pr:#42"))
        #expect(matches("pr:pragma"))            // PR title
        #expect(!matches("pr:ABC-123"))          // a ticket, not a PR
    }

    @Test func linkQualifierSearchesEveryLink() {
        #expect(matches("link:ABC-123"))
        #expect(matches("link:ticket"))          // kind
        #expect(matches("link:\"PR\""))          // kind label via quoted value
        #expect(matches("link:triaged"))         // state
        #expect(matches("link:example.test"))    // url
        #expect(!matches("link:reconcile"))
    }

    @Test func qualifierNamesAreCaseInsensitive() {
        #expect(SessionSearch("Branch:fix").terms == [.init(field: .branch, text: "fix")])
    }

    @Test func unknownQualifierIsPlainText() {
        #expect(SessionSearch("foo:bar").terms == [.init(field: nil, text: "foo:bar")])
        let colon = Fixtures.decoded("c", extra: ["last_prompt": "set foo:bar in the config"])
        #expect(matches("foo:bar", colon))
        #expect(!matches("foo:bar"))
    }

    @Test func emptyQualifierValueIsPlainText() {
        #expect(SessionSearch("branch:").terms == [.init(field: nil, text: "branch:")])
        #expect(SessionSearch("branch:\"\"").terms == [.init(field: nil, text: "branch:")])
        let colon = Fixtures.decoded("c", extra: ["title": "rename branch: main to trunk"])
        #expect(matches("branch:", colon))
    }

    @Test func spaceAfterQualifierColonIsIgnored() {
        #expect(SessionSearch("branch: main").terms == [.init(field: .branch, text: "main")])
        #expect(SessionSearch("repo:  agentboard").terms == [.init(field: .repo, text: "agentboard")])
        #expect(SessionSearch("state: \"needs you\"").terms == [.init(field: .state, text: "needs you")])
        #expect(SessionSearch("Branch:\tfix").terms == [.init(field: .branch, text: "fix")])
        #expect(SessionSearch("branch: fix flaky").terms
            == [.init(field: .branch, text: "fix"), .init(field: nil, text: "flaky")])
        #expect(matches("branch: fix-retry"))
        #expect(!matches("branch: flaky"))       // in a prompt, not the branch
        #expect(matches("repo:  widget"))
        #expect(matches("state: \"needs you\""))
        #expect(!matches("tool: claude"))
    }

    @Test func spacedFormMatchesLikeTheUnspacedForm() {
        let main = Fixtures.decoded("m", extra: ["branch": "main"])
        let other = Fixtures.decoded("o", extra: ["branch": "feature", "title": "merge main"])
        for query in ["branch:main", "branch: main"] {
            #expect(matches(query, main))
            #expect(!matches(query, other))
        }
    }

    @Test func unknownQualifierWithSpaceStaysPlainText() {
        #expect(SessionSearch("foo: bar").terms == [.init(field: nil, text: "foo:"), .init(field: nil, text: "bar")])
    }

    @Test func bareQualifierAtTheEndOrBeforeAnotherQualifierIsPlainText() {
        #expect(SessionSearch("main branch:").terms == [.init(field: nil, text: "main"), .init(field: nil, text: "branch:")])
        #expect(SessionSearch("branch:  ").terms == [.init(field: nil, text: "branch:")])
        // The next term is a qualifier of its own, so it is not taken as the value.
        #expect(SessionSearch("branch: tool:codex").terms
            == [.init(field: nil, text: "branch:"), .init(field: .tool, text: "codex")])
        // An explicitly empty quoted value does not reach for the next term.
        #expect(SessionSearch("branch:\"\" main").terms
            == [.init(field: nil, text: "branch:"), .init(field: nil, text: "main")])
        // An empty quoted phrase after the colon is not a value.
        #expect(SessionSearch("branch: \"\"").terms == [.init(field: nil, text: "branch:")])
    }

    @Test func bareQualifierTakesAnUnknownQualifierOrQuotedColonAsItsValue() {
        // Neither is a known qualifier, so the lookahead consumes it as the value.
        #expect(SessionSearch("branch: foo:bar").terms == [.init(field: .branch, text: "foo:bar")])
        #expect(SessionSearch("branch: \"tool:codex\"").terms == [.init(field: .branch, text: "tool:codex")])
    }

    @Test func quotedColonIsNotAQualifier() {
        #expect(SessionSearch("\"branch:fix\"").terms == [.init(field: nil, text: "branch:fix")])
    }

    @Test func textBeforeTheFirstQuoteJoinsThePhrase() {
        #expect(SessionSearch("fe\"at x\"").terms == [.init(field: nil, text: "feat x")])
    }

    @Test func separateAndAdjacentQuotedRuns() {
        #expect(SessionSearch("\"a\" \"b\"").terms == [.init(field: nil, text: "a"), .init(field: nil, text: "b")])
        // Adjacent runs with no space between them form one token.
        #expect(SessionSearch("\"a\"\"b\"").terms == [.init(field: nil, text: "ab")])
    }

    @Test func quotedQualifierNameIsPlainText() {
        // The colon follows the first quote, so the name is literal text.
        #expect(SessionSearch("\"branch\":fix").terms == [.init(field: nil, text: "branch:fix")])
    }

    @Test func qualifiedAndPlainTermsCombine() {
        #expect(matches("tool:codex flaky branch:retry"))
        #expect(!matches("tool:claude flaky"))
    }

    // MARK: Collections

    @Test func filterAppliesPickersAndQuery() {
        let sessions = Fixtures.snapshot([
            Fixtures.session("a", "running", extra: ["tool": "claude", "project": "p1", "title": "alpha"]),
            Fixtures.session("b", "running", extra: ["tool": "codex", "project": "p1", "title": "beta"]),
            Fixtures.session("c", "running", extra: ["tool": "codex", "project": "p2", "title": "alpha"]),
        ]).sessions
        let keys = { (s: [Session]) in s.map(\.key) }
        #expect(keys(SessionSearch("").filter(sessions)) == ["a", "b", "c"])
        #expect(keys(SessionSearch("").filter(sessions, tool: "codex")) == ["b", "c"])
        #expect(keys(SessionSearch("").filter(sessions, project: "p1")) == ["a", "b"])
        #expect(keys(SessionSearch("alpha").filter(sessions)) == ["a", "c"])
        #expect(keys(SessionSearch("alpha").filter(sessions, tool: "codex", project: "p2")) == ["c"])
    }

    // MARK: Notes

    @Test func noteTextMatchesItsSession() {
        let session = Fixtures.decoded("n", "review", extra: [
            "notes": [["id": 1, "kind": "update", "text": "Deployed PR #314 to staging",
                       "ts": "2026-10-15T00:00:00Z", "last_ts": "2026-10-15T00:00:00Z"]],
            "notes_count": 1,
        ])
        #expect(matches("#314", session))
        #expect(matches("staging", session))
        #expect(!matches("staging", Fixtures.decoded("plain", "review")))
    }
}
