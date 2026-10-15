import Foundation

/// Watches a directory for writes using a DispatchSource file-system-object
/// source, debouncing bursts of events (e.g. WAL checkpoints).
@MainActor
final class DirectoryWatcher {
    private var source: DispatchSourceFileSystemObject?
    private var fileDescriptor: CInt = -1
    private var debounceTask: Task<Void, Never>?
    private let debounceInterval: Duration

    init(debounceInterval: Duration = .milliseconds(500)) {
        self.debounceInterval = debounceInterval
    }

    func start(path: String, onChange: @escaping @MainActor () -> Void) {
        stop()
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
        debounceTask = Task { @MainActor in
            try? await Task.sleep(for: interval)
            guard !Task.isCancelled else { return }
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
