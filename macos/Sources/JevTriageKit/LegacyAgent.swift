import Foundation

/// The pre-app LaunchAgent (`launchd/com.soderlind.jev-mail-triage.plist`) that runs `serve`.
public struct LegacyInstallation: Equatable, Sendable {
    public var plist: URL?
    public var loaded: Bool

    public static let none = LegacyInstallation(plist: nil, loaded: false)

    public init(plist: URL?, loaded: Bool) {
        self.plist = plist
        self.loaded = loaded
    }

    public var isPresent: Bool { plist != nil || loaded }
}

public enum LegacyMigrationError: Error, LocalizedError, Equatable {
    case stillLoaded
    case renameFailed(String)

    public var errorDescription: String? {
        switch self {
        case .stillLoaded:
            return "The legacy LaunchAgent is still running. Stop it with launchctl bootout and try again."
        case .renameFailed(let reason):
            return "Could not set the legacy LaunchAgent file aside: \(reason)"
        }
    }
}

/// Detects and retires the legacy LaunchAgent. Every call blocks briefly; run off the main actor.
public struct LegacyAgent: Sendable {
    public static let label = "com.soderlind.jev-mail-triage"

    public typealias Launchctl = @Sendable ([String]) -> Int32

    public let paths: AppPaths
    public let uid: uid_t
    public let launchctl: Launchctl

    public init(paths: AppPaths, uid: uid_t = getuid(), launchctl: @escaping Launchctl = LegacyAgent.runLaunchctl) {
        self.paths = paths
        self.uid = uid
        self.launchctl = launchctl
    }

    public var plistURL: URL { paths.launchAgents.appending(path: "\(Self.label).plist") }
    var serviceTarget: String { "gui/\(uid)/\(Self.label)" }

    public func detect() -> LegacyInstallation {
        let plist = FileManager.default.fileExists(atPath: plistURL.path) ? plistURL : nil
        return LegacyInstallation(plist: plist, loaded: launchctl(["print", serviceTarget]) == 0)
    }

    /// Stops the legacy job and renames its plist so launchd will not load it at next login.
    /// The file is kept (`.migrated-<timestamp>`) so the migration can be undone by hand.
    @discardableResult
    public func migrate(now: Date = Date(), waitSeconds: Double = 15) throws -> URL? {
        if launchctl(["print", serviceTarget]) == 0 {
            _ = launchctl(["bootout", serviceTarget])
            let deadline = Date().addingTimeInterval(waitSeconds)
            while launchctl(["print", serviceTarget]) == 0 {
                guard Date() < deadline else { throw LegacyMigrationError.stillLoaded }
                Thread.sleep(forTimeInterval: 0.5)
            }
        }
        guard FileManager.default.fileExists(atPath: plistURL.path) else { return nil }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        let target = plistURL.appendingPathExtension("migrated-\(formatter.string(from: now))")
        do {
            try FileManager.default.moveItem(at: plistURL, to: target)
        } catch {
            throw LegacyMigrationError.renameFailed(error.localizedDescription)
        }
        return target
    }

    public static func runLaunchctl(_ arguments: [String]) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return -1
        }
        process.waitUntilExit()
        return process.terminationStatus
    }
}
