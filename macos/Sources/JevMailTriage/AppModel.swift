import AppKit
import Foundation
import JevTriageKit
import Observation

struct AppMessage: LocalizedError {
    let errorDescription: String?
    let recoverySuggestion: String?

    init(_ message: String, suggestion: String? = nil) {
        errorDescription = message
        recoverySuggestion = suggestion
    }
}

/// An error shown to the user, with an optional recovery hint.
struct Notice: Identifiable, Equatable {
    let id = UUID()
    let title: String
    let message: String
    let suggestion: String?

    init(_ title: String, _ error: Error) {
        self.title = title
        self.message = error.localizedDescription
        self.suggestion = (error as? HelperError)?.recoverySuggestion
            ?? (error as? LocalizedError)?.recoverySuggestion
    }
}

/// All app state. Every helper call is async so SwiftUI never blocks on helper work.
@MainActor @Observable
final class AppModel {
    let paths = AppPaths()
    let helper: HelperClient
    private let files: StateFiles
    private let legacyAgent: LegacyAgent

    private(set) var phase: Phase = .starting
    private(set) var config: AppConfig?
    private(set) var configured = false
    private(set) var bridgeSecretPresent = false
    private(set) var typeSafeSecretPresent = false
    private(set) var registration: AgentRegistration = .notRegistered
    private(set) var legacy: LegacyInstallation = .none
    private(set) var worker: WorkerStatus?
    private(set) var control = WorkerControl(paused: false, generation: 0)
    private(set) var status: StatusResult?
    private(set) var launchAtLogin = false
    private(set) var busy: Set<String> = []
    var notice: Notice?

    private var pollTask: Task<Void, Never>?
    private var lastSlowRefresh = Date.distantPast

    init() {
        helper = HelperClient(executable: Self.helperExecutable())
        files = StateFiles(paths: paths)
        legacyAgent = LegacyAgent(paths: paths)
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in await self?.refresh(full: true) }
        }
        startPolling()
    }

    /// The helper inside the app bundle; `JEV_HELPER` overrides it for `swift run` development.
    static func helperExecutable() -> URL {
        if let override = ProcessInfo.processInfo.environment["JEV_HELPER"], !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        return Bundle.main.bundleURL.appending(
            path: "Contents/Helpers/JevMailTriageHelper.app/Contents/MacOS/jev-mail-triage-helper")
    }

    var isDryRun: Bool { !(config?.policy.moveEnabled ?? false) }
    var needsSetup: Bool { phase == .needsSetup }
    func isBusy(_ key: String) -> Bool { busy.contains(key) }

    // MARK: Refresh

    private func startPolling() {
        pollTask = Task { [weak self] in
            await self?.refresh(full: true)
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                await self?.refresh(full: false)
            }
        }
    }

    /// Cheap refresh reads status files directly; a full refresh also asks the helper and launchctl.
    func refresh(full: Bool) async {
        let files = self.files
        let local = await Task.detached { () -> (WorkerStatus?, WorkerControl, Bool, AgentRegistration) in
            let status = files.status()
            let alive = status.map { StateFiles.isAlive(pid: $0.pid) } ?? false
            return (status, files.control(), alive, BackgroundService.registration())
        }.value
        (worker, control, registration) = (local.0, local.1, local.3)
        let alive = local.2

        if full || Date().timeIntervalSince(lastSlowRefresh) > 30 {
            lastSlowRefresh = Date()
            await refreshSlow()
        }
        phase = Lifecycle.phase(
            LifecycleInputs(
                configured: configured, secretsPresent: bridgeSecretPresent && typeSafeSecretPresent,
                registration: registration, legacy: legacy, worker: worker, workerAlive: alive, control: control))
    }

    private func refreshSlow() async {
        let legacyAgent = self.legacyAgent
        legacy = await Task.detached { legacyAgent.detect() }.value
        launchAtLogin = BackgroundService.launchAtLogin
        do {
            let result = try await helper.call("config.get", as: ConfigResult.self)
            configured = result.exists ?? (result.config != nil)
            config = result.config
        } catch let error as HelperError where error.code == "not_configured" {
            configured = false
            config = nil
        } catch {
            notice = Notice("Could not read settings", error)
        }
        let account = config?.bridge.accountId ?? "primary"
        (bridgeSecretPresent, typeSafeSecretPresent) = await Task.detached {
            (
                KeychainPresence.exists(account: KeychainPresence.bridgeAccount(account)),
                KeychainPresence.exists(account: KeychainPresence.typeSafeAccount)
            )
        }.value
        if configured { status = try? await helper.call("status", as: StatusResult.self) }
    }

    private func perform(_ key: String, _ title: String, _ work: () async throws -> Void) async {
        busy.insert(key)
        defer { busy.remove(key) }
        do {
            try await work()
        } catch HelperClientError.cancelled {
            return
        } catch {
            notice = Notice(title, error)
        }
        await refresh(full: true)
    }

    // MARK: Background lifecycle

    /// Registers the LaunchAgent (if needed) and clears any pause.
    func start() async {
        guard !legacy.isPresent else {
            notice = Notice(
                "Migrate the legacy LaunchAgent first",
                AppMessage(
                    "The legacy LaunchAgent would run a second worker against the same mailbox.",
                    suggestion: "Choose Migrate in the dashboard to replace it."))
            return
        }
        await perform("start", "Could not start background triage") {
            _ = try await helper.call("worker.control", params: ["action": "resume"], as: ControlResult.self)
            if try await BackgroundService.register() == .requiresApproval {
                BackgroundService.openLoginItemsSettings()
            }
        }
    }

    func pause() async {
        await perform("pause", "Could not pause triage") {
            _ = try await helper.call("worker.control", params: ["action": "pause"], as: ControlResult.self)
        }
    }

    /// Unregisters the LaunchAgent entirely; triage stops until Start is chosen again.
    func turnOffBackground() async {
        await perform("start", "Could not turn off background triage") {
            try await BackgroundService.unregister()
        }
    }

    func setLaunchAtLogin(_ enabled: Bool) async {
        await perform("login", "Could not change Launch at Login") {
            try BackgroundService.setLaunchAtLogin(enabled)
        }
    }

    /// Stops and sets aside the legacy agent, lets the helper adopt existing Keychain items,
    /// then starts the bundled agent. Nothing happens without the user's confirmation.
    func migrateLegacy() async {
        let legacyAgent = self.legacyAgent
        await perform("migrate", "Migration did not finish") {
            _ = try await Task.detached { try legacyAgent.migrate() }.value
            legacy = .none
            if configured {
                _ = try await helper.call("secrets.adopt", timeout: 120, as: AdoptResult.self)
            }
        }
        if !legacy.isPresent, notice == nil, configured { await start() }
    }

    // MARK: Settings

    func saveSettings(_ draft: AppConfig) async -> Bool {
        var saved = false
        await perform("settings", "Settings were not saved") {
            let patch: JSONValue = [
                "bridge": draft.bridge.patch, "service": draft.service.patch,
                "policy": draft.policy.patch, "gate": draft.gate.patch,
            ]
            let result = try await helper.call("config.set", params: ["patch": patch], as: ConfigResult.self)
            config = result.config
            _ = try? await helper.call("worker.control", params: ["action": "reload"], as: ControlResult.self)
            saved = true
        }
        return saved
    }

    /// Turns automatic moves on or off. Enabling sends the explicit confirmation flag.
    func setMoves(enabled: Bool) async {
        await perform("moves", "Could not change automatic moves") {
            var params: [String: JSONValue] = ["patch": ["policy": ["move_enabled": .bool(enabled)]]]
            if enabled { params["confirm_enable_moves"] = true }
            let result = try await helper.call("config.set", params: params, as: ConfigResult.self)
            config = result.config
            _ = try? await helper.call("worker.control", params: ["action": "reload"], as: ControlResult.self)
        }
    }

    func storeSecret(_ name: String, value: String, accountID: String?) async throws {
        var params: [String: JSONValue] = ["name": .string(name), "value": .string(value)]
        if let accountID { params["account_id"] = .string(accountID) }
        _ = try await helper.call("secrets.set", params: params, timeout: 60, as: StoredSecret.self)
    }

    func updateSecret(_ name: String, value: String) async {
        let account = config?.bridge.accountId
        await perform("secret", "The secret was not saved") {
            try await storeSecret(name, value: value, accountID: account)
        }
    }

    // MARK: Setup

    func checkBridge(_ bridge: BridgeSettings, password: String?) async throws -> BridgeCheck {
        var params: [String: JSONValue] = ["overrides": ["bridge": bridge.patch]]
        if let password, !password.isEmpty { params["password"] = .string(password) }
        return try await helper.call("check.bridge", params: params, timeout: 45, as: BridgeCheck.self)
    }

    /// Bridge folders for pickers; nil until loaded. Uses the saved Keychain password.
    private(set) var folders: [String]?
    private(set) var loadingFolders = false

    func listFolders(_ bridge: BridgeSettings, password: String?) async throws -> [String] {
        var params: [String: JSONValue] = ["overrides": ["bridge": bridge.patch]]
        if let password, !password.isEmpty { params["password"] = .string(password) }
        return try await helper.call("bridge.folders", params: params, timeout: 45, as: FolderList.self).folders
    }

    /// Best effort: on failure pickers fall back to free text, so errors are not surfaced.
    func loadFolders(force: Bool = false) async {
        guard let bridge = config?.bridge, bridgeSecretPresent, !loadingFolders, force || folders == nil else { return }
        loadingFolders = true
        defer { loadingFolders = false }
        folders = try? await listFolders(bridge, password: nil)
    }

    func checkTypeSafe(apiKey: String?) async throws -> TypeSafeCheck {
        var params: [String: JSONValue] = [:]
        if let apiKey, !apiKey.isEmpty { params["api_key"] = .string(apiKey) }
        return try await helper.call("check.typesafe", params: params, timeout: 45, as: TypeSafeCheck.self)
    }

    /// Saves Bridge settings and secrets. Moves stay off (dry run) for a new configuration.
    func completeSetup(bridge: BridgeSettings, password: String?, apiKey: String?) async throws {
        let result = try await helper.call(
            "config.set", params: ["patch": ["bridge": bridge.patch]], as: ConfigResult.self)
        config = result.config
        configured = true
        if let password, !password.isEmpty {
            try await storeSecret("bridge-password", value: password, accountID: bridge.accountId)
        }
        if let apiKey, !apiKey.isEmpty {
            try await storeSecret("typesafe-api-key", value: apiKey, accountID: nil)
        }
        await refresh(full: true)
    }

    // MARK: Data

    func loadStats(days: Int?) async throws -> StatsResult {
        var params: [String: JSONValue] = [:]
        if let days { params["days"] = .number(Double(days)) }
        return try await helper.call("stats", params: params, as: StatsResult.self)
    }

    func loadCandidates(limit: Int = 200) async throws -> CandidatesResult {
        try await helper.call("candidates", params: ["limit": .number(Double(limit))], as: CandidatesResult.self)
    }

    /// Fetches headers read-only for display. The result lives only in the caller's view state.
    func fetchHeaders(_ keys: [CandidateKey]) async throws -> CandidateHeadersResult {
        try await helper.call(
            "candidates.headers", params: ["items": .array(keys.prefix(50).map(\.json))], timeout: 60,
            as: CandidateHeadersResult.self)
    }
}
