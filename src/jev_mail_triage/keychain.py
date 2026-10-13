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
            "Secrets require the macOS Keychain backend; refusing to use another keyring."
        )


def get_bridge_password(account_id: str) -> str:
    _require_macos_keychain()
    try:
        value = keyring.get_password(SERVICE_NAME, f"bridge:{account_id}")
    except keyring.errors.KeyringError as exc:
        raise ConfigurationError("Could not read Bridge credentials from macOS Keychain.") from exc
    if not value:
        raise ConfigurationError(
            f"No Bridge password is stored in Keychain for account {account_id!r}. "
            "Run `jev-mail-triage secret set bridge-password`."
        )
    return value


def get_typesafe_api_key() -> str:
    _require_macos_keychain()
    try:
        value = keyring.get_password(SERVICE_NAME, TYPESAFE_KEY_ACCOUNT)
    except keyring.errors.KeyringError as exc:
        raise ConfigurationError(
            "Could not read TypeSafe credentials from macOS Keychain."
        ) from exc
    if not value:
        raise ConfigurationError(
            "No TypeSafe API key is stored in Keychain. "
            "Run `jev-mail-triage secret set typesafe-api-key`."
        )
    return value


def set_secret(secret_name: str, account_id: str | None = None) -> None:
    _require_macos_keychain()
    if secret_name == "bridge-password":
        if not account_id:
            raise ConfigurationError("Bridge password setup requires a configured account_id.")
        key = f"bridge:{account_id}"
        prompt = "Bridge password: "
    elif secret_name == "typesafe-api-key":
        key = TYPESAFE_KEY_ACCOUNT
        prompt = "TypeSafe API key: "
    else:
        raise ConfigurationError("Secret name must be bridge-password or typesafe-api-key.")
    secret = getpass.getpass(prompt)
    if not secret.strip():
        raise ConfigurationError("Secret cannot be empty.")
    try:
        keyring.set_password(SERVICE_NAME, key, secret)
    except keyring.errors.KeyringError as exc:
        raise ConfigurationError("Could not store the secret in macOS Keychain.") from exc
