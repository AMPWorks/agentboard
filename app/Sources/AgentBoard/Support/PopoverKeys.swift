import AppKit
import SwiftUI

/// What a key press in the popover should do. Pure, so the Escape and ⌘F
/// rules are testable without a window.
enum PopoverKeyAction: Equatable {
    /// Escape with a query: clear it and swallow the key.
    case clearQuery
    /// ⌘F: focus the search field and swallow the key.
    case focusSearch
    /// Escape with no query: take focus off the search field and let the
    /// key through, so the popover handles Escape as it did before the
    /// field existed. If the popover is still open afterwards, focus goes
    /// back to the field so typing is never stranded.
    case releaseFocus
    case pass

    static let escapeKeyCode: UInt16 = 53

    static func action(
        keyCode: UInt16, characters: String?, modifiers: NSEvent.ModifierFlags, queryIsEmpty: Bool
    ) -> PopoverKeyAction {
        let mods = modifiers.intersection([.command, .option, .control, .shift])
        if keyCode == escapeKeyCode && mods.isEmpty {
            return queryIsEmpty ? .releaseFocus : .clearQuery
        }
        if mods == .command && characters?.lowercased() == "f" {
            return .focusSearch
        }
        return .pass
    }
}

/// A local key-down monitor scoped to the popover's window. The search
/// field's editor would otherwise take Escape first, and the accessory app
/// has no Find menu to carry ⌘F.
@MainActor
final class PopoverKeyMonitor {
    weak var window: NSWindow?
    private var monitor: Any?

    func start(
        queryIsEmpty: @escaping @MainActor () -> Bool,
        clearQuery: @escaping @MainActor () -> Void,
        focusSearch: @escaping @MainActor () -> Void
    ) {
        stop()
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let window = self?.window, event.window === window else { return event }
            let action = PopoverKeyAction.action(
                keyCode: event.keyCode,
                characters: event.charactersIgnoringModifiers,
                modifiers: event.modifierFlags,
                queryIsEmpty: queryIsEmpty()
            )
            switch action {
            case .clearQuery:
                clearQuery()
                return nil
            case .focusSearch:
                focusSearch()
                return nil
            case .releaseFocus:
                window.makeFirstResponder(nil)
                // The panel normally closes on Escape. If it does not, put
                // focus back once the event is handled, so keystrokes still
                // land in the field instead of going nowhere until ⌘F.
                Task { @MainActor [weak self] in
                    guard let window = self?.window, window.isVisible,
                          window.firstResponder === window else { return }
                    focusSearch()
                }
                return event
            case .pass:
                return event
            }
        }
    }

    func stop() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }
}

/// Reports the NSWindow a SwiftUI view lives in.
struct WindowReader: NSViewRepresentable {
    let onWindow: @MainActor (NSWindow?) -> Void

    func makeNSView(context: Context) -> NSView { ReportingView(onWindow: onWindow) }
    func updateNSView(_ nsView: NSView, context: Context) {}

    final class ReportingView: NSView {
        let onWindow: @MainActor (NSWindow?) -> Void

        init(onWindow: @escaping @MainActor (NSWindow?) -> Void) {
            self.onWindow = onWindow
            super.init(frame: .zero)
        }

        @available(*, unavailable)
        required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            onWindow(window)
        }
    }
}
