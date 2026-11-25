import Foundation
import Testing

@testable import JevTriageKit

@Suite struct LifecycleTests {
    let now: Double = 1_764_100_020

    func status(_ state: String, heartbeat: Double? = nil, error: WorkerError? = nil, holder: Int32? = nil)
        -> WorkerStatus
    {
        WorkerStatus(
            protocol: 1, version: "1.2.0", pid: 4242, state: state, since: now - 100,
            heartbeat: heartbeat ?? now - 5, error: error, lastCycle: nil, nextCycleAt: now + 30,
            consecutiveFailures: 0, lockHolder: holder, settings: nil)
    }

    func inputs(
        configured: Bool = true, secrets: Bool = true, registration: AgentRegistration = .enabled,
        legacy: LegacyInstallation = .none, worker: WorkerStatus? = nil, alive: Bool = true, paused: Bool = false
    ) -> LifecycleInputs {
        LifecycleInputs(
            configured: configured, secretsPresent: secrets, registration: registration, legacy: legacy,
            worker: worker, workerAlive: alive, control: WorkerControl(paused: paused, generation: 1), now: now)
    }

    @Test func legacyInstallationTakesPrecedence() {
        let legacy = LegacyInstallation(plist: URL(fileURLWithPath: "/tmp/x.plist"), loaded: true)
        #expect(Lifecycle.phase(inputs(configured: false, legacy: legacy)) == .legacyInstalled(loaded: true))
    }

    @Test func setupIsRequiredUntilConfigAndSecretsExist() {
        #expect(Lifecycle.phase(inputs(configured: false)) == .needsSetup)
        #expect(Lifecycle.phase(inputs(secrets: false)) == .needsSetup)
    }

    @Test func registrationStates() {
        #expect(Lifecycle.phase(inputs(registration: .notRegistered)) == .stopped)
        #expect(Lifecycle.phase(inputs(registration: .requiresApproval)) == .needsApproval)
        if case .error(let code, _) = Lifecycle.phase(inputs(registration: .notFound)) {
            #expect(code == "agent_missing")
        } else {
            Issue.record("notFound must be an error")
        }
    }

    @Test func workerStatesMapToPhases() {
        #expect(Lifecycle.phase(inputs(worker: status("idle"))) == .running(idle: true))
        #expect(Lifecycle.phase(inputs(worker: status("running"))) == .running(idle: false))
        #expect(Lifecycle.phase(inputs(worker: status("waiting_for_lock", holder: 77))) == .waitingForLock(holder: 77))
        let down = WorkerError(code: "bridge_unavailable", message: "Bridge down", at: now)
        #expect(
            Lifecycle.phase(inputs(worker: status("bridge_unavailable", error: down)))
                == .bridgeUnavailable(message: "Bridge down", retryAt: now + 30))
        let bad = WorkerError(code: "bridge_auth", message: "Wrong password", at: now)
        #expect(
            Lifecycle.phase(inputs(worker: status("error", error: bad)))
                == .error(code: "bridge_auth", message: "Wrong password"))
    }

    @Test func pauseIsReportedEvenBeforeWorkerAcknowledges() {
        #expect(Lifecycle.phase(inputs(worker: status("running"), paused: true)) == .paused)
        #expect(Lifecycle.phase(inputs(worker: nil, paused: true)) == .paused)
        #expect(Lifecycle.phase(inputs(worker: status("paused"), alive: false, paused: true)) == .paused)
    }

    @Test func resumeIsStartingUntilWorkerPicksItUp() {
        #expect(Lifecycle.phase(inputs(worker: status("paused"))) == .starting)
        #expect(Lifecycle.phase(inputs(worker: nil)) == .starting)
    }

    @Test func deadOrStaleWorkerIsUnresponsiveAfterGracePeriod() {
        #expect(Lifecycle.phase(inputs(worker: status("idle", heartbeat: now - 10), alive: false)) == .starting)
        #expect(Lifecycle.phase(inputs(worker: status("idle", heartbeat: now - 300), alive: false)) == .unresponsive)
        #expect(Lifecycle.phase(inputs(worker: status("running", heartbeat: now - 300), alive: true)) == .unresponsive)
    }

    @Test func protocolMismatchIsAnError() {
        let old = WorkerStatus(
            protocol: 99, version: "9", pid: 1, state: "idle", since: now, heartbeat: now, error: nil,
            lastCycle: nil, nextCycleAt: nil, consecutiveFailures: 0, lockHolder: nil, settings: nil)
        if case .error(let code, _) = Lifecycle.phase(inputs(worker: old)) {
            #expect(code == "protocol_mismatch")
        } else {
            Issue.record("expected protocol mismatch")
        }
    }
}

@Suite struct StateFilesTests {
    @Test func readsWorkerFilesWrittenByPython() throws {
        let home = FileManager.default.temporaryDirectory.appending(path: "jev-state-\(UUID().uuidString)")
        let paths = AppPaths(home: home)
        try FileManager.default.createDirectory(at: paths.stateDirectory, withIntermediateDirectories: true)
        let files = StateFiles(paths: paths)
        #expect(files.status() == nil)
        #expect(files.control() == WorkerControl(paused: false, generation: 0))

        try fixture("worker-status").write(to: paths.statusFile)
        try #"{"protocol": 1, "paused": true, "generation": 4, "updated_at": 1.0}"#.write(
            to: paths.controlFile, atomically: true, encoding: .utf8)
        let status = try #require(files.status())
        #expect(status.state == "bridge_unavailable")
        #expect(status.lastCycle?.moved == 1)
        #expect(status.settings?["gate"]?.stringValue == "shadow")
        #expect(files.control() == WorkerControl(paused: true, generation: 4))
    }

    @Test func aliveCheck() {
        #expect(StateFiles.isAlive(pid: getpid()))
        #expect(!StateFiles.isAlive(pid: 0))
    }
}

@Suite struct LegacyAgentTests {
    final class FakeLaunchctl: @unchecked Sendable {
        private let lock = NSLock()
        var loaded: Bool
        var unloadOnBootout: Bool
        private(set) var calls: [[String]] = []

        init(loaded: Bool, unloadOnBootout: Bool = true) {
            self.loaded = loaded
            self.unloadOnBootout = unloadOnBootout
        }

        func run(_ arguments: [String]) -> Int32 {
            lock.lock()
            defer { lock.unlock() }
            calls.append(arguments)
            if arguments.first == "bootout", unloadOnBootout { loaded = false }
            if arguments.first == "print" { return loaded ? 0 : 113 }
            return 0
        }
    }

    func makeHome(withPlist: Bool) throws -> AppPaths {
        let paths = AppPaths(home: FileManager.default.temporaryDirectory.appending(path: "jev-legacy-\(UUID().uuidString)"))
        try FileManager.default.createDirectory(at: paths.launchAgents, withIntermediateDirectories: true)
        if withPlist {
            try "<plist/>".write(
                to: paths.launchAgents.appending(path: "\(LegacyAgent.label).plist"), atomically: true, encoding: .utf8)
        }
        return paths
    }

    @Test func detectsPlistAndLoadedJob() throws {
        let fake = FakeLaunchctl(loaded: true)
        let agent = LegacyAgent(paths: try makeHome(withPlist: true), uid: 501, launchctl: fake.run)
        let found = agent.detect()
        #expect(found.loaded && found.plist != nil)
        #expect(fake.calls == [["print", "gui/501/com.soderlind.jev-mail-triage"]])

        let none = LegacyAgent(paths: try makeHome(withPlist: false), uid: 501, launchctl: FakeLaunchctl(loaded: false).run)
        #expect(none.detect() == .none)
    }

    @Test func migrationStopsJobAndKeepsRenamedPlist() throws {
        let fake = FakeLaunchctl(loaded: true)
        let paths = try makeHome(withPlist: true)
        let agent = LegacyAgent(paths: paths, uid: 501, launchctl: fake.run)
        let moved = try #require(try agent.migrate(now: Date(timeIntervalSince1970: 0)))
        #expect(fake.calls.contains(["bootout", "gui/501/com.soderlind.jev-mail-triage"]))
        #expect(moved.lastPathComponent.hasPrefix("com.soderlind.jev-mail-triage.plist.migrated-1970"))
        #expect(FileManager.default.fileExists(atPath: moved.path))
        #expect(agent.detect() == .none)
    }

    @Test func migrationRefusesWhileJobKeepsRunning() throws {
        let fake = FakeLaunchctl(loaded: true, unloadOnBootout: false)
        let paths = try makeHome(withPlist: true)
        let agent = LegacyAgent(paths: paths, uid: 501, launchctl: fake.run)
        #expect(throws: LegacyMigrationError.stillLoaded) { try agent.migrate(waitSeconds: 0.2) }
        #expect(FileManager.default.fileExists(atPath: agent.plistURL.path))
    }
}
