"""Fixed locations shared by the background worker, the helper protocol, and the app."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

APP_SUPPORT_DIR = Path.home() / "Library/Application Support/jev-mail-triage"
LOG_DIR = Path.home() / "Library/Logs/JevMailTriage"
CONTROL_FILE = "worker-control.json"
STATUS_FILE = "worker-status.json"


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None
