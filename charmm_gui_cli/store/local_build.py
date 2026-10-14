"""Exclusive local initialization; failed attempts cannot occupy a task name."""

from contextlib import contextmanager
from pathlib import Path
import json
import shutil

from charmm_gui_cli.store.auth import ToolError
from .layout import modern, task_root


def _remote_evidence(directory):
    """Even concurrent submissions must survive a local initialization failure."""
    try:
        if any(directory.rglob("*.intent.json")):
            return True
        for path in (directory / "full-run.json", directory / "bilayer/run.json"):
            if path.is_symlink():
                return True
            if path.is_file():
                record = json.loads(path.read_text())
                if not isinstance(record, dict) or any(record.get(key) for key in ("source_job_id", "build_job_id", "job_id")):
                    return True
                if record.get("state") in ("submitting", "submission_unknown"):
                    return True
    except (OSError, ValueError):
        return True  # Unreadable evidence is never proof that nothing submitted.
    return False


@contextmanager
def local_initialization(directory):
    """Reserve a new directory and roll back only this attempt's local files.

    This context must never enclose remote requests. Existing roots are never
    removed, including foreign roots and initialized/resumable tasks.
    """
    directory = Path(directory).resolve()
    root = task_root(directory)
    if directory.exists() or (modern(directory) and root.exists()):
        raise ToolError(key="build.directory_exists", next_step="charmm-gui-cli jobs list")
    owned = root if modern(directory) else directory
    owned.mkdir(mode=0o700, parents=True, exist_ok=False)
    identity = owned.stat().st_ino
    try:
        if modern(directory):
            directory.mkdir(mode=0o700)
            from .auth import atomic_json
            atomic_json(directory / "path-mode.json", {"version": 1})
        yield directory
    except BaseException:
        # A rename/replacement by another process is not ours to remove.
        if (modern(directory) and owned.is_dir() and not owned.is_symlink()
                and owned.stat().st_ino == identity and not _remote_evidence(directory)):
            shutil.rmtree(owned)
        raise
