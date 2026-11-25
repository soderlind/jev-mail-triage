import Foundation

public enum HelperClientError: Error, Equatable, Sendable, LocalizedError {
    case helperMissing(String)
    case launchFailed(String)
    case timedOut(command: String, seconds: Double)
    case cancelled
    case terminated(status: Int32, signaled: Bool, stderr: String)
    case malformedResponse(String)
    case protocolMismatch(helper: Int, app: Int)

    public var errorDescription: String? {
        switch self {
        case .helperMissing(let path):
            return "The bundled helper is missing at \(path). Reinstall Jev Mail Triage."
        case .launchFailed(let reason):
            return "The helper could not be started: \(reason)"
        case .timedOut(let command, let seconds):
            return "The helper did not answer \(command) within \(Int(seconds)) seconds."
        case .cancelled:
            return "The request was cancelled."
        case .terminated(let status, let signaled, _):
            return signaled
                ? "The helper crashed (signal \(status))."
                : "The helper exited with status \(status) without answering."
        case .malformedResponse(let reason):
            return "The helper sent an invalid response (\(reason))."
        case .protocolMismatch(let helper, let app):
            return "The helper speaks protocol \(helper) but the app expects \(app). Reinstall Jev Mail Triage."
        }
    }
}

/// Raw result of one helper process.
public struct ProcessOutput: Sendable {
    public let stdout: Data
    public let stderr: Data
    public let status: Int32
    public let signaled: Bool
}

/// Runs one process with argument arrays only (no shell), feeding stdin and collecting output.
/// Never blocks the caller's actor: all waiting happens on background queues.
public enum ProcessRunner {
    public static func run(
        executable: URL, arguments: [String], stdin: Data, environment: [String: String]?,
        timeout: TimeInterval, label: String
    ) async throws -> ProcessOutput {
        let state = RunState()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<ProcessOutput, Error>) in
                let process = Process()
                process.executableURL = executable
                process.arguments = arguments
                if let environment { process.environment = environment }
                let input = Pipe(), output = Pipe(), errors = Pipe()
                process.standardInput = input
                process.standardOutput = output
                process.standardError = errors

                let group = DispatchGroup()
                group.enter()
                process.terminationHandler = { _ in group.leave() }
                do {
                    try process.run()
                } catch {
                    continuation.resume(throwing: HelperClientError.launchFailed(error.localizedDescription))
                    return
                }
                state.attach(process)

                let collected = Collected()
                group.enter()
                DispatchQueue.global(qos: .utility).async {
                    collected.setStdout(output.fileHandleForReading.readDataToEndOfFile())
                    group.leave()
                }
                group.enter()
                DispatchQueue.global(qos: .utility).async {
                    collected.setStderr(errors.fileHandleForReading.readDataToEndOfFile())
                    group.leave()
                }
                DispatchQueue.global(qos: .utility).async {
                    let handle = input.fileHandleForWriting
                    // A helper that exits early must not raise SIGPIPE in the app.
                    _ = fcntl(handle.fileDescriptor, F_SETNOSIGPIPE, 1)
                    try? handle.write(contentsOf: stdin)
                    try? handle.close()
                }
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout) {
                    state.stop(reason: .timedOut)
                }
                group.notify(queue: .global(qos: .utility)) {
                    switch state.stopReason {
                    case .timedOut:
                        continuation.resume(throwing: HelperClientError.timedOut(command: label, seconds: timeout))
                    case .cancelled:
                        continuation.resume(throwing: HelperClientError.cancelled)
                    case nil:
                        continuation.resume(
                            returning: ProcessOutput(
                                stdout: collected.stdout, stderr: collected.stderr,
                                status: process.terminationStatus,
                                signaled: process.terminationReason == .uncaughtSignal))
                    }
                }
            }
        } onCancel: {
            state.stop(reason: .cancelled)
        }
    }
}

enum StopReason: Sendable { case timedOut, cancelled }

/// Tracks a running process so timeout and cancellation can stop it: SIGTERM, then SIGKILL.
final class RunState: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var reason: StopReason?
    private var finished = false

    var stopReason: StopReason? {
        lock.lock()
        defer { lock.unlock() }
        finished = true
        return reason
    }

    func attach(_ process: Process) {
        lock.lock()
        self.process = process
        let pending = reason
        lock.unlock()
        if pending != nil { terminate(process) }
    }

    func stop(reason newReason: StopReason) {
        lock.lock()
        guard !finished, reason == nil else {
            lock.unlock()
            return
        }
        reason = newReason
        let running = process
        lock.unlock()
        if let running { terminate(running) }
    }

    private func terminate(_ process: Process) {
        guard process.isRunning else { return }
        let pid = process.processIdentifier
        process.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
            if process.isRunning { kill(pid, SIGKILL) }
        }
    }
}

final class Collected: @unchecked Sendable {
    private let lock = NSLock()
    private var out = Data()
    private var err = Data()

    func setStdout(_ data: Data) { lock.withLock { out = data } }
    func setStderr(_ data: Data) { lock.withLock { err = data } }
    var stdout: Data { lock.withLock { out } }
    var stderr: Data { lock.withLock { err } }
}

/// Client for the bundled helper's versioned JSON protocol (`helper rpc`).
/// Requests, including secrets, travel on stdin; never in process arguments.
public struct HelperClient: Sendable {
    public let executable: URL
    public let configPath: URL?
    public let defaultTimeout: TimeInterval

    public init(executable: URL, configPath: URL? = nil, defaultTimeout: TimeInterval = 20) {
        self.executable = executable
        self.configPath = configPath
        self.defaultTimeout = defaultTimeout
    }

    public var arguments: [String] {
        var arguments: [String] = []
        if let configPath { arguments += ["--config", configPath.path] }
        return arguments + ["rpc"]
    }

    /// Inherit the user environment minus variables that could redirect the frozen interpreter.
    public static func helperEnvironment(_ base: [String: String] = ProcessInfo.processInfo.environment)
        -> [String: String]
    {
        base.filter { key, _ in !key.hasPrefix("PYTHON") && key != "VIRTUAL_ENV" }
    }

    public func call<Result: Decodable & Sendable>(
        _ command: String, params: [String: JSONValue] = [:], timeout: TimeInterval? = nil,
        as type: Result.Type = Result.self
    ) async throws -> Result {
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw HelperClientError.helperMissing(executable.path)
        }
        let id = UUID().uuidString
        let request: JSONValue = [
            "protocol": .number(Double(helperProtocolVersion)), "id": .string(id),
            "command": .string(command), "params": .object(params),
        ]
        var body = try JSONEncoder().encode(request)
        body.append(0x0A)
        let output = try await ProcessRunner.run(
            executable: executable, arguments: arguments, stdin: body,
            environment: Self.helperEnvironment(), timeout: timeout ?? defaultTimeout, label: command)
        return try Self.interpret(output, id: id, as: type)
    }

    static func interpret<Result: Decodable>(_ output: ProcessOutput, id: String, as type: Result.Type) throws
        -> Result
    {
        let stderr = String(decoding: output.stderr.suffix(4096), as: UTF8.self)
        do {
            let result = try ResponseDecoder.decode(output.stdout, expectingID: id, as: type)
            guard output.status == 0, !output.signaled else {
                throw HelperClientError.malformedResponse("success reported with exit status \(output.status)")
            }
            return result
        } catch let error as HelperClientError {
            if case .malformedResponse = error, output.signaled || output.stdout.isEmpty {
                throw HelperClientError.terminated(
                    status: output.status, signaled: output.signaled, stderr: stderr)
            }
            throw error
        }
    }
}
