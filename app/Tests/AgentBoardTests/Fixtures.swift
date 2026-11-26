import Foundation
@testable import AgentBoard

/// Builds snapshot values through the same JSON decoding the app uses, since
/// `Session` only has a decoding initializer.
enum Fixtures {
    /// `extra` adds or overrides snapshot JSON fields (e.g. `"branch"`,
    /// `"links"`, `"triage"`), using the snapshot's snake_case keys.
    static func session(
        _ key: String, _ state: String, alive: Bool = false, extra: [String: Any] = [:]
    ) -> [String: Any] {
        let base: [String: Any] = [
            "key": key,
            "tool": "claude",
            "session_id": key,
            "state": state,
            "title": key,
            "cwd": "/tmp/project",
            "started_at": "2026-10-15T00:00:00Z",
            "updated_at": "2026-10-15T00:00:00Z",
            "alive": alive,
            "can_focus": false,
            "resume_command": "",
            "links": [],
        ]
        return base.merging(extra) { _, given in given }
    }

    /// Decodes one session through `snapshot`.
    static func decoded(
        _ key: String, _ state: String = "running", alive: Bool = false, extra: [String: Any] = [:]
    ) -> Session {
        snapshot([session(key, state, alive: alive, extra: extra)]).sessions[0]
    }

    /// `usage` is the snapshot's top-level `usage` JSON value, if any
    /// (normally a list of source objects).
    static func snapshot(
        _ sessions: [[String: Any]], counts: [String: Int]? = nil, usage: Any? = nil,
        top: [String: Any] = [:]
    ) -> Snapshot {
        // `Counts` decodes every key, so start from all-zero.
        var all: [String: Int] = [:]
        for key in ["needs_you", "interrupted", "running", "review", "unconcluded", "snoozed", "done"] {
            all[key] = 0
        }
        if let counts {
            all.merge(counts) { _, given in given }
        } else {
            for session in sessions {
                let state = session["state"] as! String
                if all[state] != nil { all[state]! += 1 }
            }
        }
        var json: [String: Any] = [
            "generated_at": "2026-10-15T00:00:00Z",
            "counts": all,
            "sessions": sessions,
        ]
        if let usage { json["usage"] = usage }
        json.merge(top) { _, given in given }
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(Snapshot.self, from: data)
    }

    static let peerID = "11111111-2222-4333-8444-555555555555"

    /// A session of the paired Mac `peerID` ("Mini"), keyed like the CLI's.
    static func remote(
        _ id: String, _ state: String, stale: Bool = false, remoteOpen: String? = "tmux",
        updatedAt: String = "2026-10-15T00:00:00Z", extra: [String: Any] = [:]
    ) -> [String: Any] {
        var fields: [String: Any] = [
            "session_id": id,
            "machine": ["id": peerID, "name": "Mini"],
            "stale": stale,
            "updated_at": updatedAt,
            "can_focus": remoteOpen != nil,
        ]
        if let remoteOpen { fields["remote_open"] = remoteOpen }
        return session("claude:\(id)@\(peerID)", state, alive: true,
                       extra: fields.merging(extra) { _, given in given })
    }

    /// The snapshot's `peers` list with the one paired Mac.
    static func peers(status: String = "online", lastSeen: String = "2026-10-15T00:00:00Z",
                      clockSkew: Double? = nil) -> [String: Any] {
        var peer: [String: Any] = ["id": peerID, "name": "Mini", "host": "mini.local", "status": status,
                                   "last_seen_at": lastSeen, "session_count": 1, "allow_open": true]
        if let clockSkew { peer["clock_skew_secs"] = clockSkew }
        return ["peers": [peer]]
    }

    /// One session per (key, state) pair, all alive.
    static func needsYou(_ keys: String...) -> Snapshot {
        snapshot(keys.map { session($0, "needs_you", alive: true) })
    }
}
