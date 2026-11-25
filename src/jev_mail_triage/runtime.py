"""Shared building blocks for the CLI, the background worker, and the app protocol."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import TypeVar

from typesafe_sdk import RetryPolicy, TypeSafeClient

from .config import Config
from .errors import RetryableError, TriageError
from .gate import LocalGate
from .imap_client import BridgeImap
from .keychain import get_bridge_password, get_typesafe_api_key
from .models import MailItem
from .service import LAST_TRAINED_KEY, run_training
from .state import StateStore

logger = logging.getLogger("jev_mail_triage")
T = TypeVar("T")
BridgeRunner = Callable[[Config, Callable[[BridgeImap], T]], T]
SERVE_TRAINING_LIMIT = 200


def make_assessor(config: Config, api_key: str | None = None) -> TypeSafeClient:
    return TypeSafeClient(
        api_key=api_key or get_typesafe_api_key(),
        model=config.policy.model,
        retry=RetryPolicy(max_retries=0),
        timeout=30,
    )


def with_bridge(
    config: Config,
    operation: Callable[[BridgeImap], T],
    *,
    attempts: int | None = None,
    password: str | None = None,
    sleep: Callable[[float], object] | None = None,
) -> T:
    """Run ``operation`` in one Bridge session, retrying transient failures with backoff."""
    total = config.service.max_attempts if attempts is None else attempts
    for attempt in range(total):
        try:
            secret = password or get_bridge_password(config.bridge.account_id)
            with BridgeImap(config.bridge, secret) as mailbox:
                return operation(mailbox)
        except RetryableError:
            if attempt + 1 >= total:
                raise
            (sleep or time.sleep)(min(5 * (2**attempt), config.service.backoff_max_seconds))
    raise AssertionError("Bridge retry loop exited without a result.")


def make_gate(config: Config, store: StateStore) -> LocalGate | None:
    return LocalGate(config.gate, config.policy, store) if config.gate.enabled else None


def train_in_background(
    config: Config, store: StateStore, gate: LocalGate, bridge: BridgeRunner[object]
) -> None:
    """Train during continuous operation; failures never block triage."""
    try:
        training = bridge(
            config,
            lambda mailbox: run_training(config, store, mailbox, gate, SERVE_TRAINING_LIMIT),
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


def candidate_rows(items: Sequence[MailItem]) -> list[dict[str, object]]:
    """Stored review decisions; scores and identifiers only, never message content."""
    return [
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
            "seen_at": item.seen_at,
        }
        for item in items
    ]


def with_headers(
    mailbox: BridgeImap, items: Sequence[MailItem], rows: Sequence[dict[str, object]]
) -> list[dict[str, object]]:
    """Fetch identifying headers read-only for display; the result is never persisted."""
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
