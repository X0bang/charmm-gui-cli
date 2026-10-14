"""Task locks and UTC timestamps, independent of pipeline orchestration."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from pathlib import Path

from .auth import ToolError


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def run_lock(directory):
    directory = Path(directory)
    if not directory.is_dir():
        raise ToolError("Run directory does not exist.")
    with (directory / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ToolError(key="run.lock_busy", category="lock_busy", retryable=True,
                            retry_after_seconds=10, next_step="charmm-gui-cli jobs list") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
