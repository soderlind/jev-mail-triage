import Foundation
import JevTriageKit
import ServiceManagement

/// Wraps `SMAppService` for the bundled worker LaunchAgent and the launch-at-login item.
/// Calls run off the main actor; registration can take a moment and may prompt.
enum BackgroundService {
    static let agentPlistName = "com.soderlind.JevMailTriage.worker.plist"

    static func registration() -> AgentRegistration {
        let raw: AgentRegistration
        switch SMAppService.agent(plistName: agentPlistName).status {
        case .notRegistered: raw = .notRegistered
        case .enabled: raw = .enabled
        case .requiresApproval: raw = .requiresApproval
        case .notFound: raw = .notFound
        @unknown default: raw = .notFound
        }
        let plist = Bundle.main.bundleURL.appending(path: "Contents/Library/LaunchAgents/\(agentPlistName)")
        return raw.resolved(agentPlistPresent: FileManager.default.fileExists(atPath: plist.path))
    }

    /// Registers the agent. Returns the resulting status; `.requiresApproval` means the user
    /// must allow the background item in System Settings → General → Login Items.
    static func register() async throws -> AgentRegistration {
        try await Task.detached(priority: .userInitiated) {
            let service = SMAppService.agent(plistName: agentPlistName)
            if service.status != .enabled {
                do {
                    try service.register()
                } catch {
                    // Registration "fails" when approval is pending; the status tells which.
                    if service.status != .requiresApproval { throw error }
                }
            }
            return registration()
        }.value
    }

    static func unregister() async throws {
        try await SMAppService.agent(plistName: agentPlistName).unregister()
    }

    static var launchAtLogin: Bool { SMAppService.mainApp.status == .enabled }

    static func setLaunchAtLogin(_ enabled: Bool) throws {
        if enabled {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
    }

    static func openLoginItemsSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }
}
