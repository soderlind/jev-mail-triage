from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

from .errors import TriageError


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for a short read-modify-write across processes.

    Unlike ``ProcessLock`` this waits for the lock instead of failing.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


class ProcessLock:
    def __init__(self, database_path: Path) -> None:
        self.path = database_path.with_suffix(database_path.suffix + ".lock")
        self._file = None

    def __enter__(self) -> Self:
        descriptor: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(descriptor, 0o600)
            self._file = os.fdopen(descriptor, "r+")
            descriptor = None
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise TriageError("Could not create the process lock file.") from exc
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            self._file = None
            raise TriageError(
                "Another jev-mail-triage process is already running.", code="busy"
            ) from exc
        # Record the owner so other processes can explain who holds the lock.
        self._file.seek(0)
        self._file.truncate()
        self._file.write(f"{os.getpid()}\n")
        self._file.flush()
        return self

    @staticmethod
    def holder(database_path: Path) -> int | None:
        """PID recorded by the current or last lock owner, if any; not proof it is alive."""
        path = database_path.with_suffix(database_path.suffix + ".lock")
        try:
            text = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return int(text) if text.isdigit() else None

    @staticmethod
    def is_held(database_path: Path) -> bool:
        path = database_path.with_suffix(database_path.suffix + ".lock")
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
