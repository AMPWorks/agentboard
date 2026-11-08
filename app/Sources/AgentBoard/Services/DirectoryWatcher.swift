import Foundation

/// One file's identity and last write (inode, size, modification time), or
/// nil fields when it does not exist or is empty. Two equal stamps mean the
/// file was not written in between.
///
/// An empty file stamps as `.missing`: it holds nothing a reader could see.
/// That matters for the database's `-wal`: every reader (each `agentboard
/// snapshot`) creates it empty and deletes it on close without writing a
/// frame, so a stamp taken while a snapshot runs must not differ from one
/// taken between runs.
struct FileStamp: Equatable, Sendable {
    var inode: UInt64?
    var size: Int64?
    var modifiedSeconds: Int?
    var modifiedNanoseconds: Int?

    static let missing = FileStamp()

    init(inode: UInt64? = nil, size: Int64? = nil, modifiedSeconds: Int? = nil, modifiedNanoseconds: Int? = nil) {
        self.inode = inode
        self.size = size
        self.modifiedSeconds = modifiedSeconds
        self.modifiedNanoseconds = modifiedNanoseconds
    }

    static func of(_ path: String) -> FileStamp {
        var info = stat()
        guard stat(path, &info) == 0, info.st_size > 0 else { return .missing }
        return FileStamp(inode: UInt64(info.st_ino), size: Int64(info.st_size),
                         modifiedSeconds: Int(info.st_mtimespec.tv_sec),
                         modifiedNanoseconds: Int(info.st_mtimespec.tv_nsec))
    }

    static func of(_ paths: [String]) -> [FileStamp] { paths.map(of) }
}

/// Watches a directory for writes using a DispatchSource file-system-object
/// source, debouncing bursts of events (e.g. WAL checkpoints).
///
/// With `stampedFiles`, an event counts only when one of those files changed
/// since the last event that counted. Every `agentboard snapshot` opens the
/// WAL-mode database, which creates and then deletes `-wal`/`-shm` beside it;
/// that is a directory write, so without the check each refresh the watcher
/// starts would fire the watcher again and the app would refresh every
/// debounce interval forever.
@MainActor
final class DirectoryWatcher {
    private var source: DispatchSourceFileSystemObject?
    private var fileDescriptor: CInt = -1
    private var debounceTask: Task<Void, Never>?
    private let debounceInterval: Duration
    private let stamp: @MainActor ([String]) -> [FileStamp]
    private var stampedFiles: [String] = []
    private var lastStamps: [FileStamp] = []

    init(debounceInterval: Duration = .milliseconds(500),
         stamp: @escaping @MainActor ([String]) -> [FileStamp] = { FileStamp.of($0) }) {
        self.debounceInterval = debounceInterval
        self.stamp = stamp
    }

    /// Whether an event should reach `onChange`: always without stamped
    /// files, else only when their stamps moved since the last event that
    /// counted. Not a read-only check: a `true` answer moves the baseline,
    /// so asking twice for one event gives `true` then `false`. Internal
    /// (not private) only so tests can drive it without a real directory.
    func consumeEventIfStampsMoved() -> Bool {
        guard !stampedFiles.isEmpty else { return true }
        let stamps = stamp(stampedFiles)
        guard stamps != lastStamps else { return false }
        lastStamps = stamps
        return true
    }

    /// Starts watching `path`. `stampedFiles` (see the type's comment) are
    /// stamped now, so the first event counts only if they change after this.
    func start(path: String, stampedFiles: [String] = [], onChange: @escaping @MainActor () -> Void) {
        stop()
        self.stampedFiles = stampedFiles
        lastStamps = stampedFiles.isEmpty ? [] : stamp(stampedFiles)
        let fd = open(path, O_EVTONLY)
        guard fd >= 0 else { return }
        fileDescriptor = fd

        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .rename, .extend],
            queue: .main
        )
        source.setEventHandler { [weak self] in
            self?.debounce(onChange: onChange)
        }
        source.setCancelHandler { [fd] in
            close(fd)
        }
        source.resume()
        self.source = source
    }

    private func debounce(onChange: @escaping @MainActor () -> Void) {
        debounceTask?.cancel()
        let interval = debounceInterval
        debounceTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: interval)
            guard !Task.isCancelled, let self, self.consumeEventIfStampsMoved() else { return }
            onChange()
        }
    }

    func stop() {
        debounceTask?.cancel()
        debounceTask = nil
        source?.cancel()
        source = nil
    }
}
