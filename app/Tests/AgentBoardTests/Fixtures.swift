import Foundation
@testable import AgentBoard

/// Builds snapshot values through the same JSON decoding the app uses, since
/// `Session` only has a decoding initializer.
enum Fixtures {
    static func session(_ key: String, _ state: String, alive: Bool = false) -> [String: Any] {
        [
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
    }

    static func snapshot(_ sessions: [[String: Any]], counts: [String: Int]? = nil) -> Snapshot {
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
        let json: [String: Any] = [
            "generated_at": "2026-10-15T00:00:00Z",
            "counts": all,
            "sessions": sessions,
        ]
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(Snapshot.self, from: data)
    }

    /// One session per (key, state) pair, all alive.
    static func needsYou(_ keys: String...) -> Snapshot {
        snapshot(keys.map { session($0, "needs_you", alive: true) })
    }
}
