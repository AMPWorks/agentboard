import AppKit
import Testing
@testable import AgentBoard

@Suite("UsageLine")
struct UsageLineTests {
    let now = Date(timeIntervalSince1970: 1_800_000_000)
    let posix = Locale(identifier: "en_US_POSIX")
    let utc = TimeZone(identifier: "UTC")!

    private func iso(_ offset: TimeInterval) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: now.addingTimeInterval(offset))
    }

    private func source(
        _ windows: [UsageWindow], label: String = "Claude", updatedAt: String? = nil,
        origin: String? = "~/.claude/rate-limits.json"
    ) -> UsageSource {
        UsageSource(source: label.lowercased(), label: label, updatedAt: updatedAt,
                    origin: origin, windows: windows)
    }

    private func w(_ label: String, _ pct: Double, resets: TimeInterval? = nil) -> UsageWindow {
        UsageWindow(label: label, usedPercentage: pct, resetsAt: resets.map(iso))
    }

    @Test func windowsKeepTheSourceOrderAndLabels() {
        let s = source([w("7d", 70, resets: 3 * 86_400), w("5h", 57, resets: 6_900),
                        w("spend limit", 10), w("zeta window", 5)])
        #expect(UsageLine.shownWindows(s, now: now).map(\.label)
            == ["7d", "5h", "spend limit", "zeta window"])
        #expect(UsageLine.text(s, now: now)
            == "Claude · 7d 70% · 5h 57% · spend limit 10% · zeta window 5% · resets in 1h 55m")
    }

    @Test func eachSourceIsLabelledByItsOwnLabel() {
        let codex = source([w("5h", 12)], label: "Codex", origin: nil)
        let acme = source([w("daily", 3)], label: "Acme agent", origin: nil)
        #expect(UsageLine.text(codex, now: now) == "Codex · 5h 12%")
        #expect(UsageLine.text(acme, now: now) == "Acme agent · daily 3%")
        #expect(UsageLine.detail(acme, now: now, timeZone: utc, locale: posix)
            .hasSuffix("Reported by Acme agent."))
    }

    @Test func shownSourcesSkipThoseWithNothingLeft() {
        let expired = source([w("5h", 57, resets: -60)], label: "Old")
        let live = source([w("5h", 12)], label: "Codex")
        #expect(UsageLine.shownSources([expired, live], now: now).map(\.label) == ["Codex"])
        #expect(UsageLine.shownSources([], now: now).isEmpty)
    }

    @Test func expiredWindowIsOmitted() {
        let s = source([w("5h", 57, resets: -60), w("7d", 70, resets: 2 * 3_600 + 5 * 60)])
        #expect(UsageLine.text(s, now: now) == "Claude · 7d 70% · resets in 2h 5m")
    }

    @Test func allExpiredIsNil() {
        let s = source([w("5h", 57, resets: -60), w("7d", 70, resets: 0)])
        #expect(UsageLine.text(s, now: now) == nil)
        #expect(UsageLine.text(source([]), now: now) == nil)
    }

    @Test func soonestResetIsNamed() {
        let s = source([w("5h", 1, resets: 4 * 3_600), w("7d", 2, resets: 30 * 60)])
        #expect(UsageLine.text(s, now: now)?.hasSuffix("resets in 30m") == true)
    }

    @Test func noResetTimeMeansNoResetPart() {
        #expect(UsageLine.text(source([w("5h", 33.25)]), now: now) == "Claude · 5h 33.3%")
    }

    @Test func spendWindowWithDollars() {
        let s = source([UsageWindow(label: "spend limit", usedPercentage: 62.8, usedUsd: 314.2,
                                    limitUsd: 500, period: "monthly")])
        #expect(UsageLine.text(s, now: now) == "Claude · spend limit $314 of $500")
        #expect(UsageLine.detail(s, now: now, timeZone: utc, locale: posix)
            .hasPrefix("Claude spend limit: 62.8% used ($314 of $500, monthly)."))
    }

    @Test func spendOverTheLimitShowsAboveHundred() {
        let s = source([w("spend limit", 130)])
        #expect(UsageLine.text(s, now: now) == "Claude · spend limit 130%")
        #expect(UsageLine.tint(s, now: now) == .critical)
    }

    @Test func tints() {
        func tint(_ pct: Double) -> UsageLine.Tint {
            UsageLine.tint(source([w("5h", pct), w("7d", 10)]), now: now)
        }
        #expect(tint(79.9) == .normal)
        #expect(tint(80) == .high)
        #expect(tint(94.9) == .high)
        #expect(tint(95) == .critical)
        // An expired window does not tint.
        #expect(UsageLine.tint(source([w("5h", 99, resets: -1), w("7d", 10)]), now: now) == .normal)
        #expect(UsageLine.tint(source([]), now: now) == .normal)
    }

    @Test func detailNamesResetTimesAndTheOrigin() {
        let s = source([w("5h", 57, resets: 6_900), w("7d", 70, resets: -60)],
                       updatedAt: iso(-300))
        let detail = UsageLine.detail(s, now: now, timeZone: utc, locale: posix)
        let lines = detail.split(separator: "\n").map(String.init)
        #expect(lines.count == 3)
        #expect(lines[0].hasPrefix("Claude 5h: 57% used, resets "))
        #expect(lines[1].hasPrefix("Claude 7d: reset at "))
        #expect(lines[2].hasPrefix("Read from ~/.claude/rate-limits.json, updated "))
        #expect(lines[2].contains("5 minutes ago"))
    }

    @Test func staleNumbersShowTheirAge() {
        let windows = [w("5h", 57, resets: 6_900)]
        let stale = source(windows, label: "Codex", updatedAt: iso(-3 * 3_600))
        #expect(UsageLine.text(stale, now: now) == "Codex as of 3h ago · 5h 57% · resets in 1h 55m")
        let detail = UsageLine.detail(stale, now: now, timeZone: utc, locale: posix)
        #expect(detail.split(separator: "\n").last?
            .hasPrefix("These numbers are only as fresh as Codex's last report") == true)
        #expect(UsageLine.staleAge(source(windows, updatedAt: iso(-2 * 86_400 - 60)), now: now)
            == "2d")
        // Within the hour (or no updated_at): the line is unchanged.
        for fresh in [source(windows, updatedAt: iso(-3_600)), source(windows)] {
            #expect(UsageLine.text(fresh, now: now) == "Claude · 5h 57% · resets in 1h 55m")
            #expect(!UsageLine.detail(fresh, now: now, timeZone: utc, locale: posix)
                .contains("only as fresh"))
        }
    }

    @Test func durations() {
        #expect(UsageLine.duration(10) == "1m")
        #expect(UsageLine.duration(59 * 60) == "59m")
        #expect(UsageLine.duration(3_600) == "1h 0m")
        #expect(UsageLine.duration(26 * 3_600 + 90) == "1d 2h")
    }
}

@Suite("ModelName and ContextSize")
struct ModelNameTests {
    @Test func shortNames() {
        #expect(ModelName.short("claude-opus-5-5") == "Opus 5.5")
        #expect(ModelName.short("claude-fable-5-1") == "Fable 5.1")
        #expect(ModelName.short("claude-sonnet-4-5-20250929") == "Sonnet 4.5")
        #expect(ModelName.short("claude-opus-4-20250514") == "Opus 4")
        #expect(ModelName.short("claude-3-5-haiku-20241022") == "Haiku 3.5")
        #expect(ModelName.short("claude-opus-5-5[1m]") == "Opus 5.5 [1m]")
    }

    @Test func passThrough() {
        #expect(ModelName.short("gpt-5-codex") == "gpt-5-codex")
        #expect(ModelName.short("claude-") == "claude-")
        #expect(ModelName.short("claude-opus") == "claude-opus")
        #expect(ModelName.short("claude-opus-5-5-x1") == "claude-opus-5-5-x1")
        #expect(ModelName.short("claude-5-opus-5") == "claude-5-opus-5")
        #expect(ModelName.short("") == "")
        // `claude-` only counts at a boundary.
        #expect(ModelName.short("myclaude-opus-5-5") == "myclaude-opus-5-5")
    }

    @Test func providerWrappersAreStripped() {
        #expect(ModelName.short("us.anthropic.claude-sonnet-4-5-20250929-v1:0") == "Sonnet 4.5")
        #expect(ModelName.short("anthropic.claude-opus-4-1-20250805-v1:0") == "Opus 4.1")
        #expect(ModelName.short("us.anthropic.claude-sonnet-4-5-20250929-v1:0[1m]") == "Sonnet 4.5 [1m]")
        #expect(ModelName.short("claude-opus-4-1@20250805[1m]") == "Opus 4.1 [1m]")
        #expect(ModelName.short("claude-opus-4-1@20250805") == "Opus 4.1")
        #expect(ModelName.short("claude-3-5-sonnet-v2@20241022") == "Sonnet 3.5 v2")
        #expect(ModelName.short(
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
                + "us.anthropic.claude-sonnet-4-5-20250929-v1:0") == "Sonnet 4.5")
        // Unwrapped but not shortenable: the bare claude- id, not the wrapper.
        #expect(ModelName.short("us.anthropic.claude-instant-v1") == "claude-instant-v1")
    }

    @Test func longModelIdsAreCappedOnTheRow() {
        let arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123xyz"
        let session = Fixtures.decoded("a", extra: ["model": arn, "context_tokens": 308_011])
        let text = SessionModelTag.text(session)
        #expect(text == "arn:aws:bed…/abc123xyz · 308k")
        #expect(SessionModelTag.help(session).hasPrefix("Model \(arn);"))
        #expect(SessionModelTag.middleTruncated("Opus 5.5", to: 22) == "Opus 5.5")
        #expect(SessionModelTag.middleTruncated("abcdefghij", to: 5) == "ab…ij")
        #expect(SessionModelTag.middleTruncated(arn, to: 22).count == 22)
    }

    @Test func contextSizeBoundaries() {
        #expect(ContextSize.short(0) == "0")
        #expect(ContextSize.short(950) == "950")
        #expect(ContextSize.short(999) == "999")
        #expect(ContextSize.short(1_000) == "1k")
        #expect(ContextSize.short(9_500) == "9.5k")
        #expect(ContextSize.short(9_960) == "10k")
        #expect(ContextSize.short(10_000) == "10k")
        #expect(ContextSize.short(308_011) == "308k")
        #expect(ContextSize.short(999_400) == "999k")
        #expect(ContextSize.short(999_600) == "1M")
        #expect(ContextSize.short(1_200_000) == "1.2M")
        #expect(ContextSize.full(308_011, locale: Locale(identifier: "en_US")) == "308,011")
    }

    @Test func sessionTag() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let formatter = ISO8601DateFormatter()
        let at = formatter.string(from: now.addingTimeInterval(-12 * 60))
        let session = Fixtures.decoded("a", extra: [
            "model": "claude-opus-5-5", "context_tokens": 308_011, "context_at": at,
        ])
        #expect(SessionModelTag.text(session) == "Opus 5.5 · 308k")
        let help = SessionModelTag.help(session, now: now, locale: Locale(identifier: "en_US"))
        #expect(help.hasPrefix("Model claude-opus-5-5; context 308,011 tokens, as of 12"))
        let modelOnly = Fixtures.decoded("b", extra: ["model": "gpt-5-codex"])
        #expect(SessionModelTag.text(modelOnly) == "gpt-5-codex")
        #expect(SessionModelTag.help(modelOnly) == "Model gpt-5-codex")
        let contextOnly = Fixtures.decoded("c", extra: ["context_tokens": 51_200])
        #expect(SessionModelTag.text(contextOnly) == "Context 51k")
        #expect(SessionModelTag.help(contextOnly).hasPrefix("Context 51"))
        #expect(SessionModelTag.text(Fixtures.decoded("d")) == nil)
    }
}
