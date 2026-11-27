// Helper for scripts/capture-popover.sh, compiled on demand into app/build/.
//
//   capture-tool click X Y   left-click at a global point (top-left origin),
//                            then put the pointer back where it was
//   capture-tool window PID  print the id of PID's largest on-screen window
//                            above the normal level (the popover panel), or
//                            exit 1 when it has none
//   capture-tool bounds PID  print that window's global "x y w h" (points,
//                            top-left origin), or exit 1
//   capture-tool backdrop X Y W H
//                            show a plain white window at that global rect
//                            (top-left origin) above normal windows and
//                            below the popover, print "ready", and keep it up
//                            until killed (or 60 s, so a hard-killed run
//                            never leaves it on screen)
//   capture-tool luminance FILE
//                            print the image's mean relative luminance, 0 to 1
import AppKit
import CoreGraphics
import Foundation
import ImageIO

func click(_ point: CGPoint) {
    let source = CGEventSource(stateID: .hidSystemState)
    let saved = CGEvent(source: nil)?.location
    for type in [CGEventType.mouseMoved, .leftMouseDown, .leftMouseUp] {
        CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point,
                mouseButton: .left)?.post(tap: .cghidEventTap)
        // A menu bar that hides itself only takes the click once the
        // pointer has revealed it.
        usleep(type == .mouseMoved ? 700_000 : 40_000)
    }
    if let saved { CGWarpMouseCursorPosition(saved) }
}

func popoverWindow(pid: Int32) -> (id: Int, bounds: CGRect)? {
    let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
    var best: (id: Int, bounds: CGRect)?
    for window in info where (window[kCGWindowOwnerPID as String] as? Int32) == pid {
        guard let layer = window[kCGWindowLayer as String] as? Int, layer > 0,
              let id = window[kCGWindowNumber as String] as? Int,
              let dict = window[kCGWindowBounds as String] as? NSDictionary,
              let bounds = CGRect(dictionaryRepresentation: dict) else { continue }
        let area = bounds.width * bounds.height
        if area > 1000, area > (best.map { $0.bounds.width * $0.bounds.height } ?? 0) {
            best = (id, bounds)
        }
    }
    return best
}

/// Shows the white backdrop window and never returns.
@MainActor
func backdrop(_ rect: CGRect) -> Never {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    // AppKit's origin is the primary screen's bottom-left corner.
    let primaryHeight = NSScreen.screens.first?.frame.height ?? rect.maxY
    let frame = NSRect(x: rect.minX, y: primaryHeight - rect.maxY, width: rect.width, height: rect.height)
    let window = NSWindow(contentRect: frame, styleMask: .borderless, backing: .buffered, defer: false)
    window.backgroundColor = .white
    window.isOpaque = true
    window.hasShadow = false
    window.level = .floating
    window.ignoresMouseEvents = true
    window.collectionBehavior = [.canJoinAllSpaces, .stationary]
    window.setFrame(frame, display: true)
    window.orderFrontRegardless()
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
        print("ready")
        fflush(stdout)
    }
    // A capture needs a few seconds; never outlive a run that died hard.
    DispatchQueue.main.asyncAfter(deadline: .now() + 60) { exit(0) }
    app.run()
    exit(0)
}

func meanLuminance(_ path: String) -> Double? {
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
          let space = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
    let width = image.width, height = image.height
    guard width > 0, height > 0 else { return nil }
    var pixels = [UInt8](repeating: 0, count: width * height * 4)
    let drawn = pixels.withUnsafeMutableBytes { buffer -> Bool in
        guard let context = CGContext(
            data: buffer.baseAddress, width: width, height: height, bitsPerComponent: 8,
            bytesPerRow: width * 4, space: space,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
        // Transparent pixels count as black, which never flatters the result.
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return true
    }
    guard drawn else { return nil }
    var linear = [Double](repeating: 0, count: 256)
    for v in 0..<256 {
        let c = Double(v) / 255
        linear[v] = c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
    }
    var total = 0.0
    for i in stride(from: 0, to: pixels.count, by: 4) {
        total += 0.2126 * linear[Int(pixels[i])] + 0.7152 * linear[Int(pixels[i + 1])]
            + 0.0722 * linear[Int(pixels[i + 2])]
    }
    return total / Double(width * height)
}

let args = CommandLine.arguments
switch (args.count, args.count > 1 ? args[1] : "") {
case (4, "click"):
    guard let x = Double(args[2]), let y = Double(args[3]) else { exit(2) }
    click(CGPoint(x: x, y: y))
case (3, "window"):
    guard let pid = Int32(args[2]), let found = popoverWindow(pid: pid) else { exit(1) }
    print(found.id)
case (3, "bounds"):
    guard let pid = Int32(args[2]), let found = popoverWindow(pid: pid) else { exit(1) }
    let b = found.bounds.integral
    print("\(Int(b.minX)) \(Int(b.minY)) \(Int(b.width)) \(Int(b.height))")
case (6, "backdrop"):
    let numbers = args[2...5].compactMap(Double.init)
    guard numbers.count == 4 else { exit(2) }
    MainActor.assumeIsolated {
        backdrop(CGRect(x: numbers[0], y: numbers[1], width: numbers[2], height: numbers[3]))
    }
case (3, "luminance"):
    guard let value = meanLuminance(args[2]) else { exit(1) }
    print(String(format: "%.4f", value))
default:
    FileHandle.standardError.write(Data(
        "usage: capture-tool click X Y | window PID | bounds PID | backdrop X Y W H | luminance FILE\n".utf8))
    exit(2)
}
