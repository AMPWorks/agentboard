// Helper for scripts/capture-popover.sh, compiled on demand into app/build/.
//
//   capture-tool click X Y   left-click at a global point (top-left origin),
//                            then put the pointer back where it was
//   capture-tool window PID  print the id of PID's largest on-screen window
//                            above the normal level (the popover panel), or
//                            exit 1 when it has none
import CoreGraphics
import Foundation

func click(_ point: CGPoint) {
    let source = CGEventSource(stateID: .hidSystemState)
    let saved = CGEvent(source: nil)?.location
    for type in [CGEventType.mouseMoved, .leftMouseDown, .leftMouseUp] {
        CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point,
                mouseButton: .left)?.post(tap: .cghidEventTap)
        usleep(40_000)
    }
    if let saved { CGWarpMouseCursorPosition(saved) }
}

func popoverWindow(pid: Int32) -> Int? {
    let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
    var best: (id: Int, area: Double)?
    for window in info where (window[kCGWindowOwnerPID as String] as? Int32) == pid {
        guard let layer = window[kCGWindowLayer as String] as? Int, layer > 0,
              let id = window[kCGWindowNumber as String] as? Int,
              let bounds = window[kCGWindowBounds as String] as? [String: Double] else { continue }
        let area = (bounds["Width"] ?? 0) * (bounds["Height"] ?? 0)
        if area > 1000, area > (best?.area ?? 0) { best = (id, area) }
    }
    return best?.id
}

let args = CommandLine.arguments
switch (args.count, args.count > 1 ? args[1] : "") {
case (4, "click"):
    guard let x = Double(args[2]), let y = Double(args[3]) else { exit(2) }
    click(CGPoint(x: x, y: y))
case (3, "window"):
    guard let pid = Int32(args[2]), let id = popoverWindow(pid: pid) else { exit(1) }
    print(id)
default:
    FileHandle.standardError.write(Data("usage: capture-tool click X Y | window PID\n".utf8))
    exit(2)
}
