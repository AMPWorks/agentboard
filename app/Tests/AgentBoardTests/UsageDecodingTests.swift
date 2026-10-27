import AppKit
import Testing
@testable import AgentBoard

@Suite("Usage and model decoding")
struct UsageDecodingTests {
    private let validUsage: [[String: Any]] = [
        [
            "source": "claude", "label": "Claude",
            "updated_at": "2026-10-24T11:34:03Z", "origin": "~/.claude/rate-limits.json",
            "windows": [
                ["label": "5h", "used_percentage": 57, "resets_at": "2026-10-24T13:30:00Z"],
                ["label": "7d", "used_percentage": 70.5, "resets_at": "2026-10-27T17:00:00Z"],
            ],
        ],
        [
            "source": "codex", "label": "Codex", "updated_at": "2026-10-24T11:00:00Z",
            "windows": [["label": "5h", "used_percentage": 12]],
        ],
    ]

    @Test func snapshotWithUsageDecodes() {
        let snapshot = Fixtures.snapshot([Fixtures.session("a", "running")], usage: validUsage)
        #expect(snapshot.sessions.count == 1)
        #expect(snapshot.usage.map(\.source) == ["claude", "codex"])
        let claude = snapshot.usage[0]
        #expect(claude.label == "Claude")
        #expect(claude.updatedAt == "2026-10-24T11:34:03Z")
        #expect(claude.origin == "~/.claude/rate-limits.json")
        #expect(claude.windows.map(\.label) == ["5h", "7d"])
        #expect(claude.windows[1].usedPercentage == 70.5)
        #expect(claude.windows[1].resetsDate != nil)
        #expect(snapshot.usage[1].origin == nil)
    }

    @Test func snapshotWithoutUsageDecodes() {
        let snapshot = Fixtures.snapshot([Fixtures.session("a", "running")])
        #expect(snapshot.usage.isEmpty)
        #expect(snapshot.sessions.count == 1)
    }

    @Test func nullOrMalformedUsageNeverBlanksTheBoard() throws {
        for usage in ["null", #""surprise""#, "{}", #"{"source": "claude"}"#, "[1, null, \"x\"]"] {
            let json = #"{"generated_at": "x", "counts": {}, "sessions": [], "usage": "#
                + usage + "}"
            let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
            #expect(snapshot.usage.isEmpty, "usage: \(usage)")
        }
    }

    @Test func aBadSourceOrWindowIsDroppedOthersKept() {
        let usage: [Any] = [
            ["label": "no source name", "windows": [["label": "5h", "used_percentage": 1]]],
            "not an object",
            NSNull(),
            [
                "source": "acme",
                "windows": [
                    ["label": "5h", "used_percentage": "57"],
                    ["used_percentage": 10],
                    ["label": "7d", "used_percentage": 70],
                    ["label": "spend", "used_percentage": 62.8, "used_usd": 314.2,
                     "limit_usd": 500, "period": "monthly"],
                ],
            ],
        ]
        let snapshot = Fixtures.snapshot([Fixtures.session("a", "running")], usage: usage)
        #expect(snapshot.sessions.count == 1)
        #expect(snapshot.usage.count == 1)
        let acme = snapshot.usage[0]
        #expect(acme.label == "acme", "label falls back to the source name")
        #expect(acme.windows.map(\.label) == ["7d", "spend"])
        #expect(acme.windows[1].usedUsd == 314.2)
        #expect(acme.windows[1].limitUsd == 500)
        #expect(acme.windows[1].period == "monthly")
    }

    @Test func sessionFieldsDecode() {
        let session = Fixtures.decoded("a", extra: [
            "model": "claude-opus-5-5", "context_tokens": 308_011,
            "context_at": "2026-10-24T11:00:00.123Z",
        ])
        #expect(session.model == "claude-opus-5-5")
        #expect(session.contextTokens == 308_011)
        #expect(session.contextDate != nil)
    }

    @Test func sessionFieldsAbsentOrNull() {
        let absent = Fixtures.decoded("a")
        #expect(absent.model == nil && absent.contextTokens == nil && absent.contextAt == nil)
        let null = Fixtures.decoded("b", extra: [
            "model": NSNull(), "context_tokens": NSNull(), "context_at": NSNull(),
        ])
        #expect(null.model == nil && null.contextTokens == nil)
    }

    @Test func malformedSessionFieldDropsOnlyThatField() {
        let session = Fixtures.decoded("a", extra: ["model": 5, "context_tokens": "big"])
        #expect(session.model == nil)
        #expect(session.contextTokens == nil)
        #expect(session.key == "a")
    }

    @Test func contextOnlyChangeIsAChange() {
        let before = Fixtures.decoded("a", extra: ["context_tokens": 1000])
        let after = Fixtures.decoded("a", extra: ["context_tokens": 2000])
        #expect(before != after)
        let model = Fixtures.decoded("a", extra: ["model": "claude-fable-5-1"])
        #expect(Fixtures.decoded("a") != model)
        let at = Fixtures.decoded("a", extra: ["context_at": "2026-10-24T11:00:00Z"])
        #expect(Fixtures.decoded("a") != at)
        #expect(Fixtures.decoded("a", extra: ["context_tokens": 1000]) == before)
    }

    @Test func emptySnapshotHasNoUsage() {
        #expect(Snapshot.empty.usage.isEmpty)
    }
}
