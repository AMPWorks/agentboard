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

    /// The Settings toggle (`@AppStorage`) that turns on Verbose logging.
    static let verboseLoggingKey = "verboseLogging"
    static let logLevelVariable = "AGENTBOARD_LOG_LEVEL"

    /// The child's environment: `base`, plus `AGENTBOARD_LOG_LEVEL=debug`
    /// when Verbose logging is on, so one switch raises both the app's and
    /// the CLI's logging. Never overrides a level already set in `base`
    /// (e.g. one the app itself was launched with).
    static func environment(base: [String: String], verbose: Bool) -> [String: String] {
        var environment = base
        if verbose, (environment[logLevelVariable] ?? "").isEmpty {
            environment[logLevelVariable] = "debug"
        }
        return environment
    }

    func run(executable: String, arguments: [String], workingDirectory: String? = nil) async throws -> Data {
        let verbose = UserDefaults.standard.bool(forKey: Self.verboseLoggingKey)
        let started = ContinuousClock.now
        return try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = arguments
            process.environment = Self.environment(base: ProcessInfo.processInfo.environment, verbose: verbose)
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
                let err = String(decoding: stderrBuffer.snapshot(), as: UTF8.self)
                CLIInvocationRecord(
                    arguments: arguments,
                    exitCode: finished.terminationStatus,
                    duration: ContinuousClock.now - started,
                    stderrTail: err
                ).log(verbose: verbose)
                if finished.terminationStatus == 0 {
                    continuation.resume(returning: out)
                } else {
                    continuation.resume(throwing: CLIError.nonZeroExit(finished.terminationStatus, err))
                }
            }

            do {
                try process.run()
            } catch {
                stdoutPipe.fileHandleForReading.readabilityHandler = nil
                stderrPipe.fileHandleForReading.readabilityHandler = nil
                CLIInvocationRecord(
                    arguments: arguments,
                    exitCode: -1,
                    duration: ContinuousClock.now - started,
                    stderrTail: "launch failed: \(error.localizedDescription)"
                ).log(verbose: verbose)
                continuation.resume(throwing: CLIError.launchFailed(error.localizedDescription))
            }
        }
    }
}
