import Foundation

/// A short display name for a model id, by a formatting rule rather than a
/// table, so a new model needs no code change.
enum ModelName {
    /// `claude-opus-5-5` -> `Opus 5.5`, `claude-sonnet-4-5-20250929` ->
    /// `Sonnet 4.5`, `claude-3-5-haiku-20241022` -> `Haiku 3.5`. A
    /// bracketed suffix is kept (`claude-opus-5-5[1m]` -> `Opus 5.5 [1m]`).
    /// Provider wrappers are stripped by rule first: anything before a
    /// `claude-` that starts the string or follows `.`, `/` or `:` (Bedrock's
    /// `us.anthropic.`, an inference-profile ARN), a trailing Bedrock
    /// `-v1:0` and a trailing Vertex `@20250805`, so
    /// `us.anthropic.claude-sonnet-4-5-20250929-v1:0` -> `Sonnet 4.5` and
    /// `claude-3-5-sonnet-v2@20241022` -> `Sonnet 3.5 v2`. A wrapped id the
    /// rule cannot shorten comes back unwrapped (`claude-...`).
    /// Anything else (`gpt-5-codex`, an unexpected shape) is returned as-is.
    static func short(_ id: String) -> String {
        guard let core = unwrapped(id) else { return id }
        return format(core) ?? core
    }

    /// The `claude-...` id inside a provider wrapper, or nil when `id` has
    /// no `claude-` at a boundary.
    private static func unwrapped(_ id: String) -> String? {
        let prefix = "claude-"
        var searchFrom = id.startIndex
        var start: String.Index?
        while let range = id.range(of: prefix, range: searchFrom..<id.endIndex) {
            if range.lowerBound == id.startIndex
                || [".", "/", ":"].contains(id[id.index(before: range.lowerBound)]) {
                start = range.lowerBound
                break
            }
            searchFrom = range.upperBound
        }
        guard let start else { return nil }
        var core = String(id[start...])
        // A bracketed suffix (`[1m]`) rides outside the wrapper rules.
        var bracket = ""
        if core.hasSuffix("]"), let open = core.lastIndex(of: "[") {
            bracket = String(core[open...])
            core = String(core[..<open])
        }
        // Bedrock's `-v1:0` and Vertex's `@20250805`.
        if let at = core.lastIndex(of: "@"), at < core.index(before: core.endIndex),
           core[core.index(after: at)...].allSatisfy(\.isASCIIDigit) {
            core = String(core[..<at])
        }
        if let match = core.range(of: #"-v[0-9]+:[0-9]+$"#, options: .regularExpression) {
            core = String(core[..<match.lowerBound])
        }
        return core + bracket
    }

    private static func format(_ id: String) -> String? {
        let prefix = "claude-"
        guard id.hasPrefix(prefix) else { return nil }
        var body = String(id.dropFirst(prefix.count))
        var suffix = ""
        if body.hasSuffix("]"), let open = body.lastIndex(of: "[") {
            suffix = " " + body[open...]
            body = String(body[..<open])
        }
        var segments = body.split(separator: "-", omittingEmptySubsequences: false).map(String.init)
        if let last = segments.last, last.count == 8, last.allSatisfy(\.isASCIIDigit) {
            segments.removeLast()
        }
        // A trailing revision (`claude-3-5-sonnet-v2`) is kept as ` v2`.
        if segments.count > 1, let last = segments.last, last.count > 1, last.first == "v",
           last.dropFirst().allSatisfy(\.isASCIIDigit) {
            suffix = " " + last + suffix
            segments.removeLast()
        }
        guard !segments.isEmpty, segments.allSatisfy({ !$0.isEmpty }) else { return nil }
        let isNumber: (String) -> Bool = { $0.allSatisfy(\.isASCIIDigit) }
        let isWord: (String) -> Bool = { $0.allSatisfy { $0.isASCII && $0.isLetter } }
        guard segments.allSatisfy({ isNumber($0) || isWord($0) }) else { return nil }

        // Words then numbers (current ids) or numbers then words (older ones).
        let words: [String]
        let numbers: [String]
        if let firstNumber = segments.firstIndex(where: isNumber), firstNumber > 0,
           segments[firstNumber...].allSatisfy(isNumber) {
            words = Array(segments[..<firstNumber])
            numbers = Array(segments[firstNumber...])
        } else if let firstWord = segments.firstIndex(where: isWord), firstWord > 0,
                  segments[firstWord...].allSatisfy(isWord) {
            numbers = Array(segments[..<firstWord])
            words = Array(segments[firstWord...])
        } else {
            return nil
        }
        let family = words.map { $0.prefix(1).uppercased() + $0.dropFirst() }.joined(separator: " ")
        return "\(family) \(numbers.joined(separator: "."))\(suffix)"
    }
}

private extension Character {
    var isASCIIDigit: Bool { isASCII && isNumber }
}

/// A context size in tokens for display.
enum ContextSize {
    /// `950`, `9.5k`, `308k`, `1.2M`.
    static func short(_ tokens: Int) -> String {
        let value = Double(max(0, tokens))
        if value < 1_000 { return "\(Int(value))" }
        if value < 10_000 { return trimmed(value / 1_000) + "k" }
        let thousands = (value / 1_000).rounded()
        if thousands < 1_000 { return "\(Int(thousands))k" }
        return trimmed(value / 1_000_000) + "M"
    }

    /// `308,011` in the given locale's grouping.
    static func full(_ tokens: Int, locale: Locale = .current) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.locale = locale
        return formatter.string(from: NSNumber(value: tokens)) ?? "\(tokens)"
    }

    /// One decimal, without a trailing `.0`.
    private static func trimmed(_ value: Double) -> String {
        let rounded = (value * 10).rounded() / 10
        if rounded == rounded.rounded() { return "\(Int(rounded))" }
        return String(format: "%.1f", rounded)
    }
}

/// The model/context tag on a session row (`Opus 5.5 · 308k`) and its
/// tooltip.
enum SessionModelTag {
    /// The longest model part the row shows; a longer one (an id the
    /// rule cannot shorten, such as an application inference-profile ARN)
    /// is cut in the middle, so it never squeezes out the project and
    /// branch. The tooltip always has the full id.
    static let maxModelCharacters = 22

    /// `Opus 5.5 · 308k`, `gpt-5-codex`, `Context 51k` (an agent that
    /// reported only its context size), or nil when neither is known.
    static func text(_ session: Session) -> String? {
        let context = session.contextTokens.map(ContextSize.short)
        guard let model = session.model, !model.isEmpty else {
            return context.map { "Context \($0)" }
        }
        var text = middleTruncated(ModelName.short(model), to: maxModelCharacters)
        if let context { text += " · " + context }
        return text
    }

    /// `text` itself when it fits, else its head and tail around `…`,
    /// `limit` characters in all.
    static func middleTruncated(_ text: String, to limit: Int) -> String {
        guard limit > 1, text.count > limit else { return text }
        let tail = (limit - 1) / 2
        let head = limit - 1 - tail
        return text.prefix(head) + "…" + text.suffix(tail)
    }

    /// `Model claude-opus-5-5; context 308,011 tokens as of 12 min ago`.
    static func help(_ session: Session, now: Date = Date(), locale: Locale = .current) -> String {
        var parts: [String] = []
        if let model = session.model, !model.isEmpty { parts.append("Model \(model)") }
        if let context = contextDescription(session, now: now, locale: locale) {
            parts.append((parts.isEmpty ? "Context " : "context ") + context)
        }
        return parts.joined(separator: "; ")
    }

    /// `308,011 tokens, as of 12 min ago`, or nil when unknown.
    static func contextDescription(
        _ session: Session, now: Date = Date(), locale: Locale = .current
    ) -> String? {
        guard let tokens = session.contextTokens else { return nil }
        var text = "\(ContextSize.full(tokens, locale: locale)) tokens"
        if let at = session.contextDate {
            let relative = RelativeDateTimeFormatter()
            relative.unitsStyle = .abbreviated
            relative.locale = locale
            text += ", as of \(relative.localizedString(for: at, relativeTo: now))"
        }
        return text
    }
}
