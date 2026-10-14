import sqlite3
from pathlib import Path

from jev_mail_triage.models import Assessment, Decision, Outcome
from jev_mail_triage.state import StateStore


def test_first_seen_uidvalidity_baselines_and_new_uids_deduplicate(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    assert store.synchronize_mailbox("account", "INBOX", 11, 6) == "baseline"
    assert store.cursor("account", "INBOX") == 5
    assert store.discover("account", "INBOX", 11, [6, 7, 7]) == 2
    assert store.discover("account", "INBOX", 11, [6, 7]) == 0
    assert store.cursor("account", "INBOX") == 7


def test_same_message_id_on_distinct_uids_remains_distinct(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.synchronize_mailbox("account", "INBOX", 11, 1)
    store.discover("account", "INBOX", 11, [1, 2])
    first = store.get_item("account", "INBOX", 11, 1)
    second = store.get_item("account", "INBOX", 11, 2)
    assert first is not None and second is not None
    assessment = Assessment(0.1, 0.2, "jev-latest", "email-v1")
    for item in (first, second):
        store.record_assessment(
            item,
            message_id="<duplicate@example.test>",
            fingerprint=f"fingerprint-{item.uid}",
            assessment=assessment,
            decision=Decision(Outcome.LEAVE, "below_threshold"),
            truncated=False,
        )
    assert store.counts("account", "INBOX")["assessed"] == 2
    assert first.decision_outcome is None
    assert store.status_counts("account")["INBOX"]["move_decision"] == 0


def test_uidvalidity_change_rebaselines_without_reusing_old_uid_records(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.synchronize_mailbox("account", "INBOX", 11, 3)
    store.discover("account", "INBOX", 11, [1, 2])
    assert store.synchronize_mailbox("account", "INBOX", 12, 9) == "rebaseline"
    assert store.cursor("account", "INBOX") == 8
    assert store.discover("account", "INBOX", 12, [1, 2]) == 2
    assert store.status_counts("account")["INBOX"]["superseded"] == 2


def test_retryable_failures_can_be_explicitly_requeued(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.synchronize_mailbox("account", "INBOX", 11, 2)
    store.discover("account", "INBOX", 11, [1])
    item = store.get_item("account", "INBOX", 11, 1)
    assert item is not None
    store.mark_needs_attention(item, "attempt_limit_reached")
    assert store.retry_failed("account", "INBOX", 11, 10) == 1
    assert store.due_items("account", "INBOX", 11, 10)[0].status == "queued"


def test_retry_backoff_keeps_move_journal_recoverable(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.synchronize_mailbox("account", "INBOX", 11, 2)
    store.discover("account", "INBOX", 11, [1])
    item = store.get_item("account", "INBOX", 11, 1)
    assert item is not None
    store.record_assessment(
        item,
        message_id=None,
        fingerprint="synthetic-fingerprint",
        assessment=Assessment(0.99, 0.0, "model", "policy"),
        decision=Decision(Outcome.MOVE, "spam_threshold"),
        truncated=False,
    )
    item = store.get_item("account", "INBOX", 11, 1)
    assert item is not None
    store.begin_move(item, destination_before="5:1")
    moving = store.get_item("account", "INBOX", 11, 1)
    assert moving is not None
    store.schedule_move_retry(moving, delay_seconds=0)
    assert store.moving_items("account", "INBOX", 11, 10)[0].status == "moving"


def test_database_file_is_private_and_sql_connections_close(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)
    assert database.stat().st_mode & 0o777 == 0o600
    connection = store._connect()
    connection.close()
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("SQLite connection was not closed.")


def test_schema_v1_is_upgraded_without_dropping_existing_message_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE messages (
            account_id TEXT, mailbox TEXT, uid_validity INTEGER, uid INTEGER,
            status TEXT, next_attempt_at REAL
        );
        INSERT INTO messages VALUES ('account', 'INBOX', 11, 1, 'assessed', NULL);
        PRAGMA user_version=1;
        """
    )
    connection.close()

    StateStore(database)
    upgraded = sqlite3.connect(database)
    version = upgraded.execute("PRAGMA user_version").fetchone()[0]
    columns = {row[1] for row in upgraded.execute("PRAGMA table_info(messages)")}
    existing = upgraded.execute("SELECT status FROM messages WHERE uid=1").fetchone()[0]
    upgraded.close()
    assert version == 3
    assert {"decision_outcome", "decision_reason", "local_verdict", "local_reason"}.issubset(
        columns
    )
    assert existing == "assessed"
