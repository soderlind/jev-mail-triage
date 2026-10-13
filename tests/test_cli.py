from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import pytest

from jev_mail_triage import cli
from jev_mail_triage.cli import _evaluation_rows, _metrics
from jev_mail_triage.config import BridgeConfig, Config, PolicyConfig, ServiceConfig
from jev_mail_triage.errors import ConfigurationError, RetryableError
from jev_mail_triage.models import Assessment, Decision, Outcome
from jev_mail_triage.state import StateStore


def _config(tmp_path: Path) -> Config:
    return Config(
        bridge=BridgeConfig("test", "127.0.0.1", 1143, "starttls", "user", "INBOX", None),
        policy=PolicyConfig(False, 0.98, 0.95, 4096, "Jev Review", None, (), "jev-latest"),
        service=ServiceConfig(60, 20, 3, 6),
        database_path=tmp_path / "state.sqlite3",
    )


def _candidate(config: Config, account: str = "test", outcome: Outcome = Outcome.MOVE) -> None:
    store = StateStore(config.database_path)
    store.synchronize_mailbox(account, "INBOX", 11, 1)
    store.discover(account, "INBOX", 11, [1])
    item = store.get_item(account, "INBOX", 11, 1)
    assert item is not None
    store.record_assessment(
        item,
        message_id="<synthetic@example.test>",
        fingerprint="synthetic",
        assessment=Assessment(0.99, 0.1, "model", "policy"),
        decision=Decision(outcome, "spam_threshold"),
        truncated=False,
    )


def test_candidates_local_only_filters_account_and_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    _candidate(config)
    _candidate(config, "another-account")
    _candidate(config, "clean-account", Outcome.LEAVE)

    def forbidden(*_args: object) -> None:
        pytest.fail("Local candidate listing must not use Bridge or TypeSafe.")

    monkeypatch.setattr(cli, "_with_bridge", forbidden)
    monkeypatch.setattr(cli, "_assessor", forbidden)
    cli._candidates(config, 20, headers=False, as_json=True)
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["uid"] == 1
    assert rows[0]["spam_score"] == 0.99
    assert "headers" not in rows[0]
    assert StateStore.candidates(config.database_path, "clean-account", 20) == []


@pytest.mark.parametrize("validity,missing", [(11, False), (12, False), (11, True)])
def test_candidate_headers_are_readonly_and_guard_uidvalidity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    validity: int,
    missing: bool,
) -> None:
    config = _config(tmp_path)
    _candidate(config)
    calls: list[int] = []

    class Bridge:
        def select(self, mailbox: str, *, readonly: bool) -> tuple[int, int]:
            assert mailbox == "INBOX" and readonly
            return validity, 2

        def fetch_headers(self, uid: int) -> dict[str, str | bool] | None:
            calls.append(uid)
            return None if missing else {"subject": "Synthetic\x1b[31m"}

    monkeypatch.setattr(cli, "_with_bridge", lambda _config, operation: operation(Bridge()))
    cli._candidates(config, 20, headers=True, as_json=True)
    output = capsys.readouterr().out
    row = json.loads(output)[0]
    assert "\x1b" not in output
    if validity != 11:
        assert row["header_status"] == "uidvalidity_changed"
        assert calls == []
    else:
        assert calls == [1]
        assert row["header_status"] == ("missing" if missing else "available")


@pytest.mark.parametrize("limit", [0, 1001])
def test_candidates_reject_invalid_limit(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ConfigurationError, match="limit"):
        cli._candidates(_config(tmp_path), limit, headers=False, as_json=False)


def test_candidates_does_not_create_missing_database(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(ConfigurationError, match="No state database"):
        cli._candidates(config, 20, headers=False, as_json=False)
    assert not config.database_path.exists()


def test_candidates_limits_rows_without_updating_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _candidate(config)
    store = StateStore(config.database_path)
    store.discover("test", "INBOX", 11, [2])
    item = store.get_item("test", "INBOX", 11, 2)
    assert item is not None
    store.record_assessment(
        item,
        message_id=None,
        fingerprint="second",
        assessment=Assessment(0.99, 0.1, "model", "policy"),
        decision=Decision(Outcome.MOVE, "spam_threshold"),
        truncated=False,
    )
    before = store.status_counts("test")
    assert [item.uid for item in StateStore.candidates(config.database_path, "test", 1)] == [2]
    assert store.status_counts("test") == before


def test_candidate_headers_skip_non_pending_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    _candidate(config)
    store = StateStore(config.database_path)
    item = store.get_item("test", "INBOX", 11, 1)
    assert item is not None
    store.mark_needs_attention(item, "synthetic")

    class Bridge:
        def select(self, _mailbox: str, *, readonly: bool) -> tuple[int, int]:
            assert readonly
            return 11, 2

        def fetch_headers(self, _uid: int) -> None:
            pytest.fail("Non-pending source must not be fetched.")

    monkeypatch.setattr(cli, "_with_bridge", lambda _config, operation: operation(Bridge()))
    cli._candidates(config, 20, headers=True, as_json=True)
    assert json.loads(capsys.readouterr().out)[0]["header_status"] == "source_not_pending"


def test_metrics_report_false_positives_precision_and_recall() -> None:
    assert _metrics([True, False, True, False], [True, True, False, False]) == (
        1,
        0.5,
        0.5,
    )


def test_label_csv_resolves_synthetic_eml_paths(tmp_path: Path) -> None:
    (tmp_path / "sample.eml").write_text("From: test@example.test\n\nfixture")
    labels = tmp_path / "labels.csv"
    labels.write_text("path,spam,scam_phishing\nsample.eml,yes,0\n")
    rows = _evaluation_rows(labels)
    assert rows == [(tmp_path / "sample.eml", True, False)]


def test_bridge_failures_use_bounded_exponential_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Bridge:
        attempts = 0

        def __init__(self, *_args: object) -> None:
            pass

        def __enter__(self) -> Self:
            _Bridge.attempts += 1
            if _Bridge.attempts < 3:
                raise RetryableError("synthetic connection failure")
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    delays: list[int] = []
    monkeypatch.setattr(cli, "BridgeImap", _Bridge)
    monkeypatch.setattr(cli, "get_bridge_password", lambda _account: "synthetic")
    monkeypatch.setattr(cli.time, "sleep", delays.append)

    assert cli._with_bridge(_config(tmp_path), lambda _mailbox: "connected") == "connected"
    assert _Bridge.attempts == 3
    assert delays == [5, 6]


def test_bridge_failure_attempts_stop_at_configured_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Bridge:
        attempts = 0

        def __init__(self, *_args: object) -> None:
            pass

        def __enter__(self) -> Self:
            _Bridge.attempts += 1
            raise RetryableError("synthetic connection failure")

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(cli, "BridgeImap", _Bridge)
    monkeypatch.setattr(cli, "get_bridge_password", lambda _account: "synthetic")
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    with pytest.raises(RetryableError):
        cli._with_bridge(_config(tmp_path), lambda _mailbox: "unreachable")
    assert _Bridge.attempts == 3
