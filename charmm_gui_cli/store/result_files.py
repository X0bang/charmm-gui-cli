"""Owned result files, unchanged model checks and presentation-file protection."""

import charmm_gui_cli.store.tasks as _task_store

import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import uuid
from .auth import ToolError, atomic_json, atomic_write
from .layout import modern, task_root

MARKER = ".charmm-gui-delivery.json"


def creation_context():
    return {"task_id": uuid.uuid4().hex, "creation_directory": str(Path.cwd().resolve())}


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _error(key, target):
    return ToolError(key=key, params={"path": str(target)}, category="delivery_error",
                     next_step="charmm-gui-cli jobs export NAME --to NEW_DIRECTORY")


def _tree(source, prefix):
    if source.is_symlink() or not source.is_dir():
        raise _error("delivery.unsafe", source)
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise _error("delivery.unsafe", path)
        if path.is_file():
            files[str(Path(prefix) / path.relative_to(source))] = path
    return files


def check_destination(directory):
    """Reject a foreign default destination before submitting a new build."""
    directory = Path(directory).resolve()
    if modern(directory):
        return  # Root was exclusively reserved before any remote submission.
    manifest = _task_store._read_json(directory / "full-run.json")
    context = manifest.get("delivery_context", {})
    if not context.get("creation_directory"):
        return
    name = _task_store._job_metadata(directory).get("name", directory.name)
    target = Path(context["creation_directory"]) / _task_store.validate_name(name)
    if target.is_symlink():
        raise _error("delivery.foreign", target)
    if target.exists():
        marker = target / MARKER
        try:
            record = json.loads(marker.read_text()) if marker.is_file() and not marker.is_symlink() else {}
        except (OSError, ValueError):
            record = {}
        if not isinstance(record, dict) or record.get("task_id") != context["task_id"] or record.get("run_directory") != str(directory):
            raise _error("delivery.foreign", target)


def verify_model(archive, model):
    """Check the one extracted model without creating a second copy."""
    files = _tree(Path(model), "model")
    observed = {}
    try:
        with tarfile.open(archive, "r|gz") as handle:
            for member in handle:
                if member.isfile():
                    with handle.extractfile(member) as source:
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                    observed[str(Path(member.name))] = digest.hexdigest()
    except (tarfile.TarError, EOFError):
        raise ToolError(key="delivery.archive_invalid", next_step="charmm-gui-cli jobs status NAME") from None
    if observed != {name[6:]: _digest(path) for name, path in files.items()}:
        raise ToolError(key="delivery.model_changed", next_step="charmm-gui-cli jobs status NAME")


def _system_configuration(directory, manifest):
    """Record actual submitted patches, never global requests as applied facts."""
    requested = manifest["config"]
    applied = json.loads(json.dumps(requested))
    adjustments = manifest.get("terminal_adjustments", [])
    applied["terminal_adjustments"] = adjustments
    preparation = applied.setdefault("preparation", {})
    preparation.pop("n_terminal", None)
    preparation.pop("c_terminal", None)
    terminals = dict(manifest.get("applied_terminals", {}))
    intent = directory / "preparation/parameterization.intent.json"
    if intent.is_file() and not intent.is_symlink():
        submitted = _task_store._read_json(intent)
        if submitted.get("state") == "response_received":
            terminals = {key: value for key, value in submitted.get("fields", []) if key.startswith("terminal[")}
    certificate = manifest.get("reused_preparation", {}).get("certificate", {})
    if not terminals and certificate:
        from charmm_gui_cli.store.discovery import source_directory
        try:
            source = source_directory(certificate)
        except ToolError:
            source = Path(certificate.get("source_directory", ""))
        intent = source / "preparation/parameterization.intent.json"
        expected = certificate.get("evidence_sha256", {}).get("preparation/parameterization.intent.json")
        if expected and intent.is_file() and not intent.is_symlink() and _digest(intent) == expected:
            source = _task_store._read_json(intent)
            if source.get("state") == "response_received":
                terminals = {key: value for key, value in source.get("fields", []) if key.startswith("terminal[")}
    preparation["terminals"] = terminals
    for entry in adjustments:
        params = entry.get("params", {})
        if params.get("chain") and params.get("patch"):
            terminals.setdefault("terminal[" + params["chain"] + "][first]", params["patch"])
    return {"requested": requested, "applied": applied}


def _publish_generated(directory, generated):
    """Preserve edited display documents; never overwrite edited input settings."""
    root = task_root(directory)
    marker = directory / "generated-files.json"
    if marker.is_symlink():
        raise _error("delivery.unsafe", marker)
    previous = _task_store._read_json(marker) if marker.is_file() else {}
    for section in ("files", "pending"):
        values = previous.get(section, {})
        if not isinstance(values, dict) or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in values.values()):
            raise _error("delivery.unsafe", marker)
    expected = {name: hashlib.sha256(content.encode("utf-8")).hexdigest() for name, content in generated.items()}
    edited = []
    for name in generated:
        path = root / name
        accepted = {expected[name], previous.get("files", {}).get(name), previous.get("pending", {}).get(name)}
        if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root)
                or path.exists() and not path.is_file()):
            raise _error("delivery.modified", path)
        if path.exists() and _digest(path) not in accepted:
            if name not in ("report.txt", "README.txt"):
                raise _error("delivery.modified", path)
            edited.append(path)
    for path in edited:
        suffix = 0
        while True:
            backup = path.with_name(path.name + ".user-edited" + (f".{suffix}" if suffix else ""))
            try:
                os.link(path, backup, follow_symlinks=False)
                break
            except FileExistsError:
                suffix += 1
    atomic_json(marker, {"files": previous.get("files", {}), "pending": expected})
    for name, content in generated.items():
        atomic_write(root / name, content)
    atomic_json(marker, {"files": expected})
