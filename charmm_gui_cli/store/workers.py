"""Process-lock metadata independent of launch, networking and pipeline state."""

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from .auth import ToolError
from .paths import read_value
from .layout import log_path


def claim(directory):
    path = Path(directory) / "run.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise ToolError(key="background.running", next_step="charmm-gui-cli jobs list") from None
    os.fchmod(fd, 0o600)
    return fd


def metadata(directory):
    path = Path(directory) / "run.lock"
    try:
        value = read_value(path)
        if not isinstance(value, dict):
            value = {}
    except (OSError, ValueError):
        value = {}
    running = False
    if path.exists():
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                running = True
            finally:
                os.close(fd)
        except OSError:
            pass
    value["running"] = running
    try:
        start = datetime.fromisoformat(value["started_at"])
        end = datetime.fromisoformat(value["finished_at"]) if not running and value.get("finished_at") else datetime.now(timezone.utc)
        value["elapsed_seconds"] = max(0, int((end - start).total_seconds()))
    except (KeyError, TypeError, ValueError):
        value["elapsed_seconds"] = 0
    value["log"] = str(log_path(directory))
    return value


def write_lock(fd, value):
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(value).encode())
    os.fsync(fd)
