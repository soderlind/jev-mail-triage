import Foundation

/// Registration state of the bundled LaunchAgent, mirrored from `SMAppService.Status`.
public enum AgentRegistration: Equatable, Sendable {
    case notRegistered
    case enabled
    case requiresApproval
    case notFound

    /// `SMAppService` reports a never-registered agent as `.notFound`, the same status it
    /// uses when the plist is missing. Only a missing plist is a real error.
    public func resolved(agentPlistPresent: Bool) -> AgentRegistration {
        self == .notFound && agentPlistPresent ? .notRegistered : self
    }
}

/// What the app shows as the overall state; derived purely from observable inputs.
public enum Phase: Equatable, Sendable {
    case needsSetup
    case legacyInstalled(loaded: Bool)
    case needsApproval
    case stopped
    case paused
    case starting
    case running(idle: Bool)
    case bridgeUnavailable(message: String, retryAt: Double?)
    case waitingForLock(holder: Int32?)
    case error(code: String, message: String)
    case unresponsive

    public var title: String {
        switch self {
        case .needsSetup: return "Setup required"
        case .legacyInstalled: return "Legacy LaunchAgent found"
        case .needsApproval: return "Needs approval in System Settings"
        case .stopped: return "Stopped"
        case .paused: return "Paused"
        case .starting: return "Starting…"
        case .running(let idle): return idle ? "Running · idle" : "Running · checking mail"
        case .bridgeUnavailable: return "Bridge unavailable"
        case .waitingForLock: return "Waiting for another worker"
        case .error: return "Error"
        case .unresponsive: return "Worker not responding"
        }
    }

    public var symbol: String {
        switch self {
        case .running: return "envelope.badge.shield.half.filled"
        case .paused, .stopped: return "pause.circle"
        case .starting: return "hourglass"
        case .needsSetup, .needsApproval, .legacyInstalled: return "exclamationmark.circle"
        case .bridgeUnavailable, .waitingForLock: return "bolt.horizontal.circle"
        case .error, .unresponsive: return "exclamationmark.triangle"
        }
    }

    /// Whether triage work can currently happen in the background.
    public var isActive: Bool {
        switch self {
        case .running, .starting, .bridgeUnavailable, .waitingForLock, .unresponsive, .error: return true
        default: return false
        }
    }
}

public struct LifecycleInputs: Equatable, Sendable {
    public var configured: Bool
    public var secretsPresent: Bool
    public var registration: AgentRegistration
    public var legacy: LegacyInstallation
    public var worker: WorkerStatus?
    public var workerAlive: Bool
    public var control: WorkerControl
    public var now: Double

    public init(
        configured: Bool, secretsPresent: Bool, registration: AgentRegistration,
        legacy: LegacyInstallation = .none, worker: WorkerStatus? = nil, workerAlive: Bool = false,
        control: WorkerControl = WorkerControl(paused: false, generation: 0),
        now: Double = Date().timeIntervalSince1970
    ) {
        self.configured = configured
        self.secretsPresent = secretsPresent
        self.registration = registration
        self.legacy = legacy
        self.worker = worker
        self.workerAlive = workerAlive
        self.control = control
        self.now = now
    }
}

public enum Lifecycle {
    /// The worker writes a heartbeat every 15 s; one TypeSafe call can hold it for its timeout.
    public static let staleAfter: Double = 120
    /// launchd may wait 10 s (throttle) before restarting a crashed worker.
    public static let restartGrace: Double = 60

    public static func phase(_ input: LifecycleInputs) -> Phase {
        if input.legacy.isPresent { return .legacyInstalled(loaded: input.legacy.loaded) }
        if !input.configured || !input.secretsPresent { return .needsSetup }
        switch input.registration {
        case .requiresApproval: return .needsApproval
        case .notFound:
            return .error(code: "agent_missing", message: "The background agent is missing from the app bundle.")
        case .notRegistered: return .stopped
        case .enabled: break
        }
        guard let status = input.worker else { return input.control.paused ? .paused : .starting }
        guard status.protocol == helperProtocolVersion else {
            return .error(
                code: "protocol_mismatch",
                message: "The background worker speaks protocol \(status.protocol). Restart it.")
        }
        let age = input.now - status.heartbeat
        if !input.workerAlive {
            if input.control.paused { return .paused }
            return age < restartGrace ? .starting : .unresponsive
        }
        if age > staleAfter { return .unresponsive }
        if input.control.paused { return .paused }
        switch status.state {
        case "starting", "paused", "stopped": return .starting
        case "running": return .running(idle: false)
        case "idle": return .running(idle: true)
        case "bridge_unavailable":
            return .bridgeUnavailable(
                message: status.error?.message ?? "Proton Mail Bridge is not reachable.",
                retryAt: status.nextCycleAt)
        case "waiting_for_lock": return .waitingForLock(holder: status.lockHolder)
        case "error":
            return .error(
                code: status.error?.code ?? "error", message: status.error?.message ?? "The worker reported an error.")
        default:
            return .error(code: "unknown_state", message: "Unknown worker state \(status.state).")
        }
    }
}
