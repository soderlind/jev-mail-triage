import JevTriageKit
import SwiftUI

struct MenuBarView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            PhaseHeader(phase: model.phase, nextCheck: model.worker?.nextCycleAt)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(PhaseHeader.tint(model.phase).opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
            if model.configured {
                Label {
                    Text(model.isDryRun ? "Dry run: assessing only, nothing is moved" : "Automatic moves are on")
                        .foregroundStyle(.primary)
                } icon: {
                    Image(systemName: model.isDryRun ? "eye" : "tray.and.arrow.down")
                        .foregroundStyle(model.isDryRun ? Color.secondary : Color.orange)
                }
                .font(.callout)
            }
            if let cycle = model.worker?.lastCycle {
                Text(
                    "Last check \(Date(timeIntervalSince1970: cycle.finishedAt).formatted(.relative(presentation: .named))): "
                        + "\(cycle.assessed) assessed, \(cycle.moved) moved"
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            if let notice = model.notice {
                NoticeView(notice: notice) { model.notice = nil }
            }
            Divider()
            LifecycleButtons()
            Divider()
            HStack {
                Button("Dashboard") { open(WindowID.dashboard) }
                Button("Settings…") { open(WindowID.settings) }
                    .keyboardShortcut(",")
                    .disabled(!model.configured)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }
            }
        }
        .padding(14)
        .frame(width: 320)
        // The default glass panel lets busy windows show through; keep text legible.
        .background(Color(nsColor: .windowBackgroundColor))
        .task { await model.refresh(full: true) }
    }

    private func open(_ id: String) {
        NSApp.activate()
        openWindow(id: id)
    }
}

/// Start / pause controls appropriate to the current phase.
struct LifecycleButtons: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        HStack {
            switch model.phase {
            case .needsSetup, .legacyInstalled:
                Button(model.phase == .needsSetup ? "Set Up…" : "Review Migration…") {
                    openWindow(id: WindowID.dashboard)
                    NSApp.activate()
                }
                .buttonStyle(.borderedProminent)
            case .needsApproval:
                Button("Open Login Items Settings") { BackgroundService.openLoginItemsSettings() }
                    .buttonStyle(.borderedProminent)
            case .stopped, .paused:
                Button {
                    Task { await model.start() }
                } label: {
                    Label(model.phase == .stopped ? "Start" : "Resume", systemImage: "play.fill")
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.isBusy("start"))
            default:
                Button {
                    Task { await model.pause() }
                } label: {
                    Label("Pause", systemImage: "pause.fill")
                }
                .disabled(model.isBusy("pause"))
            }
            if model.isBusy("start") || model.isBusy("pause") || model.isBusy("migrate") {
                ProgressView().controlSize(.small)
            }
        }
    }
}

struct PhaseHeader: View {
    let phase: Phase
    var nextCheck: Double? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label {
                Text(phase.title).foregroundStyle(.primary)
            } icon: {
                Image(systemName: phase.symbol).foregroundStyle(Self.tint(phase))
            }
            .font(.headline)
            if let detail {
                Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    static func tint(_ phase: Phase) -> Color {
        switch phase {
        case .running: return .green
        case .error, .unresponsive: return .red
        case .bridgeUnavailable, .waitingForLock, .needsApproval, .legacyInstalled, .needsSetup: return .orange
        default: return .secondary
        }
    }

    private var detail: String? {
        switch phase {
        case .running(let idle):
            guard idle, let nextCheck else { return idle ? nil : "Assessing new mail." }
            return "Next check \(Date(timeIntervalSince1970: nextCheck).formatted(.relative(presentation: .named)))."
        case .starting: return "Waiting for the background worker to report."
        case .needsSetup: return "Connect Proton Mail Bridge and add your TypeSafe API key."
        case .legacyInstalled(let loaded):
            return loaded
                ? "The older command-line LaunchAgent is running. Migrate to avoid two workers."
                : "An older command-line LaunchAgent file is installed. Migrate to replace it."
        case .needsApproval: return "Allow Jev Mail Triage under System Settings → General → Login Items."
        case .stopped: return "The background worker is not installed."
        case .paused: return "Triage is paused. No mail is read or moved."
        case .bridgeUnavailable(let message, let retryAt):
            let retry = retryAt.map {
                " Retrying \(Date(timeIntervalSince1970: $0).formatted(.relative(presentation: .named)))."
            } ?? ""
            return message + retry + " Is Proton Mail Bridge running?"
        case .waitingForLock(let holder):
            return "Another triage process\(holder.map { " (PID \($0))" } ?? "") holds the lock. Only one worker runs at a time."
        case .error(let code, let message):
            let hint = HelperError(code: code, message: message, retryable: false).recoverySuggestion
            return [message, hint].compactMap { $0 }.joined(separator: " ")
        case .unresponsive:
            return "The worker has not reported for a while. Check ~/Library/Logs/JevMailTriage/worker.log."
        default: return nil
        }
    }
}

struct NoticeView: View {
    let notice: Notice
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .top) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(notice.title).font(.callout.bold())
                Text(notice.message).font(.callout)
                if let suggestion = notice.suggestion {
                    Text(suggestion).font(.caption).foregroundStyle(.secondary)
                }
            }
            .textSelection(.enabled)
            Spacer()
            Button(action: dismiss) { Image(systemName: "xmark") }.buttonStyle(.plain)
        }
        .padding(8)
        .background(.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 6))
    }
}
