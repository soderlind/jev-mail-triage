from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from typesafe_sdk import RetryPolicy, TypeSafeClient

from .classifier import JevAssessor
from .config import DEFAULT_CONFIG_PATH, Config, load_config
from .errors import ConfigurationError, RetryableError, TriageError
from .gate import LocalGate, verdict_assessment
from .imap_client import BridgeImap
from .keychain import get_bridge_password, get_typesafe_api_key, set_secret
from .lock import ProcessLock
from .message import prepare_message
from .models import LocalLabel, Outcome
from .policy import decide
from .service import (
    LAST_TRAINED_KEY,
    TrainingResult,
    run_backfill,
    run_cycle,
    run_training,
    training_due,
)
from .state import StateStore

logger = logging.getLogger("jev_mail_triage")
T = TypeVar("T")
_SERVE_TRAINING_LIMIT = 200


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-mail-triage",
        description="Dry-run-first Proton Mail Bridge triage using TypeSafe Jev.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("serve", help="Poll continuously using the configured move setting.")
    commands.add_parser("poll", help="Run one cycle in dry-run mode.")

    backfill = commands.add_parser(
        "backfill", help="Assess a bounded number of existing Inbox messages."
    )
    backfill.add_argument("--limit", required=True, type=int)
    backfill.add_argument("--dry-run", action="store_true")

    status = commands.add_parser("status", help="Show local assessment counts, not message data.")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    candidates = commands.add_parser(
        "candidates", help="List stored move-to-review decisions without changing mail."
    )
    candidates.add_argument("--limit", type=int, default=20, help="Maximum rows (1-1000).")
    candidates.add_argument("--json", action="store_true", help="Emit a JSON array.")
    candidates.add_argument(
        "--headers",
        action="store_true",
        help="Explicitly fetch identifying headers from Bridge, read-only; no TypeSafe calls.",
    )

    train = commands.add_parser(
        "train",
        help="Train the local gate from your Spam, ham, and Sent folders (read-only).",
    )
    train.add_argument(
        "--limit", type=int, default=500, help="Maximum new messages per folder (1-5000)."
    )

    retry = commands.add_parser("retry", help="Re-queue failed assessments for explicit retry.")
    retry.add_argument("--limit", type=int, default=20)

    secret = commands.add_parser("secret", help="Store a secret in macOS Keychain.")
    secret_commands = secret.add_subparsers(dest="secret_action", required=True)
    secret_set = secret_commands.add_parser("set")
    secret_set.add_argument("name", choices=("bridge-password", "typesafe-api-key"))

    evaluate = commands.add_parser(
        "evaluate", help="Evaluate labelled .eml fixtures using the configured TypeSafe model."
    )
    evaluate.add_argument("--labels", type=Path, required=True, help="CSV: path,spam,scam_phishing")
    return parser


def _assessor(config: Config) -> TypeSafeClient:
    return TypeSafeClient(
        api_key=get_typesafe_api_key(),
        model=config.policy.model,
        retry=RetryPolicy(max_retries=0),
        timeout=30,
    )


def _with_bridge(config: Config, operation: Callable[[BridgeImap], T]) -> T:
    for attempt in range(config.service.max_attempts):
        try:
            with BridgeImap(
                config.bridge, get_bridge_password(config.bridge.account_id)
            ) as mailbox:
                return operation(mailbox)
        except RetryableError:
            if attempt + 1 >= config.service.max_attempts:
                raise
            delay = min(5 * (2**attempt), config.service.backoff_max_seconds)
            time.sleep(delay)
    raise AssertionError("Bridge retry loop exited without a result.")


def _gate(config: Config, store: StateStore) -> LocalGate | None:
    return LocalGate(config.gate, config.policy, store) if config.gate.enabled else None


def _print_training(result: TrainingResult) -> None:
    print(
        f"trained spam={result.spam} ham={result.ham} corrections={result.corrections} "
        f"new_correspondents={result.correspondents} skipped={result.skipped}"
    )
    if result.missing_folders:
        print(f"missing folders (skipped): {', '.join(result.missing_folders)}")


def _train(config: Config, limit: int) -> None:
    if not 1 <= limit <= 5000:
        raise ConfigurationError("Training limit must be between 1 and 5000.")
    with ProcessLock(config.database_path):
        store = StateStore(config.database_path)
        gate = LocalGate(config.gate, config.policy, store)
        result = _with_bridge(
            config, lambda mailbox: run_training(config, store, mailbox, gate, limit)
        )
        totals = store.gate_totals()
    _print_training(result)
    print(
        f"model spam_examples={int(totals['spam_examples'])} "
        f"ham_examples={int(totals['ham_examples'])} "
        f"correspondents={int(totals['correspondents'])}"
    )
    if not config.gate.enabled:
        print("Note: [gate] mode is 'off'; set it to 'shadow' to start using the local gate.")


def _run_cycle(config: Config, *, force_dry_run: bool) -> None:
    with ProcessLock(config.database_path):
        store = StateStore(config.database_path)
        gate = _gate(config, store)
        with _assessor(config) as client:
            assessor = JevAssessor(client, model=config.policy.model)
            result = _with_bridge(
                config,
                lambda mailbox: run_cycle(
                    config, store, mailbox, assessor, force_dry_run=force_dry_run, gate=gate
                ),
            )
    print(
        f"baseline={result.baseline} discovered={result.discovered} "
        f"assessed={result.assessed} moved={result.moved} attention={result.attention}"
    )


def _serve(config: Config) -> None:
    with ProcessLock(config.database_path):
        store = StateStore(config.database_path)
        gate = _gate(config, store)
        with _assessor(config) as client:
            assessor = JevAssessor(client, model=config.policy.model)
            while True:
                started_at = time.monotonic()
                if gate is not None and training_due(config, store):
                    _serve_training(config, store, gate)
                try:
                    result = _with_bridge(
                        config,
                        lambda mailbox: run_cycle(config, store, mailbox, assessor, gate=gate),
                    )
                    logger.info(
                        "Cycle complete: baseline=%s discovered=%d assessed=%d moved=%d attention=%d",
                        result.baseline,
                        result.discovered,
                        result.assessed,
                        result.moved,
                        result.attention,
                    )
                except RetryableError as exc:
                    logger.warning(
                        "Cycle deferred after transient failure (%s).", type(exc).__name__
                    )
                elapsed = time.monotonic() - started_at
                time.sleep(max(0.0, config.service.poll_interval_seconds - elapsed))


def _serve_training(config: Config, store: StateStore, gate: LocalGate) -> None:
    """Train in the background of serve; failures never block triage."""
    try:
        training = _with_bridge(
            config,
            lambda mailbox: run_training(config, store, mailbox, gate, _SERVE_TRAINING_LIMIT),
        )
    except TriageError as exc:
        # Postpone the next attempt by a full interval instead of retrying every cycle.
        store.set_gate_meta(LAST_TRAINED_KEY, time.time())
        logger.warning("Local gate training skipped (%s).", type(exc).__name__)
        return
    logger.info(
        "Local gate trained: spam=%d ham=%d corrections=%d new_correspondents=%d "
        "skipped=%d missing_folders=%d",
        training.spam,
        training.ham,
        training.corrections,
        training.correspondents,
        training.skipped,
        len(training.missing_folders),
    )


def _backfill(config: Config, limit: int, *, force_dry_run: bool) -> None:
    if not 1 <= limit <= 1000:
        raise ConfigurationError("Backfill limit must be between 1 and 1000.")
    with ProcessLock(config.database_path):
        store = StateStore(config.database_path)
        gate = _gate(config, store)
        with _assessor(config) as client:
            assessor = JevAssessor(client, model=config.policy.model)
            result = _with_bridge(
                config,
                lambda mailbox: run_backfill(
                    config,
                    store,
                    mailbox,
                    assessor,
                    limit,
                    force_dry_run=force_dry_run,
                    gate=gate,
                ),
            )
    print(
        f"baseline={result.baseline} discovered={result.discovered} "
        f"assessed={result.assessed} moved={result.moved} attention={result.attention}"
    )


def _status(config: Config, *, as_json: bool) -> None:
    import json

    store = StateStore(config.database_path)
    rows = store.status_counts(config.bridge.account_id)
    if as_json:
        print(json.dumps(rows, sort_keys=True))
        return
    if not rows:
        print("No stored assessments.")
    for mailbox, statuses in rows.items():
        counts = " ".join(f"{status}={count}" for status, count in sorted(statuses.items()))
        print(f"{mailbox}: {counts}")
    totals = store.gate_totals()
    print(
        f"local gate: mode={config.gate.mode} spam_examples={int(totals['spam_examples'])} "
        f"ham_examples={int(totals['ham_examples'])} "
        f"correspondents={int(totals['correspondents'])}"
    )


def _candidates(config: Config, limit: int, *, headers: bool, as_json: bool) -> None:
    items = StateStore.candidates(config.database_path, config.bridge.account_id, limit)
    rows: list[dict[str, object]] = [
        {
            "mailbox": item.mailbox,
            "uid_validity": item.uid_validity,
            "uid": item.uid,
            "message_id": item.message_id,
            "spam_score": item.spam_score,
            "scam_score": item.scam_score,
            "decision_reason": item.decision_reason,
            "model_version": item.model_version,
            "local_verdict": item.local_verdict,
            "local_reason": item.local_reason,
            "status": item.status,
            "truncated": item.truncated,
        }
        for item in items
    ]
    if headers and rows:

        def lookup(mailbox: BridgeImap) -> list[dict[str, object]]:
            enriched: list[dict[str, object]] = []
            for item, row in zip(items, rows, strict=True):
                result = dict(row)
                validity, _ = mailbox.select(item.mailbox, readonly=True)
                if validity != item.uid_validity:
                    result["header_status"] = "uidvalidity_changed"
                elif item.status != "assessed":
                    result["header_status"] = "source_not_pending"
                else:
                    fetched = mailbox.fetch_headers(item.uid)
                    result["header_status"] = "missing" if fetched is None else "available"
                    if fetched is not None:
                        result["headers"] = fetched
                enriched.append(result)
            return enriched

        rows = _with_bridge(config, lookup)
    if as_json:
        print(json.dumps(rows, sort_keys=True))
    elif not rows:
        print("No stored move-to-review decisions.")
    else:
        for row in rows:
            print(json.dumps(row, sort_keys=True))


def _retry(config: Config, limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ConfigurationError("Retry limit must be between 1 and 1000.")
    with ProcessLock(config.database_path):
        store = StateStore(config.database_path)

        def requeue(mailbox: BridgeImap) -> int:
            inbox = mailbox.resolve_mailbox(config.bridge.mailbox)
            uid_validity, uid_next = mailbox.select(inbox, readonly=True)
            store.synchronize_mailbox(config.bridge.account_id, inbox, uid_validity, uid_next)
            return store.retry_failed(config.bridge.account_id, inbox, uid_validity, limit)

        retried = _with_bridge(config, requeue)
    print(f"re-queued={retried}")


def _label(value: str, column: str, row_number: int) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ConfigurationError(
        f"Invalid {column} label on CSV row {row_number}; use true/false, 1/0, or yes/no."
    )


def _evaluation_rows(labels_path: Path) -> list[tuple[Path, bool, bool]]:
    try:
        with labels_path.expanduser().open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            required = {"path", "spam", "scam_phishing"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise ConfigurationError(
                    "Labels CSV must contain columns: path,spam,scam_phishing."
                )
            rows: list[tuple[Path, bool, bool]] = []
            for row_number, row in enumerate(reader, start=2):
                path_value = row.get("path")
                spam_value = row.get("spam")
                scam_value = row.get("scam_phishing")
                if (
                    not isinstance(path_value, str)
                    or not path_value.strip()
                    or not isinstance(spam_value, str)
                    or not spam_value.strip()
                    or not isinstance(scam_value, str)
                    or not scam_value.strip()
                ):
                    raise ConfigurationError(f"Missing label value on CSV row {row_number}.")
                relative_path = Path(path_value.strip())
                if relative_path.suffix.casefold() != ".eml":
                    raise ConfigurationError(
                        f"Evaluation fixture on CSV row {row_number} must be an .eml file."
                    )
                fixture_path = (
                    relative_path
                    if relative_path.is_absolute()
                    else labels_path.expanduser().parent / relative_path
                )
                rows.append(
                    (
                        fixture_path,
                        _label(spam_value, "spam", row_number),
                        _label(scam_value, "scam_phishing", row_number),
                    )
                )
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"Could not read evaluation labels: {exc}") from exc
    if not rows:
        raise ConfigurationError("Labels CSV contains no fixture rows.")
    return rows


def _metrics(expected: list[bool], predicted: list[bool]) -> tuple[int, float, float]:
    true_positive = sum(actual and guess for actual, guess in zip(expected, predicted))
    false_positive = sum(not actual and guess for actual, guess in zip(expected, predicted))
    false_negative = sum(actual and not guess for actual, guess in zip(expected, predicted))
    precision = (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    )
    return false_positive, precision, recall


def _evaluate(config: Config, labels_path: Path) -> None:
    rows = _evaluation_rows(labels_path)
    expected_spam: list[bool] = []
    predicted_spam: list[bool] = []
    expected_scam: list[bool] = []
    predicted_scam: list[bool] = []
    expected_review: list[bool] = []
    predicted_review: list[bool] = []
    review_count = 0
    gate = _gate(config, StateStore(config.database_path)) if config.gate.enabled else None
    local_counts = {"spam": 0, "clean": 0, "deferred": 0, "false_positives": 0, "missed": 0}

    with _assessor(config) as client:
        assessor = JevAssessor(client, model=config.policy.model)
        for fixture_path, spam_label, scam_label in rows:
            try:
                with fixture_path.open("rb") as source:
                    raw = source.read(2 * 1024 * 1024 + 1)
            except (OSError, ValueError) as exc:
                raise ConfigurationError(
                    f"Could not read evaluation fixture {fixture_path}: {exc}"
                ) from exc
            prepared = prepare_message(
                raw,
                max_input_bytes=config.policy.max_input_bytes,
                trusted_authserv_id=config.policy.trusted_authserv_id,
                raw_truncated=len(raw) > 2 * 1024 * 1024,
            )
            assessment = assessor.assess(prepared)
            decision = decide(prepared, assessment, config.policy)
            verdict = gate.evaluate(prepared) if gate is not None else None
            if gate is not None:
                if verdict is None:
                    local_counts["deferred"] += 1
                elif verdict.label == LocalLabel.SPAM:
                    local_counts["spam"] += 1
                    local_counts["false_positives"] += not (spam_label or scam_label)
                else:
                    local_counts["clean"] += 1
                    local_counts["missed"] += spam_label or scam_label
                if verdict is not None and config.gate.enforced:
                    decision = decide(prepared, verdict_assessment(verdict), config.policy)
            spam_guess = assessment.spam_score >= config.policy.spam_threshold
            scam_guess = assessment.scam_score >= config.policy.scam_threshold
            review_guess = decision.outcome == Outcome.MOVE
            expected_spam.append(spam_label)
            predicted_spam.append(spam_guess)
            expected_scam.append(scam_label)
            predicted_scam.append(scam_guess)
            expected_review.append(spam_label or scam_label)
            predicted_review.append(review_guess)
            review_count += review_guess

    print("class false_positives precision recall")
    for name, expected, predicted in (
        ("spam", expected_spam, predicted_spam),
        ("scam_phishing", expected_scam, predicted_scam),
        ("review", expected_review, predicted_review),
    ):
        false_positives, precision, recall = _metrics(expected, predicted)
        print(f"{name} {false_positives} {precision:.3f} {recall:.3f}")
    print(f"review_volume={review_count}/{len(rows)} ({review_count / len(rows):.1%})")
    if gate is not None:
        decided = local_counts["spam"] + local_counts["clean"]
        print(
            f"local_gate mode={config.gate.mode} decided_locally={decided}/{len(rows)} "
            f"local_spam={local_counts['spam']} local_clean={local_counts['clean']} "
            f"false_positives={local_counts['false_positives']} "
            f"missed_spam_or_scam={local_counts['missed']}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "secret":
            account_id = None
            if args.name == "bridge-password":
                account_id = load_config(args.config).bridge.account_id
            set_secret(args.name, account_id)
            print(f"Stored {args.name} in macOS Keychain.")
            return 0

        config = load_config(args.config)
        if args.command == "status":
            _status(config, as_json=args.json)
        elif args.command == "candidates":
            _candidates(config, args.limit, headers=args.headers, as_json=args.json)
        elif args.command == "poll":
            _run_cycle(config, force_dry_run=True)
        elif args.command == "serve":
            _serve(config)
        elif args.command == "backfill":
            _backfill(config, args.limit, force_dry_run=args.dry_run)
        elif args.command == "retry":
            _retry(config, args.limit)
        elif args.command == "train":
            _train(config, args.limit)
        elif args.command == "evaluate":
            _evaluate(config, args.labels)
        return 0
    except TriageError as exc:
        print(f"jev-mail-triage: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
