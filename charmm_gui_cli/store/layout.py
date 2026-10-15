"""Paths for single-directory tasks, with read compatibility for older runs."""

from pathlib import Path

INTERNAL = ".charmm-gui"


def task_path(output):
    """Translate a user output directory to its private working directory."""
    root = Path(output).expanduser().resolve()
    return root if root.name == INTERNAL else root / INTERNAL


def task_root(directory):
    directory = Path(directory).resolve()
    return directory.parent if directory.name == INTERNAL else directory


def modern(directory):
    return Path(directory).name == INTERNAL


def inputs_path(directory):
    return task_root(directory) / "inputs"


def log_path(directory):
    return task_root(directory) / "run.log"


def previews_path(directory):
    """Visible images for single-directory tasks; preserve legacy run layout."""
    directory = Path(directory).resolve()
    return task_root(directory) / "previews" if modern(directory) else directory / "results" / "previews"


def archive_path(directory):
    directory = Path(directory).resolve()
    if directory.name == "bilayer" and directory.parent.name == INTERNAL:
        return directory.parent.parent / "charmm-gui.tgz"
    if modern(directory):
        return directory.parent / "charmm-gui.tgz"
    return directory / "charmm-gui.tgz"


def full_archive_path(directory):
    return archive_path(Path(directory) / "bilayer")
