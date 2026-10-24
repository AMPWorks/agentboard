/// A dashboard sidebar row. "All" needs a tag of its own: SwiftUI's macOS
/// `List` reads a `nil` selection as "nothing selected", so a row tagged
/// `nil` can never be clicked back into.
enum SidebarFilter: Hashable, Sendable {
    case all
    case state(SessionState)

    /// The state the session list narrows to, or nil for every state. An
    /// empty selection (a ⌘-click deselect) also lists every state.
    static func state(for selection: SidebarFilter?) -> SessionState? {
        guard case .state(let state)? = selection else { return nil }
        return state
    }
}
