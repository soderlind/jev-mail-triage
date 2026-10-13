from pathlib import Path

import pytest

from jev_mail_triage.errors import TriageError
from jev_mail_triage.lock import ProcessLock


def test_process_lock_rejects_a_second_worker(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with (
        ProcessLock(database),
        pytest.raises(TriageError, match="already running"),
        ProcessLock(database),
    ):
        raise AssertionError("A second worker unexpectedly acquired the lock.")
