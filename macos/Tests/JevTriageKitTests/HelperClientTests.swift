import Foundation
import Testing

@testable import JevTriageKit

/// Exercises the real process path against small shell scripts standing in for the helper.
@Suite struct HelperClientTests {
    let directory: URL

    init() throws {
        directory = FileManager.default.temporaryDirectory.appending(path: "jev-helper-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    func helper(_ body: String) throws -> HelperClient {
        let url = directory.appending(path: "helper-\(UUID().uuidString).sh")
        try "#!/bin/sh\n\(body)\n".write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
        return HelperClient(executable: url, configPath: directory.appending(path: "config.toml"))
    }

    /// Echoes a success response that carries the request id back, and records stdin and argv.
    var echoScript: String {
        """
        printf '%s\\n' "$@" > "\(directory.path)/argv"
        read -r line
        printf '%s' "$line" > "\(directory.path)/stdin"
        id=$(printf '%s' "$line" | sed -E 's/.*"id":"([^"]+)".*/\\1/')
        printf '{"protocol":1,"id":"%s","ok":true,"result":{"stored":"typesafe-api-key"}}\\n' "$id"
        echo diagnostics >&2
        """
    }

    @Test func sendsRequestOnStdinAndSecretsNeverAppearInArguments() async throws {
        let client = try helper(echoScript)
        let result = try await client.call(
            "secrets.set", params: ["name": "typesafe-api-key", "value": "sk-secret-value"], as: StoredSecret.self)
        #expect(result.stored == "typesafe-api-key")
        let argv = try String(contentsOf: directory.appending(path: "argv"), encoding: .utf8)
        let stdin = try String(contentsOf: directory.appending(path: "stdin"), encoding: .utf8)
        #expect(argv.split(separator: "\n") == ["--config", "\(directory.path)/config.toml", "rpc"])
        #expect(!argv.contains("sk-secret-value"))
        #expect(stdin.contains("sk-secret-value"))
        #expect(stdin.contains(#""command":"secrets.set""#))
    }

    @Test func helperErrorIsSurfacedWithItsCode() async throws {
        let client = try helper(
            """
            read -r line
            id=$(printf '%s' "$line" | sed -E 's/.*"id":"([^"]+)".*/\\1/')
            printf '{"protocol":1,"id":"%s","ok":false,"error":{"code":"bridge_unavailable","message":"down","retryable":true}}\\n' "$id"
            exit 3
            """)
        await #expect(throws: HelperError(code: "bridge_unavailable", message: "down", retryable: true)) {
            try await client.call("check.bridge", as: BridgeCheck.self)
        }
    }

    @Test func timesOutAndKillsAHungHelper() async throws {
        let client = try helper("exec sleep 30")
        let started = Date()
        await #expect(throws: HelperClientError.timedOut(command: "status", seconds: 0.5)) {
            try await client.call("status", timeout: 0.5, as: StatusResult.self)
        }
        #expect(Date().timeIntervalSince(started) < 10)
    }

    @Test func crashWithoutResponseIsReportedAsTermination() async throws {
        let client = try helper("echo 'Traceback: boom' >&2\nkill -SEGV $$")
        do {
            _ = try await client.call("status", as: StatusResult.self)
            Issue.record("expected termination")
        } catch HelperClientError.terminated(let status, let signaled, let stderr) {
            #expect(signaled)
            #expect(status == SIGSEGV)
            #expect(stderr.contains("boom"))
        }
    }

    @Test func nonzeroExitWithoutOutputIsTermination() async throws {
        let client = try helper("cat >/dev/null\nexit 1")
        await #expect(throws: HelperClientError.terminated(status: 1, signaled: false, stderr: "")) {
            try await client.call("status", as: StatusResult.self)
        }
    }

    @Test func strayStdoutIsMalformed() async throws {
        let client = try helper("cat >/dev/null\necho hello world")
        await #expect(throws: HelperClientError.malformedResponse("not a protocol response")) {
            try await client.call("status", as: StatusResult.self)
        }
    }

    @Test func successWithFailingExitStatusIsRejected() async throws {
        let client = try helper(echoScript.replacingOccurrences(of: "echo diagnostics >&2", with: "exit 4"))
        await #expect(throws: HelperClientError.malformedResponse("success reported with exit status 4")) {
            try await client.call("secrets.set", as: StoredSecret.self)
        }
    }

    @Test func cancellationStopsTheHelper() async throws {
        let client = try helper("exec sleep 30")
        let task = Task { try await client.call("status", timeout: 60, as: StatusResult.self) }
        try await Task.sleep(for: .milliseconds(300))
        task.cancel()
        await #expect(throws: HelperClientError.cancelled) { try await task.value }
    }

    @Test func helperThatExitsBeforeReadingStdinDoesNotCrashTheApp() async throws {
        let client = try helper("exit 2")
        let big = String(repeating: "x", count: 512 * 1024)
        await #expect(throws: HelperClientError.self) {
            try await client.call("secrets.set", params: ["value": .string(big)], as: StoredSecret.self)
        }
    }

    @Test func missingHelperIsReported() async {
        let client = HelperClient(executable: URL(fileURLWithPath: "/nonexistent/helper"))
        await #expect(throws: HelperClientError.helperMissing("/nonexistent/helper")) {
            try await client.call("hello", as: HelloResult.self)
        }
    }

    @Test func environmentDropsPythonOverrides() {
        let env = HelperClient.helperEnvironment(["PYTHONPATH": "/x", "PYTHONHOME": "/y", "VIRTUAL_ENV": "/v", "HOME": "/h"])
        #expect(env == ["HOME": "/h"])
    }
}
