"""Read and update the TOML configuration for the macOS app.

The CLI keeps reading the same file with ``load_config``. Updates are restricted to an
allowlist of keys, validated by ``load_config`` before an atomic replace, and the previous
file is kept as ``config.toml.bak``. Rewriting drops TOML comments.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import DEFAULT_DATABASE_PATH, Config, load_config
from .errors import ConfigurationError
from .lock import file_lock

_STR = "string"
_INT = "integer"
_NUM = "number"
_BOOL = "boolean"
_LIST = "string_list"
_OPT_STR = "optional_string"

EDITABLE: dict[str, dict[str, str]] = {
    "bridge": {
        "account_id": _STR,
        "host": _STR,
        "port": _INT,
        "security_mode": _STR,
        "username": _STR,
        "mailbox": _STR,
        "tls_ca_file": _OPT_STR,
    },
    "service": {
        "poll_interval_seconds": _INT,
        "batch_size": _INT,
        "max_attempts": _INT,
        "backoff_max_seconds": _INT,
    },
    "policy": {
        "move_enabled": _BOOL,
        "spam_threshold": _NUM,
        "scam_threshold": _NUM,
        "max_input_bytes": _INT,
        "review_folder": _STR,
        "model": _STR,
        "trusted_authserv_id": _OPT_STR,
        "allowlist": _LIST,
    },
    "gate": {
        "mode": _STR,
        "allowed_languages": _LIST,
        "spam_folders": _LIST,
        "ham_folders": _LIST,
        "sent_folders": _LIST,
        "min_examples": _INT,
        "bayes_spam_threshold": _NUM,
        "bayes_clean_threshold": _NUM,
        "near_duplicate_similarity": _NUM,
        "learn_from_jev": _BOOL,
        "train_interval_seconds": _INT,
    },
}

# Starting point for a first-time app setup; dry-run stays the default.
NEW_CONFIG: dict[str, Any] = {
    "database_path": str(DEFAULT_DATABASE_PATH),
    "bridge": {"account_id": "primary", "host": "127.0.0.1", "mailbox": "INBOX"},
    "service": {},
    "policy": {"move_enabled": False, "review_folder": "Folders/Jev Review"},
    "gate": {"mode": "off"},
}


def read_document(path: Path) -> dict[str, Any] | None:
    try:
        return tomllib.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Could not read configuration file: {exc}") from exc


def describe(config: Config) -> dict[str, Any]:
    """JSON-safe view of the effective configuration. Contains no secrets."""
    data = asdict(config)
    data["database_path"] = str(config.database_path)
    ca = config.bridge.tls_ca_file
    data["bridge"]["tls_ca_file"] = str(ca) if ca is not None else None
    for section in ("policy", "gate"):
        for key, value in data[section].items():
            if isinstance(value, tuple):
                data[section][key] = list(value)
    return data


def _check_value(section: str, key: str, kind: str, value: Any) -> Any:
    name = f"{section}.{key}"
    if value is None:
        return None
    if kind == _STR and isinstance(value, str):
        return value
    if kind == _OPT_STR and isinstance(value, str):
        return value if value.strip() else None
    if kind == _BOOL and isinstance(value, bool):
        return value
    if kind == _INT and isinstance(value, int) and not isinstance(value, bool):
        return value
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == _NUM and is_number and math.isfinite(value):
        return float(value)
    if kind == _LIST and isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        return list(value)
    raise ConfigurationError(f"Setting {name!r} must be a {kind.replace('_', ' ')}.")


def merge(document: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply ``{section: {key: value}}``; ``None`` removes a key so its default applies."""
    if not isinstance(patch, dict):
        raise ConfigurationError("Settings patch must be an object of sections.")
    merged = json.loads(json.dumps(document))
    for section, values in patch.items():
        allowed = EDITABLE.get(section)
        if allowed is None or not isinstance(values, dict):
            raise ConfigurationError(f"Unknown settings section {section!r}.")
        table = merged.setdefault(section, {})
        if not isinstance(table, dict):
            raise ConfigurationError(f"Configuration section [{section}] must be a table.")
        for key, value in values.items():
            kind = allowed.get(key)
            if kind is None:
                raise ConfigurationError(f"Setting {section}.{key} cannot be changed here.")
            checked = _check_value(section, key, kind, value)
            if checked is None:
                table.pop(key, None)
            else:
                table[key] = checked
    return merged


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are a valid subset of TOML basic strings.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(entry) for entry in value) + "]"
    raise ConfigurationError(f"Cannot write configuration value of type {type(value).__name__}.")


def dumps(document: dict[str, Any]) -> str:
    lines = ["# Written by Jev Mail Triage. Previous version: config.toml.bak", ""]
    tables = {key: value for key, value in document.items() if isinstance(value, dict)}
    for key, value in document.items():
        if key not in tables:
            lines.append(f"{key} = {_toml_value(value)}")
    for name, table in tables.items():
        lines += ["", f"[{name}]"]
        for key, value in table.items():
            if isinstance(value, dict):
                raise ConfigurationError("Nested configuration tables are not supported.")
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def update_config(path: Path, patch: dict[str, Any]) -> Config:
    """Validate and atomically write ``patch`` merged into the config file at ``path``."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Concurrent saves from separate helper processes must each merge into the latest file.
    with file_lock(path.with_name(f".{path.name}.lock")):
        return _update_locked(path, patch)


def _update_locked(path: Path, patch: dict[str, Any]) -> Config:
    existing = read_document(path)
    merged = merge(NEW_CONFIG if existing is None else existing, patch)
    text = dumps(merged)
    fd, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        config = load_config(temp)
        if existing is not None:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return config
