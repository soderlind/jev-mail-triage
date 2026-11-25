from __future__ import annotations

import getpass
import sys

import keyring

from .errors import ConfigurationError

SERVICE_NAME = "jev-mail-triage"
TYPESAFE_KEY_ACCOUNT = "typesafe-api-key"


def _require_macos_keychain() -> None:
    backend = keyring.get_keyring()
    if sys.platform != "darwin" or backend.__class__.__module__ != "keyring.backends.macOS":
        raise ConfigurationError(
            "Secrets require the macOS Keychain backend; refusing to use another keyring.",
            code="keychain_unavailable",
        )


def get_bridge_password(account_id: str) -> str:
    _require_macos_keychain()
    try:
        value = keyring.get_password(SERVICE_NAME, f"bridge:{account_id}")
    except keyring.errors.KeyringError as exc:
        raise ConfigurationError(
            "Could not read Bridge credentials from macOS Keychain.", code="keychain_denied"
        ) from exc
    if not value:
        raise ConfigurationError(
            f"No Bridge password is stored in Keychain for account {account_id!r}. "
            "Run `jev-mail-triage secret set bridge-password`.",
            code="secret_missing",
        )
    return value


def get_typesafe_api_key() -> str:
    _require_macos_keychain()
    try:
        value = keyring.get_password(SERVICE_NAME, TYPESAFE_KEY_ACCOUNT)
    except keyring.errors.KeyringError as exc:
        raise ConfigurationError(
            "Could not read TypeSafe credentials from macOS Keychain.", code="keychain_denied"
        ) from exc
    if not value:
        raise ConfigurationError(
            "No TypeSafe API key is stored in Keychain. "
            "Run `jev-mail-triage secret set typesafe-api-key`.",
            code="secret_missing",
        )
    return value


SECRET_NAMES = ("bridge-password", "typesafe-api-key")


def _secret_account(secret_name: str, account_id: str | None) -> str:
    if secret_name == "bridge-password":
        if not account_id:
            raise ConfigurationError("Bridge password setup requires a configured account_id.")
        return f"bridge:{account_id}"
    if secret_name == "typesafe-api-key":
        return TYPESAFE_KEY_ACCOUNT
    raise ConfigurationError("Secret name must be bridge-password or typesafe-api-key.")


def store_secret(secret_name: str, account_id: str | None, secret: str) -> None:
    """Store a secret non-interactively. The caller supplies it out of band (never argv)."""
    _require_macos_keychain()
    key = _secret_account(secret_name, account_id)
    if not secret.strip():
        raise ConfigurationError("Secret cannot be empty.", code="invalid_secret")
    try:
        # keyring deletes and re-adds the item, so it is owned by the calling binary.
        keyring.set_password(SERVICE_NAME, key, secret)
    except keyring.errors.KeyringError as exc:
        raise ConfigurationError(
            "Could not store the secret in macOS Keychain.", code="keychain_denied"
        ) from exc


def secret_present(secret_name: str, account_id: str | None) -> bool:
    """Whether a secret exists. Reads the item, so macOS may ask for Keychain access."""
    _require_macos_keychain()
    key = _secret_account(secret_name, account_id)
    try:
        return bool(keyring.get_password(SERVICE_NAME, key))
    except keyring.errors.KeyringError as exc:
        raise ConfigurationError(
            "Could not read the secret from macOS Keychain.", code="keychain_denied"
        ) from exc


def set_secret(secret_name: str, account_id: str | None = None) -> None:
    _require_macos_keychain()
    _secret_account(secret_name, account_id)
    prompt = "Bridge password: " if secret_name == "bridge-password" else "TypeSafe API key: "
    store_secret(secret_name, account_id, getpass.getpass(prompt))
