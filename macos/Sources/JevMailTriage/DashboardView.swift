import JevTriageKit
import SwiftUI

enum DashboardPane: String, CaseIterable, Identifiable {
    case overview = "Overview"
    case statistics = "Statistics"
    case review = "Review"

    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .overview: return "gauge.with.dots.needle.50percent"
        case .statistics: return "chart.bar"
        case .review: return "tray.full"
        }
    }
}

struct DashboardView: View {
    @Environment(AppModel.self) private var model
    @State private var pane: DashboardPane? = .overview
    @State private var showSetup = false

    var body: some View {
        NavigationSplitView {
            List(DashboardPane.allCases, selection: $pane) { item in
                Label(item.rawValue, systemImage: item.symbol).tag(item)
            }
            .disabled(!model.configured)
            .navigationSplitViewColumnWidth(170)
        } detail: {
            Group {
                if !model.configured || showSetup {
                    SetupView(onFinish: { showSetup = false })
                } else {
                    switch pane ?? .overview {
                    case .overview: OverviewView(showSetup: $showSetup)
                    case .statistics: StatisticsView()
                    case .review: ReviewView()
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .task { await model.refresh(full: true) }
    }
}

struct OverviewView: View {
    @Environment(AppModel.self) private var model
    @Binding var showSetup: Bool
    @State private var confirmMoves = false
    @State private var confirmMigration = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let notice = model.notice {
                    NoticeView(notice: notice) { model.notice = nil }
                }
                GroupBox {
                    VStack(alignment: .leading, spacing: 10) {
                        PhaseHeader(phase: model.phase)
                        LifecycleButtons()
                        if model.phase == .needsSetup {
                            Button("Open Setup") { showSetup = true }
                        }
                        if registeredAndNotLegacy {
                            Button("Turn Off Background Worker…", role: .destructive) {
                                Task { await model.turnOffBackground() }
                            }
                            .buttonStyle(.link)
                            .help("Unregisters the LaunchAgent. Triage stops until you choose Start.")
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                if case .legacyInstalled(let loaded) = model.phase {
                    migrationCard(loaded: loaded)
                }
                movesCard
                if let cycle = model.worker?.lastCycle { lastCycleCard(cycle) }
                if let status = model.status { mailboxCard(status) }
            }
            .padding(20)
        }
        .navigationTitle("Overview")
        .confirmationDialog("Enable automatic moves?", isPresented: $confirmMoves) {
            Button("Enable Automatic Moves") { Task { await model.setMoves(enabled: true) } }
        } message: {
            Text(
                "Messages Jev scores above your thresholds will be moved from the Inbox to “\(model.config?.policy.reviewFolder ?? "review")”. "
                    + "Nothing is ever deleted, and you can move a message back to train the local gate.")
        }
        .confirmationDialog("Migrate the legacy LaunchAgent?", isPresented: $confirmMigration) {
            Button("Stop Legacy Agent and Migrate") { Task { await model.migrateLegacy() } }
        } message: {
            Text(
                "This stops “\(LegacyAgent.label)”, renames its plist to …plist.migrated-<date> (kept for undo), "
                    + "lets the bundled helper take ownership of your Keychain items (macOS asks once per item), "
                    + "and starts the app’s background worker. Your config and history are reused.")
        }
    }

    private var registeredAndNotLegacy: Bool {
        model.registration == .enabled && !model.legacy.isPresent
    }

    private func migrationCard(loaded: Bool) -> some View {
        GroupBox("Legacy installation") {
            VStack(alignment: .leading, spacing: 8) {
                Text(
                    loaded
                        ? "The command-line LaunchAgent \(LegacyAgent.label) is running \"jev-mail-triage serve\". "
                            + "The app will not start a second worker while it is installed."
                        : "The file \(model.legacy.plist?.path ?? "") is installed and would start at next login.")
                Button("Migrate…") { confirmMigration = true }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isBusy("migrate"))
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var movesCard: some View {
        GroupBox("Mail actions") {
            VStack(alignment: .leading, spacing: 8) {
                if model.isDryRun {
                    Label("Dry run is on. Jev assesses new mail and records scores; nothing is moved.", systemImage: "eye")
                    Button("Enable Automatic Moves…") { confirmMoves = true }
                        .disabled(model.isBusy("moves"))
                } else {
                    Label(
                        "Automatic moves are on. Likely spam and scams go to “\(model.config?.policy.reviewFolder ?? "")”. Mail is never deleted.",
                        systemImage: "tray.and.arrow.down")
                    Button("Switch Back to Dry Run") { Task { await model.setMoves(enabled: false) } }
                        .disabled(model.isBusy("moves"))
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func lastCycleCard(_ cycle: LastCycle) -> some View {
        GroupBox("Last check") {
            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 4) {
                GridRow {
                    Text("Finished")
                    Text(Date(timeIntervalSince1970: cycle.finishedAt).formatted(date: .abbreviated, time: .standard))
                }
                GridRow { Text("Discovered"); Text("\(cycle.discovered)") }
                GridRow { Text("Assessed"); Text("\(cycle.assessed)") }
                GridRow { Text("Moved"); Text("\(cycle.moved)") }
                GridRow { Text("Needs attention"); Text("\(cycle.attention)") }
                if cycle.baseline { GridRow { Text("Baseline"); Text("Existing mail recorded, not assessed") } }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func mailboxCard(_ status: StatusResult) -> some View {
        GroupBox("Mailboxes") {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(status.counts.keys.sorted(), id: \.self) { mailbox in
                    let counts = status.counts[mailbox] ?? [:]
                    HStack(alignment: .firstTextBaseline) {
                        Text(mailbox).bold().frame(width: 160, alignment: .leading)
                        Text(
                            counts.sorted { $0.key < $1.key }.map { "\($0.key.replacingOccurrences(of: "_", with: " ")) \($0.value)" }
                                .joined(separator: " · ")
                        )
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    }
                }
                Text(
                    "Local gate: \(status.gate.mode) · \(Int(status.gate.totals["spam_examples"] ?? 0)) spam / "
                        + "\(Int(status.gate.totals["ham_examples"] ?? 0)) ham examples"
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct StatisticsView: View {
    @Environment(AppModel.self) private var model
    @State private var days: Int = 7
    @State private var result: StatsResult?
    @State private var error: Notice?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Picker("Period", selection: $days) {
                    Text("24 hours").tag(1)
                    Text("7 days").tag(7)
                    Text("30 days").tag(30)
                    Text("All time").tag(0)
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 420)
                if let error { NoticeView(notice: error) { self.error = nil } }
                if let stats = result?.stats {
                    Grid(alignment: .leading, horizontalSpacing: 32, verticalSpacing: 6) {
                        row("Discovered", stats.discovered)
                        row("Assessed", stats.assessed)
                        row("Waiting", stats.waiting)
                        row("Flagged (spam + scam)", stats.flagged)
                        row("  Spam", stats.spam)
                        row("  Scam", stats.scam)
                        row("Moved to review", stats.moved)
                        row("Left in Inbox", stats.leftInPlace)
                        row("Allowlisted", stats.allowlisted)
                        row("Too large (truncated)", stats.tooLarge)
                        row("Restored by you", stats.restoredByUser)
                        row("Needs attention", stats.needsAttention)
                        Divider().gridCellColumns(2)
                        row("Decided by Jev", stats.decidedByJev)
                        row("Decided locally", stats.decidedLocally)
                        row("Local gate: spam", stats.localSpam)
                        row("Local gate: clean", stats.localClean)
                        row("Local gate disagreed with Jev", stats.localDisagree)
                    }
                    if !stats.localReasons.isEmpty {
                        Text("Local gate reasons").font(.headline)
                        ForEach(stats.localReasons.sorted { $0.value > $1.value }, id: \.key) { reason, count in
                            Text("\(reason): \(count)").font(.callout)
                        }
                    }
                } else if error == nil {
                    ProgressView()
                }
            }
            .padding(20)
        }
        .navigationTitle("Statistics")
        .task(id: days) { await load() }
    }

    @ViewBuilder private func row(_ title: String, _ value: Int) -> some View {
        GridRow {
            Text(title)
            Text(value.formatted()).monospacedDigit().gridColumnAlignment(.trailing)
        }
    }

    private func load() async {
        do {
            result = try await model.loadStats(days: days == 0 ? nil : days)
            error = nil
        } catch {
            self.error = Notice("Could not load statistics", error)
        }
    }
}

/// Stored review candidates (scores only). Headers are fetched on demand, read-only,
/// kept in this view's memory, and discarded when the view goes away.
struct ReviewView: View {
    @Environment(AppModel.self) private var model
    @State private var result: CandidatesResult?
    @State private var headers: [CandidateKey: Candidate] = [:]
    @State private var selection = Set<CandidateKey>()
    @State private var error: Notice?
    @State private var loadingHeaders = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Messages Jev flagged for review. Subjects and senders are fetched from Bridge only when you ask, and are not saved.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    Task { await showHeaders() }
                } label: {
                    Label("Show Headers", systemImage: "text.magnifyingglass")
                }
                .disabled(selection.isEmpty || loadingHeaders)
                .help("Fetch Subject, From, and Date for up to 50 selected messages")
                if loadingHeaders { ProgressView().controlSize(.small) }
            }
            if let error { NoticeView(notice: error) { self.error = nil } }
            Table(result?.candidates ?? [], selection: $selection) {
                TableColumn("Mailbox", value: \.mailbox).width(min: 80, ideal: 110)
                TableColumn("Spam") { Text(score($0.spamScore)).monospacedDigit() }.width(55)
                TableColumn("Scam") { Text(score($0.scamScore)).monospacedDigit() }.width(55)
                TableColumn("Reason") { Text($0.decisionReason ?? "—") }.width(min: 90, ideal: 120)
                TableColumn("Local") { Text($0.localVerdict ?? "—") }.width(55)
                TableColumn("Status", value: \.status).width(min: 60, ideal: 80)
                TableColumn("From") { Text(header($0, "from")) }.width(min: 120, ideal: 180)
                TableColumn("Subject") { Text(header($0, "subject")) }.width(min: 160, ideal: 260)
            }
        }
        .padding(16)
        .navigationTitle("Review")
        .toolbar {
            Button {
                Task { await load() }
            } label: {
                Label("Reload", systemImage: "arrow.clockwise")
            }
        }
        .task { await load() }
        .onDisappear {
            headers = [:]
            selection = []
        }
    }

    private func score(_ value: Double?) -> String {
        value.map { String(format: "%.2f", $0) } ?? "—"
    }

    private func header(_ candidate: Candidate, _ name: String) -> String {
        guard let fetched = headers[candidate.key] else { return "" }
        if let value = fetched.headers?[name]?.stringValue { return value }
        switch fetched.headerStatus {
        case "missing": return "(message no longer in mailbox)"
        case "uidvalidity_changed": return "(mailbox changed; cannot match)"
        case "source_not_pending": return "(already handled)"
        default: return ""
        }
    }

    private func load() async {
        do {
            result = try await model.loadCandidates()
            error = nil
        } catch {
            self.error = Notice("Could not load review candidates", error)
        }
    }

    private func showHeaders() async {
        loadingHeaders = true
        defer { loadingHeaders = false }
        do {
            let fetched = try await model.fetchHeaders(Array(selection))
            for candidate in fetched.candidates { headers[candidate.key] = candidate }
            error = nil
        } catch {
            self.error = Notice("Could not fetch headers", error)
        }
    }
}
