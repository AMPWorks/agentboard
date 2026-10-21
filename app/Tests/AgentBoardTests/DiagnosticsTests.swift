import AppKit
import Testing
@testable import AgentBoard

@Suite("ArgumentRedactor")
struct ArgumentRedactorTests {
    @Test(arguments: ["--note", "--text", "--title"])
    func redactsFlagValueInBothForms(flag: String) {
        #expect(ArgumentRedactor.redact(["mark", flag, "hello world"]) == ["mark", flag, "<redacted:11 chars>"])
        #expect(ArgumentRedactor.redact(["mark", "\(flag)=secret"]) == ["mark", "\(flag)=<redacted:6 chars>"])
        #expect(ArgumentRedactor.redact(["mark", "\(flag)="]) == ["mark", "\(flag)=<redacted:0 chars>"])
    }

    @Test func keepsToolsIdsPathsAndFlagsVerbatim() {
        let argv = ["focus", "claude", "0f7c-abc", "--hours", "4", "/Users/me/some path", "--json"]
        #expect(ArgumentRedactor.redact(argv) == argv)
    }

    @Test func trailingFlagWithoutValueIsKept() {
        #expect(ArgumentRedactor.redact(["status", "--text"]) == ["status", "--text"])
    }

    @Test func truncatesOnlyArgumentsLongerThan200() {
        let exactly200 = String(repeating: "a", count: 200)
        let long = String(repeating: "b", count: 201)
        let redacted = ArgumentRedactor.redact([exactly200, long])
        #expect(redacted[0] == exactly200)
        #expect(redacted[1] == String(repeating: "b", count: 80) + "...")
    }

    @Test func otherFlagsWithEqualsAreNotRedacted() {
        #expect(ArgumentRedactor.redact(["--session=claude:abc"]) == ["--session=claude:abc"])
    }
}

@Suite("CLIInvocationRecord")
struct CLIInvocationRecordTests {
    @Test func messageHasArgumentsExitCodeAndDuration() {
        let record = CLIInvocationRecord(
            arguments: ["focus", "claude", "abc"], exitCode: 0,
            duration: .milliseconds(412), stderrTail: ""
        )
        #expect(record.message == #"args="focus claude abc" exit=0 ms=412"#)
    }

    @Test func messageRedactsArgumentsAndIncludesStderr() {
        let record = CLIInvocationRecord(
            arguments: ["mark", "claude", "abc", "done", "--note", "my private note"], exitCode: 1,
            duration: .seconds(2), stderrTail: "error: something broke\n"
        )
        let message = record.message
        #expect(message.contains("<redacted:15 chars>"))
        #expect(!message.contains("my private note"))
        #expect(message.contains("exit=1"))
        #expect(message.contains("ms=2000"))
        #expect(message.hasSuffix(#"stderr="error: something broke""#))
    }

    @Test func stderrKeepsOnlyTheLast512Bytes() {
        let stderr = String(repeating: "x", count: 1000) + "THE-END"
        let record = CLIInvocationRecord(
            arguments: ["snapshot", "--json"], exitCode: 2, duration: .zero, stderrTail: stderr
        )
        let tail = CLIInvocationRecord.tail(stderr, bytes: CLIInvocationRecord.stderrTailBytes)
        #expect(tail.utf8.count == 512)
        #expect(tail.hasSuffix("THE-END"))
        #expect(record.message.contains("stderr=\"\(tail)\""))
    }

    @Test func multilineStderrIsEscapedOntoOneLine() {
        let record = CLIInvocationRecord(
            arguments: ["focus", "claude", "abc"], exitCode: 1, duration: .zero,
            stderrTail: "line one\nline \"two\""
        )
        #expect(!record.message.contains("\n"))
        #expect(record.message.contains(#"stderr="line one\nline \"two\"""#))
    }

    @Test func argumentsWithSpacesAreQuoted() {
        let record = CLIInvocationRecord(
            arguments: ["backfill", "--transcript", "/tmp/a b.jsonl"], exitCode: 0,
            duration: .zero, stderrTail: ""
        )
        #expect(record.message.hasPrefix(#"args="backfill --transcript '/tmp/a b.jsonl'""#))
    }

    @Test func onlySnapshotAndPathsArePolled() {
        func record(_ arguments: [String]) -> CLIInvocationRecord {
            CLIInvocationRecord(arguments: arguments, exitCode: 0, duration: .zero, stderrTail: "")
        }
        #expect(record(["snapshot", "--json"]).isPolled)
        #expect(record(["paths", "--json"]).isPolled)
        #expect(!record(["focus", "claude", "abc"]).isPolled)
        #expect(!record(["refresh-links"]).isPolled)
        #expect(!record([]).isPolled)
    }
}

@Suite("CLIRunner.environment")
struct CLIRunnerEnvironmentTests {
    @Test func leavesEnvironmentAloneWhenNotVerbose() {
        let base = ["PATH": "/usr/bin", "HOME": "/Users/me"]
        #expect(CLIRunner.environment(base: base, verbose: false) == base)
    }

    @Test func setsDebugLevelWhenVerbose() {
        let env = CLIRunner.environment(base: ["PATH": "/usr/bin"], verbose: true)
        #expect(env["AGENTBOARD_LOG_LEVEL"] == "debug")
        #expect(env["PATH"] == "/usr/bin")
    }

    @Test func neverOverridesAnExplicitLevel() {
        let base = ["AGENTBOARD_LOG_LEVEL": "warn"]
        #expect(CLIRunner.environment(base: base, verbose: true)["AGENTBOARD_LOG_LEVEL"] == "warn")
        #expect(CLIRunner.environment(base: base, verbose: false)["AGENTBOARD_LOG_LEVEL"] == "warn")
    }
}
