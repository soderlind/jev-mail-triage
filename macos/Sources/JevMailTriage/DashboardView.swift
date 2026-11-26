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

/// Shared wording for the confirmation shown wherever automatic moves can be enabled.
enum MovesCopy {
    static func confirmation(reviewFolder: String) -> String {
        "Messages Jev scores above your thresholds will be moved from the Inbox to “\(reviewFolder)”. "
            + "Nothing is ever deleted, and you can move a message back to train the local gate."
    }
}

struct OverviewView: View {
    @Environment(AppModel.self) private var model
    @Binding var showSetup: Bool
    @State private var confirmMoves = false
    @State private var confirmMigration = false
    @State private var confirmTurnOff = false

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
                            Button("Turn Off Background Worker…", role: .destructive) { confirmTurnOff = true }
                            .buttonStyle(.link)
                            .help("Unregisters the LaunchAgent. Triage stops until you choose Start.")
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                if case .legacyInstalled(let loaded) = model.phase {
                    migrationCard(loaded: loaded)
                }
                if let attention = model.attention, attention.total > 0 { attentionCard(attention) }
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
            Text(MovesCopy.confirmation(reviewFolder: model.config?.policy.reviewFolder ?? "review"))
        }
        .confirmationDialog("Turn off the background worker?", isPresented: $confirmTurnOff) {
            Button("Turn Off", role: .destructive) { Task { await model.turnOffBackground() } }
        } message: {
            Text(
                "Triage stops completely and the worker is removed from Login Items. New mail is not checked until you choose Start. "
                    + "To stop for a while instead, use Pause.")
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

    private func attentionCard(_ attention: AttentionResult) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(attention.reasons) { reason in
                    HStack(alignment: .firstTextBaseline) {
                        Text(reason.count.formatted()).font(.headline).monospacedDigit().frame(minWidth: 32, alignment: .trailing)
                        Text(reason.explanation).fixedSize(horizontal: false, vertical: true)
                    }
                }
                HStack {
                    if attention.retryable > 0 {
                        Button("Retry \(attention.retryable.formatted())") { Task { await model.retryAttention() } }
                            .disabled(model.isBusy("attention"))
                            .help("Queue these messages to be assessed again on the next check")
                        if model.isBusy("attention") { ProgressView().controlSize(.small) }
                    }
                    if attention.retryable < attention.total {
                        Text("Interrupted moves are never retried automatically. Look for the message in the Inbox and “\(model.config?.policy.reviewFolder ?? "review")”.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            Label("Needs attention", systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
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
                GridRow { Text("Discovered"); Text(cycle.discovered.formatted()).monospacedDigit() }
                GridRow { Text("Assessed"); Text(cycle.assessed.formatted()).monospacedDigit() }
                GridRow { Text("Moved"); Text(cycle.moved.formatted()).monospacedDigit() }
                GridRow { Text("Needs attention"); Text(cycle.attention.formatted()).monospacedDigit() }
                if cycle.recordedBaseline { GridRow { Text("Baseline"); Text("Existing mail recorded, not assessed") } }
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
                    StatsSummary(stats: stats)
                    if stats.assessed == 0 {
                        ContentUnavailableView(
                            "No mail assessed in this period", systemImage: "chart.pie",
                            description: Text("Charts appear once the worker has assessed new mail."))
                    } else {
                        HStack(alignment: .top, spacing: 16) {
                            ChartCard("Outcome") { OutcomeChart(stats: stats) }
                            VStack(spacing: 16) {
                                ChartCard("Decided by") {
                                    ShareBar(slices: [
                                        StatSlice(id: "jev", label: "Jev", value: stats.decidedByJev),
                                        StatSlice(id: "local", label: "Local gate", value: stats.decidedLocally),
                                    ])
                                    if let note = gateNote {
                                        Text(note).font(.caption).foregroundStyle(.secondary)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                }
                                ChartCard("Local gate verdicts") {
                                    ShareBar(slices: [
                                        StatSlice(id: "clean", label: "Clean", value: stats.localClean),
                                        StatSlice(id: "spam", label: "Spam", value: stats.localSpam),
                                    ])
                                    if stats.localDisagree > 0 {
                                        Label("Disagreed with Jev \(stats.localDisagree.formatted()) times", systemImage: "arrow.left.arrow.right")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                        if !stats.reasonSlices.isEmpty {
                            ChartCard("Local gate reasons") { ReasonChart(slices: stats.reasonSlices) }
                        }
                    }
                    DisclosureGroup("All numbers") {
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
                        .padding(.top, 6)
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

    /// Explains how much the local gate decides in the current mode.
    private var gateNote: String? {
        switch model.config?.gate.mode {
        case "enforce": return "In enforce mode the local gate decides spam on its own; mail it considers clean is still checked by Jev."
        case "shadow": return "The local gate is in shadow mode: it records its verdicts, but Jev decides every message."
        case "off": return "The local gate is off, so Jev decides every message."
        default: return nil
        }
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
    @State private var sortOrder = [KeyPathComparator(\Candidate.seenSort, order: .reverse)]
    @State private var error: Notice?
    @State private var loadingHeaders = false

    private static let headerLimit = 50

    private var rows: [Candidate] { (result?.candidates ?? []).sorted(using: sortOrder) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Messages Jev scored above your thresholds. Subjects and senders are fetched from Bridge only when you ask, and are not saved.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Text(countText).font(.caption).foregroundStyle(selection.count > Self.headerLimit ? .orange : .secondary)
                }
                Spacer()
                if loadingHeaders { ProgressView().controlSize(.small) }
                Button {
                    Task { await showHeaders(selection) }
                } label: {
                    Label("Show Headers", systemImage: "text.magnifyingglass")
                }
                .disabled(selection.isEmpty || selection.count > Self.headerLimit || loadingHeaders)
                .help("Fetch Subject and From for the selected messages (up to \(Self.headerLimit))")
            }
            if let error { NoticeView(notice: error) { self.error = nil } }
            Table(rows, selection: $selection, sortOrder: $sortOrder) {
                TableColumn("Seen", value: \.seenSort) { Text(seen($0)).monospacedDigit() }.width(min: 90, ideal: 120)
                TableColumn("Spam", value: \.spamSort) { Text(score($0.spamScore)).monospacedDigit() }.width(min: 44, ideal: 52)
                TableColumn("Scam", value: \.scamSort) { Text(score($0.scamScore)).monospacedDigit() }.width(min: 44, ideal: 52)
                TableColumn("Reason") { Text(MailLabels.reason($0.decisionReason)) }.width(min: 90, ideal: 120)
                TableColumn("Local gate") { Text(MailLabels.localVerdict($0.localVerdict)) }.width(min: 60, ideal: 70)
                TableColumn("Status") { Text(MailLabels.status($0.status)) }.width(min: 70, ideal: 90)
                TableColumn("From") { Text(header($0, "from")) }.width(min: 120, ideal: 180)
                TableColumn("Subject") { Text(header($0, "subject")) }.width(min: 160, ideal: 260)
            }
            .contextMenu(forSelectionType: CandidateKey.self) { keys in
                Button("Show Headers") { Task { await showHeaders(keys) } }
                    .disabled(keys.isEmpty || keys.count > Self.headerLimit || loadingHeaders)
            }
            .overlay {
                if let result, result.candidates.isEmpty {
                    ContentUnavailableView(
                        "Nothing flagged yet", systemImage: "tray",
                        description: Text("Messages appear here when Jev scores them above your spam or scam threshold."))
                }
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

    private var countText: String {
        let total = result?.candidates.count ?? 0
        var text = "\(total.formatted()) messages"
        if !selection.isEmpty { text += " · \(selection.count.formatted()) selected" }
        if selection.count > Self.headerLimit { text += " (headers: select up to \(Self.headerLimit))" }
        return text
    }

    private func seen(_ candidate: Candidate) -> String {
        candidate.seenAt.map { Date(timeIntervalSince1970: $0).formatted(date: .abbreviated, time: .shortened) } ?? "—"
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

    private func showHeaders(_ keys: Set<CandidateKey>) async {
        guard !keys.isEmpty, keys.count <= Self.headerLimit else { return }
        loadingHeaders = true
        defer { loadingHeaders = false }
        do {
            let fetched = try await model.fetchHeaders(Array(keys))
            for candidate in fetched.candidates { headers[candidate.key] = candidate }
            error = nil
        } catch {
            self.error = Notice("Could not fetch headers", error)
        }
    }
}

/// Non-optional sort keys for the review table; missing values sort first.
extension Candidate {
    var seenSort: Double { seenAt ?? 0 }
    var spamSort: Double { spamScore ?? -1 }
    var scamSort: Double { scamScore ?? -1 }
}
