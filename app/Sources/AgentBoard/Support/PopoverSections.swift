import Foundation

/// The menu bar popover's sections, computed from the snapshot and the
/// popover's query: filter first, then group. Pure so the grouping, the
/// query-only Snoozed/Done sections and the hidden-sessions notice are
/// testable without SwiftUI.
struct PopoverSections: Sendable, Equatable {
    enum Kind: String, Sendable, CaseIterable {
        case needsYou, review, running, unconcluded, snoozed, done

        var title: String {
            switch self {
            case .needsYou: return "Needs you"
            case .review: return "Review"
            case .running: return "Running"
            case .unconcluded: return "Unconcluded"
            case .snoozed: return "Snoozed"
            case .done: return "Done"
            }
        }

        /// Snoozed and Done appear only while a query is active.
        var queryOnly: Bool { self == .snoozed || self == .done }

        func contains(_ state: SessionState) -> Bool {
            switch self {
            case .needsYou: return state == .needsYou || state == .interrupted
            case .review: return state == .review
            case .running: return state == .running
            case .unconcluded: return state == .unconcluded
            case .snoozed: return state == .snoozed
            case .done: return state == .done
            }
        }
    }

    struct Section: Sendable, Equatable, Identifiable {
        let kind: Kind
        let sessions: [Session]
        var id: Kind { kind }
    }

    /// Non-empty sections in display order.
    let sections: [Section]
    /// A query is active (non-empty after parsing).
    let isFiltering: Bool
    /// The snapshot holds no sessions at all.
    let snapshotIsEmpty: Bool
    /// Sessions in the snapshot that no section shows because of the query.
    /// Zero when not filtering. Sessions in a state no section can show
    /// (`.unknown`, from a newer CLI) are never counted, since the query is
    /// not what hides them.
    let hiddenCount: Int
    /// How many of the hidden sessions are Needs you or Interrupted.
    let hiddenNeedingAttention: Int

    static func make(from sessions: [Session], query: SessionSearch) -> PopoverSections {
        let filtering = !query.isEmpty
        let matched = filtering ? sessions.filter(query.matches) : sessions
        let kinds = Kind.allCases.filter { filtering || !$0.queryOnly }
        let built = kinds.compactMap { kind -> Section? in
            let members = matched
                .filter { kind.contains($0.state) }
                .sorted { $0.updatedAt > $1.updatedAt }
            return members.isEmpty ? nil : Section(kind: kind, sessions: members)
        }
        var hidden = 0
        var hiddenAttention = 0
        if filtering {
            let shown = Set(built.flatMap { $0.sessions.map(\.key) })
            for session in sessions where !shown.contains(session.key) && session.state != .unknown {
                hidden += 1
                if session.state == .needsYou || session.state == .interrupted { hiddenAttention += 1 }
            }
        }
        return PopoverSections(
            sections: built,
            isFiltering: filtering,
            snapshotIsEmpty: sessions.isEmpty,
            hiddenCount: hidden,
            hiddenNeedingAttention: hiddenAttention
        )
    }

    func section(_ kind: Kind) -> Section? { sections.first { $0.kind == kind } }

    /// Headers carry a count while a query is active (Unconcluded's
    /// disclosure label always does).
    func header(for section: Section) -> String {
        if isFiltering || section.kind == .unconcluded {
            return "\(section.kind.title) (\(section.sessions.count))"
        }
        return section.kind.title
    }

    /// The Unconcluded disclosure opens by itself while a query is active
    /// and it has matches.
    var expandsUnconcluded: Bool { isFiltering && section(.unconcluded) != nil }

    /// A query is active, the snapshot has sessions, and none match.
    var hasNoMatches: Bool { isFiltering && !snapshotIsEmpty && sections.isEmpty }

    /// One line under the search field while a query is active, e.g.
    /// "Filtered: 3 sessions hidden".
    var hiddenNotice: String? {
        guard isFiltering else { return nil }
        let noun = hiddenCount == 1 ? "session" : "sessions"
        var text = "Filtered: \(hiddenCount) \(noun) hidden"
        if hiddenNeedingAttention > 0 {
            text += ", \(hiddenNeedingAttention) need\(hiddenNeedingAttention == 1 ? "s" : "") you"
        }
        return text
    }

    /// Emphasize the notice when the query hides a session that needs the user.
    var noticeIsEmphasized: Bool { hiddenNeedingAttention > 0 }
}
