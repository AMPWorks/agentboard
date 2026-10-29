import Foundation

/// The row tag and tooltip for a session's permission mode, as Claude Code
/// reports it in every hook payload (`permission_mode`).
enum PermissionMode {
    /// The row tag for a permission mode, nil when there is nothing to say
    /// (nil, empty, or "default", which Claude Code labels Manual). An
    /// unknown mode shows as itself: Claude Code adds modes, and one must
    /// never be hidden.
    static func tag(_ mode: String?) -> String? {
        guard let mode = normalized(mode), mode != "default" else { return nil }
        switch mode {
        case "plan": return "plan"
        case "acceptEdits": return "accept edits"
        // Spelled out so it cannot be read as the "auto" (finished on its
        // own) tag.
        case "auto": return "auto mode"
        case "dontAsk": return "don't ask"
        case "bypassPermissions": return "bypass"
        default: return mode
        }
    }

    /// The row tag for a session: nil once it has ended or is done, where
    /// the mode is history rather than how the session will behave when it
    /// next needs something. The detail view still shows it.
    static func rowTag(for session: Session) -> String? {
        guard session.endedAt == nil, session.state != .done else { return nil }
        return tag(session.permissionMode)
    }

    /// The tooltip for the tag. A known mode is "as of the last hook event":
    /// a Shift+Tab mode change at an idle prompt fires no hook, so the tag
    /// can lag until the session's next event.
    static func help(_ mode: String?) -> String {
        guard let mode = normalized(mode) else {
            return "Permission mode: not reported yet"
        }
        return "\(meaning(mode)) (as of the last hook event)"
    }

    private static func meaning(_ mode: String) -> String {
        switch mode {
        case "default":
            return "Manual: the session asks before tools that need permission"
        case "plan":
            return "Plan mode: the session proposes a plan and waits for approval before editing"
        case "acceptEdits":
            return "Accept edits: file edits go through without asking; other tools still ask"
        case "auto":
            return "Auto mode: a classifier approves or denies tool calls without asking; denials are recorded"
        case "dontAsk":
            return "Don't ask: tools that are not allowed are denied instead of asking you"
        case "bypassPermissions":
            return "Bypass permissions: nothing asks you"
        default:
            return "Permission mode: \(mode)"
        }
    }

    /// The detail view's value: the documented name as reported, or "—".
    static func detail(_ mode: String?) -> String {
        normalized(mode) ?? "—"
    }

    private static func normalized(_ mode: String?) -> String? {
        guard let mode = mode?.trimmingCharacters(in: .whitespacesAndNewlines),
              !mode.isEmpty else { return nil }
        return mode
    }
}
