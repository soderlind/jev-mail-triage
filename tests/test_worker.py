from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from jev_mail_triage import config_store
from jev_mail_triage.config import Config, load_config
from jev_mail_triage.errors import RetryableError
from jev_mail_triage.lock import ProcessLock
from jev_mail_triage.paths import STATUS_FILE, read_json
from jev_mail_triage.service import CycleResult
from jev_mail_triage.worker import Worker, WorkerDeps, read_control, write_control

OK = CycleResult("steady", 1, 1, 0, 0)


class _Clock:
    """Monotonic time that jumps ahead so every wait ends on its first check."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 10_000.0
        return self.now


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    config_store.update_config(
        path, {"bridge": {"username": "synthetic", "port": 1143, "security_mode": "starttls"}}
    )
    path.write_text(
        path.read_text().replace(
            str(config_store.DEFAULT_DATABASE_PATH), str(tmp_path / "state.sqlite3")
        )
    )
    return path


@contextmanager
def _no_assessor(_config: Config) -> Iterator[None]:
    yield None


def _worker(
    tmp_path: Path, bridge: Callable[[Config, Callable[[Any], Any]], Any], **overrides: Any
) -> Worker:
    options: dict[str, Any] = {
        "bridge": bridge,
        "assessor": _no_assessor,
        "cycle": lambda *_args, **_kwargs: OK,
        "monotonic": _Clock(),
        "tick_seconds": 0.0,
    }
    deps = WorkerDeps(**(options | overrides))
    return Worker(tmp_path / "config.toml", tmp_path / "state", deps)


def _status(tmp_path: Path) -> dict[str, Any]:
    status = read_json(tmp_path / "state" / STATUS_FILE)
    assert status is not None
    return status


def test_worker_reports_bridge_outage_then_recovers(tmp_path: Path) -> None:
    _write_config(tmp_path)
    states: list[str] = []
    outcomes: list[Exception | None] = [RetryableError("down", code="bridge_unavailable"), None]

    def bridge(_config: Config, operation: Callable[[Any], Any]) -> Any:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        result = operation(object())
        worker.request_stop()
        return result

    worker = _worker(tmp_path, bridge)
    original = worker._publish

    def record(state: str, **kwargs: Any) -> None:
        states.append(state)
        original(state, **kwargs)

    worker._publish = record  # type: ignore[method-assign]
    assert worker.run() == 0
    assert "bridge_unavailable" in states
    assert states.index("bridge_unavailable") < states.index("idle")
    status = _status(tmp_path)
    assert status["state"] == "stopped"
    assert status["last_cycle"]["assessed"] == 1
    assert status["error"] is None


def test_worker_releases_the_lock_while_backing_off_after_a_failure(tmp_path: Path) -> None:
    _write_config(tmp_path)

    def bridge(*_args: object) -> Any:
        raise RetryableError("down", code="bridge_unavailable")

    worker = _worker(tmp_path, bridge)
    original = worker._publish
    lock_free: list[bool] = []

    def record(state: str, **kwargs: Any) -> None:
        original(state, **kwargs)
        if state == "bridge_unavailable":
            with ProcessLock(tmp_path / "state.sqlite3"):
                lock_free.append(True)
            worker.request_stop()

    worker._publish = record  # type: ignore[method-assign]
    worker.run()
    assert lock_free == [True]
    assert _status(tmp_path)["consecutive_failures"] == 1


def test_worker_stays_alive_and_reports_missing_configuration(tmp_path: Path) -> None:
    def bridge(*_args: object) -> Any:
        pytest.fail("No Bridge session without a valid configuration.")

    worker = _worker(tmp_path, bridge)
    errors: list[str] = []
    original = worker._publish

    def record(state: str, **kwargs: Any) -> None:
        original(state, **kwargs)
        if state == "error":
            errors.append(_status(tmp_path)["error"]["code"])
            worker.request_stop()

    worker._publish = record  # type: ignore[method-assign]
    assert worker.run() == 0
    assert errors == ["not_configured"]


def test_paused_worker_releases_the_single_worker_lock(tmp_path: Path) -> None:
    _write_config(tmp_path)
    write_control(tmp_path / "state", paused=True)

    def bridge(*_args: object) -> Any:
        pytest.fail("A paused worker must not contact Bridge.")

    worker = _worker(tmp_path, bridge)
    original = worker._publish
    lock_free: list[bool] = []

    def record(state: str, **kwargs: Any) -> None:
        original(state, **kwargs)
        if state == "paused":
            with ProcessLock(tmp_path / "state.sqlite3"):
                lock_free.append(True)
            worker.request_stop()

    worker._publish = record  # type: ignore[method-assign]
    worker.run()
    assert lock_free == [True]


def test_worker_waits_instead_of_running_beside_another_process(tmp_path: Path) -> None:
    _write_config(tmp_path)

    def bridge(*_args: object) -> Any:
        pytest.fail("A second worker must not run a cycle.")

    worker = _worker(tmp_path, bridge)
    original = worker._publish

    def record(state: str, **kwargs: Any) -> None:
        original(state, **kwargs)
        if state == "waiting_for_lock":
            worker.request_stop()

    worker._publish = record  # type: ignore[method-assign]
    with ProcessLock(tmp_path / "state.sqlite3"):
        worker.run()
    status = _status(tmp_path)
    assert status["error"]["code"] == "busy"
    assert isinstance(status["lock_holder"], int)


def test_pause_request_stops_a_cycle_between_messages(tmp_path: Path) -> None:
    _write_config(tmp_path)
    seen: list[bool] = []

    def cycle(*_args: object, stop: Callable[[], bool], **_kwargs: object) -> CycleResult:
        seen.append(stop())
        write_control(tmp_path / "state", paused=True)
        seen.append(stop())
        worker.request_stop()
        return OK

    worker = _worker(tmp_path, lambda _config, operation: operation(object()), cycle=cycle)
    worker.run()
    assert seen == [False, True]
    assert read_control(tmp_path / "state").paused is True


def test_config_change_ends_the_session_for_a_reload(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    loads: list[int] = []

    def load(config_path: Path) -> Config:
        loads.append(1)
        if len(loads) == 2:
            worker.request_stop()
        return load_config(config_path)

    def bridge(_config: Config, operation: Callable[[Any], Any]) -> Any:
        result = operation(object())
        text = path.read_text().replace("poll_interval_seconds", "#")
        path.write_text(text + "\n")
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        return result

    worker = _worker(tmp_path, bridge, load=load)
    worker.run()
    assert len(loads) == 2


def test_status_file_contains_no_message_data(tmp_path: Path) -> None:
    _write_config(tmp_path)

    def bridge(_config: Config, operation: Callable[[Any], Any]) -> Any:
        worker.request_stop()
        return operation(object())

    worker = _worker(tmp_path, bridge)
    worker.run()
    status = _status(tmp_path)
    assert set(status) == {
        "protocol",
        "version",
        "pid",
        "state",
        "since",
        "heartbeat",
        "error",
        "last_cycle",
        "next_cycle_at",
        "consecutive_failures",
        "lock_holder",
        "settings",
    }
    assert set(status["last_cycle"]) == {
        "finished_at",
        "baseline",
        "discovered",
        "assessed",
        "moved",
        "attention",
    }
    json.dumps(status)
