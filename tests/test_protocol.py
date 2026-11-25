from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Self

import httpx2
import pytest
from typesafe_sdk import TypeSafeAuthenticationError

from jev_mail_triage import config_store, keychain, protocol, runtime
from jev_mail_triage.config import load_config
from jev_mail_triage.models import Assessment, Decision, Outcome
from jev_mail_triage.state import StateStore

BRIDGE = {
    "bridge": {"username": "synthetic", "port": 1143, "security_mode": "starttls"},
}


def _ctx(tmp_path: Path) -> protocol.Context:
    return protocol.Context(tmp_path / "config.toml", tmp_path / "state")


def _configured(tmp_path: Path) -> protocol.Context:
    ctx = _ctx(tmp_path)
    patch = json.loads(json.dumps(BRIDGE))
    config_store.update_config(ctx.config_path, patch)
    text = ctx.config_path.read_text().replace(
        str(config_store.DEFAULT_DATABASE_PATH), str(tmp_path / "state.sqlite3")
    )
    ctx.config_path.write_text(text)
    return ctx


def _call(ctx: protocol.Context, command: str, **params: Any) -> tuple[dict[str, Any], int]:
    raw = json.dumps({"protocol": 1, "id": "t", "command": command, "params": params})
    return protocol.handle(raw.encode(), ctx)


def _candidate(ctx: protocol.Context, uid: int) -> None:
    config = ctx.config()
    store = StateStore(config.database_path)
    store.synchronize_mailbox("primary", "INBOX", 11, 1)
    store.discover("primary", "INBOX", 11, [uid])
    item = store.get_item("primary", "INBOX", 11, uid)
    assert item is not None
    store.record_assessment(
        item,
        message_id=None,
        fingerprint=f"synthetic-{uid}",
        assessment=Assessment(0.99, 0.1, "model", "policy"),
        decision=Decision(Outcome.MOVE, "spam_threshold"),
        truncated=False,
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"not json", "bad_request"),
        (b"[]", "bad_request"),
        (b'{"protocol": 2, "command": "hello"}', "unsupported_protocol"),
        (b'{"protocol": 1, "command": "mail.delete"}', "unknown_command"),
        (b'{"protocol": 1, "command": "hello", "params": []}', "bad_request"),
    ],
)
def test_malformed_requests_get_protocol_errors(tmp_path: Path, raw: bytes, code: str) -> None:
    response, exit_code = protocol.handle(raw, _ctx(tmp_path))
    assert response["ok"] is False
    assert response["protocol"] == 1
    assert response["error"]["code"] == code
    assert exit_code == protocol.EXIT_PROTOCOL


def test_serve_request_writes_one_json_line_and_keeps_stray_output_off_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def noisy(_ctx: protocol.Context, _params: dict[str, Any]) -> dict[str, Any]:
        print("diagnostic from a dependency")
        return {"value": 1}

    monkeypatch.setitem(protocol._HANDLERS, "test.noisy", noisy)
    stdout = io.StringIO()
    request = io.BytesIO(b'{"protocol": 1, "id": 7, "command": "test.noisy"}')
    exit_code = protocol.serve_request(_ctx(tmp_path), request, stdout)

    lines = stdout.getvalue().splitlines()
    assert exit_code == 0
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"id": 7, "ok": True, "protocol": 1, "result": {"value": 1}}
    assert "diagnostic from a dependency" in capsys.readouterr().err


def test_unconfigured_status_is_a_configuration_error(tmp_path: Path) -> None:
    response, exit_code = _call(_ctx(tmp_path), "status")
    assert response["error"]["code"] == "not_configured"
    assert response["error"]["retryable"] is False
    assert exit_code == protocol.EXIT_CONFIGURATION


def test_unexpected_exception_is_internal_without_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_ctx: protocol.Context, _params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("secret detail")

    monkeypatch.setitem(protocol._HANDLERS, "test.broken", broken)
    response, exit_code = _call(_ctx(tmp_path), "test.broken")
    assert response["error"]["code"] == "internal"
    assert "secret detail" not in json.dumps(response)
    assert exit_code == protocol.EXIT_INTERNAL


def test_first_config_set_creates_a_dry_run_config(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    response, exit_code = _call(ctx, "config.set", patch=BRIDGE)
    assert exit_code == 0
    assert response["result"]["config"]["policy"]["move_enabled"] is False
    assert load_config(ctx.config_path).bridge.username == "synthetic"
    assert ctx.config_path.stat().st_mode & 0o777 == 0o600


def test_enabling_moves_requires_explicit_confirmation(tmp_path: Path) -> None:
    ctx = _configured(tmp_path)
    before = ctx.config_path.read_text()

    response, exit_code = _call(ctx, "config.set", patch={"policy": {"move_enabled": True}})
    assert response["error"]["code"] == "confirmation_required"
    assert exit_code == protocol.EXIT_CONFIGURATION
    assert ctx.config_path.read_text() == before

    response, _ = _call(
        ctx, "config.set", patch={"policy": {"move_enabled": True}}, confirm_enable_moves=True
    )
    assert response["result"]["config"]["policy"]["move_enabled"] is True
    assert ctx.config_path.with_name("config.toml.bak").read_text() == before


@pytest.mark.parametrize(
    "patch",
    [
        {"service": {"batch_size": 50}},
        {"policy": {"spam_threshold": "high"}},
        {"database_path": "/tmp/elsewhere"},
        {"bridge": {"host": "mail.example.test"}},
        {"policy": {"delete_spam": True}},
    ],
)
def test_invalid_or_unsupported_settings_leave_config_unchanged(
    tmp_path: Path, patch: dict[str, Any]
) -> None:
    ctx = _configured(tmp_path)
    before = ctx.config_path.read_text()
    response, exit_code = _call(ctx, "config.set", patch=patch)
    assert response["ok"] is False
    assert exit_code == protocol.EXIT_CONFIGURATION
    assert ctx.config_path.read_text() == before
    assert not list(tmp_path.glob(".config-*"))


def test_config_round_trip_preserves_existing_values(tmp_path: Path) -> None:
    ctx = _configured(tmp_path)
    original = load_config(ctx.config_path)
    _call(ctx, "config.set", patch={"gate": {"mode": "shadow", "allowed_languages": ["en", "no"]}})
    updated = load_config(ctx.config_path)
    assert updated.bridge == original.bridge
    assert updated.gate.mode == "shadow"
    assert updated.gate.allowed_languages == ("en", "nb", "nn")


def test_secret_is_taken_from_the_request_and_never_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _configured(tmp_path)
    stored: list[tuple[str, str | None, str]] = []
    monkeypatch.setattr(keychain, "store_secret", lambda *args: stored.append(args))
    response, exit_code = _call(ctx, "secrets.set", name="bridge-password", value="hunter2-x")
    assert exit_code == 0
    assert stored == [("bridge-password", "primary", "hunter2-x")]
    assert "hunter2-x" not in json.dumps(response)


def test_header_lookup_is_limited_to_stored_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _configured(tmp_path)
    _candidate(ctx, 5)

    class Bridge:
        def select(self, _mailbox: str, *, readonly: bool) -> tuple[int, int]:
            assert readonly
            return 11, 10

        def fetch_headers(self, uid: int) -> dict[str, object]:
            assert uid == 5
            return {"subject": "Synthetic", "sender": "s@example.test"}

    calls: list[int | None] = []

    def fake_bridge(_config: object, operation: Any, *, attempts: int | None = None) -> Any:
        calls.append(attempts)
        return operation(Bridge())

    monkeypatch.setattr(runtime, "with_bridge", fake_bridge)
    response, _ = _call(
        ctx,
        "candidates.headers",
        items=[
            {"mailbox": "INBOX", "uid_validity": 11, "uid": 5},
            {"mailbox": "INBOX", "uid_validity": 11, "uid": 6},
        ],
    )
    result = response["result"]
    assert [row["uid"] for row in result["candidates"]] == [5]
    assert result["candidates"][0]["headers"]["subject"] == "Synthetic"
    assert result["not_candidates"] == [{"mailbox": "INBOX", "uid_validity": 11, "uid": 6}]
    assert calls == [1]

    calls.clear()
    response, _ = _call(
        ctx, "candidates.headers", items=[{"mailbox": "INBOX", "uid_validity": 11, "uid": 9}]
    )
    assert response["result"]["candidates"] == []
    assert calls == []


def test_check_bridge_uses_unsaved_overrides_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _configured(tmp_path)
    before = ctx.config_path.read_text()
    seen: dict[str, object] = {}

    class Bridge:
        capabilities = frozenset({"MOVE", "UIDPLUS"})

        def __init__(self, config: Any, password: str) -> None:
            seen["port"] = config.port
            seen["password"] = password

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def list_mailboxes(self) -> list[str]:
            return ["INBOX", "Folders/Jev Review"]

        def resolve_mailbox(self, name: str) -> str:
            return name

        def select(self, _mailbox: str, *, readonly: bool) -> tuple[int, int]:
            assert readonly
            return 1, 1

    monkeypatch.setattr(runtime, "BridgeImap", Bridge)
    response, exit_code = _call(
        ctx, "check.bridge", overrides={"bridge": {"port": 1144}}, password="typed-in"
    )
    assert exit_code == 0
    assert seen == {"port": 1144, "password": "typed-in"}
    assert response["result"]["review_folder_exists"] is True
    assert ctx.config_path.read_text() == before


def test_typesafe_rejection_is_reported_as_actionable_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _configured(tmp_path)

    class Models:
        def list(self) -> object:
            raise TypeSafeAuthenticationError(401, None, httpx2.Headers())

    class Client:
        models = Models()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(runtime, "make_assessor", lambda _config, _key=None: Client())
    response, exit_code = _call(ctx, "check.typesafe", api_key="synthetic")
    assert response["error"]["code"] == "typesafe_auth"
    assert exit_code == protocol.EXIT_CONFIGURATION


def test_worker_control_pause_and_resume_bump_generation(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    paused, _ = _call(ctx, "worker.control", action="pause")
    resumed, _ = _call(ctx, "worker.control", action="resume")
    assert paused["result"]["control"] == {"paused": True, "generation": 1}
    assert resumed["result"]["control"] == {"paused": False, "generation": 2}
    bad, exit_code = _call(ctx, "worker.control", action="delete")
    assert bad["error"]["code"] == "bad_request"
    assert exit_code == protocol.EXIT_PROTOCOL
