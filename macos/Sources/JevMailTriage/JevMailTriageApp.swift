import AppKit
import JevTriageKit
import SwiftUI

@main
struct JevMailTriageApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            MenuBarView().environment(delegate.model)
        } label: {
            Image(systemName: delegate.model.phase.symbol)
                .accessibilityLabel("Jev Mail Triage: \(delegate.model.phase.title)")
        }
        .menuBarExtraStyle(.window)

        Window("Jev Mail Triage", id: WindowID.dashboard) {
            DashboardView().environment(delegate.model)
        }
        .defaultSize(width: 920, height: 620)
        .defaultLaunchBehavior(.presented)

        // A regular window rather than a `Settings` scene: `SettingsLink` does not reliably
        // open from a MenuBarExtra in an LSUIElement app.
        Window("Jev Mail Triage Settings", id: WindowID.settings) {
            SettingsView().environment(delegate.model)
        }
        .windowResizability(.contentSize)
    }
}

enum WindowID {
    static let dashboard = "dashboard"
    static let settings = "settings"
}

/// Closing windows keeps triage running (it lives in the LaunchAgent, not in this process).
/// Quitting explains whether background triage stays active and offers to pause it first.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if model.hasUnsavedSettings {
            let alert = NSAlert()
            alert.messageText = "Discard unsaved settings?"
            alert.informativeText = "You changed settings without saving. Quitting discards those changes."
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Discard and Quit")
            NSApp.activate()
            guard alert.runModal() == .alertSecondButtonReturn else { return .terminateCancel }
            model.revertSettings()
        }
        guard model.phase.isActive else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "Background triage keeps running"
        alert.informativeText =
            "Quitting closes the menu bar app only. The background worker keeps checking your inbox"
            + (model.isDryRun ? " in dry-run mode (nothing is moved)." : " and moving likely spam to review.")
            + " Pause it if you want triage to stop."
        alert.addButton(withTitle: "Quit")
        alert.addButton(withTitle: "Pause and Quit")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate()
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            return .terminateNow
        case .alertSecondButtonReturn:
            Task { @MainActor in
                await model.pause()
                // A failed pause shows a notice; stay open so triage isn't left running unnoticed.
                sender.reply(toApplicationShouldTerminate: model.control.paused)
            }
            return .terminateLater
        default:
            return .terminateCancel
        }
    }
}
