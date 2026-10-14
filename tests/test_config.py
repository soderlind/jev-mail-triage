from pathlib import Path

import pytest

from jev_mail_triage.config import load_config
from jev_mail_triage.errors import ConfigurationError

PROJECT_ROOT = Path(__file__).parents[1]


def test_example_config_defaults_to_loopback_tls_and_dry_run() -> None:
    config = load_config(PROJECT_ROOT / "config.example.toml")
    assert config.bridge.host == "127.0.0.1"
    assert config.service.poll_interval_seconds == 60
    assert config.policy.move_enabled is False


def test_remote_bridge_host_is_rejected(tmp_path: Path) -> None:
    config_text = (PROJECT_ROOT / "config.example.toml").read_text(encoding="utf-8")
    remote_config = tmp_path / "config.toml"
    remote_config.write_text(
        config_text.replace('host = "127.0.0.1"', 'host = "mail.example.test"')
    )
    with pytest.raises(ConfigurationError, match="loopback"):
        load_config(remote_config)


def _with_gate(tmp_path: Path, gate: str) -> Path:
    config_text = (PROJECT_ROOT / "config.example.toml").read_text(encoding="utf-8")
    base = config_text.split("[gate]")[0]
    path = tmp_path / "config.toml"
    path.write_text(f"{base}[gate]\n{gate}\n", encoding="utf-8")
    return path


def test_example_config_enables_gate_in_shadow_mode_with_allowed_languages() -> None:
    gate = load_config(PROJECT_ROOT / "config.example.toml").gate
    assert gate.mode == "shadow"
    assert gate.enabled and not gate.enforced
    assert gate.allowed_languages == ("en", "nb", "nn", "da", "sv")
    assert gate.spam_folders == ("Spam",)


def test_gate_is_off_when_section_is_missing(tmp_path: Path) -> None:
    config_text = (PROJECT_ROOT / "config.example.toml").read_text(encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(config_text.split("# Local gate")[0], encoding="utf-8")
    gate = load_config(path).gate
    assert not gate.enabled
    assert gate.allowed_languages == ()


def test_norwegian_alias_is_expanded(tmp_path: Path) -> None:
    gate = load_config(_with_gate(tmp_path, 'mode = "enforce"\nallowed_languages = ["no"]')).gate
    assert gate.enforced
    assert gate.allowed_languages == ("nb", "nn")


@pytest.mark.parametrize(
    ("gate", "message"),
    [
        ('mode = "auto"', "Gate mode"),
        ('allowed_languages = ["xx"]', "language"),
        ("bayes_clean_threshold = 0.9\nbayes_spam_threshold = 0.5", "below"),
        ("near_duplicate_similarity = 0.2", "at least 0.5"),
        ('spam_folders = ["Archive"]', "both"),
        ('ham_folders = ["INBOX"]', "monitored"),
        ('spam_folders = ["Jev Review"]', "review"),
        ("learn_from_jev = 1", "true or false"),
    ],
)
def test_invalid_gate_settings_are_rejected(tmp_path: Path, gate: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_config(_with_gate(tmp_path, gate))
