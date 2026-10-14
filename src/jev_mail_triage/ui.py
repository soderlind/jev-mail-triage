"""Terminal rendering with Rich. Machine-readable --json output never goes through here."""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .service import TrainingResult

_STATUS_STYLES = {
    "assessed": "green",
    "moved": "yellow",
    "moving": "yellow",
    "queued": "cyan",
    "retry_wait": "cyan",
    "needs_attention": "bold red",
    "move_decision": "red",
}


def console(*, stderr: bool = False) -> Console:
    # Highlighting would colour numbers and words inside untrusted values at random.
    return Console(stderr=stderr, highlight=False)


def safe_text(value: object, style: str = "") -> Text:
    """Untrusted mail data as plain text: no Rich markup, no terminal control characters."""
    cleaned = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} else character
        for character in str(value)
    )
    return Text(" ".join(cleaned.split()), style=style)


def error(message: str) -> None:
    console(stderr=True).print(Text("jev-mail-triage: ", style="bold red") + safe_text(message))


def _table(title: str | None = None, **kwargs: object) -> Table:
    return Table(
        title=title,
        title_justify="left",
        title_style="bold",
        box=box.ROUNDED,
        header_style="bold",
        **kwargs,  # type: ignore[arg-type]
    )


def _count(value: object, style: str = "") -> Text:
    return Text(f"{value:,}" if isinstance(value, int) else str(value), style=style)


def render_status(
    rows: Mapping[str, Counter[str]], gate_mode: str, gate_totals: Mapping[str, float]
) -> None:
    out = console()
    if not rows:
        out.print("No stored assessments.", style="dim")
    else:
        keys = sorted({key for counts in rows.values() for key in counts})
        table = _table("Status")
        table.add_column("Mailbox")
        for key in keys:
            table.add_column(key, justify="right", style=_STATUS_STYLES.get(key, ""))
        for mailbox, counts in rows.items():
            table.add_row(safe_text(mailbox), *(_count(counts.get(key, 0)) for key in keys))
        out.print(table)
    out.print(_gate_line(gate_mode, gate_totals))


def _gate_line(mode: str, totals: Mapping[str, float]) -> Text:
    mode_style = {"off": "dim", "shadow": "cyan", "enforce": "green"}.get(mode, "")
    return Text.assemble(
        ("Local gate ", "bold"),
        (mode, f"bold {mode_style}"),
        f"  spam examples {int(totals['spam_examples']):,}",
        f"  kept examples {int(totals['ham_examples']):,}",
        f"  correspondents {int(totals['correspondents']):,}",
    )


def _share(part: int, whole: int) -> Text:
    return Text(f"{part / whole:.1%}" if whole else "", style="dim")


def render_stats(
    stats: Mapping[str, object],
    *,
    period: str,
    review_folder: str,
    move_enabled: bool,
    gate_mode: str,
) -> None:
    def number(key: str) -> int:
        value = stats.get(key, 0)
        return value if isinstance(value, int) else 0

    assessed = number("assessed")
    table = _table(f"Statistics ({period})")
    table.add_column("")
    table.add_column("Messages", justify="right")
    table.add_column("Share", justify="right")

    def section(label: str, key: str | None = None, style: str = "") -> None:
        value = number(key) if key else None
        table.add_row(
            Text(label, style=f"bold {style}"),
            _count(value, f"bold {style}") if value is not None else Text(""),
            _share(value, assessed) if key and key != "discovered" else Text(""),
        )

    def row(label: str, key: str, style: str = "", *, share: bool = True) -> None:
        value = number(key)
        table.add_row(
            Text(f"  {label}", style=style if value else "dim"),
            _count(value, style if value else "dim"),
            _share(value, assessed) if share else Text(""),
        )

    section("Mail found", "discovered")
    row("assessed", "assessed", share=False)
    row("waiting", "waiting", "cyan", share=False)
    row("needs attention", "needs_attention", "bold red", share=False)
    table.add_section()
    section("Spam or phishing", "flagged", "red")
    row("spam", "spam", "red")
    row("scam/phishing", "scam", "magenta")
    row(f"moved to {review_folder}", "moved", "yellow")
    if number("moving"):
        row("move in progress", "moving", "yellow")
    table.add_section()
    section("Left in place", "left_in_place", "green")
    row("too large to judge", "too_large")
    row("allowlisted", "allowlisted")
    row("restored by you", "restored_by_user")
    table.add_section()
    section("Decided by")
    row("Jev (sent to TypeSafe)", "decided_by_jev")
    row("local gate", "decided_locally", "green")

    out = console()
    out.print(table)
    if not move_enabled:
        out.print(
            f"Moves are off: flagged mail stayed in place. Set move_enabled to use "
            f"{review_folder}.",
            style="yellow",
            markup=False,
        )
    if number("local_spam") or number("local_clean"):
        gate = _table(f"Local gate ({gate_mode})")
        gate.add_column("Verdict")
        gate.add_column("Messages", justify="right")
        gate.add_row("spam", _count(number("local_spam"), "red"))
        gate.add_row("clean", _count(number("local_clean"), "green"))
        disagree = number("local_disagree")
        gate.add_row("disagreed with Jev", _count(disagree, "bold yellow" if disagree else "dim"))
        reasons = stats.get("local_reasons")
        if isinstance(reasons, Mapping) and reasons:
            gate.add_section()
            for reason, count in reasons.items():
                gate.add_row(Text(f"  {reason}", style="dim"), _count(count, "dim"))
        out.print(gate)


def _score(value: object, threshold: float) -> Text:
    if not isinstance(value, int | float):
        return Text("-", style="dim")
    return Text(f"{value:.2f}", style="bold red" if value >= threshold else "")


def _candidate_why(row: Mapping[str, object]) -> Text:
    lines = [safe_text(row.get("decision_reason") or "-")]
    local = row.get("local_verdict")
    if local:
        lines.append(
            safe_text(f"local: {row.get('local_reason')}", "red" if local == "spam" else "green")
        )
    for line in lines:
        line.truncate(28, overflow="ellipsis")
    return Text("\n").join(lines)


def _candidate_message(row: Mapping[str, object]) -> Text:
    headers = row.get("headers")
    if not isinstance(headers, Mapping):
        return safe_text(str(row.get("header_status", "")).replace("_", " "), "dim")
    message = safe_text(headers.get("subject") or "(no subject)", "bold")
    return (
        message
        + Text("\n")
        + safe_text(f"{headers.get('from', '')}  {headers.get('date', '')}", "dim")
    )


def render_candidates(
    rows: Sequence[Mapping[str, object]], *, spam_threshold: float, scam_threshold: float
) -> None:
    out = console()
    if not rows:
        out.print("No stored move-to-review decisions.", style="dim")
        return
    headings = ("UID", "Spam", "Scam", "Status", "Why")
    grid: list[list[Text]] = []
    for row in rows:
        status = str(row.get("status", ""))
        grid.append(
            [
                Text(str(row.get("uid", ""))),
                _score(row.get("spam_score"), spam_threshold),
                _score(row.get("scam_score"), scam_threshold),
                Text(status, style=_STATUS_STYLES.get(status, "")),
                _candidate_why(row),
            ]
        )
    widths = [
        max(len(heading), *(max(map(len, cell.plain.split("\n"))) for cell in column))
        for heading, column in zip(headings, zip(*grid, strict=True), strict=True)
    ]
    table = _table(f"Move-to-review decisions ({len(rows)})")
    for heading, width in zip(headings, widths, strict=True):
        justify = "right" if heading in {"UID", "Spam", "Scam"} else "left"
        table.add_column(heading, justify=justify, no_wrap=True, min_width=width)
    if any("header_status" in row for row in rows):
        # Rich shrinks all no_wrap columns alike when space runs out; give Message the rest.
        borders = 3 * (len(headings) + 1) + 1
        table.add_column(
            "Message",
            no_wrap=True,
            overflow="ellipsis",
            max_width=max(8, out.width - sum(widths) - borders),
        )
        for cells, row in zip(grid, rows, strict=True):
            cells.append(_candidate_message(row))
    for cells in grid:
        table.add_row(*cells)
    out.print(table)


def render_training(result: TrainingResult, totals: Mapping[str, float], *, gate_mode: str) -> None:
    out = console()
    table = _table("Training")
    table.add_column("")
    table.add_column("This run", justify="right")
    table.add_column("Model total", justify="right")
    table.add_row("spam", _count(result.spam, "red"), _count(int(totals["spam_examples"])))
    table.add_row("kept mail", _count(result.ham, "green"), _count(int(totals["ham_examples"])))
    table.add_row(
        "correspondents",
        _count(result.correspondents),
        _count(int(totals["correspondents"])),
    )
    table.add_row(
        "corrections", _count(result.corrections, "yellow" if result.corrections else "dim"), ""
    )
    table.add_row("skipped", _count(result.skipped, "dim"), "")
    out.print(table)
    if result.missing_folders:
        out.print(
            Text("Missing folders (skipped): ", style="yellow")
            + safe_text(", ".join(result.missing_folders))
        )
    if gate_mode == "off":
        out.print(
            "Note: [gate] mode is 'off'; set it to 'shadow' to start using the local gate.",
            style="yellow",
            markup=False,
        )


def render_evaluation(
    metrics: Sequence[tuple[str, int, float, float]],
    *,
    review_count: int,
    total: int,
    gate_mode: str | None,
    local_counts: Mapping[str, int],
) -> None:
    out = console()
    table = _table("Evaluation")
    table.add_column("Class")
    table.add_column("False positives", justify="right")
    table.add_column("Precision", justify="right")
    table.add_column("Recall", justify="right")
    for name, false_positives, precision, recall in metrics:
        table.add_row(
            name,
            _count(false_positives, "bold red" if false_positives else "green"),
            f"{precision:.3f}",
            f"{recall:.3f}",
        )
    out.print(table)
    out.print(f"Review volume: {review_count}/{total} ({review_count / total:.1%})", markup=False)
    if gate_mode is None:
        return
    decided = local_counts["spam"] + local_counts["clean"]
    gate = _table(f"Local gate ({gate_mode})")
    gate.add_column("")
    gate.add_column("Files", justify="right")
    gate.add_row("decided locally", f"{decided}/{total}")
    gate.add_row("spam verdicts", _count(local_counts["spam"], "red"))
    gate.add_row("clean verdicts", _count(local_counts["clean"], "green"))
    false_positives = local_counts["false_positives"]
    missed = local_counts["missed"]
    gate.add_row(
        "false positives", _count(false_positives, "bold red" if false_positives else "green")
    )
    gate.add_row("missed spam or scam", _count(missed, "bold red" if missed else "green"))
    out.print(gate)
