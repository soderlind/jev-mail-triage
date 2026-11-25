import Foundation

/// Well-known locations shared with the Python engine (`paths.py`, `config.py`).
public struct AppPaths: Sendable {
    public let home: URL

    public init(home: URL = FileManager.default.homeDirectoryForCurrentUser) {
        self.home = home
    }

    public var config: URL { home.appending(path: ".config/jev-mail-triage/config.toml") }
    public var stateDirectory: URL { home.appending(path: "Library/Application Support/jev-mail-triage") }
    public var logDirectory: URL { home.appending(path: "Library/Logs/JevMailTriage") }
    public var launchAgents: URL { home.appending(path: "Library/LaunchAgents") }
    public var statusFile: URL { stateDirectory.appending(path: "worker-status.json") }
    public var controlFile: URL { stateDirectory.appending(path: "worker-control.json") }
}

/// Reads the worker's status and control files directly, so polling never spawns a process.
public struct StateFiles: Sendable {
    public let paths: AppPaths

    public init(paths: AppPaths) {
        self.paths = paths
    }

    public func status() -> WorkerStatus? {
        guard let data = try? Data(contentsOf: paths.statusFile) else { return nil }
        return try? ResponseDecoder.jsonDecoder.decode(WorkerStatus.self, from: data)
    }

    /// Mirrors `worker.read_control`: a missing or invalid file means "not paused".
    public func control() -> WorkerControl {
        guard let data = try? Data(contentsOf: paths.controlFile),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return WorkerControl(paused: false, generation: 0) }
        return WorkerControl(
            paused: object["paused"] as? Bool == true, generation: object["generation"] as? Int ?? 0)
    }

    public static func isAlive(pid: Int32) -> Bool {
        guard pid > 0 else { return false }
        return kill(pid, 0) == 0 || errno == EPERM
    }
}
