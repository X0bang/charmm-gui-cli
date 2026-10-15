"""Bounded read-only task discovery and explicit identity-checked relinking."""

import charmm_gui_cli.store.tasks as _task_store

import json
import os
from pathlib import Path
import re
import shlex

from .auth import ToolError, atomic_json
from .layout import task_root
from .paths import read_value


def task_id(directory):
    manifest = read_value(Path(directory) / "full-run.json")
    embedded = Path(directory) / "job.json"
    job = read_value(embedded) if embedded.is_file() else {}
    context = manifest.get("delivery_context", {}) if isinstance(manifest, dict) else None
    if not isinstance(job, dict) or not isinstance(context, dict):
        raise ToolError(key="jobs.identity_invalid", params={"path": str(task_root(directory))}, next_step="charmm-gui-cli jobs list")
    values = [value for value in (job.get("task_id"), context.get("task_id")) if value is not None]
    if not values:
        return None
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value) for value in values) or len(set(values)) != 1:
        raise ToolError(key="jobs.identity_invalid", params={"path": str(task_root(directory))}, next_step="charmm-gui-cli jobs list")
    return values[0]


def matches(record, directory):
    directory = Path(directory)
    try:
        manifest = read_value(directory / "full-run.json")
        if manifest.get("schema_version") != 2:
            return False
        expected = record.get("task_id")
        if expected is not None:
            return task_id(directory) == expected
        # Historical indexes have no UUID. Match their original embedded name,
        # directory and registration timestamp; never trust a name alone.
        job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        return (bool(record.get("created_at")) and job.get("name") == record.get("name")
                and job.get("created_at") == record["created_at"] and job.get("directory") == record.get("directory"))
    except (OSError, ValueError, KeyError, TypeError, ToolError):
        return False


def unavailable(record):
    name = record.get("name", "TASK")
    raise ToolError(key="jobs.directory_missing", params={"name": name, "path": record.get("directory", "")},
                    next_step="charmm-gui-cli jobs relink " + shlex.quote(str(name)) + " /path/to/task")


def discover(record):
    found, visited = [], 0
    def unreadable(error):
        raise ToolError(key="jobs.discovery_unreadable", params={"path": str(error.filename or Path.cwd())},
                        next_step="charmm-gui-cli jobs relink " + shlex.quote(str(record.get("name", "TASK"))) + " /path/to/task")
    for base, children, _ in os.walk(Path.cwd(), followlinks=False, onerror=unreadable):
        visited += 1
        if visited > 10000:
            raise ToolError(key="jobs.discovery_limit", next_step="charmm-gui-cli jobs relink " + shlex.quote(str(record.get("name", "TASK"))) + " /path/to/task")
        base = Path(base)
        children[:] = [name for name in children if not name.startswith(".") and name not in
                       ("model", "inputs", "node_modules", "__pycache__", "venv") and not (base / name).is_symlink()]
        private = base / ".charmm-gui"
        if private.is_dir() and not private.is_symlink() and matches(record, private):
            found.append(private.resolve())
            children[:] = []  # Nested results are not independent tasks.
    if len(found) > 1:
        raise ToolError(key="jobs.discovery_ambiguous", params={"name": record.get("name", "TASK"), "paths": ", ".join(str(task_root(p)) for p in found)},
                        next_step="charmm-gui-cli jobs relink " + shlex.quote(str(record.get("name", "TASK"))) + " /path/to/chosen/task")
    if not found:
        unavailable(record)
    return found[0]


def locate(record):
    directory = Path(record["directory"]).expanduser().resolve()
    from .quarantine import resolve_directory
    retained = resolve_directory(directory, record.get('task_id'))
    if retained is not None:
        return retained
    if (directory / "full-run.json").is_file():
        if record.get("task_id") is not None and not matches(record, directory):
            raise ToolError(key="jobs.identity_invalid", params={"path": str(task_root(directory))}, next_step="charmm-gui-cli jobs list")
        return directory
    if not record.get("task_id") and isinstance(record.get("name"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", record["name"]):
        for registry in _task_store._registries():
            name_record = registry / "names" / (record["name"] + ".json")
            if name_record.is_file():
                registered = _task_store._read_json(name_record)
                if registered.get("directory") == record.get("directory"):
                    record = {**record, **registered}
                break
    return discover(record)


def relink(name, target, *, _lease=None):
    from charmm_gui_cli.store.workers import metadata
    from charmm_gui_cli.store.locking import run_lock
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise ToolError(key="input.task_name", next_step="charmm-gui-cli jobs relink -h")
    records = [registry / "names" / (str(name) + ".json") for registry in _task_store._registries()]
    target = Path(target).expanduser().resolve()
    directory = target / ".charmm-gui" if (target / ".charmm-gui/full-run.json").is_file() else target
    def registration():
        source = next((path for path in records if path.is_file()), None)
        if source is not None:
            return _task_store._read_json(source)
        from .lifecycle import forgotten_binding
        record = forgotten_binding(name, directory)
        if record is None:
            raise ToolError(key="jobs.unknown", params={"name": name}, next_step="charmm-gui-cli jobs list")
        return record
    registration()
    names = _task_store._registry() / "names"
    names.mkdir(mode=0o700, parents=True, exist_ok=True)
    with run_lock(names), run_lock(directory):
        record = registration()
        if _lease is not None:
            from .lifecycle import _borrowed_lease
            _borrowed_lease(directory, _lease)
        elif metadata(directory)["running"]:
            raise ToolError(key="background.running", next_step="charmm-gui-cli jobs list")
        if not matches(record, directory):
            raise ToolError(key="jobs.relink_mismatch", params={"name": name, "path": str(task_root(directory))}, next_step="charmm-gui-cli jobs list")
        old = Path(record["directory"]).resolve()
        if old != directory and (old / "full-run.json").is_file():
            raise ToolError(key="jobs.original_present", params={"path": str(task_root(old))}, next_step="charmm-gui-cli jobs status " + shlex.quote(str(name)))
        updated = {**record, "directory": str(directory)}
        identifier = task_id(directory)
        if identifier:
            updated["task_id"] = identifier
        atomic_json(names / (str(name) + ".json"), updated)
        _task_store._publish_run(_task_store._registry(), _task_store._run_record(directory))
    return {"name": name, "directory": str(task_root(directory)), "task_id": identifier,
            "state": "relinked", "next_step": "charmm-gui-cli jobs status " + shlex.quote(str(name))}


def source_directory(certificate):
    """Resolve a bound source by its UUID; never substitute an unrelated run."""
    directory = Path(certificate["source_directory"])
    expected = certificate.get("source_task_id")
    from .quarantine import resolve_directory
    retained = resolve_directory(directory, expected)
    if retained is not None:
        return retained
    if (directory / "full-run.json").is_file():
        if expected is not None and task_id(directory) != expected:
            raise ToolError(key="jobs.identity_invalid", params={"path": str(task_root(directory))}, next_step="charmm-gui-cli jobs list")
        return directory.resolve()
    for registry in _task_store._registries():
        for path in sorted((registry / "names").glob("*.json")):
            record = _task_store._read_json(path)
            if (expected is not None and record.get("task_id") == expected or expected is None and record.get("directory") == str(directory)):
                return locate(record)
    if expected:
        return discover({"directory": str(directory), "task_id": expected, "name": expected})
    unavailable({"directory": str(directory), "name": directory.parent.name})
