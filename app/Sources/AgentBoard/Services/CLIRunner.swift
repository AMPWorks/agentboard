import Foundation

/// Runs an external executable off the main thread and collects its output.
struct CLIRunner: Sendable {
    enum CLIError: Error, LocalizedError {
        case launchFailed(String)
        case nonZeroExit(Int32, String)

        var errorDescription: String? {
            switch self {
            case .launchFailed(let reason): return "Failed to launch process: \(reason)"
            case .nonZeroExit(let code, let stderr): return "Exited \(code): \(stderr)"
            }
        }
    }

    /// Accumulates pipe output under a lock; safe to touch from readability handlers.
    private final class Accumulator: @unchecked Sendable {
        private let lock = NSLock()
        private var data = Data()

        func append(_ chunk: Data) {
            lock.lock()
            data.append(chunk)
            lock.unlock()
        }

        func snapshot() -> Data {
            lock.lock()
            defer { lock.unlock() }
            return data
        }
    }

    func run(executable: String, arguments: [String], workingDirectory: String? = nil) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = arguments
            if let workingDirectory {
                process.currentDirectoryURL = URL(fileURLWithPath: workingDirectory)
            }

            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            process.standardOutput = stdoutPipe
            process.standardError = stderrPipe

            let stdoutBuffer = Accumulator()
            let stderrBuffer = Accumulator()

            // Readability handlers keep both pipes drained while the child
            // runs so neither can fill up and block it. An empty chunk means
            // EOF: stop the handler there instead of letting it spin.
            stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
                let chunk = handle.availableData
                if chunk.isEmpty { handle.readabilityHandler = nil } else { stdoutBuffer.append(chunk) }
            }
            stderrPipe.fileHandleForReading.readabilityHandler = { handle in
                let chunk = handle.availableData
                if chunk.isEmpty { handle.readabilityHandler = nil } else { stderrBuffer.append(chunk) }
            }

            process.terminationHandler = { finished in
                // The termination handler can fire before the readability
                // handlers have delivered the child's last chunk(s). Detach
                // them and read whatever is still buffered in each pipe to
                // EOF, so a snapshot's trailing bytes are never dropped --
                // which would make its JSON decode fail intermittently.
                stdoutPipe.fileHandleForReading.readabilityHandler = nil
                stderrPipe.fileHandleForReading.readabilityHandler = nil
                stdoutBuffer.append(stdoutPipe.fileHandleForReading.readDataToEndOfFile())
                stderrBuffer.append(stderrPipe.fileHandleForReading.readDataToEndOfFile())
                let out = stdoutBuffer.snapshot()
                if finished.terminationStatus == 0 {
                    continuation.resume(returning: out)
                } else {
                    let err = String(data: stderrBuffer.snapshot(), encoding: .utf8) ?? ""
                    continuation.resume(throwing: CLIError.nonZeroExit(finished.terminationStatus, err))
                }
            }

            do {
                try process.run()
            } catch {
                stdoutPipe.fileHandleForReading.readabilityHandler = nil
                stderrPipe.fileHandleForReading.readabilityHandler = nil
                continuation.resume(throwing: CLIError.launchFailed(error.localizedDescription))
            }
        }
    }
}
