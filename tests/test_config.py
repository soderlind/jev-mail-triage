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
