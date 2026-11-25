"""Versioned JSON protocol between the macOS app and the bundled helper.

One request is read from stdin and exactly one response line is written to stdout.
Secrets travel only inside the stdin request, never in argv. Diagnostics go to stderr.

Request::

    {"protocol": 1, "id": "optional", "command": "status", "params": {}}

Response::

    {"protocol": 1, "id": "...", "ok": true, "result": {...}}
    {"protocol": 1, "id": "...", "ok": false,
     "error": {"code": "bridge_unavailable", "message": "...", "retryable": true}}

The exit code mirrors the outcome so callers can rely on it without parsing:
0 ok, 1 internal, 2 configuration, 3 unavailable (retry later), 4 busy, 5 protocol.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from . import __version__, config_store, keychain, runtime
from .config import Config, load_config
from .errors import ConfigurationError, RetryableError, TriageError
from .imap_client import BridgeImap
from .lock import ProcessLock
from .paths import STATUS_FILE, read_json
from .state import StateStore
from .worker import PROTOCOL_VERSION, read_control, write_control

logger = logging.getLogger("jev_mail_triage")
MAX_REQUEST_BYTES = 1_000_000
MAX_HEADER_LOOKUPS = 50

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CONFIGURATION = 2
EXIT_UNAVAILABLE = 3
EXIT_BUSY = 4
EXIT_PROTOCOL = 5

_EXIT_FOR_CODE = {
    "bad_request": EXIT_PROTOCOL,
    "unsupported_protocol": EXIT_PROTOCOL,
    "unknown_command": EXIT_PROTOCOL,
    "busy": EXIT_BUSY,
    "bridge_unavailable": EXIT_UNAVAILABLE,
    "typesafe_unavailable": EXIT_UNAVAILABLE,
    "internal": EXIT_INTERNAL,
}
_RETRYABLE_CODES = frozenset({"bridge_unavailable", "typesafe_unavailable", "busy"})


class ProtocolError(TriageError):
    """A malformed or unsupported request."""


class Context:
    def __init__(self, config_path: Path, state_dir: Path) -> None:
        self.config_path = config_path.expanduser()
        self.state_dir = state_dir

    def config(self) -> Config:
        return load_config(self.config_path)


Handler = Callable[[Context, dict[str, Any]], dict[str, Any]]
_HANDLERS: dict[str, Handler] = {}


def command(name: str) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        _HANDLERS[name] = handler
        return handler

    return register


# Parameter helpers ------------------------------------------------------------------


def _int_param(params: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ProtocolError(
            f"Parameter {key!r} must be an integer from {low} to {high}.", code="bad_request"
        )
    return value


def _str_param(params: dict[str, Any], key: str, *, required: bool = True) -> str | None:
    value = params.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"Parameter {key!r} must be a non-empty string.", code="bad_request")
    return value


def _config_with_overrides(ctx: Context, overrides: Any) -> Config:
    """Validate unsaved settings with the real loader without touching the config file."""
    if overrides is None:
        return ctx.config()
    if not isinstance(overrides, dict):
        raise ProtocolError("Parameter 'overrides' must be an object.", code="bad_request")
    existing = config_store.read_document(ctx.config_path)
    merged = config_store.merge(
        config_store.NEW_CONFIG if existing is None else existing, overrides
    )
    fd, name = tempfile.mkstemp(prefix=".jev-check-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(config_store.dumps(merged))
        return load_config(Path(name))
    finally:
        Path(name).unlink(missing_ok=True)


def _worker_status(ctx: Context) -> dict[str, Any]:
    status = read_json(ctx.state_dir / STATUS_FILE)
    alive = False
    if status is not None and isinstance(status.get("pid"), int):
        try:
            os.kill(status["pid"], 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
    control = read_control(ctx.state_dir)
    return {
        "status": status,
        "alive": alive,
        "control": {"paused": control.paused, "generation": control.generation},
    }


# Commands ---------------------------------------------------------------------------


@command("hello")
def _hello(_ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "helper_version": __version__,
        "python": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "keychain_backend": _keychain_backend(),
        "commands": sorted(_HANDLERS),
    }


def _keychain_backend() -> str:
    """Module of the resolved keyring backend; reads no secrets."""
    import keyring

    return type(keyring.get_keyring()).__module__


@command("config.get")
def _config_get(ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
    exists = ctx.config_path.is_file()
    return {
        "path": str(ctx.config_path),
        "exists": exists,
        "config": config_store.describe(ctx.config()) if exists else None,
    }


@command("config.set")
def _config_set(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    patch = params.get("patch")
    if not isinstance(patch, dict):
        raise ProtocolError("Parameter 'patch' must be an object.", code="bad_request")
    policy_patch = patch.get("policy")
    enabling = isinstance(policy_patch, dict) and policy_patch.get("move_enabled") is True
    if enabling:
        current = ctx.config().policy.move_enabled if ctx.config_path.is_file() else False
        if not current and params.get("confirm_enable_moves") is not True:
            raise ConfigurationError(
                "Enabling automatic moves needs explicit confirmation.",
                code="confirmation_required",
            )
    config = config_store.update_config(ctx.config_path, patch)
    return {"path": str(ctx.config_path), "config": config_store.describe(config)}


@command("secrets.set")
def _secrets_set(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    name = _str_param(params, "name")
    if name not in keychain.SECRET_NAMES:
        raise ProtocolError(
            "Secret name must be bridge-password or typesafe-api-key.", code="bad_request"
        )
    value = params.get("value")
    if not isinstance(value, str):
        raise ProtocolError("Parameter 'value' must be a string.", code="bad_request")
    account = _str_param(params, "account_id", required=False)
    if name == "bridge-password" and account is None:
        account = ctx.config().bridge.account_id
    keychain.store_secret(name, account, value)
    return {"stored": name}


@command("secrets.adopt")
def _secrets_adopt(ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
    """Re-save existing items so this helper owns them; macOS may ask once to allow it."""
    account = ctx.config().bridge.account_id
    adopted: list[str] = []
    missing: list[str] = []
    for name, read in (
        ("bridge-password", lambda: keychain.get_bridge_password(account)),
        ("typesafe-api-key", keychain.get_typesafe_api_key),
    ):
        try:
            value = read()
        except ConfigurationError as exc:
            if exc.code != "secret_missing":
                raise
            missing.append(name)
            continue
        keychain.store_secret(name, account, value)
        adopted.append(name)
    return {"adopted": adopted, "missing": missing}


@command("check.bridge")
def _check_bridge(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    """Read-only connection check; never creates folders or changes mail."""
    config = _config_with_overrides(ctx, params.get("overrides"))
    password = params.get("password")
    if password is not None and (not isinstance(password, str) or not password):
        raise ProtocolError("Parameter 'password' must be a non-empty string.", code="bad_request")

    def inspect(mailbox: BridgeImap) -> dict[str, Any]:
        available = mailbox.list_mailboxes()
        inbox = mailbox.resolve_mailbox(config.bridge.mailbox)
        mailbox.select(inbox, readonly=True)
        training = config.gate.spam_folders + config.gate.ham_folders + config.gate.sent_folders
        return {
            "connected": True,
            "mailbox": inbox,
            "supports_move": "MOVE" in mailbox.capabilities,
            "supports_uidplus": "UIDPLUS" in mailbox.capabilities,
            "review_folder": config.policy.review_folder,
            "review_folder_exists": config.policy.review_folder in available,
            "missing_training_folders": [f for f in training if f not in available]
            if config.gate.enabled
            else [],
            "folders": sorted(available),
        }

    return runtime.with_bridge(config, inspect, attempts=1, password=password)


@command("bridge.folders")
def _bridge_folders(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    """List folders for pickers. Unlike check.bridge it selects nothing, so it works
    even when the configured mailbox is wrong."""
    config = _config_with_overrides(ctx, params.get("overrides"))
    password = params.get("password")
    if password is not None and (not isinstance(password, str) or not password):
        raise ProtocolError("Parameter 'password' must be a non-empty string.", code="bad_request")
    folders = runtime.with_bridge(
        config, lambda mailbox: mailbox.list_mailboxes(), attempts=1, password=password
    )
    return {"folders": sorted(folders)}


@command("check.typesafe")
def _check_typesafe(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    """Validate the API key by listing models; no email content is sent."""
    from typesafe_sdk import (
        TypeSafeAPIConnectionError,
        TypeSafeAuthenticationError,
        TypeSafeError,
        TypeSafePermissionDeniedError,
    )

    api_key = params.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
        raise ProtocolError("Parameter 'api_key' must be a non-empty string.", code="bad_request")
    if ctx.config_path.is_file():
        config = ctx.config()
    else:
        config = _config_with_overrides(ctx, {})
    try:
        with runtime.make_assessor(config, api_key) as client:
            models = [model.name for model in client.models.list().models]
    except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as exc:
        raise ConfigurationError(
            "TypeSafe rejected the API key; check that it was copied completely.",
            code="typesafe_auth",
        ) from exc
    except TypeSafeAPIConnectionError as exc:
        raise RetryableError(
            "Could not reach TypeSafe; check the network connection.",
            code="typesafe_unavailable",
        ) from exc
    except TypeSafeError as exc:
        raise RetryableError(
            f"TypeSafe returned an error ({type(exc).__name__}).", code="typesafe_unavailable"
        ) from exc
    return {
        "valid": True,
        "model": config.policy.model,
        "model_available": config.policy.model in models,
        "models": models,
    }


@command("status")
def _status(ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
    config = ctx.config()
    store = StateStore(config.database_path)
    return {
        "counts": store.status_counts(config.bridge.account_id),
        "gate": {"mode": config.gate.mode, "totals": store.gate_totals()},
        "move_enabled": config.policy.move_enabled,
        "review_folder": config.policy.review_folder,
        "lock_held": ProcessLock.is_held(config.database_path),
        "lock_holder": ProcessLock.holder(config.database_path),
        "worker": _worker_status(ctx),
    }


@command("stats")
def _stats(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    config = ctx.config()
    days = params.get("days")
    if days is not None:
        days = _int_param(params, "days", 1, 1, 3650)
    since = time.time() - days * 86400 if days else None
    return {
        "days": days,
        "stats": StateStore(config.database_path).statistics(config.bridge.account_id, since),
    }


@command("candidates")
def _candidates(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    config = ctx.config()
    limit = _int_param(params, "limit", 50, 1, 1000)
    items = StateStore.candidates(config.database_path, config.bridge.account_id, limit)
    return {
        "candidates": runtime.candidate_rows(items),
        "spam_threshold": config.policy.spam_threshold,
        "scam_threshold": config.policy.scam_threshold,
    }


@command("candidates.headers")
def _candidate_headers(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch headers on demand, only for stored candidates. Nothing is persisted."""
    config = ctx.config()
    keys = params.get("items")
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_HEADER_LOOKUPS:
        raise ProtocolError(
            f"Parameter 'items' must list 1 to {MAX_HEADER_LOOKUPS} candidates.", code="bad_request"
        )
    wanted: set[tuple[str, int, int]] = set()
    for key in keys:
        if not isinstance(key, dict):
            raise ProtocolError("Each item must be an object.", code="bad_request")
        mailbox, validity, uid = key.get("mailbox"), key.get("uid_validity"), key.get("uid")
        if (
            not isinstance(mailbox, str)
            or not isinstance(validity, int)
            or not isinstance(uid, int)
        ):
            raise ProtocolError(
                "Each item needs mailbox, uid_validity, and uid.", code="bad_request"
            )
        wanted.add((mailbox, validity, uid))
    stored = StateStore.candidates(config.database_path, config.bridge.account_id, 1000)
    items = [i for i in stored if (i.mailbox, i.uid_validity, i.uid) in wanted]
    rows = runtime.candidate_rows(items)
    if rows:
        rows = runtime.with_bridge(
            config, lambda mailbox: runtime.with_headers(mailbox, items, rows), attempts=1
        )
    found = {(i.mailbox, i.uid_validity, i.uid) for i in items}
    unknown = [{"mailbox": m, "uid_validity": v, "uid": u} for (m, v, u) in sorted(wanted - found)]
    return {"candidates": rows, "not_candidates": unknown}


@command("attention")
def _attention(ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
    config = ctx.config()
    return {"reasons": StateStore(config.database_path).attention_summary(config.bridge.account_id)}


@command("attention.retry")
def _attention_retry(ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
    """Re-queue failed assessments and wake the worker. Rows with a move in progress are
    left alone; they need checking by hand."""
    config = ctx.config()
    store = StateStore(config.database_path)
    retried = store.retry_attention(config.bridge.account_id, 1000)
    if retried:
        write_control(ctx.state_dir, reload=True)
    return {"retried": retried, "reasons": store.attention_summary(config.bridge.account_id)}


@command("worker.status")
def _worker_status_command(ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
    return _worker_status(ctx)


@command("worker.control")
def _worker_control(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
    action = _str_param(params, "action")
    if action == "pause":
        control = write_control(ctx.state_dir, paused=True)
    elif action == "resume":
        control = write_control(ctx.state_dir, paused=False)
    elif action == "reload":
        control = write_control(ctx.state_dir, reload=True)
    else:
        raise ProtocolError("Action must be pause, resume, or reload.", code="bad_request")
    return {"control": {"paused": control.paused, "generation": control.generation}}


# Dispatch ---------------------------------------------------------------------------


def _error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": code in _RETRYABLE_CODES}


def _error_code(exc: TriageError) -> str:
    if exc.code:
        return exc.code
    if isinstance(exc, ConfigurationError):
        return "configuration"
    if isinstance(exc, RetryableError):
        return "bridge_unavailable"
    return "triage_error"


def _exit_code(code: str, exc: BaseException | None) -> int:
    if code in _EXIT_FOR_CODE:
        return _EXIT_FOR_CODE[code]
    if isinstance(exc, RetryableError):
        return EXIT_UNAVAILABLE
    if isinstance(exc, ConfigurationError):
        return EXIT_CONFIGURATION
    return EXIT_INTERNAL


def handle(raw: bytes, ctx: Context) -> tuple[dict[str, Any], int]:
    request_id: Any = None
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise ProtocolError("Request is too large.", code="bad_request")
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolError("Request is not valid JSON.", code="bad_request") from exc
        if not isinstance(request, dict):
            raise ProtocolError("Request must be a JSON object.", code="bad_request")
        request_id = request.get("id")
        if request_id is not None and not isinstance(request_id, (str, int)):
            request_id = None
            raise ProtocolError("Request id must be a string or integer.", code="bad_request")
        if request.get("protocol") != PROTOCOL_VERSION:
            raise ProtocolError(
                f"Unsupported protocol version; this helper speaks {PROTOCOL_VERSION}.",
                code="unsupported_protocol",
            )
        name = request.get("command")
        handler = _HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            raise ProtocolError(f"Unknown command {name!r}.", code="unknown_command")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolError("Parameter 'params' must be an object.", code="bad_request")
        result = handler(ctx, params)
    except TriageError as exc:
        code = _error_code(exc)
        logger.info("Request failed: %s", code)
        return _response(request_id, error=_error(code, str(exc))), _exit_code(code, exc)
    except Exception:
        logger.exception("Helper request failed unexpectedly.")
        return _response(
            request_id, error=_error("internal", "The helper failed unexpectedly; see its log.")
        ), EXIT_INTERNAL
    return _response(request_id, result=result), EXIT_OK


def _response(
    request_id: Any, *, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None
) -> dict[str, Any]:
    response: dict[str, Any] = {"protocol": PROTOCOL_VERSION, "id": request_id, "ok": error is None}
    if error is None:
        response["result"] = result
    else:
        response["error"] = error
    return response


def serve_request(
    ctx: Context, stdin: IO[bytes] | None = None, stdout: IO[str] | None = None
) -> int:
    source = stdin if stdin is not None else sys.stdin.buffer
    target = stdout if stdout is not None else sys.stdout
    raw = source.read(MAX_REQUEST_BYTES + 1)
    # Anything a dependency prints must not corrupt the single JSON response line.
    with contextlib.redirect_stdout(sys.stderr):
        response, exit_code = handle(raw, ctx)
    target.write(json.dumps(response, sort_keys=True, default=_json_default) + "\n")
    target.flush()
    return exit_code


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"Cannot encode {type(value).__name__}")
