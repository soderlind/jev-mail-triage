import AppKit
import JevTriageKit
import SwiftUI
import UniformTypeIdentifiers

/// Guided setup: privacy notice → Bridge → TypeSafe → finish. Moves stay off (dry run).
struct SetupView: View {
    @Environment(AppModel.self) private var model
    var onFinish: () -> Void = {}

    enum Step: Int { case privacy, bridge, typeSafe, finish }

    @State private var step: Step = .privacy
    @State private var bridge = BridgeSettings()
    @State private var password = ""
    @State private var apiKey = ""
    @State private var bridgeCheck: BridgeCheck?
    @State private var folders: [String]?
    @State private var typeSafeCheck: TypeSafeCheck?
    @State private var checking = false
    @State private var error: Notice?
    @State private var loaded = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Set Up Jev Mail Triage").font(.largeTitle.bold())
                ProgressView(value: Double(step.rawValue), total: 3).frame(maxWidth: 360)
                if let error { NoticeView(notice: error) { self.error = nil } }
                switch step {
                case .privacy: privacy
                case .bridge: bridgeForm
                case .typeSafe: typeSafeForm
                case .finish: finish
                }
            }
            .padding(24)
            .frame(maxWidth: 680, alignment: .leading)
        }
        .onAppear(perform: prefill)
    }

    private func prefill() {
        guard !loaded else { return }
        loaded = true
        if let existing = model.config?.bridge { bridge = existing }
    }

    // MARK: Steps

    private var privacy: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("What this app does with your mail").font(.title2.bold())
            Text(
                "Jev Mail Triage reads new messages in your Proton Mail Inbox through Proton Mail Bridge on this Mac and asks TypeSafe’s Jev model whether each one looks like spam or a scam."
            )
            VStack(alignment: .leading, spacing: 6) {
                Text("Sent to TypeSafe for each new message:").bold()
                Label("Sender name and address", systemImage: "person")
                Label("Subject", systemImage: "text.alignleft")
                Label("Plain-text body (HTML converted to text)", systemImage: "doc.plaintext")
                Label("Link destinations found in the message", systemImage: "link")
                Text("Input is capped at 64 KiB by default. Attachments and images are not sent. If the local gate is set to enforce, messages it can decide confidently are not sent at all.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            .padding(12)
            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            VStack(alignment: .leading, spacing: 6) {
                Text("Stored on this Mac:").bold()
                Text("Scores, decisions, and message identifiers (UID, Message-ID) in ~/Library/Application Support/jev-mail-triage. Message bodies, subjects, and senders are never stored.")
                Text("Your Bridge password and TypeSafe API key are stored only in the macOS Keychain, owned by the app’s bundled helper.")
            }
            .font(.callout)
            Text("Mail is never deleted. Until you turn on automatic moves, the app runs in dry run and moves nothing.")
                .font(.callout.bold())
            HStack {
                Spacer()
                Button("Continue") { step = .bridge }.buttonStyle(.borderedProminent)
            }
        }
    }

    private var bridgeForm: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Proton Mail Bridge").font(.title2.bold())
            Text("Open Proton Mail Bridge, choose your account, and copy the IMAP details from Mailbox details.")
                .foregroundStyle(.secondary)
            Form {
                TextField("Username", text: $bridge.username, prompt: Text("you@proton.me"))
                SecureField(
                    "Bridge password", text: $password,
                    prompt: Text(model.bridgeSecretPresent ? "Saved in Keychain — leave empty to keep" : "From Bridge, not your Proton password"))
                TextField("Host", text: $bridge.host)
                TextField("IMAP port", value: $bridge.port, format: .number.grouping(.never))
                Picker("Security", selection: $bridge.securityMode) {
                    Text("STARTTLS").tag("starttls")
                    Text("SSL/TLS").tag("implicit_tls")
                }
                FolderField(title: "Mailbox", selection: $bridge.mailbox, folders: folders)
                CertificatePicker(path: $bridge.tlsCaFile)
            }
            .formStyle(.grouped)
            .editableFieldStyle()
            .onChange(of: bridge) { bridgeCheck = nil }
            .onChange(of: password) { bridgeCheck = nil }
            if let check = bridgeCheck { BridgeCheckSummary(check: check) }
            HStack {
                Button("Back") { step = .privacy }
                Spacer()
                if checking { ProgressView().controlSize(.small) }
                Button("Test Connection") { Task { await testBridge() } }
                    .disabled(checking || bridge.username.isEmpty || (password.isEmpty && !model.bridgeSecretPresent))
                Button("Continue") { step = .typeSafe }
                    .buttonStyle(.borderedProminent)
                    .disabled(bridgeCheck == nil)
            }
        }
    }

    private var typeSafeForm: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("TypeSafe API key").font(.title2.bold())
            Text("Create a key in your TypeSafe account. Checking the key lists available models; no email is sent.")
                .foregroundStyle(.secondary)
            Form {
                SecureField(
                    "API key", text: $apiKey,
                    prompt: Text(model.typeSafeSecretPresent ? "Saved in Keychain — leave empty to keep" : "sk-…"))
            }
            .formStyle(.grouped)
            .editableFieldStyle()
            .onChange(of: apiKey) { typeSafeCheck = nil }
            if let check = typeSafeCheck {
                Label(
                    check.modelAvailable
                        ? "Key accepted. Model \(check.model) is available."
                        : "Key accepted, but model \(check.model) is not listed. Choose another model in Settings.",
                    systemImage: check.modelAvailable ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
                )
                .foregroundStyle(check.modelAvailable ? .green : .orange)
            }
            HStack {
                Button("Back") { step = .bridge }
                Spacer()
                if checking { ProgressView().controlSize(.small) }
                Button("Check Key") { Task { await testTypeSafe() } }
                    .disabled(checking || (apiKey.isEmpty && !model.typeSafeSecretPresent))
                Button("Continue") { step = .finish }
                    .buttonStyle(.borderedProminent)
                    .disabled(typeSafeCheck == nil)
            }
        }
    }

    private var finish: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Ready").font(.title2.bold())
            Label("Dry run: new mail is assessed and scored, but nothing is moved.", systemImage: "eye")
            Label("The first check records existing Inbox mail as a baseline without assessing it.", systemImage: "flag")
            Label("You can enable automatic moves from the dashboard when you trust the results.", systemImage: "tray.and.arrow.down")
            if model.legacy.isPresent {
                Label("A legacy LaunchAgent is installed. Migrate it from the dashboard before starting.", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
            }
            HStack {
                Button("Back") { step = .typeSafe }
                Spacer()
                if checking { ProgressView().controlSize(.small) }
                Button(model.legacy.isPresent ? "Save" : "Save and Start") { Task { await save() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(checking)
            }
        }
    }

    // MARK: Actions

    private func testBridge() async {
        checking = true
        defer { checking = false }
        do {
            bridgeCheck = try await model.checkBridge(bridge, password: password)
            folders = bridgeCheck?.folders
            error = nil
        } catch {
            bridgeCheck = nil
            self.error = Notice("Bridge connection failed", error)
            // A wrong mailbox fails the check but listing may still work; offer the picker.
            if let listed = try? await model.listFolders(bridge, password: password) { folders = listed }
        }
    }

    private func testTypeSafe() async {
        checking = true
        defer { checking = false }
        do {
            typeSafeCheck = try await model.checkTypeSafe(apiKey: apiKey)
            error = nil
        } catch {
            typeSafeCheck = nil
            self.error = Notice("TypeSafe key check failed", error)
        }
    }

    private func save() async {
        checking = true
        defer { checking = false }
        do {
            try await model.completeSetup(bridge: bridge, password: password, apiKey: apiKey)
            password = ""
            apiKey = ""
            if !model.legacy.isPresent { await model.start() }
            onFinish()
        } catch {
            self.error = Notice("Setup was not saved", error)
        }
    }
}

struct BridgeCheckSummary: View {
    let check: BridgeCheck

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label {
                Text("Connected. Opened \(check.mailbox) read-only.")
            } icon: {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            }
            if !check.supportsMove {
                Label("Bridge does not advertise MOVE; moves will not be possible.", systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
            if !check.reviewFolderExists {
                Text("Review folder “\(check.reviewFolder)” does not exist yet; it is created on the first move.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            if !check.missingTrainingFolders.isEmpty {
                Text("Local gate training folders not found: \(check.missingTrainingFolders.joined(separator: ", ")).")
                    .font(.callout).foregroundStyle(.orange)
            }
        }
    }
}

/// Selects the certificate exported from Bridge when TLS verification needs it.
struct CertificatePicker: View {
    @Binding var path: String?

    var body: some View {
        LabeledContent("TLS certificate") {
            HStack {
                Text(path.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "System trust (default)")
                    .foregroundStyle(path == nil ? .secondary : .primary)
                    .help(path ?? "Uses macOS trust settings")
                Button("Choose…", action: choose)
                if path != nil { Button("Clear") { path = nil } }
            }
        }
        .help("Needed when Bridge's self-signed certificate is not trusted. Export it from Bridge settings.")
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.title = "Choose Bridge certificate"
        panel.allowedContentTypes = [UTType(filenameExtension: "pem") ?? .data, .x509Certificate]
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url { path = url.path }
    }
}
