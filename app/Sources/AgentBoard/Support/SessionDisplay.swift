import Foundation

/// What a session row shows as its title, in one place so the popover, the
/// dashboard, the detail view and the Updates feed agree. A live
/// `agentboard status` text (what the session says it is doing right now)
/// leads; the session's own title (custom, generated, `agentboard title`,
/// Claude's summary or the first prompt, as the CLI resolved it) is then
/// the subtitle.
///
/// A Needs you row is the exception: its title leads and the status text
/// is the subtitle. The status was set while the session worked and may no
/// longer be true now that it waits (the snapshot has no time it was set,
/// so staleness cannot be judged), and it must never stand in red for
/// what the session needs; the reason line says that.
enum SessionDisplay {
    static func statusLine(_ session: Session) -> String? {
        guard let text = session.statusText?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else { return nil }
        return text
    }

    /// True when the session's own title leads over its status text.
    private static func titleLeads(_ session: Session) -> Bool {
        session.state == .needsYou && !session.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    static func primaryTitle(_ session: Session) -> String {
        guard let status = statusLine(session), !titleLeads(session) else { return session.title }
        return status
    }

    /// The line under the primary one: the session title under a status
    /// line, or on a Needs you row the status text under the title. Nil when
    /// there is no status, or when one of the two is empty or repeats the
    /// other.
    static func secondaryTitle(_ session: Session) -> String? {
        guard let status = statusLine(session) else { return nil }
        let title = session.title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, title != status else { return nil }
        return titleLeads(session) ? status : title
    }
}

extension SessionDisplay {
    /// Notification types the hook stores as the reason when a needs-you
    /// notification carries no message, in plain words.
    static let notificationReasons: [String: String] = [
        "permission_prompt": "Needs your permission",
        "elicitation_dialog": "Asks you a question",
        "elicitation_url_dialog": "Asks you to open a link",
        "agent_needs_input": "Needs your input",
        "idle_prompt": "Waiting for your next prompt",
    ]

    /// Reasons a PreToolUse of AskUserQuestion / ExitPlanMode records.
    static let toolReasons: [String: String] = [
        "question": "Asks you a question",
        "plan approval": "Waiting for your plan approval",
    ]

    static let interruptedReason = "Stopped mid-turn: resume it to continue"

    /// Claude Code's StopFailure error types, which a failed turn's reason
    /// starts with ("<error>: <first line>", hook.failure_reason).
    static let failureReasons: [String: String] = [
        "rate_limit": "rate limit",
        "authentication_failed": "not signed in",
        "billing_error": "billing problem",
        "invalid_request": "invalid request",
        "server_error": "API server error",
        "max_output_tokens": "output too long",
        "overloaded": "API overloaded",
        "unknown": "unknown error",
    ]

    /// The row's reason line in plain words, or nil when there is none: a
    /// bare tool name on a Needs you row is a permission prompt for that
    /// tool ("Needs permission: Bash"), a raw notification type becomes a
    /// sentence, and an "<type>: <text>" or "StopFailure: <text>" prefix is
    /// spelled out. Anything else (MCP questions, failures, review reasons)
    /// already reads as text and is shown as is; the tooltip keeps the raw
    /// value.
    static func reasonText(_ session: Session) -> String? {
        guard let raw = session.waitingReason?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty else {
            // The CLI gives an interrupted session no reason; say what
            // happened and what to do, like every other Needs you row.
            return session.state == .interrupted ? interruptedReason : nil
        }
        if let plain = notificationReasons[raw] ?? toolReasons[raw] { return plain }
        for (type, plain) in notificationReasons {
            let prefix = type + ": "
            if raw.hasPrefix(prefix) {
                let rest = raw.dropFirst(prefix.count).trimmingCharacters(in: .whitespaces)
                return rest.isEmpty ? plain : "\(plain): \(rest)"
            }
        }
        if raw.hasPrefix("StopFailure: ") {
            return "Turn failed: " + raw.dropFirst("StopFailure: ".count)
        }
        for (error, plain) in failureReasons {
            if raw == error { return "Turn failed (\(plain))" }
            if raw.hasPrefix(error + ": ") {
                return "Turn failed (\(plain)): " + raw.dropFirst(error.count + 2)
            }
        }
        if session.state == .needsYou, isToolName(raw), !isNeedsYouNoteHeadline(raw, of: session) {
            return "Needs permission: \(raw)"
        }
        return raw
    }

    /// True when `raw` is the headline of one of the session's open
    /// needs_you notes (reconcile.note_headline): a one-word headline such
    /// as "Blocked" looks like a tool name but is the note's own text, so
    /// it is shown as is. A headline is a note line without its markdown
    /// emphasis or leading marker characters, so a line matches when it
    /// equals `raw` or ends with it after a non-alphanumeric character.
    static func isNeedsYouNoteHeadline(_ raw: String, of session: Session) -> Bool {
        session.notes.contains { note in
            guard note.kind == .needsYou else { return false }
            return note.text.split(whereSeparator: \.isNewline).contains { line in
                let cleaned = line.replacingOccurrences(of: "**", with: "")
                    .trimmingCharacters(in: CharacterSet.whitespaces.union(CharacterSet(charactersIn: "*")))
                guard cleaned.hasSuffix(raw) else { return false }
                guard let before = cleaned.dropLast(raw.count).unicodeScalars.last else { return true }
                return !CharacterSet.alphanumerics.contains(before)
            }
        }
    }

    /// A Claude Code tool name as a PermissionRequest reports it: `Bash`,
    /// `WebFetch`, `mcp__server__tool`.
    static func isToolName(_ text: String) -> Bool {
        if text.hasPrefix("mcp__") { return !text.contains(" ") }
        guard let first = text.unicodeScalars.first, CharacterSet.uppercaseLetters.contains(first) else {
            return false
        }
        return text.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) || $0 == "_" }
    }

    /// What kind of thing the reason line reports, for its glyph.
    enum ReasonKind: Equatable, CaseIterable {
        /// A permission prompt (a raw `permission_prompt`, or a bare tool
        /// name on a Needs you row).
        case permission
        /// A question, a plan to approve, a link to open or input to give.
        case question
        /// A failed turn (`StopFailure`, a Claude Code error type).
        case failure

        var systemImage: String {
            switch self {
            case .permission: "lock"
            case .question: "questionmark.bubble"
            case .failure: "exclamationmark.triangle"
            }
        }

        /// The glyph's spoken name.
        var label: String {
            switch self {
            case .permission: "Permission"
            case .question: "Question"
            case .failure: "Failure"
            }
        }
    }

    /// Notification types (bare or with a message) that ask the user
    /// something; `toolReasons` (exact values only) ask too.
    static let questionNotifications: Set<String> = [
        "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input",
    ]

    /// The reason's kind from its raw prefix, with the same rules as
    /// `reasonText`; nil for anything else (an idle prompt, a review reason,
    /// a Running row's background tasks, free text such as an MCP question,
    /// which the snapshot cannot tell apart from other text).
    static func reasonKind(_ session: Session) -> ReasonKind? {
        guard let raw = session.waitingReason?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty else { return nil }
        func matches(_ type: String) -> Bool { raw == type || raw.hasPrefix(type + ": ") }
        if matches("permission_prompt") { return .permission }
        if toolReasons[raw] != nil || questionNotifications.contains(where: matches) { return .question }
        if notificationReasons.keys.contains(where: matches) { return nil }
        if raw.hasPrefix("StopFailure: ") || failureReasons.keys.contains(where: matches) { return .failure }
        if session.state == .needsYou, isToolName(raw), !isNeedsYouNoteHeadline(raw, of: session) {
            return .permission
        }
        return nil
    }

    /// A reason the user acts on (Needs you, Interrupted, Review) is set in
    /// a medium weight, so it stands out from a secondary title above it; a
    /// Running row's background-task note and the rest are regular.
    static func reasonIsActionable(_ session: Session) -> Bool {
        switch session.state {
        case .needsYou, .interrupted, .review: return true
        case .running, .unconcluded, .snoozed, .done, .unknown: return false
        }
    }
}

/// The PR buttons on a session row: one per PR link that has a URL, the
/// ones that need the user first, capped with a "+N" for the rest. Merged
/// and closed PRs are left off unless `showFinished` is set (Settings →
/// Show closed and merged PRs). The filter runs before the cap, so "+N"
/// counts only PRs the row would show.
enum PRButtons {
    static let defaultCap = 3

    struct Layout: Equatable {
        var shown: [Link]
        /// PR links left out of `shown` (the "+N" button opens the detail view).
        var overflow: Int
    }

    /// Awaiting the user, then open, merged, closed, anything else; newest
    /// (highest number) first within each group.
    static func rank(_ link: Link) -> Int {
        if link.awaitsYou == true { return 0 }
        switch link.state {
        case "OPEN": return 1
        case "MERGED": return 2
        case "CLOSED": return 3
        default: return 4
        }
    }

    /// A merged or closed PR: left off the row buttons unless Settings →
    /// Show closed and merged PRs is on. Display only; the CLI's state rules
    /// read the same links unchanged. A nil, `DONE` or nonstandard state
    /// stays visible, and so does a PR that awaits the user.
    static func isFinished(_ link: Link) -> Bool {
        link.isPR && link.awaitsYou != true
            && (link.state == "MERGED" || link.state == "CLOSED")
    }

    static func layout(for links: [Link], showFinished: Bool = false, cap: Int = defaultCap) -> Layout {
        let prs = links.filter { $0.isPR && $0.openURL != nil && (showFinished || !isFinished($0)) }
        let ordered = prs.enumerated().sorted { lhs, rhs in
            let (l, r) = (rank(lhs.element), rank(rhs.element))
            if l != r { return l < r }
            let (ln, rn) = (lhs.element.number ?? -1, rhs.element.number ?? -1)
            if ln != rn { return ln > rn }
            return lhs.offset < rhs.offset
        }.map(\.element)
        let shown = Array(ordered.prefix(max(0, cap)))
        return Layout(shown: shown, overflow: ordered.count - shown.count)
    }
}
