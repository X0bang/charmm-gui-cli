"""Task indexes, identities and durable records; no build orchestration."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from .auth import ToolError, atomic_json, default_token_path
from .paths import read_value, encode
from .layout import task_root
from .locking import run_lock


def _read_json(path):
    try:
        value = read_value(path)
    except (OSError, ValueError):
        raise ToolError(f"Cannot read local run metadata: {Path(path).name}.") from None
    if not isinstance(value, dict):
        raise ToolError("Local run metadata must contain a JSON object.")
    return value


def _new_json(path, value):
    """Publish a private JSON file atomically, without replacing another file."""
    path = Path(path)
    value = encode(path, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".managed-", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(name, path)
    except FileExistsError:
        raise ToolError(f"Managed output already exists; refusing to overwrite {path.name}.") from None
    finally:
        Path(name).unlink(missing_ok=True)


def _registry():
    # Task discovery is data, not authentication material.
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "charmm-gui-cli/index"


def _registries():
    return (_registry(), default_token_path().parent)


def validate_name(name):
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
            or name.lower().endswith((".pdb", ".sdf", ".yaml", ".yml", ".mol2", ".cif"))):
        raise ToolError(key="input.task_name", next_step="charmm-gui-cli build -h")
    return name


def check_name_available(name, directory=None):
    validate_name(name)
    path = next((registry / "names" / (name + ".json") for registry in _registries()
                 if (registry / "names" / (name + ".json")).exists()), _registry() / "names" / (name + ".json"))
    if path.exists():
        stored = _read_json(path)
        if not isinstance(stored.get("directory"), str) or not stored["directory"]:
            raise ToolError("Registered job name has unreadable directory metadata; inspect the local name record.")
        if directory is None or Path(stored.get("directory", "")).resolve() != Path(directory).resolve():
            raise ToolError(f"Job name {name} is already registered. Use jobs resume {name}, or choose a new name.")


def assign_name(directory, name):
    """Bind one unique human-readable name to an existing full build."""
    directory = Path(directory).expanduser().resolve()
    validate_name(name)
    manifest = _read_json(directory / "full-run.json")
    if manifest.get("schema_version") != 2:
        raise ToolError("Only initialized full builds can be named.")
    from charmm_gui_cli.store.indexing import writes_allowed
    if not writes_allowed():
        # A dry-run may retain a local identity for later launch, but must not
        # mutate global names/history/latest-run or even create the index.
        with run_lock(directory):
            existing = directory / "job.json"
            if existing.exists():
                record = _read_json(existing)
                if record.get("name") != name:
                    raise ToolError("This job already has a different name; existing names are retained.")
                return record
            from charmm_gui_cli.store.discovery import task_id
            record = {"name": name, "directory": str(directory), "task_id": task_id(directory) or uuid.uuid4().hex,
                      "created_at": datetime.now(timezone.utc).isoformat()}
            _new_json(existing, record)
            return record
    names = _registry() / "names"
    names.mkdir(mode=0o700, parents=True, exist_ok=True)
    with run_lock(names), run_lock(directory):
        check_name_available(name, directory)
        existing = directory / "job.json"
        if existing.exists() and _read_json(existing).get("name") != name:
            raise ToolError("This job already has a different name; existing names are retained.")
        from charmm_gui_cli.store.discovery import task_id
        identifier = task_id(directory) or uuid.uuid4().hex
        record = {"name": name, "directory": str(directory), "task_id": identifier,
                  "created_at": datetime.now(timezone.utc).isoformat()}
        if not existing.exists():
            _new_json(existing, record)
        else:
            record = {**_read_json(existing), "directory": str(directory), "task_id": identifier}
            if "task_id" not in _read_json(existing):
                atomic_json(existing, record)
        destination = names / (name + ".json")
        if not destination.exists():
            _new_json(destination, record)
    remember_run(directory)
    return record


def resolve_run(target):
    """Accept a named job or an explicit directory, without creating anything."""
    if target is None:
        return latest_directory()
    path = Path(target).expanduser()
    explicit = path.is_absolute() or "/" in str(target) or str(target).startswith("~")
    if explicit:
        path = path.resolve()
        return path / ".charmm-gui" if (path / ".charmm-gui/full-run.json").is_file() else path
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(target)):
        raise ToolError(key="input.task_name")
    record_path = next((registry / "names" / (str(target) + ".json") for registry in _registries()
                        if (registry / "names" / (str(target) + ".json")).is_file()), _registry() / "names" / (str(target) + ".json"))
    if not record_path.is_file():
        if (path / ".charmm-gui/full-run.json").is_file():
            return (path / ".charmm-gui").resolve()
        if (path / "full-run.json").is_file():
            return path.resolve()
        raise ToolError(key="jobs.unknown", params={"name": target}, next_step="charmm-gui-cli jobs list")
    record = _read_json(record_path)
    if not isinstance(record.get("directory"), str) or not record["directory"]:
        raise ToolError("Named job has unreadable directory metadata; inspect its local name record.")
    from charmm_gui_cli.store.discovery import locate
    return locate(record)


def _job_metadata(directory):
    path = directory / "job.json"
    try:
        return {"name": _read_json(path)["name"]} if path.is_file() else {}
    except (ToolError, KeyError):
        return {}


def record_recovery(directory, error=None, *, state=None):
    """Retain each recovery event and publish the current actionable status."""
    directory = Path(directory).resolve()
    event = {"updated_at": datetime.now(timezone.utc).isoformat(), "active": error is not None}
    if state is not None:
        event["state"] = state
    if error is not None:
        if hasattr(error, "for_task"):
            error.for_task(_job_metadata(directory).get("name") or str(task_root(directory)))
        event["error"] = error.as_dict() if hasattr(error, "as_dict") else {
            "category": "operation_error", "message": str(error), "retryable": False}
        category = event["error"]["category"]
        manifest_path = directory / "full-run.json"
        manifest = _read_json(manifest_path)
        event["failed_stage"] = manifest.get("state", "unknown")
        ambiguous = category == "submission_unknown" or manifest.get("state") == "submission_unknown"
        event["execution_state"] = ("submission_unknown" if ambiguous else
                                    "interrupted" if category == "interrupted" else
                                    "failed_remote" if category in ("api_error", "http_error", "remote_error", "preparation_failed") or manifest.get("state") in ("remote_error", "preparation_failed") else
                                    "failed_local")
        name = _job_metadata(directory).get("name")
        resume = f"charmm-gui-cli jobs resume {name}" if name else f"charmm-gui-cli build-resume {directory}"
        if category in ("authentication", "auth_required"):
            event["next_step"] = "charmm-gui-cli login; " + resume
        elif category == "submission_unknown":
            event["next_step"] = event["error"].get("next_step") or "Check the existing remote job; use jobs attach with its cloned build ID when available."
        elif event["error"].get("retryable"):
            event["next_step"] = resume
        else:
            event["next_step"] = event["error"].get("next_step") or resume
    with run_lock(directory):
        _new_json(directory / "recovery-history" / (uuid.uuid4().hex + ".json"), event)
        atomic_json(directory / "recovery.json", event)
        # Keep the pipeline stage intact for safe recovery; execution outcome is
        # independent and durable rather than inventing a completed remote step.
        manifest = _read_json(directory / "full-run.json")
        manifest["execution_state"] = event.get("execution_state", state)
        if error is not None:
            manifest["failure"] = event
        else:
            manifest.pop("failure", None)
        atomic_json(directory / "full-run.json", manifest)
    return event


def _recovery_summary(directory):
    path = directory / "recovery.json"
    if path.is_file():
        try:
            event = _read_json(path)
            if event.get("active"):
                return {"recovery": event, "next_step": event.get("next_step"),
                        "execution_state": event.get("execution_state"), "failed_stage": event.get("failed_stage")}
        except ToolError:
            pass
    return {}


def new_run_path(name=None):
    """Return an unused default run path; create only its parent directory."""
    parent = Path.cwd()
    if name:
        return (parent / validate_name(name) / ".charmm-gui").resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for _ in range(10):
        directory = parent / f"{stamp}-{uuid.uuid4().hex[:12]}"
        if not directory.exists():
            return (directory / ".charmm-gui").resolve()
    raise ToolError("Could not allocate a new managed run path.")


def new_batch_path(name=None, *, kind="batch", source=None):
    if name:
        return (Path.cwd() / validate_name(name)).resolve()
    label = re.sub(r"[^A-Za-z0-9_.-]", "-", Path(source).stem if source else kind).strip(".-")[:40] or kind
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (Path.cwd() / f"{kind}-{label}-{stamp}-{uuid.uuid4().hex[:6]}").resolve()


def remember_run(directory):
    """Remember a successfully initialized run, without copying credentials."""
    directory = Path(directory).expanduser().resolve()
    manifest = _read_json(directory / "full-run.json")
    if manifest.get("schema_version") != 2:
        raise ToolError("Only full builds can be remembered.")
    record = {"directory": str(directory), "remembered_at": datetime.now(timezone.utc).isoformat(),
              "state": manifest.get("state", "unknown"), **_job_metadata(directory)}
    from charmm_gui_cli.store.discovery import task_id
    identifier = task_id(directory)
    if identifier:
        record["task_id"] = identifier
    from charmm_gui_cli.store.indexing import writes_allowed
    if not writes_allowed():
        return record
    registry = _registry()
    _new_json(registry / "run-history" / f"{uuid.uuid4().hex}.json", record)
    atomic_json(registry / "latest-run.json", record)
    return record


def _bilayer_summary(directory):
    """Use the API poller's durable local status while full-build waits."""
    path = directory / "bilayer" / "run.json"
    if not path.is_file():
        return {}
    try:
        manifest = _read_json(path)
    except ToolError:
        return {}
    result = {}
    for key in ("state", "remote_status", "last_output_file"):
        value = manifest.get(key)
        if isinstance(value, str):
            result[key] = value
    if isinstance(manifest.get("job_id"), str):
        result["build_job_id"] = manifest["job_id"]
    if isinstance(manifest.get("last_error"), dict):
        result["last_error"] = manifest["last_error"]
    return result


def list_records():
    """Return newest readable runs; stale/moved records are not usable runs."""
    records = []
    files = []
    for registry in _registries():
        files.extend((registry / "run-history").glob("*.json"))
        if (registry / "latest-run.json").is_file():
            files.append(registry / "latest-run.json")
    for path in files:
        try:
            record = _read_json(path)
            from charmm_gui_cli.store.discovery import locate
            directory = locate(record)
            manifest = _read_json(directory / "full-run.json")
        except (ToolError, KeyError, TypeError):
            # A history entry can outlive a mounted/moved directory. It does
            # not prevent resuming another readable initialized run.
            continue
        if manifest.get("schema_version") == 2:
            records.append({"directory": str(directory), "remembered_at": record.get("remembered_at", ""),
                            "state": manifest.get("state", "unknown"),
                            **{key: manifest[key] for key in ("execution_state", "last_error") if key in manifest}})
    records.sort(key=lambda item: item["remembered_at"], reverse=True)
    unique, seen = [], set()
    for record in records:
        if record["directory"] not in seen:
            unique.append(record)
            seen.add(record["directory"])
    return unique


def latest_directory():
    records = list_records()
    if not records:
        raise ToolError("No readable recent build is recorded. Start a build or supply its run directory.")
    return Path(records[0]["directory"])
