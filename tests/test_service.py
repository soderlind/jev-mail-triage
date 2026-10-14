from dataclasses import replace
from pathlib import Path

import pytest

from jev_mail_triage.config import BridgeConfig, Config, GateConfig, PolicyConfig, ServiceConfig
from jev_mail_triage.errors import ConfigurationError
from jev_mail_triage.gate import LocalGate
from jev_mail_triage.imap_client import FetchedMessage
from jev_mail_triage.message import prepare_message
from jev_mail_triage.models import Assessment, Decision, Outcome
from jev_mail_triage.service import run_cycle, run_training, training_due
from jev_mail_triage.state import StateStore

MESSAGE = (
    b"From: offer@bulk.example.test\r\n"
    b"Subject: Unrequested offer\r\n"
    b"Message-ID: <synthetic@example.test>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Claim the unexpected reward."
)


def _config(path: Path, *, moves: bool, gate: GateConfig | None = None) -> Config:
    return Config(
        bridge=BridgeConfig(
            account_id="test",
            host="127.0.0.1",
            port=1143,
            security_mode="starttls",
            username="synthetic",
            mailbox="INBOX",
            tls_ca_file=None,
        ),
        policy=PolicyConfig(
            move_enabled=moves,
            spam_threshold=0.98,
            scam_threshold=0.95,
            max_input_bytes=4096,
            review_folder="Jev Review",
            trusted_authserv_id=None,
            allowlist=(),
            model="jev-latest",
        ),
        service=ServiceConfig(
            poll_interval_seconds=60,
            batch_size=20,
            max_attempts=5,
            backoff_max_seconds=300,
        ),
        database_path=path,
        gate=gate or GateConfig(),
    )


class _FakeAssessor:
    def assess(self, prepared: object) -> Assessment:
        return Assessment(0.99, 0.1, "jev-latest", "email-v1")


class _FakeMailbox:
    def __init__(self, capabilities: frozenset[str]) -> None:
        self.capabilities = capabilities
        self.boxes: dict[str, dict[int, tuple[bytes, bool]]] = {"INBOX": {}}
        self.selected = "INBOX"
        self.readonly = True
        self.validity = {"INBOX": 77}

    def resolve_mailbox(self, configured_name: str) -> str:
        return "INBOX"

    def list_mailboxes(self) -> list[str]:
        return sorted(self.boxes)

    def select(self, mailbox: str, *, readonly: bool = True) -> tuple[int, int]:
        self.selected = mailbox
        self.readonly = readonly
        return self.validity.setdefault(mailbox, 1), max(self.boxes.get(mailbox, {}), default=0) + 1

    def search_uids(self, start_uid: int | None = None) -> list[int]:
        uids = sorted(self.boxes.get(self.selected, {}))
        return uids if start_uid is None else [uid for uid in uids if uid >= start_uid]

    def fetch_uid(self, uid: int) -> FetchedMessage | None:
        entry = self.boxes.get(self.selected, {}).get(uid)
        return FetchedMessage(entry[0], False) if entry else None

    def ensure_review_folder(self, folder: str) -> None:
        self.boxes.setdefault(folder, {})
        self.validity.setdefault(folder, 88)

    def move_uid(self, uid: int, target_folder: str) -> int | None:
        assert not self.readonly
        message = self.boxes[self.selected].pop(uid)
        target = self.boxes.setdefault(target_folder, {})
        new_uid = max(target, default=0) + 1
        target[new_uid] = message
        return new_uid

    def copy_uid(self, uid: int, target_folder: str) -> int:
        assert not self.readonly
        message = self.boxes[self.selected][uid]
        target = self.boxes.setdefault(target_folder, {})
        new_uid = max(target, default=0) + 1
        target[new_uid] = message
        return new_uid

    def delete_uid_and_expunge(self, uid: int) -> None:
        assert not self.readonly
        del self.boxes[self.selected][uid]

    def new_matching_messages(
        self,
        folder: str,
        baseline: str,
        expected_fingerprint: str,
        *,
        max_input_bytes: int,
    ) -> list[tuple[int, FetchedMessage]]:
        from jev_mail_triage.message import prepare_message

        validity_text, uid_next_text = baseline.split(":")
        if int(validity_text) != self.validity[folder]:
            return []
        matches = []
        for uid, (raw, _) in self.boxes.get(folder, {}).items():
            if uid < int(uid_next_text):
                continue
            fetched = FetchedMessage(raw, False)
            prepared = prepare_message(raw, max_input_bytes=max_input_bytes)
            if prepared.fingerprint == expected_fingerprint:
                matches.append((uid, fetched))
        return matches


def _add_message(mailbox: _FakeMailbox, uid: int, *, read: bool = False) -> None:
    mailbox.boxes["INBOX"][uid] = (MESSAGE, read)


def test_poll_baselines_existing_mail_then_dry_run_preserves_flags_and_location(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "state.sqlite3", moves=False)
    store = StateStore(config.database_path)
    mailbox = _FakeMailbox(frozenset({"MOVE"}))
    _add_message(mailbox, 1, read=True)
    first = run_cycle(config, store, mailbox, _FakeAssessor())
    assert first.baseline == "baseline"
    assert first.assessed == 0

    _add_message(mailbox, 2, read=False)
    result = run_cycle(config, store, mailbox, _FakeAssessor())
    assert result.discovered == 1
    assert result.assessed == 1
    assert result.moved == 0
    assert 2 in mailbox.boxes["INBOX"]
    assert mailbox.boxes["INBOX"][2][1] is False
    assessed = store.get_item("test", "INBOX", 77, 2)
    assert assessed is not None
    assert assessed.status == "assessed"


def test_restart_reconciles_completed_uid_move_without_repeating_it(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "state.sqlite3", moves=True)
    store = StateStore(config.database_path)
    mailbox = _FakeMailbox(frozenset({"MOVE"}))
    mailbox.ensure_review_folder("Jev Review")
    run_cycle(config, store, mailbox, _FakeAssessor())
    _add_message(mailbox, 1)
    mailbox.select("INBOX", readonly=True)
    validity, uid_next = mailbox.select("INBOX", readonly=True)
    store.synchronize_mailbox("test", "INBOX", validity, uid_next)
    store.discover("test", "INBOX", validity, [1])
    item = store.get_item("test", "INBOX", validity, 1)
    assert item is not None
    prepared = prepare_message(MESSAGE, max_input_bytes=4096)
    store.record_assessment(
        item,
        message_id=prepared.message_id,
        fingerprint=prepared.fingerprint,
        assessment=Assessment(0.99, 0.0, "jev-latest", "email-v1"),
        decision=Decision(Outcome.MOVE, "spam_threshold"),
        truncated=False,
    )
    assessed = store.get_item("test", "INBOX", validity, 1)
    assert assessed is not None
    store.begin_move(assessed, destination_before="88:1")
    mailbox.select("INBOX", readonly=False)
    mailbox.move_uid(1, "Jev Review")

    result = run_cycle(config, store, mailbox, _FakeAssessor())
    recovered = store.get_item("test", "INBOX", validity, 1)
    assert result.moved == 1
    assert recovered is not None and recovered.status == "moved"
    assert len(mailbox.boxes["Jev Review"]) == 1


def test_interrupted_copy_is_marked_ambiguous_without_deleting_source(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "state.sqlite3", moves=True)
    store = StateStore(config.database_path)
    mailbox = _FakeMailbox(frozenset({"UIDPLUS"}))
    mailbox.ensure_review_folder("Jev Review")
    run_cycle(config, store, mailbox, _FakeAssessor())
    _add_message(mailbox, 1)
    validity, uid_next = mailbox.select("INBOX", readonly=True)
    store.synchronize_mailbox("test", "INBOX", validity, uid_next)
    store.discover("test", "INBOX", validity, [1])
    item = store.get_item("test", "INBOX", validity, 1)
    assert item is not None
    prepared = prepare_message(MESSAGE, max_input_bytes=4096)
    store.record_assessment(
        item,
        message_id=prepared.message_id,
        fingerprint=prepared.fingerprint,
        assessment=Assessment(0.99, 0.0, "jev-latest", "email-v1"),
        decision=Decision(Outcome.MOVE, "spam_threshold"),
        truncated=False,
    )
    assessed = store.get_item("test", "INBOX", validity, 1)
    assert assessed is not None
    store.begin_move(assessed, phase="copy_intent", destination_before="88:1")
    mailbox.select("INBOX", readonly=False)
    mailbox.copy_uid(1, "Jev Review")

    run_cycle(config, store, mailbox, _FakeAssessor())
    unresolved = store.get_item("test", "INBOX", validity, 1)
    assert unresolved is not None and unresolved.status == "needs_attention"
    assert unresolved.last_error == "copy_outcome_ambiguous"
    assert 1 in mailbox.boxes["INBOX"]
    assert len(mailbox.boxes["Jev Review"]) == 1
    assert assessed.decision_outcome == "move"
    assert assessed.decision_reason == "spam_threshold"
    assert store.status_counts("test")["INBOX"]["move_decision"] == 1


def test_move_mode_moves_only_scored_message_to_review_folder(tmp_path: Path) -> None:
    config = _config(tmp_path / "state.sqlite3", moves=True)
    store = StateStore(config.database_path)
    mailbox = _FakeMailbox(frozenset({"MOVE"}))
    run_cycle(config, store, mailbox, _FakeAssessor())
    _add_message(mailbox, 1, read=True)
    result = run_cycle(config, store, mailbox, _FakeAssessor())
    assert result.moved == 1
    assert not mailbox.boxes["INBOX"]
    assert len(mailbox.boxes["Jev Review"]) == 1
    assert store.counts("test", "INBOX")["moved"] == 1


def test_uidplus_fallback_copies_verifies_then_expunge_source(tmp_path: Path) -> None:
    config = _config(tmp_path / "state.sqlite3", moves=True)
    store = StateStore(config.database_path)
    mailbox = _FakeMailbox(frozenset({"UIDPLUS"}))
    run_cycle(config, store, mailbox, _FakeAssessor())
    _add_message(mailbox, 1, read=True)
    result = run_cycle(config, store, mailbox, _FakeAssessor())
    assert result.moved == 1
    assert not mailbox.boxes["INBOX"]
    assert mailbox.boxes["Jev Review"][1][1] is True


def test_move_without_uid_move_or_uidplus_fails_without_changing_mail(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "state.sqlite3", moves=True)
    store = StateStore(config.database_path)
    mailbox = _FakeMailbox(frozenset())
    run_cycle(config, store, mailbox, _FakeAssessor())
    _add_message(mailbox, 1)
    result = run_cycle(config, store, mailbox, _FakeAssessor())
    assert result.moved == 0
    assert result.attention == 1
    assert 1 in mailbox.boxes["INBOX"]


def test_one_malformed_message_does_not_block_later_queue_items(tmp_path: Path) -> None:
    config = _config(tmp_path / "state.sqlite3", moves=False)
    store = StateStore(config.database_path)
    mailbox = _FakeMailbox(frozenset())
    run_cycle(config, store, mailbox, _FakeAssessor())
    malformed = (
        b"From: broken@example.test\r\n"
        b"Content-Type: text/plain; charset=x-no-such-charset\r\n\r\nbody"
    )
    mailbox.boxes["INBOX"][1] = (
        malformed,
        False,
    )
    _add_message(mailbox, 2)
    result = run_cycle(config, store, mailbox, _FakeAssessor())
    assert result.assessed == 2
    assert store.get_item("test", "INBOX", 77, 1).status == "needs_attention"
    assert store.get_item("test", "INBOX", 77, 2).status == "assessed"


GERMAN_SPAM = (
    "From: konto@bank.example.test\r\n"
    "Subject: Ihr Konto wurde gesperrt\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    "Sehr geehrter Kunde, Ihr Konto wurde vorübergehend gesperrt. Bitte bestätigen Sie "
    "Ihre Daten innerhalb von 24 Stunden, um eine Schließung zu vermeiden."
).encode()


class _CountingAssessor:
    def __init__(self, assessment: Assessment) -> None:
        self.assessment = assessment
        self.calls = 0

    def assess(self, prepared: object) -> Assessment:
        self.calls += 1
        return self.assessment


def _gate_setup(
    tmp_path: Path, mode: str, *, moves: bool = True
) -> tuple[Config, StateStore, LocalGate, _FakeMailbox]:
    gate_config = GateConfig(mode=mode, allowed_languages=("en", "nb", "nn", "da", "sv"))
    config = _config(tmp_path / "state.sqlite3", moves=moves, gate=gate_config)
    store = StateStore(config.database_path)
    gate = LocalGate(config.gate, config.policy, store)
    mailbox = _FakeMailbox(frozenset({"MOVE"}))
    run_cycle(config, store, mailbox, _FakeAssessor(), gate=gate)
    return config, store, gate, mailbox


def test_enforce_mode_decides_locally_without_calling_jev(tmp_path: Path) -> None:
    config, store, gate, mailbox = _gate_setup(tmp_path, "enforce")
    mailbox.boxes["INBOX"][1] = (GERMAN_SPAM, False)
    assessor = _CountingAssessor(Assessment(0.0, 0.0, "jev-latest", "email-v1"))
    result = run_cycle(config, store, mailbox, assessor, gate=gate)
    assert assessor.calls == 0
    assert result.moved == 1
    item = store.get_item("test", "INBOX", 77, 1)
    assert item.local_verdict == "spam"
    assert item.local_reason == "language_not_allowed"
    # Local verdicts are never fed back as training data.
    assert store.gate_totals()["spam_examples"] == 0


def test_shadow_mode_records_local_verdict_but_jev_decides(tmp_path: Path) -> None:
    config, store, gate, mailbox = _gate_setup(tmp_path, "shadow")
    mailbox.boxes["INBOX"][1] = (GERMAN_SPAM, False)
    assessor = _CountingAssessor(Assessment(0.01, 0.0, "jev-latest", "email-v1"))
    result = run_cycle(config, store, mailbox, assessor, gate=gate)
    assert assessor.calls == 1
    assert result.moved == 0
    item = store.get_item("test", "INBOX", 77, 1)
    assert (item.local_verdict, item.local_reason) == ("spam", "language_not_allowed")
    assert store.status_counts("test")["INBOX"]["local_disagree"] == 1
    # A confidently clean Jev result is learned as ham.
    assert store.gate_totals()["ham_examples"] == 1


def test_jev_move_decision_is_learned_as_spam(tmp_path: Path) -> None:
    config, store, gate, mailbox = _gate_setup(tmp_path, "shadow")
    _add_message(mailbox, 1)
    run_cycle(config, store, mailbox, _FakeAssessor(), gate=gate)
    assert store.gate_totals()["spam_examples"] == 1


def test_learning_from_jev_can_be_disabled(tmp_path: Path) -> None:
    config, store, gate, mailbox = _gate_setup(tmp_path, "shadow")
    config = replace(config, gate=replace(config.gate, learn_from_jev=False))
    _add_message(mailbox, 1)
    run_cycle(config, store, mailbox, _FakeAssessor(), gate=gate)
    assert store.gate_totals()["spam_examples"] == 0


def test_message_restored_by_user_is_not_moved_again_even_with_gate_off(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "state.sqlite3", moves=True)
    store = StateStore(config.database_path)
    mailbox = _FakeMailbox(frozenset({"MOVE"}))
    run_cycle(config, store, mailbox, _FakeAssessor())
    _add_message(mailbox, 1)
    assert run_cycle(config, store, mailbox, _FakeAssessor()).moved == 1

    # The user drags the message from Jev Review back to the inbox: it gets a new UID.
    mailbox.boxes["INBOX"][2] = mailbox.boxes["Jev Review"].pop(1)
    assessor = _CountingAssessor(Assessment(0.99, 0.1, "jev-latest", "email-v1"))
    result = run_cycle(config, store, mailbox, assessor)
    assert assessor.calls == 0
    assert result.moved == 0
    assert 2 in mailbox.boxes["INBOX"]
    item = store.get_item("test", "INBOX", 77, 2)
    assert (item.local_verdict, item.local_reason) == ("clean", "user_restored")


def _raw(sender: str, subject: str, body: str, to: str = "me@example.test") -> bytes:
    return (
        f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}"
    ).encode()


def test_training_reads_folders_once_and_counts_corrections(tmp_path: Path) -> None:
    config, store, gate, mailbox = _gate_setup(tmp_path, "shadow", moves=False)
    _add_message(mailbox, 1)
    # Jev leaves this message alone, then the user files it as junk: a correction.
    run_cycle(config, store, mailbox, _CountingAssessor(Assessment(0.2, 0.0, "j", "e")), gate=gate)
    mailbox.boxes["Spam"] = {1: (MESSAGE, False), 2: (_raw("x@y.test", "Win", "big prize"), False)}
    mailbox.boxes["Archive"] = {1: (_raw("boss@work.test", "Plan", "see agenda"), False)}
    mailbox.boxes["Sent"] = {
        1: (_raw("me@example.test", "Re", "ok", to="Pal <Pal@Example.test>, b@c.test"), False)
    }

    result = run_training(config, store, mailbox, gate, 100)
    assert (result.spam, result.ham, result.corrections) == (2, 1, 1)
    assert result.correspondents == 2
    assert result.missing_folders == []
    assert all(mailbox.selected != folder or mailbox.readonly for folder in mailbox.boxes)
    assert not training_due(config, store)

    again = run_training(config, store, mailbox, gate, 100)
    assert (again.spam, again.ham, again.correspondents) == (0, 0, 0)


def test_training_reports_missing_folders_and_validates_limit(tmp_path: Path) -> None:
    config, store, gate, mailbox = _gate_setup(tmp_path, "shadow", moves=False)
    result = run_training(config, store, mailbox, gate, 10)
    assert result.missing_folders == ["Spam", "Archive", "Sent"]
    with pytest.raises(ConfigurationError):
        run_training(config, store, mailbox, gate, 0)


def test_training_is_due_only_when_gate_enabled(tmp_path: Path) -> None:
    config = _config(tmp_path / "state.sqlite3", moves=False)
    store = StateStore(config.database_path)
    assert not training_due(config, store)
    enabled = replace(config, gate=GateConfig(mode="shadow", train_interval_seconds=60))
    assert training_due(enabled, store, now=1000.0)
    store.set_gate_meta("last_trained_at", 990.0)
    assert not training_due(enabled, store, now=1000.0)
    assert training_due(enabled, store, now=1050.0)
