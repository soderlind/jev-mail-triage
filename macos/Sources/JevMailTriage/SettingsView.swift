import JevTriageKit
import SwiftUI

/// Edits the helper's allowlisted settings. `move_enabled` changes only via an explicit,
/// confirmed action, and secrets go straight to the helper's Keychain items.
struct SettingsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Group {
            if let binding = Binding($model.settingsDraft) {
                TabView {
                    BridgeSettingsTab(config: binding).tabItem { Label("Bridge", systemImage: "network") }
                    PolicySettingsTab(config: binding).tabItem { Label("Policy", systemImage: "slider.horizontal.3") }
                    GateSettingsTab(config: binding).tabItem { Label("Local Gate", systemImage: "line.3.horizontal.decrease.circle") }
                    ServiceSettingsTab(config: binding).tabItem { Label("Service", systemImage: "clock") }
                    GeneralSettingsTab().tabItem { Label("General", systemImage: "gearshape") }
                }
                .editableFieldStyle()
                .safeAreaInset(edge: .bottom) { footer }
            } else {
                ContentUnavailableView(
                    "Not set up", systemImage: "envelope.badge.shield.half.filled",
                    description: Text("Finish setup in the dashboard first."))
            }
        }
        .frame(width: 560, height: 520)
        .background(WindowEditedMarker(edited: model.hasUnsavedSettings))
        .onAppear { model.beginEditingSettings() }
        .task { await model.loadFolders() }
        .onChange(of: model.config) { model.beginEditingSettings() }
    }

    private var footer: some View {
        HStack {
            if let notice = model.notice {
                Text(notice.message).font(.caption).foregroundStyle(.orange).lineLimit(2)
            } else if model.hasUnsavedSettings {
                Label("Unsaved changes. Saving applies them from the worker’s next check.", systemImage: "circle.fill")
                    .font(.caption).foregroundStyle(.secondary)
                    .symbolRenderingMode(.multicolor)
            }
            Spacer()
            if model.isBusy("settings") { ProgressView().controlSize(.small) }
            Button("Revert") { model.revertSettings() }.disabled(!model.hasUnsavedSettings)
            Button("Save") { Task { await model.saveSettings() } }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut("s")
                .disabled(!model.hasUnsavedSettings || model.isBusy("settings"))
        }
        .padding(12)
        .background(.bar)
    }
}

/// Shows the standard "edited" dot in the window's close button while there are unsaved changes.
private struct WindowEditedMarker: NSViewRepresentable {
    let edited: Bool

    func makeNSView(context: Context) -> NSView { NSView() }

    func updateNSView(_ view: NSView, context: Context) {
        DispatchQueue.main.async { view.window?.isDocumentEdited = edited }
    }
}

struct BridgeSettingsTab: View {
    @Environment(AppModel.self) private var model
    @Binding var config: AppConfig
    @State private var check: BridgeCheck?
    @State private var checkError: Notice?
    @State private var checking = false

    var body: some View {
        @Bindable var model = model
        Form {
            Section {
                TextField("Username", text: $config.bridge.username).multilineTextAlignment(.leading)
                TextField("Host", text: $config.bridge.host).multilineTextAlignment(.leading)
                TextField("IMAP port", value: $config.bridge.port, format: .number.grouping(.never)).multilineTextAlignment(.leading)
                Picker("Security", selection: $config.bridge.securityMode) {
                    Text("STARTTLS").tag("starttls")
                    Text("SSL/TLS").tag("implicit_tls")
                }
                FolderField(
                    title: "Mailbox", selection: $config.bridge.mailbox, folders: model.folders,
                    loading: model.loadingFolders, reload: { Task { await model.loadFolders(force: true) } })
                CertificatePicker(path: $config.bridge.tlsCaFile)
                SecureField(
                    "Bridge password", text: $model.pendingBridgePassword,
                    prompt: Text(model.bridgeSecretPresent ? "Saved in Keychain — type to replace" : "From Bridge, not your Proton password"))
                    .multilineTextAlignment(.leading)
            }
            Section {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Button("Test Connection") { Task { await test() } }.disabled(checking)
                    if checking {
                        ProgressView().controlSize(.small)
                    } else if let check {
                        BridgeCheckSummary(check: check)
                    }
                    Spacer(minLength: 0)
                }
                if let checkError { NoticeView(notice: checkError) { self.checkError = nil } }
            } footer: {
                Text("Tests the settings above, including unsaved changes.").font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    private func test() async {
        checking = true
        defer { checking = false }
        do {
            let password = model.pendingBridgePassword
            check = try await model.checkBridge(config.bridge, password: password.isEmpty ? nil : password)
            checkError = nil
        } catch {
            check = nil
            checkError = Notice("Bridge connection failed", error)
        }
    }
}

struct PolicySettingsTab: View {
    @Environment(AppModel.self) private var model
    @Binding var config: AppConfig
    @State private var confirmMoves = false

    var body: some View {
        @Bindable var model = model
        Form {
            Section {
                LabeledContent("Automatic moves") {
                    HStack {
                        Text(model.isDryRun ? "Off (dry run)" : "On")
                            .foregroundStyle(model.isDryRun ? .secondary : Color.orange)
                        if model.isBusy("moves") { ProgressView().controlSize(.small) }
                        if model.isDryRun {
                            Button("Enable…") { confirmMoves = true }
                        } else {
                            Button("Switch to Dry Run") { Task { await model.setMoves(enabled: false) } }
                        }
                    }
                    .disabled(model.isBusy("moves"))
                }
                FolderField(
                    title: "Review folder", selection: $config.policy.reviewFolder, folders: model.folders,
                    missingNote: "created on first move",
                    loading: model.loadingFolders, reload: { Task { await model.loadFolders(force: true) } })
            } footer: {
                Text("Takes effect immediately. Mail is moved, never deleted.").font(.caption).foregroundStyle(.secondary)
            }
            Section("Thresholds") {
                ThresholdField(title: "Spam threshold", value: $config.policy.spamThreshold)
                ThresholdField(title: "Scam threshold", value: $config.policy.scamThreshold)
                TextField("Max input bytes", value: $config.policy.maxInputBytes, format: .number.grouping(.never)).multilineTextAlignment(.leading)
                TextField("Model", text: $config.policy.model).multilineTextAlignment(.leading)
            }
            Section("Allowlist") {
                OptionalTextField(title: "Trusted authserv-id", text: $config.policy.trustedAuthservId)
                ListEditor(title: "Authenticated senders or domains", items: $config.policy.allowlist)
                Text("Allowlisting requires a trusted authserv-id so forged senders are not skipped.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("TypeSafe") {
                SecureField(
                    "API key", text: $model.pendingAPIKey,
                    prompt: Text(model.typeSafeSecretPresent ? "Saved in Keychain — type to replace" : "sk-…"))
                    .multilineTextAlignment(.leading)
            }
        }
        .formStyle(.grouped)
        .confirmationDialog("Enable automatic moves?", isPresented: $confirmMoves) {
            Button("Enable Automatic Moves") { Task { await model.setMoves(enabled: true) } }
        } message: {
            Text(MovesCopy.confirmation(reviewFolder: model.config?.policy.reviewFolder ?? "review"))
        }
    }
}

struct GateSettingsTab: View {
    @Binding var config: AppConfig

    var body: some View {
        Form {
            Section {
                Picker("Mode", selection: $config.gate.mode) {
                    Text("Off").tag("off")
                    Text("Shadow (record only)").tag("shadow")
                    Text("Enforce (local spam skips Jev)").tag("enforce")
                }
                ListEditor(title: "Allowed languages (ISO 639-1; empty = all)", items: $config.gate.allowedLanguages)
                Toggle("Learn from Jev decisions", isOn: $config.gate.learnFromJev)
            }
            Section("Training folders") {
                ListEditor(title: "Spam", items: $config.gate.spamFolders)
                ListEditor(title: "Ham", items: $config.gate.hamFolders)
                ListEditor(title: "Sent", items: $config.gate.sentFolders)
                TextField("Minimum examples", value: $config.gate.minExamples, format: .number).multilineTextAlignment(.leading)
                TextField("Retrain every (seconds)", value: $config.gate.trainIntervalSeconds, format: .number.grouping(.never)).multilineTextAlignment(.leading)
            }
            Section("Thresholds") {
                ThresholdField(title: "Bayes spam", value: $config.gate.bayesSpamThreshold)
                ThresholdField(title: "Bayes clean", value: $config.gate.bayesCleanThreshold)
                ThresholdField(title: "Near-duplicate similarity", value: $config.gate.nearDuplicateSimilarity)
            }
        }
        .formStyle(.grouped)
    }
}

struct ServiceSettingsTab: View {
    @Binding var config: AppConfig

    var body: some View {
        Form {
            TextField("Check every (seconds)", value: $config.service.pollIntervalSeconds, format: .number.grouping(.never)).multilineTextAlignment(.leading)
            TextField("Batch size", value: $config.service.batchSize, format: .number).multilineTextAlignment(.leading)
            TextField("Max attempts per message", value: $config.service.maxAttempts, format: .number).multilineTextAlignment(.leading)
            TextField("Max retry backoff (seconds)", value: $config.service.backoffMaxSeconds, format: .number.grouping(.never)).multilineTextAlignment(.leading)
            Text("Database: \(config.databasePath)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
        }
        .formStyle(.grouped)
    }
}

struct GeneralSettingsTab: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Form {
            Section {
                Toggle(
                    "Open Jev Mail Triage at login",
                    isOn: Binding(
                        get: { model.launchAtLogin },
                        set: { value in Task { await model.setLaunchAtLogin(value) } }))
                Picker("When the app opens", selection: Binding(
                    get: { model.startupBehavior }, set: { model.startupBehavior = $0 })
                ) {
                    ForEach(StartupBehavior.allCases, id: \.self) { Text($0.title).tag($0) }
                }
                Text(startupHint).font(.caption).foregroundStyle(.secondary)
            }
            Section {
                Text("The background worker is listed under System Settings → General → Login Items and keeps running after you close or quit the app, unless you pause it.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Open Login Items Settings") { BackgroundService.openLoginItemsSettings() }
                LabeledContent("Logs", value: model.paths.logDirectory.path)
            }
        }
        .formStyle(.grouped)
    }

    private var startupHint: String {
        switch model.startupBehavior {
        case .remember: return "Triage continues as you left it: running if it was running, paused if you paused it."
        case .run: return "Opening the app resumes triage if it was paused or stopped."
        case .pause: return "Opening the app pauses triage. Resume it from the menu when you are ready."
        }
    }
}

struct ThresholdField: View {
    let title: String
    @Binding var value: Double

    var body: some View {
        LabeledContent(title) {
            HStack {
                Slider(value: $value, in: 0...1).frame(width: 180)
                TextField("", value: $value, format: .number.precision(.fractionLength(2...3))).frame(width: 60)
            }
        }
    }
}

/// A Bridge folder picker; free text until the folder list is available, or on request.
struct FolderField: View {
    let title: String
    @Binding var selection: String
    let folders: [String]?
    var missingNote = "not on server"
    var loading = false
    var reload: (() -> Void)?
    @State private var typing = false

    var body: some View {
        HStack {
            if let folders, !folders.isEmpty, !typing {
                Picker(title, selection: $selection) {
                    ForEach(folderChoices(folders, current: selection), id: \.self) { folder in
                        Text(folders.contains(folder) ? folder : "\(folder) (\(missingNote))").tag(folder)
                    }
                }
            } else {
                TextField(title, text: $selection).multilineTextAlignment(.leading)
            }
            if let folders, !folders.isEmpty {
                Button { typing.toggle() } label: { Image(systemName: typing ? "list.bullet" : "pencil") }
                    .buttonStyle(.borderless)
                    .help(typing ? "Choose from Bridge folders" : "Type a folder name")
            }
            if loading {
                ProgressView().controlSize(.small)
            } else if let reload {
                Button(action: reload) { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless)
                    .help("Reload folders from Proton Mail Bridge")
            }
        }
    }
}

struct OptionalTextField: View {
    let title: String
    @Binding var text: String?

    var body: some View {
        TextField(title, text: Binding(get: { text ?? "" }, set: { text = $0.isEmpty ? nil : $0 })).multilineTextAlignment(.leading)
    }
}

/// One entry per line.
struct ListEditor: View {
    let title: String
    @Binding var items: [String]
    /// Raw text keeps a just-typed newline or space; normalising it on every keystroke
    /// would make it impossible to start a new line.
    @State private var text = ""

    static func normalized(_ text: String) -> [String] {
        text.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.callout)
            TextEditor(text: $text)
            .onAppear { text = items.joined(separator: "\n") }
            .onChange(of: text) { items = Self.normalized(text) }
            .onChange(of: items) {
                // Revert or reload replaced the list; show it unless it's what was typed.
                if Self.normalized(text) != items { text = items.joined(separator: "\n") }
            }
            .font(.body.monospaced())
            .scrollContentBackground(.hidden)
            .padding(4)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 5))
            .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(Color(nsColor: .separatorColor)))
            .frame(minHeight: 48, maxHeight: 90)
        }
    }
}

extension View {
    /// Grouped forms render plain text fields borderless, which reads as a static value;
    /// bordered fields look editable. Leading alignment has to be set on each field, because
    /// the form overrides an alignment set further out.
    func editableFieldStyle() -> some View {
        textFieldStyle(.roundedBorder)
    }
}
