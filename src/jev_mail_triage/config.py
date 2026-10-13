from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

DEFAULT_CONFIG_PATH = Path.home() / ".config/jev-mail-triage/config.toml"
DEFAULT_DATABASE_PATH = Path.home() / "Library/Application Support/jev-mail-triage/state.sqlite3"


@dataclass(frozen=True)
class BridgeConfig:
    account_id: str
    host: str
    port: int
    security_mode: str
    username: str
    mailbox: str
    tls_ca_file: Path | None


@dataclass(frozen=True)
class PolicyConfig:
    move_enabled: bool
    spam_threshold: float
    scam_threshold: float
    max_input_bytes: int
    review_folder: str
    trusted_authserv_id: str | None
    allowlist: tuple[str, ...]
    model: str


@dataclass(frozen=True)
class ServiceConfig:
    poll_interval_seconds: int
    batch_size: int
    max_attempts: int
    backoff_max_seconds: int


@dataclass(frozen=True)
class Config:
    bridge: BridgeConfig
    policy: PolicyConfig
    service: ServiceConfig
    database_path: Path


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"Configuration section [{key}] is required.")
    return value


def _string(table: dict[str, Any], key: str, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Configuration value {key!r} must be a non-empty string.")
    return value.strip()


def _integer(table: dict[str, Any], key: str, default: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"Configuration value {key!r} must be an integer.")
    return value


def _threshold(table: dict[str, Any], key: str, default: float) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"Configuration value {key!r} must be a number.")
    threshold = float(value)
    if not 0.0 <= threshold <= 1.0:
        raise ConfigurationError(f"Configuration value {key!r} must be between 0 and 1.")
    return threshold


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    try:
        raw = tomllib.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"Configuration file not found: {path.expanduser()}. "
            "Copy config.example.toml and edit its Bridge settings."
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Could not read configuration file: {exc}") from exc

    bridge_raw = _table(raw, "bridge")
    policy_raw = _table(raw, "policy")
    service_raw = _table(raw, "service")
    bridge_port = _integer(bridge_raw, "port", 0)
    if not 1 <= bridge_port <= 65535:
        raise ConfigurationError("Bridge port must be between 1 and 65535.")
    security_mode = _string(bridge_raw, "security_mode", None).casefold()
    if security_mode not in {"starttls", "implicit_tls"}:
        raise ConfigurationError("Bridge security_mode must be 'starttls' or 'implicit_tls'.")

    move_enabled = policy_raw.get("move_enabled", False)
    if not isinstance(move_enabled, bool):
        raise ConfigurationError("Configuration value 'move_enabled' must be true or false.")
    allowlist_raw = policy_raw.get("allowlist", [])
    if not isinstance(allowlist_raw, list) or any(
        not isinstance(entry, str) or not entry.strip() for entry in allowlist_raw
    ):
        raise ConfigurationError("Configuration value 'allowlist' must be an array of strings.")
    authserv = policy_raw.get("trusted_authserv_id")
    if authserv is not None and (not isinstance(authserv, str) or not authserv.strip()):
        raise ConfigurationError(
            "Configuration value 'trusted_authserv_id' must be a non-empty string or omitted."
        )

    ca_file_raw = bridge_raw.get("tls_ca_file")
    if ca_file_raw is not None and (not isinstance(ca_file_raw, str) or not ca_file_raw.strip()):
        raise ConfigurationError("Configuration value 'tls_ca_file' must be a path or omitted.")
    ca_file = Path(ca_file_raw).expanduser() if ca_file_raw else None

    db_path_raw = raw.get("database_path", str(DEFAULT_DATABASE_PATH))
    if not isinstance(db_path_raw, str) or not db_path_raw.strip():
        raise ConfigurationError("Configuration value 'database_path' must be a path string.")

    service = ServiceConfig(
        poll_interval_seconds=_integer(service_raw, "poll_interval_seconds", 60),
        batch_size=_integer(service_raw, "batch_size", 20),
        max_attempts=_integer(service_raw, "max_attempts", 5),
        backoff_max_seconds=_integer(service_raw, "backoff_max_seconds", 300),
    )
    if service.poll_interval_seconds < 1:
        raise ConfigurationError("poll_interval_seconds must be positive.")
    if not 1 <= service.batch_size <= 20:
        raise ConfigurationError("batch_size must be between 1 and 20.")
    if not 1 <= service.max_attempts <= 5:
        raise ConfigurationError("max_attempts must be between 1 and 5.")
    if service.backoff_max_seconds < 1:
        raise ConfigurationError("backoff_max_seconds must be positive.")

    policy = PolicyConfig(
        move_enabled=move_enabled,
        spam_threshold=_threshold(policy_raw, "spam_threshold", 0.98),
        scam_threshold=_threshold(policy_raw, "scam_threshold", 0.95),
        max_input_bytes=_integer(policy_raw, "max_input_bytes", 65536),
        review_folder=_string(policy_raw, "review_folder", "Jev Review"),
        trusted_authserv_id=authserv.strip().casefold() if isinstance(authserv, str) else None,
        allowlist=tuple(entry.strip().casefold() for entry in allowlist_raw),
        model=_string(policy_raw, "model", "jev-latest"),
    )
    if policy.max_input_bytes < 256:
        raise ConfigurationError("max_input_bytes must be at least 256.")

    host = _string(bridge_raw, "host", None)
    if host.casefold() != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ConfigurationError("Bridge host must be a local loopback address.")
        except ValueError as exc:
            raise ConfigurationError(
                "Bridge host must be localhost or a loopback IP address."
            ) from exc

    return Config(
        bridge=BridgeConfig(
            account_id=_string(bridge_raw, "account_id", None),
            host=host,
            port=bridge_port,
            security_mode=security_mode,
            username=_string(bridge_raw, "username", None),
            mailbox=_string(bridge_raw, "mailbox", "INBOX"),
            tls_ca_file=ca_file,
        ),
        policy=policy,
        service=service,
        database_path=Path(db_path_raw).expanduser(),
    )
