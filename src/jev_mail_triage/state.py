from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from .errors import ConfigurationError
from .models import Assessment, Decision, LocalLabel, LocalVerdict, MailItem

SCHEMA_VERSION = 3
LOCAL_MODEL_VERSION = "local-gate"
# Mirrors gate.USER_RESTORED; state must not import the gate module.
USER_RESTORED_REASON = "user_restored"
_SQL_CHUNK = 500


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._initialize()
            os.chmod(self.path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.path}{suffix}")
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)
        except OSError as exc:
            raise ConfigurationError(f"Could not prepare SQLite state at {self.path}.") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if current_version not in {0, 1, 2, SCHEMA_VERSION}:
                raise ConfigurationError(
                    f"Unsupported SQLite schema version {current_version}; expected "
                    f"{SCHEMA_VERSION}."
                )
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS mailboxes (
                    account_id TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    uid_validity INTEGER NOT NULL,
                    last_seen_uid INTEGER NOT NULL,
                    baseline_at REAL NOT NULL,
                    PRIMARY KEY (account_id, mailbox)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    account_id TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    uid_validity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    message_id TEXT,
                    fingerprint TEXT,
                    spam_score REAL,
                    scam_score REAL,
                    decision_outcome TEXT,
                    decision_reason TEXT,
                    model_version TEXT,
                    policy_version TEXT,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL,
                    action_phase TEXT,
                    destination_uid INTEGER,
                    destination_before TEXT,
                    last_error TEXT,
                    local_verdict TEXT,
                    local_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (account_id, mailbox, uid_validity, uid)
                );

                CREATE INDEX IF NOT EXISTS messages_due
                    ON messages(account_id, mailbox, status, next_attempt_at, uid);
                CREATE INDEX IF NOT EXISTS messages_assessed
                    ON messages(account_id, mailbox, status, uid);

                -- Local gate model: aggregate hashed-token weights, never message text.
                CREATE TABLE IF NOT EXISTS gate_tokens (
                    token INTEGER PRIMARY KEY,
                    spam REAL NOT NULL DEFAULT 0,
                    ham REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS gate_examples (
                    example_key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    weight REAL NOT NULL,
                    source TEXT NOT NULL,
                    signature BLOB,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gate_correspondents (
                    address_hash TEXT PRIMARY KEY,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gate_seen (
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uid_validity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    PRIMARY KEY (account_id, folder, uid_validity, uid)
                );

                CREATE TABLE IF NOT EXISTS gate_meta (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL
                );
                """
            )
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(messages)")}
            for column in ("decision_outcome", "decision_reason", "local_verdict", "local_reason"):
                if column not in columns:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
            if "fingerprint" in columns:
                db.execute(
                    "CREATE INDEX IF NOT EXISTS messages_fingerprint "
                    "ON messages(account_id, fingerprint)"
                )
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def synchronize_mailbox(
        self, account_id: str, mailbox: str, uid_validity: int, uid_next: int
    ) -> str:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT uid_validity FROM mailboxes WHERE account_id=? AND mailbox=?",
                (account_id, mailbox),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO mailboxes VALUES (?, ?, ?, ?, ?)",
                    (account_id, mailbox, uid_validity, max(uid_next - 1, 0), now),
                )
                return "baseline"
            old_validity = int(row["uid_validity"])
            if old_validity != uid_validity:
                db.execute(
                    """
                    UPDATE messages
                    SET status=CASE WHEN action_phase IS NOT NULL
                                    THEN 'needs_attention' ELSE 'superseded' END,
                        action_phase=CASE WHEN action_phase IS NOT NULL
                                          THEN 'ambiguous' ELSE action_phase END,
                        last_error='uidvalidity_changed',
                        updated_at=?
                    WHERE account_id=? AND mailbox=? AND uid_validity=?
                      AND status IN ('queued','retry_wait','assessed','moving')
                    """,
                    (now, account_id, mailbox, old_validity),
                )
                db.execute(
                    """
                    UPDATE mailboxes SET uid_validity=?, last_seen_uid=?, baseline_at=?
                    WHERE account_id=? AND mailbox=?
                    """,
                    (uid_validity, max(uid_next - 1, 0), now, account_id, mailbox),
                )
                return "rebaseline"
            return "steady"

    def cursor(self, account_id: str, mailbox: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT last_seen_uid FROM mailboxes WHERE account_id=? AND mailbox=?",
                (account_id, mailbox),
            ).fetchone()
        if row is None:
            raise ConfigurationError("Mailbox baseline is missing.")
        return int(row["last_seen_uid"])

    def discover(
        self, account_id: str, mailbox: str, uid_validity: int, uids: Iterable[int]
    ) -> int:
        values = sorted(set(uids))
        if not values:
            return 0
        now = time.time()
        inserted = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for uid in values:
                cursor = db.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (account_id, mailbox, uid_validity, uid, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (account_id, mailbox, uid_validity, uid, now, now),
                )
                inserted += cursor.rowcount
            db.execute(
                """
                UPDATE mailboxes SET last_seen_uid=MAX(last_seen_uid, ?)
                WHERE account_id=? AND mailbox=? AND uid_validity=?
                """,
                (values[-1], account_id, mailbox, uid_validity),
            )
        return inserted

    def get_item(
        self, account_id: str, mailbox: str, uid_validity: int, uid: int
    ) -> MailItem | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM messages
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (account_id, mailbox, uid_validity, uid),
            ).fetchone()
        return self._item(row) if row else None

    def due_items(
        self, account_id: str, mailbox: str, uid_validity: int, limit: int
    ) -> list[MailItem]:
        now = time.time()
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM messages
                WHERE account_id=? AND mailbox=? AND uid_validity=?
                  AND (status='queued' OR
                       (status='retry_wait' AND COALESCE(next_attempt_at, 0) <= ?))
                ORDER BY uid LIMIT ?
                """,
                (account_id, mailbox, uid_validity, now, limit),
            ).fetchall()
        return [self._item(row) for row in rows]

    def assessed_candidates(
        self,
        account_id: str,
        mailbox: str,
        uid_validity: int,
        spam_threshold: float,
        scam_threshold: float,
        limit: int,
    ) -> list[MailItem]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM messages
                WHERE account_id=? AND mailbox=? AND uid_validity=?
                  AND status='assessed' AND truncated=0
                  AND (spam_score >= ? OR scam_score >= ?)
                ORDER BY uid LIMIT ?
                """,
                (
                    account_id,
                    mailbox,
                    uid_validity,
                    spam_threshold,
                    scam_threshold,
                    limit,
                ),
            ).fetchall()
        return [self._item(row) for row in rows]

    def moving_items(
        self, account_id: str, mailbox: str, uid_validity: int, limit: int
    ) -> list[MailItem]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM messages
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND status='moving'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY uid LIMIT ?
                """,
                (account_id, mailbox, uid_validity, time.time(), limit),
            ).fetchall()
        return [self._item(row) for row in rows]

    def record_assessment(
        self,
        item: MailItem,
        *,
        message_id: str | None,
        fingerprint: str,
        assessment: Assessment,
        decision: Decision,
        truncated: bool,
        local_verdict: LocalVerdict | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages
                SET message_id=?, fingerprint=?, spam_score=?, scam_score=?,
                    decision_outcome=?, decision_reason=?, model_version=?, policy_version=?,
                    truncated=?, local_verdict=?, local_reason=?, status='assessed',
                    attempts=0, next_attempt_at=NULL, last_error=NULL, updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (
                    message_id,
                    fingerprint,
                    assessment.spam_score,
                    assessment.scam_score,
                    decision.outcome.value,
                    decision.reason,
                    assessment.model_version,
                    assessment.policy_version,
                    int(truncated),
                    local_verdict.label.value if local_verdict else None,
                    local_verdict.reason if local_verdict else None,
                    time.time(),
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )

    def start_attempt(self, item: MailItem) -> int:
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages SET attempts=attempts+1, updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (
                    time.time(),
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )
            row = db.execute(
                """
                SELECT attempts FROM messages
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (item.account_id, item.mailbox, item.uid_validity, item.uid),
            ).fetchone()
        return int(row["attempts"])

    def schedule_retry(self, item: MailItem, attempts: int, delay_seconds: int) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages
                SET status='retry_wait', next_attempt_at=?, last_error='transient_failure',
                    updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (
                    now + delay_seconds,
                    now,
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )

    def schedule_move_retry(self, item: MailItem, delay_seconds: int) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages
                SET next_attempt_at=?, last_error='transient_failure', updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                  AND status='moving'
                """,
                (
                    now + delay_seconds,
                    now,
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )

    def begin_move(
        self,
        item: MailItem,
        *,
        phase: str = "move_intent",
        destination_before: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages SET status='moving', action_phase=?,
                    destination_before=?, updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (
                    phase,
                    destination_before,
                    time.time(),
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )

    def set_action_phase(
        self, item: MailItem, phase: str, destination_uid: int | None = None
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages SET action_phase=?, destination_uid=?, updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (
                    phase,
                    destination_uid,
                    time.time(),
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )

    def mark_moved(self, item: MailItem) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages SET status='moved', action_phase='complete',
                    next_attempt_at=NULL, last_error=NULL, updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (
                    time.time(),
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )

    def restore_assessed(self, item: MailItem) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages SET status='assessed', action_phase=NULL,
                    destination_uid=NULL, destination_before=NULL, attempts=0,
                    next_attempt_at=NULL, last_error=NULL, updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (
                    time.time(),
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )

    def mark_needs_attention(
        self, item: MailItem, error_code: str, *, ambiguous_action: bool = False
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages SET status='needs_attention', last_error=?,
                    action_phase=CASE WHEN ? THEN 'ambiguous' ELSE action_phase END,
                    next_attempt_at=NULL, updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (
                    error_code[:80],
                    int(ambiguous_action),
                    time.time(),
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )

    def retry_failed(self, account_id: str, mailbox: str, uid_validity: int, limit: int) -> int:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT uid FROM messages
                WHERE account_id=? AND mailbox=? AND uid_validity=?
                  AND status='needs_attention' AND action_phase IS NULL
                ORDER BY uid LIMIT ?
                """,
                (account_id, mailbox, uid_validity, limit),
            ).fetchall()
            for row in rows:
                db.execute(
                    """
                    UPDATE messages SET status='queued', attempts=0,
                        next_attempt_at=NULL, last_error=NULL, updated_at=?
                    WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                    """,
                    (now, account_id, mailbox, uid_validity, int(row["uid"])),
                )
        return len(rows)

    def mark_superseded(self, item: MailItem, error_code: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE messages SET status='needs_attention', last_error=?,
                    action_phase='ambiguous', updated_at=?
                WHERE account_id=? AND mailbox=? AND uid_validity=? AND uid=?
                """,
                (
                    error_code,
                    time.time(),
                    item.account_id,
                    item.mailbox,
                    item.uid_validity,
                    item.uid,
                ),
            )

    def counts(self, account_id: str, mailbox: str) -> Counter[str]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT status, COUNT(*) AS count FROM messages
                WHERE account_id=? AND mailbox=? GROUP BY status
                """,
                (account_id, mailbox),
            ).fetchall()
        return Counter({str(row["status"]): int(row["count"]) for row in rows})

    def status_counts(self, account_id: str) -> dict[str, Counter[str]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT mailbox, status, COUNT(*) AS count FROM messages
                WHERE account_id=? GROUP BY mailbox, status ORDER BY mailbox, status
                """,
                (account_id,),
            ).fetchall()
            candidates = db.execute(
                """
                SELECT mailbox, COUNT(*) AS count FROM messages
                WHERE account_id=? AND decision_outcome='move'
                GROUP BY mailbox ORDER BY mailbox
                """,
                (account_id,),
            ).fetchall()
            local = db.execute(
                """
                SELECT mailbox,
                    SUM(local_verdict='spam') AS local_spam,
                    SUM(local_verdict='clean') AS local_clean,
                    SUM(local_verdict IS NOT NULL AND model_version=?) AS local_decided,
                    SUM(local_verdict IS NOT NULL AND model_version<>?
                        AND ((local_verdict='spam' AND decision_outcome='leave')
                          OR (local_verdict='clean' AND decision_outcome='move'))) AS local_disagree
                FROM messages
                WHERE account_id=? AND local_verdict IS NOT NULL
                GROUP BY mailbox ORDER BY mailbox
                """,
                (LOCAL_MODEL_VERSION, LOCAL_MODEL_VERSION, account_id),
            ).fetchall()
        result: dict[str, Counter[str]] = {}
        for row in rows:
            mailbox = str(row["mailbox"])
            result.setdefault(mailbox, Counter())[str(row["status"])] = int(row["count"])
        for row in candidates:
            mailbox = str(row["mailbox"])
            result.setdefault(mailbox, Counter())["move_decision"] = int(row["count"])
        for row in local:
            counter = result.setdefault(str(row["mailbox"]), Counter())
            for key in ("local_spam", "local_clean", "local_decided", "local_disagree"):
                if row[key]:
                    counter[key] = int(row[key])
        return result

    def was_moved(self, account_id: str, fingerprint: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT 1 FROM messages
                WHERE account_id=? AND fingerprint=? AND status='moved' LIMIT 1
                """,
                (account_id, fingerprint),
            ).fetchone()
        return row is not None

    def latest_decision(self, account_id: str, fingerprint: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT decision_outcome FROM messages
                WHERE account_id=? AND fingerprint=? AND decision_outcome IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1
                """,
                (account_id, fingerprint),
            ).fetchone()
        return str(row["decision_outcome"]) if row else None

    def statistics(self, account_id: str, since: float | None = None) -> dict[str, object]:
        """Aggregate counts over all monitored mailboxes; no message data."""
        with self._connect() as db:
            row = db.execute(
                """
                SELECT
                    COUNT(*) AS discovered,
                    SUM(decision_outcome IS NOT NULL) AS assessed,
                    SUM(status IN ('queued', 'retry_wait')) AS waiting,
                    SUM(status='needs_attention') AS needs_attention,
                    SUM(decision_outcome='move') AS flagged,
                    SUM(decision_outcome='move' AND decision_reason<>'scam_threshold') AS spam,
                    SUM(decision_outcome='move' AND decision_reason='scam_threshold') AS scam,
                    SUM(status='moved') AS moved,
                    SUM(status='moving') AS moving,
                    SUM(decision_outcome='leave') AS left_in_place,
                    SUM(decision_reason='truncated_input') AS too_large,
                    SUM(decision_reason='authenticated_allowlist') AS allowlisted,
                    SUM(decision_reason=?) AS restored_by_user,
                    SUM(decision_outcome IS NOT NULL AND model_version<>?) AS decided_by_jev,
                    SUM(decision_outcome IS NOT NULL AND model_version=?) AS decided_locally,
                    SUM(local_verdict='spam') AS local_spam,
                    SUM(local_verdict='clean') AS local_clean,
                    SUM(local_verdict IS NOT NULL AND model_version<>?
                        AND ((local_verdict='spam' AND decision_outcome='leave')
                          OR (local_verdict='clean' AND decision_outcome='move'))) AS local_disagree,
                    MIN(created_at) AS first_seen,
                    MAX(created_at) AS last_seen
                FROM messages
                WHERE account_id=? AND created_at>=?
                """,
                (
                    USER_RESTORED_REASON,
                    LOCAL_MODEL_VERSION,
                    LOCAL_MODEL_VERSION,
                    LOCAL_MODEL_VERSION,
                    account_id,
                    since or 0.0,
                ),
            ).fetchone()
            reasons = db.execute(
                """
                SELECT local_reason, COUNT(*) AS count FROM messages
                WHERE account_id=? AND created_at>=? AND local_reason IS NOT NULL
                GROUP BY local_reason ORDER BY count DESC, local_reason
                """,
                (account_id, since or 0.0),
            ).fetchall()
        counts = [name for name in row.keys() if name not in ("first_seen", "last_seen")]  # noqa: SIM118
        result: dict[str, object] = {name: int(row[name] or 0) for name in counts}
        result["first_seen"] = row["first_seen"]
        result["last_seen"] = row["last_seen"]
        result["local_reasons"] = {str(r["local_reason"]): int(r["count"]) for r in reasons}
        return result

    def gate_totals(self) -> dict[str, float]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT label, COUNT(*) AS examples, SUM(weight) AS weight
                FROM gate_examples GROUP BY label
                """
            ).fetchall()
            correspondents = db.execute("SELECT COUNT(*) FROM gate_correspondents").fetchone()[0]
        totals = {
            "spam_examples": 0.0,
            "spam_weight": 0.0,
            "ham_examples": 0.0,
            "ham_weight": 0.0,
            "correspondents": float(correspondents),
        }
        for row in rows:
            label = "spam" if row["label"] == LocalLabel.SPAM.value else "ham"
            totals[f"{label}_examples"] = float(row["examples"])
            totals[f"{label}_weight"] = float(row["weight"] or 0.0)
        return totals

    def token_weights(self, tokens: Iterable[int]) -> dict[int, tuple[float, float]]:
        values = list(tokens)
        result: dict[int, tuple[float, float]] = {}
        with self._connect() as db:
            for start in range(0, len(values), _SQL_CHUNK):
                chunk = values[start : start + _SQL_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                for row in db.execute(
                    f"SELECT token, spam, ham FROM gate_tokens WHERE token IN ({placeholders})",
                    chunk,
                ):
                    result[int(row["token"])] = (float(row["spam"]), float(row["ham"]))
        return result

    def learn_example(
        self,
        example_key: str,
        label: LocalLabel,
        weight: float,
        source: str,
        signature: bytes | None,
        tokens: Iterable[int],
        *,
        relabel_weight: float = 1.0,
    ) -> str:
        """Add or relabel one training example; returns added/reinforced/relabelled/unchanged.

        Moving mail between a spam and a ham folder is a user correction, so a relabel is
        stored with at least ``relabel_weight``.
        """
        token_list = list(tokens)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT label, weight FROM gate_examples WHERE example_key=?", (example_key,)
            ).fetchone()
            deltas: list[tuple[LocalLabel, float]] = []
            if row is None:
                outcome = "added"
                deltas.append((label, weight))
            elif row["label"] == label.value:
                if weight <= float(row["weight"]):
                    return "unchanged"
                outcome = "reinforced"
                deltas.append((label, weight - float(row["weight"])))
            else:
                outcome = "relabelled"
                weight = max(weight, relabel_weight)
                deltas.append((LocalLabel(row["label"]), -float(row["weight"])))
                deltas.append((label, weight))
            for delta_label, delta in deltas:
                spam = delta if delta_label == LocalLabel.SPAM else 0.0
                ham = delta if delta_label == LocalLabel.CLEAN else 0.0
                # Deltas are passed twice: excluded.* would hold the clamped insert values,
                # which would lose negative deltas when relabelling.
                db.executemany(
                    """
                    INSERT INTO gate_tokens (token, spam, ham) VALUES (?, MAX(0, ?), MAX(0, ?))
                    ON CONFLICT(token) DO UPDATE SET
                        spam=MAX(0, spam + ?), ham=MAX(0, ham + ?)
                    """,
                    ((token, spam, ham, spam, ham) for token in token_list),
                )
            db.execute(
                """
                INSERT INTO gate_examples
                    (example_key, label, weight, source, signature, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(example_key) DO UPDATE SET
                    label=excluded.label, weight=excluded.weight, source=excluded.source,
                    signature=excluded.signature, updated_at=excluded.updated_at
                """,
                (example_key, label.value, weight, source, signature, time.time()),
            )
        return outcome

    def example_signatures(self) -> list[tuple[LocalLabel, bytes]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT label, signature FROM gate_examples WHERE signature IS NOT NULL"
            ).fetchall()
        return [(LocalLabel(row["label"]), bytes(row["signature"])) for row in rows]

    def add_correspondents(self, address_hashes: Iterable[str]) -> int:
        now = time.time()
        added = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for address_hash in set(address_hashes):
                added += db.execute(
                    "INSERT OR IGNORE INTO gate_correspondents VALUES (?, ?)",
                    (address_hash, now),
                ).rowcount
        return added

    def is_correspondent(self, address_hash: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM gate_correspondents WHERE address_hash=?", (address_hash,)
            ).fetchone()
        return row is not None

    def unseen_training_uids(
        self, account_id: str, folder: str, uid_validity: int, uids: Iterable[int]
    ) -> list[int]:
        with self._connect() as db:
            seen = {
                int(row["uid"])
                for row in db.execute(
                    """
                    SELECT uid FROM gate_seen
                    WHERE account_id=? AND folder=? AND uid_validity=?
                    """,
                    (account_id, folder, uid_validity),
                )
            }
        return [uid for uid in uids if uid not in seen]

    def mark_training_seen(self, account_id: str, folder: str, uid_validity: int, uid: int) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO gate_seen VALUES (?, ?, ?, ?)",
                (account_id, folder, uid_validity, uid),
            )

    def gate_meta(self, key: str) -> float | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM gate_meta WHERE key=?", (key,)).fetchone()
        return float(row["value"]) if row else None

    def set_gate_meta(self, key: str, value: float) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO gate_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    @classmethod
    def candidates(cls, path: Path, account_id: str, limit: int) -> list[MailItem]:
        if not 1 <= limit <= 1000:
            raise ConfigurationError("Candidate limit must be between 1 and 1000.")
        path = path.expanduser()
        if not path.is_file():
            raise ConfigurationError("No state database exists; run poll or backfill first.")
        try:
            with sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, factory=_ClosingConnection
            ) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    """
                    SELECT * FROM messages
                    WHERE account_id=? AND decision_outcome='move'
                    ORDER BY updated_at DESC, mailbox, uid_validity DESC, uid DESC
                    LIMIT ?
                    """,
                    (account_id, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ConfigurationError("Could not read candidate assessments from SQLite.") from exc
        return [cls._item(row) for row in rows]

    @staticmethod
    def _item(row: sqlite3.Row) -> MailItem:
        return MailItem(
            account_id=str(row["account_id"]),
            mailbox=str(row["mailbox"]),
            uid_validity=int(row["uid_validity"]),
            uid=int(row["uid"]),
            message_id=row["message_id"],
            fingerprint=row["fingerprint"],
            spam_score=row["spam_score"],
            scam_score=row["scam_score"],
            decision_outcome=row["decision_outcome"],
            decision_reason=row["decision_reason"],
            model_version=row["model_version"],
            policy_version=row["policy_version"],
            truncated=bool(row["truncated"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            next_attempt_at=row["next_attempt_at"],
            action_phase=row["action_phase"],
            destination_uid=row["destination_uid"],
            destination_before=row["destination_before"],
            last_error=row["last_error"],
            local_verdict=row["local_verdict"],
            local_reason=row["local_reason"],
        )
