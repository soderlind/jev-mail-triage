import Charts
import JevTriageKit
import SwiftUI

/// Consistent colours so the same category looks the same in every chart.
enum StatColor {
    static func color(_ id: String) -> Color {
        switch id {
        case "spam": return .orange
        case "scam": return .red
        case "clean": return .green
        case "allowlisted": return .blue
        case "too_large": return .gray
        case "restored": return .purple
        case "jev": return .indigo
        case "local": return .teal
        default: return .accentColor
        }
    }
}

/// Headline tiles above the charts.
struct StatsSummary: View {
    let stats: Statistics

    var body: some View {
        HStack(spacing: 12) {
            StatTile(title: "Assessed", value: stats.assessed, symbol: "envelope.open", tint: .blue,
                     caption: stats.waiting > 0 ? "\(stats.waiting.formatted()) waiting" : nil)
            StatTile(title: "Flagged", value: stats.flagged, symbol: "exclamationmark.shield", tint: .orange,
                     caption: stats.flaggedRate.map { "\($0.formatted(.percent.precision(.fractionLength(1)))) of assessed" })
            StatTile(title: "Moved to review", value: stats.moved, symbol: "tray.and.arrow.down", tint: .purple,
                     caption: stats.moving > 0 ? "\(stats.moving.formatted()) in progress" : nil)
            StatTile(title: "Needs attention", value: stats.needsAttention, symbol: "exclamationmark.triangle",
                     tint: stats.needsAttention > 0 ? .red : .secondary, caption: nil)
        }
    }
}

struct StatTile: View {
    let title: String
    let value: Int
    let symbol: String
    let tint: Color
    let caption: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(title, systemImage: symbol).font(.callout).foregroundStyle(tint)
            Text(value.formatted()).font(.system(.title, design: .rounded).bold()).monospacedDigit()
            Text(caption ?? " ").font(.caption).foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tint.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct ChartCard<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.headline)
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color(nsColor: .separatorColor).opacity(0.5)))
    }
}

/// Donut of assessed mail by outcome, with a legend carrying the counts.
struct OutcomeChart: View {
    let stats: Statistics

    var body: some View {
        let slices = stats.outcomeSlices
        HStack(alignment: .center, spacing: 16) {
            Chart(slices) { slice in
                SectorMark(angle: .value("Messages", slice.value), innerRadius: .ratio(0.62), angularInset: 1.5)
                    .foregroundStyle(StatColor.color(slice.id))
                    .cornerRadius(3)
            }
            .chartBackground { _ in
                VStack(spacing: 0) {
                    Text(stats.assessed.formatted()).font(.title3.bold()).monospacedDigit()
                    Text("assessed").font(.caption2).foregroundStyle(.secondary)
                }
            }
            .frame(width: 150, height: 150)
            Legend(slices: slices, total: stats.assessed)
        }
    }
}

/// A single stacked bar showing how a total divides, with a legend.
struct ShareBar: View {
    let slices: [StatSlice]

    var body: some View {
        let total = slices.reduce(0) { $0 + $1.value }
        if total == 0 {
            Text("No data in this period").font(.callout).foregroundStyle(.secondary)
        } else {
            Chart(slices) { slice in
                BarMark(x: .value("Messages", slice.value), stacking: .normalized)
                    .foregroundStyle(StatColor.color(slice.id))
            }
            .chartXAxis(.hidden)
            .chartLegend(.hidden)
            .frame(height: 22)
            .clipShape(RoundedRectangle(cornerRadius: 5))
            Legend(slices: slices.filter { $0.value > 0 }, total: total)
        }
    }
}

struct Legend: View {
    let slices: [StatSlice]
    let total: Int

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 4) {
            ForEach(slices) { slice in
                GridRow {
                    Circle().fill(StatColor.color(slice.id)).frame(width: 8, height: 8)
                    Text(slice.label).font(.callout)
                    Text(slice.value.formatted()).font(.callout).monospacedDigit().gridColumnAlignment(.trailing)
                    Text(share(slice.value)).font(.caption).foregroundStyle(.secondary).monospacedDigit()
                        .gridColumnAlignment(.trailing)
                }
            }
        }
    }

    private func share(_ value: Int) -> String {
        guard total > 0 else { return "" }
        return (Double(value) / Double(total)).formatted(.percent.precision(.fractionLength(0...1)))
    }
}

/// Horizontal bars for the local gate's reasons, largest first.
struct ReasonChart: View {
    let slices: [StatSlice]

    var body: some View {
        Chart(slices) { slice in
            BarMark(x: .value("Messages", slice.value), y: .value("Reason", slice.label))
                .foregroundStyle(Color.teal.gradient)
                .cornerRadius(3)
                .annotation(position: .trailing) {
                    Text(slice.value.formatted()).font(.caption).foregroundStyle(.secondary).monospacedDigit()
                }
        }
        .chartXAxis(.hidden)
        .frame(height: CGFloat(slices.count) * 28 + 8)
    }
}
