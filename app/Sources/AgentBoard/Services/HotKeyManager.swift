import Carbon.HIToolbox
import Foundation

/// Registers the global ⌃⌥⌘A hotkey via Carbon's RegisterEventHotKey. There is
/// no AppKit/SwiftUI replacement for a system-wide hotkey outside the app's
/// own menu, so Carbon remains the documented approach.
@MainActor
final class HotKeyManager {
    static let shared = HotKeyManager()

    var onHotKey: (() -> Void)?

    private var hotKeyRef: EventHotKeyRef?
    private var eventHandlerRef: EventHandlerRef?

    private static let signature: FourCharCode = {
        let bytes = [UInt8(ascii: "A"), UInt8(ascii: "B"), UInt8(ascii: "r"), UInt8(ascii: "d")]
        return bytes.reduce(FourCharCode(0)) { ($0 << 8) + FourCharCode($1) }
    }()

    func register() {
        guard eventHandlerRef == nil else { return }

        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), hotKeyEventHandler, 1, &eventType, nil, &eventHandlerRef)

        let hotKeyID = EventHotKeyID(signature: Self.signature, id: 1)
        let modifiers = UInt32(controlKey | optionKey | cmdKey)
        let keyCode = UInt32(kVK_ANSI_A)
        RegisterEventHotKey(keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &hotKeyRef)
    }

    func unregister() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }
        if let eventHandlerRef {
            RemoveEventHandler(eventHandlerRef)
            self.eventHandlerRef = nil
        }
    }

    fileprivate func fire() {
        onHotKey?()
    }
}

/// C function pointer target for InstallEventHandler; hops to MainActor to
/// reach the manager's stored handler.
private func hotKeyEventHandler(
    _ nextHandler: EventHandlerCallRef?,
    _ event: EventRef?,
    _ userData: UnsafeMutableRawPointer?
) -> OSStatus {
    Task { @MainActor in
        HotKeyManager.shared.fire()
    }
    return noErr
}
