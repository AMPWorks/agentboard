import Foundation

/// The sound one attention event plays, as Settings → Sounds stores it.
///
/// Stored in UserDefaults as one string, losslessly: `none`, `default`,
/// `system:<name>` (a sound in the system sounds folder, named without its
/// extension) or `file:<absolute path>`. Anything else, including an empty
/// string, reads as `.none`.
enum SoundChoice: Hashable, Sendable {
    case none
    /// The macOS alert sound (System Settings → Sound → Alert sound),
    /// played as the system beep, at the alert volume.
    case systemDefault
    case system(String)
    case file(String)

    static let systemPrefix = "system:"
    static let filePrefix = "file:"
    /// Where macOS keeps its built-in sounds.
    static let systemSoundsDirectory = URL(fileURLWithPath: "/System/Library/Sounds", isDirectory: true)
    /// The file types Choose file… accepts.
    static let fileExtensions = ["aiff", "aif", "wav", "mp3", "m4a"]

    init(encoded raw: String) {
        if raw == "default" {
            self = .systemDefault
        } else if raw.hasPrefix(Self.systemPrefix), raw.count > Self.systemPrefix.count {
            self = .system(String(raw.dropFirst(Self.systemPrefix.count)))
        } else if raw.hasPrefix(Self.filePrefix), raw.count > Self.filePrefix.count {
            self = .file(String(raw.dropFirst(Self.filePrefix.count)))
        } else {
            self = .none
        }
    }

    var encoded: String {
        switch self {
        case .none: return "none"
        case .systemDefault: return "default"
        case .system(let name): return Self.systemPrefix + name
        case .file(let path): return Self.filePrefix + path
        }
    }

    /// What the Settings picker shows for this choice.
    var label: String {
        switch self {
        case .none: return "None"
        case .systemDefault: return "System default"
        case .system(let name): return name
        case .file(let path): return (path as NSString).lastPathComponent
        }
    }

    /// The installed system sounds, by name without extension, sorted.
    /// Read from the folder, never a fixed list; empty if it is unreadable.
    static func systemSoundNames(in directory: URL = systemSoundsDirectory) -> [String] {
        let files = (try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]
        )) ?? []
        let names = files
            .filter { fileExtensions.contains($0.pathExtension.lowercased()) }
            .map { $0.deletingPathExtension().lastPathComponent }
        return Array(Set(names)).sorted { $0.localizedStandardCompare($1) == .orderedAscending }
    }

    /// The readable sound file this choice plays, or nil: `.none`,
    /// `.systemDefault` (played as the system beep, not from a file), a
    /// system sound that is not installed, or a file that is missing or
    /// unreadable.
    func resolvedURL(soundsDirectory: URL = SoundChoice.systemSoundsDirectory) -> URL? {
        let path: String
        switch self {
        case .none, .systemDefault:
            return nil
        case .system(let name):
            let files = (try? FileManager.default.contentsOfDirectory(
                at: soundsDirectory, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]
            )) ?? []
            guard let match = files.first(where: {
                $0.deletingPathExtension().lastPathComponent == name
                    && Self.fileExtensions.contains($0.pathExtension.lowercased())
            }) else { return nil }
            path = match.path
        case .file(let filePath):
            path = filePath
        }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
              !isDirectory.boolValue,
              FileManager.default.isReadableFile(atPath: path) else { return nil }
        return URL(fileURLWithPath: path)
    }
}
