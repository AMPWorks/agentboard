import Foundation
import OSLog

/// Swift port of `diag.redact` in core/agentboard/diag.py, so the app's OSLog
/// lines and the CLI's agentboard.log redact arguments the same way: the
/// free text after `--note`, `--text` and `--title` becomes
/// `<redacted:N chars>` (both `--flag value` and `--flag=value`), any other
/// argument over 200 characters keeps its first 80 plus `...`, and tools,
/// session ids, paths and flags pass through verbatim.
enum ArgumentRedactor {
    static let redactedFlags: Set<String> = ["--note", "--text", "--title"]
    static let longArgumentLimit = 200
    static let longArgumentKeep = 80

    static func redact(_ arguments: [String]) -> [String] {
        var output: [String] = []
        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            if redactedFlags.contains(argument) {
                output.append(argument)
                if index + 1 < arguments.count {
                    output.append("<redacted:\(arguments[index + 1].unicodeScalars.count) chars>")
                    index += 1
                }
            } else if let equals = argument.firstIndex(of: "="),
                      redactedFlags.contains(String(argument[..<equals])) {
                let value = argument[argument.index(after: equals)...]
                output.append("\(argument[..<equals])=<redacted:\(value.unicodeScalars.count) chars>")
            } else if argument.unicodeScalars.count > longArgumentLimit {
                // Counted in Unicode scalars, like Python's len().
                var kept = String.UnicodeScalarView()
                kept.append(contentsOf: argument.unicodeScalars.prefix(longArgumentKeep))
                output.append(String(kept) + "...")
            } else {
                output.append(argument)
            }
            index += 1
        }
        return output
    }
}

/// One finished `agentboard` subprocess, as logged by `CLIRunner`.
struct CLIInvocationRecord: Sendable {
    /// How much of the child's stderr the log line keeps (its tail).
    static let stderrTailBytes = 512
    /// Subcommands the app runs on a timer; logged at `.debug` unless
    /// Verbose logging is on, so routine polling never floods the log.
    static let polledCommands: Set<String> = ["snapshot", "paths"]

    let arguments: [String]
    let exitCode: Int32
    let duration: Duration
    let stderrTail: String

    var isPolled: Bool { Self.polledCommands.contains(arguments.first ?? "") }

    var milliseconds: Int64 {
        let parts = duration.components
        return parts.seconds * 1000 + parts.attoseconds / 1_000_000_000_000_000
    }

    /// `args="focus claude abc" exit=0 ms=412 stderr="..."`, with the
    /// arguments redacted and only the last 512 bytes of stderr.
    var message: String {
        var text = "args=\(Self.quoted(Self.join(ArgumentRedactor.redact(arguments)))) exit=\(exitCode) ms=\(milliseconds)"
        let tail = Self.tail(stderrTail, bytes: Self.stderrTailBytes)
        if !tail.isEmpty {
            text += " stderr=\(Self.quoted(tail))"
        }
        return text
    }

    /// The last `bytes` bytes of `text` (as UTF-8), trimmed. A multi-byte
    /// character split at the cut decodes as a replacement character.
    static func tail(_ text: String, bytes: Int) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let utf8 = Array(trimmed.utf8)
        guard utf8.count > bytes else { return trimmed }
        return String(decoding: utf8.suffix(bytes), as: UTF8.self)
    }

    private static func join(_ arguments: [String]) -> String {
        arguments.map { argument in
            let needsQuotes = argument.isEmpty || argument.contains { $0.isWhitespace || $0 == "'" || $0 == "\"" }
            guard needsQuotes else { return argument }
            return "'" + argument.replacingOccurrences(of: "'", with: "'\\''") + "'"
        }.joined(separator: " ")
    }

    private static func quoted(_ value: String) -> String {
        let escaped = value
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "\n", with: "\\n")
            .replacingOccurrences(of: "\r", with: "\\r")
            .replacingOccurrences(of: "\t", with: "\\t")
        return "\"\(escaped)\""
    }
}

extension CLIInvocationRecord {
    private static let logger = Logger(subsystem: "io.github.agentboard", category: "cli")

    /// Logs this record with every interpolation `.public`, so `log show`
    /// prints the text rather than `<private>`. `Logger` has no runtime
    /// level, so the level is chosen here: failures at `.error`, actions at
    /// `.notice` (persisted), and polled `snapshot`/`paths` calls at
    /// `.debug` unless Verbose logging promotes them to `.notice`.
    func log(verbose: Bool) {
        let message = self.message
        if exitCode != 0 {
            Self.logger.error("\(message, privacy: .public)")
        } else if isPolled && !verbose {
            Self.logger.debug("\(message, privacy: .public)")
        } else {
            Self.logger.notice("\(message, privacy: .public)")
        }
    }
}
