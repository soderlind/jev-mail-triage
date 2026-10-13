from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .classifier import JevAssessor
from .config import Config
from .errors import ConfigurationError, PermanentMessageError, RetryableError, TriageError
from .imap_client import FetchedMessage
from .message import prepare_message
from .models import Assessment, MailItem, PreparedMessage
from .policy import decide
from .state import StateStore

logger = logging.getLogger("jev_mail_triage")


class MailboxSession(Protocol):
    capabilities: frozenset[str]

    def resolve_mailbox(self, configured_name: str) -> str: ...

    def select(self, mailbox: str, *, readonly: bool = True) -> tuple[int, int]: ...

    def search_uids(self, start_uid: int | None = None) -> list[int]: ...

    def fetch_uid(self, uid: int) -> FetchedMessage | None: ...

    def ensure_review_folder(self, folder: str) -> None: ...

    def move_uid(self, uid: int, target_folder: str) -> int | None: ...

    def copy_uid(self, uid: int, target_folder: str) -> int: ...

    def delete_uid_and_expunge(self, uid: int) -> None: ...

    def new_matching_messages(
        self,
        folder: str,
        baseline: str,
        expected_fingerprint: str,
        *,
        max_input_bytes: int,
    ) -> list[tuple[int, FetchedMessage]]: ...


@dataclass(frozen=True)
class CycleResult:
    baseline: str
    discovered: int
    assessed: int
    moved: int
    attention: int


def run_cycle(
    config: Config,
    store: StateStore,
    mailbox: MailboxSession,
    assessor: JevAssessor,
    *,
    force_dry_run: bool = False,
) -> CycleResult:
    bridge = config.bridge
    inbox = mailbox.resolve_mailbox(bridge.mailbox)
    uid_validity, uid_next = mailbox.select(inbox, readonly=True)
    baseline_result = store.synchronize_mailbox(bridge.account_id, inbox, uid_validity, uid_next)
    if baseline_result != "steady":
        logger.info(
            "Mailbox baseline established (%s); existing messages left untouched.", baseline_result
        )
        return CycleResult(baseline_result, 0, 0, 0, 0)

    move_enabled = config.policy.move_enabled and not force_dry_run
    if move_enabled:
        mailbox.ensure_review_folder(config.policy.review_folder)

    before_attention = store.counts(bridge.account_id, inbox).get("needs_attention", 0)
    moved_before = store.counts(bridge.account_id, inbox).get("moved", 0)

    for item in store.moving_items(bridge.account_id, inbox, uid_validity, 20):
        _recover_move(config, store, mailbox, item, move_enabled=move_enabled)

    cursor = store.cursor(bridge.account_id, inbox)
    new_uids = [uid for uid in mailbox.search_uids(cursor + 1) if uid > cursor]
    discovered = store.discover(
        bridge.account_id, inbox, uid_validity, new_uids[: config.service.batch_size]
    )
    processed = 0
    for item in store.due_items(bridge.account_id, inbox, uid_validity, config.service.batch_size):
        if processed >= config.service.batch_size:
            break
        _process_item(config, store, mailbox, assessor, item, move_enabled=move_enabled)
        processed += 1

    if move_enabled and processed < config.service.batch_size:
        for item in store.assessed_candidates(
            bridge.account_id,
            inbox,
            uid_validity,
            config.policy.spam_threshold,
            config.policy.scam_threshold,
            config.service.batch_size - processed,
        ):
            _apply_assessed(config, store, mailbox, item)
            processed += 1

    counts = store.counts(bridge.account_id, inbox)
    return CycleResult(
        baseline=baseline_result,
        discovered=discovered,
        assessed=processed,
        moved=max(0, counts.get("moved", 0) - moved_before),
        attention=max(0, counts.get("needs_attention", 0) - before_attention),
    )


def run_backfill(
    config: Config,
    store: StateStore,
    mailbox: MailboxSession,
    assessor: JevAssessor,
    limit: int,
    *,
    force_dry_run: bool = False,
) -> CycleResult:
    if not 1 <= limit <= 1000:
        raise ConfigurationError("Backfill limit must be between 1 and 1000.")
    bridge = config.bridge
    inbox = mailbox.resolve_mailbox(bridge.mailbox)
    uid_validity, uid_next = mailbox.select(inbox, readonly=True)
    baseline_result = store.synchronize_mailbox(bridge.account_id, inbox, uid_validity, uid_next)
    move_enabled = config.policy.move_enabled and not force_dry_run
    if move_enabled:
        mailbox.ensure_review_folder(config.policy.review_folder)
    all_uids = mailbox.search_uids()
    selected = sorted((uid for uid in all_uids if uid < uid_next), reverse=True)[:limit]
    discovered = store.discover(bridge.account_id, inbox, uid_validity, selected)
    assessed = 0
    for item in store.due_items(bridge.account_id, inbox, uid_validity, limit):
        _process_item(config, store, mailbox, assessor, item, move_enabled=move_enabled)
        assessed += 1
    if move_enabled:
        remaining = max(0, limit - assessed)
        for item in store.assessed_candidates(
            bridge.account_id,
            inbox,
            uid_validity,
            config.policy.spam_threshold,
            config.policy.scam_threshold,
            remaining,
        ):
            _apply_assessed(config, store, mailbox, item)
            assessed += 1
    counts = store.counts(bridge.account_id, inbox)
    return CycleResult(
        baseline=baseline_result,
        discovered=discovered,
        assessed=assessed,
        moved=counts.get("moved", 0),
        attention=counts.get("needs_attention", 0),
    )


def _process_item(
    config: Config,
    store: StateStore,
    mailbox: MailboxSession,
    assessor: JevAssessor,
    item: MailItem,
    *,
    move_enabled: bool,
) -> None:
    current = store.get_item(item.account_id, item.mailbox, item.uid_validity, item.uid)
    if current is None:
        return
    if current.spam_score is not None and current.scam_score is not None:
        if move_enabled:
            _apply_assessed(config, store, mailbox, current)
        else:
            store.restore_assessed(current)
        return

    attempts = store.start_attempt(current)
    try:
        fetched = mailbox.fetch_uid(current.uid)
        if fetched is None:
            store.mark_needs_attention(current, "source_message_missing")
            return
        prepared = _prepare(config, fetched)
        assessment = assessor.assess(prepared)
        decision = decide(prepared, assessment, config.policy)
        store.record_assessment(
            current,
            message_id=prepared.message_id,
            fingerprint=prepared.fingerprint,
            assessment=assessment,
            decision=decision,
            truncated=prepared.truncated,
        )
        if move_enabled:
            updated = store.get_item(
                current.account_id, current.mailbox, current.uid_validity, current.uid
            )
            if updated is not None:
                _apply_assessed(config, store, mailbox, updated)
    except PermanentMessageError:
        store.mark_needs_attention(current, "malformed_message")
    except RetryableError as exc:
        _record_retry(config, store, current, attempts, exc)
    except TriageError:
        store.mark_needs_attention(current, "message_processing_error")


def _apply_assessed(
    config: Config, store: StateStore, mailbox: MailboxSession, item: MailItem
) -> None:
    if item.fingerprint is None or item.spam_score is None or item.scam_score is None:
        return
    attempts = store.start_attempt(item)
    try:
        fetched = mailbox.fetch_uid(item.uid)
        if fetched is None:
            _reconcile_missing_source(config, store, mailbox, item)
            return
        prepared = _prepare(config, fetched)
        if prepared.fingerprint != item.fingerprint:
            store.mark_needs_attention(item, "message_fingerprint_changed")
            return
        assessment = Assessment(
            spam_score=item.spam_score,
            scam_score=item.scam_score,
            model_version=item.model_version or config.policy.model,
            policy_version=item.policy_version or "unknown",
        )
        if decide(prepared, assessment, config.policy).outcome.value != "move":
            store.restore_assessed(item)
            return
        _move_with_recovery(config, store, mailbox, item, prepared)
    except PermanentMessageError:
        store.mark_needs_attention(item, "malformed_message_during_recheck")
    except RetryableError as exc:
        _record_retry(config, store, item, attempts, exc)
    except TriageError:
        store.mark_needs_attention(item, "unsafe_or_failed_movement")


def _move_with_recovery(
    config: Config,
    store: StateStore,
    mailbox: MailboxSession,
    item: MailItem,
    prepared: PreparedMessage,
) -> None:
    if "MOVE" not in mailbox.capabilities and "UIDPLUS" not in mailbox.capabilities:
        store.mark_needs_attention(item, "safe_move_capability_unavailable")
        return
    folder = config.policy.review_folder
    target_validity, target_uid_next = mailbox.select(folder, readonly=True)
    baseline = f"{target_validity}:{target_uid_next}"
    is_uid_move = "MOVE" in mailbox.capabilities
    store.begin_move(
        item,
        phase="move_intent" if is_uid_move else "copy_intent",
        destination_before=baseline,
    )
    inbox_validity, _ = mailbox.select(item.mailbox, readonly=False)
    if inbox_validity != item.uid_validity:
        store.mark_needs_attention(item, "uidvalidity_changed_before_move", ambiguous_action=True)
        return
    current = mailbox.fetch_uid(item.uid)
    if current is None:
        store.mark_needs_attention(item, "source_missing_before_move", ambiguous_action=True)
        return
    if _prepare(config, current).fingerprint != item.fingerprint:
        store.mark_needs_attention(item, "message_changed_before_move", ambiguous_action=True)
        return
    if is_uid_move:
        mailbox.move_uid(item.uid, folder)
        store.mark_moved(item)
        return

    destination_uid = mailbox.copy_uid(item.uid, folder)
    store.set_action_phase(item, "copied", destination_uid)
    mailbox.select(folder, readonly=True)
    copied = mailbox.fetch_uid(destination_uid)
    if copied is None or _prepare(config, copied).fingerprint != item.fingerprint:
        store.mark_needs_attention(
            item, "copied_message_verification_failed", ambiguous_action=True
        )
        return
    inbox_validity, _ = mailbox.select(item.mailbox, readonly=False)
    if inbox_validity != item.uid_validity:
        store.mark_needs_attention(
            item, "uidvalidity_changed_before_expunge", ambiguous_action=True
        )
        return
    mailbox.delete_uid_and_expunge(item.uid)
    store.mark_moved(item)


def _recover_move(
    config: Config,
    store: StateStore,
    mailbox: MailboxSession,
    item: MailItem,
    *,
    move_enabled: bool,
) -> None:
    if item.fingerprint is None or item.action_phase is None:
        store.mark_needs_attention(item, "incomplete_move_journal", ambiguous_action=True)
        return
    attempts = store.start_attempt(item)
    try:
        source_validity, _ = mailbox.select(item.mailbox, readonly=True)
        if source_validity != item.uid_validity:
            store.mark_needs_attention(
                item, "uidvalidity_changed_during_recovery", ambiguous_action=True
            )
            return
        source = mailbox.fetch_uid(item.uid)
        if item.action_phase == "copied":
            if item.destination_uid is None:
                store.mark_needs_attention(item, "copy_mapping_missing", ambiguous_action=True)
                return
            mailbox.select(config.policy.review_folder, readonly=True)
            destination = mailbox.fetch_uid(item.destination_uid)
            if destination is None or _prepare(config, destination).fingerprint != item.fingerprint:
                store.mark_needs_attention(item, "copied_message_not_found", ambiguous_action=True)
                return
            if source is None:
                store.mark_moved(item)
                return
            if _prepare(config, source).fingerprint != item.fingerprint:
                store.mark_needs_attention(item, "source_changed_after_copy", ambiguous_action=True)
                return
            if not move_enabled:
                store.mark_needs_attention(
                    item, "copy_requires_manual_completion", ambiguous_action=True
                )
                return
            inbox_validity, _ = mailbox.select(item.mailbox, readonly=False)
            if inbox_validity != item.uid_validity:
                store.mark_needs_attention(
                    item, "uidvalidity_changed_before_recovery_expunge", ambiguous_action=True
                )
                return
            mailbox.delete_uid_and_expunge(item.uid)
            store.mark_moved(item)
            return

        if source is None:
            if item.destination_before is None:
                store.mark_needs_attention(
                    item, "move_destination_baseline_missing", ambiguous_action=True
                )
                return
            matches = mailbox.new_matching_messages(
                config.policy.review_folder,
                item.destination_before,
                item.fingerprint,
                max_input_bytes=config.policy.max_input_bytes,
            )
            if len(matches) == 1:
                store.mark_moved(item)
            else:
                store.mark_needs_attention(item, "move_outcome_ambiguous", ambiguous_action=True)
            return

        if _prepare(config, source).fingerprint != item.fingerprint:
            store.mark_needs_attention(
                item, "source_changed_during_recovery", ambiguous_action=True
            )
            return
        if item.action_phase == "copy_intent":
            if item.destination_before is None:
                store.mark_needs_attention(
                    item, "copy_destination_baseline_missing", ambiguous_action=True
                )
                return
            matches = mailbox.new_matching_messages(
                config.policy.review_folder,
                item.destination_before,
                item.fingerprint,
                max_input_bytes=config.policy.max_input_bytes,
            )
            if matches:
                store.mark_needs_attention(item, "copy_outcome_ambiguous", ambiguous_action=True)
                return
        if not move_enabled:
            store.restore_assessed(item)
            return
        prepared = _prepare(config, source)
        _move_with_recovery(config, store, mailbox, item, prepared)
    except RetryableError as exc:
        _record_retry(config, store, item, attempts, exc)
    except PermanentMessageError:
        store.mark_needs_attention(item, "move_recovery_failed", ambiguous_action=True)
    except TriageError:
        store.mark_needs_attention(item, "move_recovery_unsafe", ambiguous_action=True)


def _reconcile_missing_source(
    config: Config, store: StateStore, mailbox: MailboxSession, item: MailItem
) -> None:
    if item.destination_before is None or item.fingerprint is None:
        store.mark_needs_attention(item, "source_missing_unreconciled", ambiguous_action=True)
        return
    matches = mailbox.new_matching_messages(
        config.policy.review_folder,
        item.destination_before,
        item.fingerprint,
        max_input_bytes=config.policy.max_input_bytes,
    )
    if len(matches) == 1:
        store.mark_moved(item)
    else:
        store.mark_needs_attention(item, "source_missing_unreconciled", ambiguous_action=True)


def _prepare(config: Config, fetched: FetchedMessage) -> PreparedMessage:
    return prepare_message(
        fetched.raw,
        max_input_bytes=config.policy.max_input_bytes,
        trusted_authserv_id=config.policy.trusted_authserv_id,
        raw_truncated=fetched.truncated,
    )


def _record_retry(
    config: Config, store: StateStore, item: MailItem, attempts: int, exc: RetryableError
) -> None:
    delay = min(5 * (2 ** max(0, attempts - 1)), config.service.backoff_max_seconds)
    current = store.get_item(item.account_id, item.mailbox, item.uid_validity, item.uid)
    if current is not None and current.status == "moving":
        if attempts >= config.service.max_attempts:
            store.mark_needs_attention(item, "attempt_limit_reached", ambiguous_action=True)
            logger.warning("Move for UID %s needs attention after bounded retries.", item.uid)
            return
        store.schedule_move_retry(item, delay)
        logger.warning(
            "Move for UID %s deferred after %s (%s/%s attempts).",
            item.uid,
            type(exc).__name__,
            attempts,
            config.service.max_attempts,
        )
        return
    if attempts >= config.service.max_attempts:
        store.mark_needs_attention(item, "attempt_limit_reached")
        logger.warning("Message UID %s needs attention after bounded retries.", item.uid)
        return
    store.schedule_retry(item, attempts, delay)
    logger.warning(
        "Message UID %s deferred after %s (%s/%s attempts).",
        item.uid,
        type(exc).__name__,
        attempts,
        config.service.max_attempts,
    )
