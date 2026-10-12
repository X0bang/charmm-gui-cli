"""Independent user deliverables; authoritative run evidence stays private."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import tarfile
import uuid

from .auth import ToolError, atomic_json, atomic_write
from .workflow import run_lock, now

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
    from . import managed
    directory = Path(directory).resolve()
    manifest = managed._read_json(directory / "full-run.json")
    context = manifest.get("delivery_context", {})
    if not context.get("creation_directory"):
        return
    name = managed._job_metadata(directory).get("name", directory.name)
    target = Path(context["creation_directory"]) / managed.validate_name(name)
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


def export_run(directory, *, to=None):
    """Copy a completed managed run, never using hard links to user artifacts.

    A marker binds a destination to one run. Refreshes replace only unchanged,
    previously exported files; foreign directories and edited files are refused.
    Failed partial exports are retained and can be retried at a new destination.
    """
    from .background import metadata
    directory = Path(directory).expanduser().resolve()
    if metadata(directory)["running"]:
        raise ToolError(key="background.running", next_step="charmm-gui-cli jobs list")
    return _export(directory, to=to)


def auto_export(directory):
    # Called by the owning worker, which legitimately holds run.lock.
    return _export(Path(directory).resolve())


def _export(directory, *, to=None):
    from . import managed
    from .i18n import t
    with run_lock(directory):
        manifest = managed._read_json(directory / "full-run.json")
        report_path = directory / "results" / "validation.json"
        report = managed._read_json(report_path) if report_path.is_file() else {}
        if report.get("validation_complete") is not True or report.get("status") not in ("validated", "validation_failed"):
            raise ToolError(key="delivery.incomplete", next_step="charmm-gui-cli jobs status NAME")
        # Verify the internal archive/input binding, not a mutable export copy.
        if managed._final_summary(directory).get("state") not in ("validated", "validation_failed"):
            raise ToolError(key="cli.stale_report")
        context = manifest.get("delivery_context")
        if not isinstance(context, dict) or not context.get("task_id"):
            context = creation_context()  # Old runs: explicit export uses today's directory.
            manifest["delivery_context"] = context
            atomic_json(directory / "full-run.json", manifest)
        if not isinstance(context.get("creation_directory"), str) or not Path(context["creation_directory"]).is_absolute():
            raise _error("delivery.unsafe", directory / "full-run.json")
        name = managed._job_metadata(directory).get("name", directory.name)
        managed.validate_name(name)
        requested = Path(to).expanduser() if to is not None else Path(context["creation_directory"]) / name
        if requested.is_symlink():
            raise _error("delivery.foreign", requested)
        target = requested.resolve()
        # No copying into the source, its parents, or any of its descendants.
        if target == directory or target in directory.parents or directory in target.parents:
            raise _error("delivery.unsafe", target)
        from .cli import report_summary
        from .reporting import display_result, render_text
        text_report = directory / "results" / "delivery-report.txt"
        atomic_write(text_report, render_text(display_result(report_summary(report, name, "reports/validation.json"),
                     {**managed._final_summary(directory), "name": name, "archive": "charmm-gui.tgz",
                      "source_job_id": manifest.get("source_job_id"), "build_job_id": manifest.get("build_job_id")}, manifest, report)))
        config_path = directory / "results" / "delivery-config.json"
        atomic_json(config_path, manifest["config"])
        guide_path = directory / "results" / "delivery-readme.txt"
        atomic_write(guide_path, t("delivery.layout", name=name) + "\n")
        files = {"charmm-gui.tgz": directory / "bilayer" / "charmm-gui.tgz",
                 "README.txt": guide_path, "reports/validation.txt": text_report,
                 "inputs/system-config.json": config_path,
                 "reports/validation.json": report_path}
        extracted = Path(report["extracted_directory"])
        if not extracted.resolve().is_relative_to(directory):
            raise _error("delivery.unsafe", extracted)
        files.update(_tree(Path(report["extracted_directory"]), "model"))
        files.update(_tree(directory / "inputs", "inputs"))
        compiled = report.get("checks", {}).get("grompp", {}).get("output_dir")
        if compiled and Path(compiled).resolve().is_relative_to(directory) and Path(compiled).is_dir():
            files.update(_tree(Path(compiled), "reports/grompp"))
        for path in files.values():
            if path.is_symlink() or not path.is_file():
                raise _error("delivery.unsafe", path)
        hashes = {name: _digest(path) for name, path in files.items()}
        # The cached validation fingerprint binds the archive, not mutable
        # extracted files. Ensure the exported model still matches that archive.
        model_hashes = {name[6:]: digest for name, digest in hashes.items() if name.startswith("model/")}
        observed = {}
        with tarfile.open(files["charmm-gui.tgz"], "r|gz") as archive:
            for member in archive:
                if member.isfile():
                    digest = hashlib.sha256()
                    with archive.extractfile(member) as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                    observed[str(Path(member.name))] = digest.hexdigest()
        if observed != model_hashes:
            raise ToolError(key="delivery.model_changed", next_step="charmm-gui-cli jobs status NAME")
        fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        marker_path = target / MARKER
        previous = {}
        if target.exists():
            if not target.is_dir() or marker_path.is_symlink() or not marker_path.is_file():
                raise _error("delivery.foreign", target)
            try:
                previous = json.loads(marker_path.read_text())
            except (OSError, ValueError):
                raise _error("delivery.foreign", target) from None
            if not isinstance(previous, dict) or previous.get("task_id") != context["task_id"] or previous.get("run_directory") != str(directory):
                raise _error("delivery.foreign", target)
            if previous.get("complete") is not True:
                raise _error("delivery.partial", target)
            if not isinstance(previous.get("files"), dict):
                raise _error("delivery.foreign", target)
            # Do not overwrite files the user changed, even in an owned export.
            for name, digest in previous.get("files", {}).items():
                path = target / name
                if (Path(name).is_absolute() or ".." in Path(name).parts or
                        any(parent.is_symlink() for parent in path.parents if parent != target) or
                        path.is_symlink() or not path.is_file() or _digest(path) != digest):
                    raise _error("delivery.modified", target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.mkdir(mode=0o700)
            except FileExistsError:
                raise _error("delivery.foreign", target) from None
        owned = previous.get("files", {})
        # Check every destination before changing any file or marker.
        for name in files:
            path = target / name
            if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != target)
                    or (path.exists() and name not in owned)):
                raise _error("delivery.modified", target)
        marker = {"version": 1, "task_id": context["task_id"], "run_directory": str(directory),
                  "complete": False, "files": hashes, "fingerprint": fingerprint}
        if previous.get("fingerprint") != fingerprint:
            atomic_json(marker_path, marker)
            for name, source in files.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if owned.get(name) == hashes[name]:
                    continue
                fd, temporary = tempfile.mkstemp(prefix=".delivery-", dir=path.parent)
                with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if _digest(Path(temporary)) != hashes[name]:
                    raise _error("delivery.changed_source", source)
                os.replace(temporary, path)
            marker.update(complete=True, exported_at=now())
            atomic_json(marker_path, marker)
        result = {"directory": str(target), "archive": str(target / "charmm-gui.tgz"),
                  "model": str(target / "model"), "report": str(target / "reports/validation.json"),
                  "inputs": str(target / "inputs"), "status": report["status"], "passed": report["passed"]}
        atomic_json(directory / "delivery.json", result)
        from .archive_transport import cleanup_download_duplicates
        cleanup_download_duplicates(directory / "bilayer" / "charmm-gui.tgz")
        return result
