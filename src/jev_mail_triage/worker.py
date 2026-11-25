"""Background worker run by the macOS app's LaunchAgent.

Unlike ``serve``, the worker never exits on configuration or Bridge problems. It reports
its state in ``worker-status.json`` (no message data), obeys pause/resume/reload requests
in ``worker-control.json``, and releases the single-worker lock while paused or broken so
CLI maintenance commands keep working. Stop requests are honoured between messages only.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, runtime
from .classifier import JevAssessor
from .config import Config, load_config
from .errors import ConfigurationError, RetryableError, TriageError
from .lock import ProcessLock, file_lock
from .paths import CONTROL_FILE, LOG_DIR, STATUS_FILE, read_json, write_json_atomic
from .service import CycleResult, run_cycle, training_due
from .state import StateStore

logger = logging.getLogger("jev_mail_triage")
PROTOCOL_VERSION = 1
_TICK_SECONDS = 1.0
_HEARTBEAT_SECONDS = 15.0
_ERROR_RETRY_SECONDS = 60.0
_LOCK_RETRY_SECONDS = 30.0
_WAKE_JUMP_SECONDS = 30.0


@dataclass(frozen=True)
class Control:
    paused: bool = False
    generation: int = 0


def read_control(state_dir: Path) -> Control:
    data = read_json(state_dir / CONTROL_FILE) or {}
    paused = data.get("paused") is True
    generation = data.get("generation")
    return Control(paused, generation if isinstance(generation, int) else 0)


def write_control(state_dir: Path, *, paused: bool | None = None, reload: bool = False) -> Control:
    # Pause and reload can arrive from separate helper processes; neither may lose the other.
    with file_lock(state_dir / f".{CONTROL_FILE}.lock"):
        return _write_control_locked(state_dir, paused=paused, reload=reload)


def _write_control_locked(state_dir: Path, *, paused: bool | None, reload: bool) -> Control:
    current = read_control(state_dir)
    updated = Control(
        current.paused if paused is None else paused,
        current.generation + 1 if reload or paused is not None else current.generation,
    )
    write_json_atomic(
        state_dir / CONTROL_FILE,
        {
            "protocol": PROTOCOL_VERSION,
            "paused": updated.paused,
            "generation": updated.generation,
            "updated_at": time.time(),
        },
    )
    return updated


@contextmanager
def _jev(config: Config) -> Iterator[JevAssessor]:
    with runtime.make_assessor(config) as client:
        yield JevAssessor(client, model=config.policy.model)


def _single_attempt(config: Config, operation: Callable[[Any], Any]) -> Any:
    # The worker owns backoff so waits stay interruptible by pause, reload, and stop.
    return runtime.with_bridge(config, operation, attempts=1)


@dataclass
class WorkerDeps:
    load: Callable[[Path], Config] = load_config
    bridge: Callable[[Config, Callable[[Any], Any]], Any] = _single_attempt
    assessor: Callable[[Config], AbstractContextManager[Any]] = _jev
    cycle: Callable[..., CycleResult] = run_cycle
    monotonic: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    tick_seconds: float = _TICK_SECONDS


@dataclass
class _Snapshot:
    state: str = "starting"
    since: float = 0.0
    error: dict[str, object] | None = None
    last_cycle: dict[str, object] | None = None
    next_cycle_at: float | None = None
    consecutive_failures: int = 0
    lock_holder: int | None = None
    settings: dict[str, object] = field(default_factory=dict)


class Worker:
    def __init__(self, config_path: Path, state_dir: Path, deps: WorkerDeps | None = None) -> None:
        self.config_path = config_path.expanduser()
        self.state_dir = state_dir
        self.deps = deps or WorkerDeps()
        self.stop_event = threading.Event()
        self._snapshot = _Snapshot(since=self.deps.wall())
        self._last_heartbeat = 0.0
        self._failures = 0

    # Lifecycle -----------------------------------------------------------------------

    def request_stop(self) -> None:
        self.stop_event.set()

    def run(self) -> int:
        logger.info("Worker %s starting (pid %d).", __version__, os.getpid())
        self._publish("starting")
        while not self.stop_event.is_set():
            control = read_control(self.state_dir)
            if control.paused:
                self._publish("paused", error=None)
                self._wait(None, self._watch(control, self._config_mtime(), track_config=False))
                continue
            changed = self._watch(control, self._config_mtime())
            try:
                config = self.deps.load(self.config_path)
            except ConfigurationError as exc:
                self._fail("error", exc, changed, _ERROR_RETRY_SECONDS)
                continue
            self._snapshot.settings = {
                "move_enabled": config.policy.move_enabled,
                "gate_mode": config.gate.mode,
                "poll_interval_seconds": config.service.poll_interval_seconds,
            }
            try:
                with ProcessLock(config.database_path):
                    self._snapshot.lock_holder = None
                    failure = self._session(config, changed)
            except ConfigurationError as exc:
                self._fail("error", exc, changed, _ERROR_RETRY_SECONDS)
            except TriageError as exc:
                if exc.code != "busy":
                    self._fail("error", exc, changed, _ERROR_RETRY_SECONDS)
                    continue
                self._snapshot.lock_holder = ProcessLock.holder(config.database_path)
                self._fail("waiting_for_lock", exc, changed, _LOCK_RETRY_SECONDS)
            else:
                if failure is not None:
                    # Back off with the lock released so CLI commands can run meanwhile.
                    state, exc = failure
                    delay = min(5 * 2 ** (self._failures - 1), config.service.backoff_max_seconds)
                    self._fail(state, exc, changed, delay)
        self._publish("stopped")
        logger.info("Worker stopped.")
        return 0

    def _session(
        self, config: Config, changed: Callable[[], bool]
    ) -> tuple[str, TriageError] | None:
        """Run cycles until something changes; return a failed cycle's state and error."""
        store = StateStore(config.database_path)
        gate = runtime.make_gate(config, store)
        with self.deps.assessor(config) as assessor:
            while not changed():
                if gate is not None and training_due(config, store):
                    runtime.train_in_background(config, store, gate, self.deps.bridge, stop=changed)
                    if changed():
                        return None
                self._publish("running", error=None)

                def cycle(mailbox: Any) -> CycleResult:
                    return self.deps.cycle(
                        config, store, mailbox, assessor, gate=gate, stop=changed
                    )

                try:
                    result = self.deps.bridge(config, cycle)
                except RetryableError as exc:
                    return self._failed("bridge_unavailable", exc)
                except ConfigurationError:
                    raise
                except TriageError as exc:
                    return self._failed("error", exc)
                self._failures = 0
                self._snapshot.consecutive_failures = 0
                self._snapshot.last_cycle = {
                    "finished_at": self.deps.wall(),
                    "baseline": result.baseline,
                    "discovered": result.discovered,
                    "assessed": result.assessed,
                    "moved": result.moved,
                    "attention": result.attention,
                }
                logger.info(
                    "Cycle complete: baseline=%s discovered=%d assessed=%d moved=%d attention=%d",
                    result.baseline,
                    result.discovered,
                    result.assessed,
                    result.moved,
                    result.attention,
                )
                self._publish("idle", error=None)
                self._wait(config.service.poll_interval_seconds, changed)
        return None

    def _failed(self, state: str, exc: TriageError) -> tuple[str, TriageError]:
        self._failures += 1
        self._snapshot.consecutive_failures = self._failures
        return state, exc

    # Waiting and change detection ---------------------------------------------------

    def _config_mtime(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime_ns / 1e9
        except OSError:
            return None

    def _watch(
        self, control: Control, config_mtime: float | None, *, track_config: bool = True
    ) -> Callable[[], bool]:
        """A check for anything that should end the current session or wait."""

        def changed() -> bool:
            self._heartbeat()
            if self.stop_event.is_set() or read_control(self.state_dir) != control:
                return True
            return track_config and self._config_mtime() != config_mtime

        return changed

    def _wait(self, seconds: float | None, changed: Callable[[], bool]) -> None:
        start_mono = self.deps.monotonic()
        start_wall = self.deps.wall()
        self._snapshot.next_cycle_at = None if seconds is None else start_wall + seconds
        self._write_status()
        while not changed():
            elapsed = self.deps.monotonic() - start_mono
            if seconds is not None and elapsed >= seconds:
                break
            # After sleep/wake the wall clock jumps ahead; check mail promptly.
            if self.deps.wall() - start_wall - elapsed > _WAKE_JUMP_SECONDS:
                logger.info("Wake from sleep detected; checking mail now.")
                break
            self.stop_event.wait(self.deps.tick_seconds)
        self._snapshot.next_cycle_at = None

    def _fail(
        self, state: str, exc: TriageError, changed: Callable[[], bool], delay: float
    ) -> None:
        logger.warning("Worker %s: %s", state, exc)
        self._publish(state, error=exc)
        self._wait(delay, changed)

    # Status file -------------------------------------------------------------------

    def _publish(self, state: str, *, error: TriageError | None | bool = False) -> None:
        if state != self._snapshot.state:
            self._snapshot.state = state
            self._snapshot.since = self.deps.wall()
        if error is None:
            self._snapshot.error = None
        elif isinstance(error, TriageError):
            self._snapshot.error = {
                "code": error.code or _default_code(error),
                "message": str(error),
                "at": self.deps.wall(),
            }
        self._write_status()

    def _heartbeat(self) -> None:
        if self.deps.monotonic() - self._last_heartbeat >= _HEARTBEAT_SECONDS:
            self._write_status()

    def _write_status(self) -> None:
        self._last_heartbeat = self.deps.monotonic()
        snapshot = self._snapshot
        try:
            write_json_atomic(
                self.state_dir / STATUS_FILE,
                {
                    "protocol": PROTOCOL_VERSION,
                    "version": __version__,
                    "pid": os.getpid(),
                    "state": snapshot.state,
                    "since": snapshot.since,
                    "heartbeat": self.deps.wall(),
                    "error": snapshot.error,
                    "last_cycle": snapshot.last_cycle,
                    "next_cycle_at": snapshot.next_cycle_at,
                    "consecutive_failures": snapshot.consecutive_failures,
                    "lock_holder": snapshot.lock_holder,
                    "settings": snapshot.settings,
                },
            )
        except OSError as exc:
            logger.warning("Could not write worker status (%s).", type(exc).__name__)


def _default_code(error: TriageError) -> str:
    if isinstance(error, ConfigurationError):
        return "configuration"
    if isinstance(error, RetryableError):
        return "bridge_unavailable"
    return "triage_error"


def configure_file_logging(log_dir: Path = LOG_DIR) -> None:
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "worker.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)


def run_worker(config_path: Path, state_dir: Path) -> int:
    worker = Worker(config_path, state_dir)

    def on_signal(_signum: int, _frame: object) -> None:
        worker.request_stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    return worker.run()
