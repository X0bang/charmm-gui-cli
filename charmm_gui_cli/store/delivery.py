"""In-place task results and explicitly requested independent exports."""

from ..layout import modern, task_root, inputs_path, log_path, full_archive_path

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import tarfile
import uuid

from ..auth import ToolError, atomic_json, atomic_write
from ..workflow import run_lock, now

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
    from .. import managed
    directory = Path(directory).resolve()
    if modern(directory):
        return  # Root was exclusively reserved before any remote submission.
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
    from ..background import metadata
    directory = Path(directory).expanduser().resolve()
    if metadata(directory)["running"]:
        raise ToolError(key="background.running", next_step="charmm-gui-cli jobs list")
    return _export(directory, to=to)


def auto_export(directory):
    # Called by the owning worker, which legitimately holds run.lock.
    if modern(directory):
        return publish_in_place(directory)
    return _export(Path(directory).resolve())


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
    from .. import managed
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
        submitted = managed._read_json(intent)
        if submitted.get("state") == "response_received":
            terminals = {key: value for key, value in submitted.get("fields", []) if key.startswith("terminal[")}
    certificate = manifest.get("reused_preparation", {}).get("certificate", {})
    if not terminals and certificate:
        intent = Path(certificate.get("source_directory", "")) / "preparation/parameterization.intent.json"
        expected = certificate.get("evidence_sha256", {}).get("preparation/parameterization.intent.json")
        if expected and intent.is_file() and not intent.is_symlink() and _digest(intent) == expected:
            source = managed._read_json(intent)
            if source.get("state") == "response_received":
                terminals = {key: value for key, value in source.get("fields", []) if key.startswith("terminal[")}
    preparation["terminals"] = terminals
    for entry in adjustments:
        params = entry.get("params", {})
        if params.get("chain") and params.get("patch"):
            terminals.setdefault("terminal[" + params["chain"] + "][first]", params["patch"])
    return {"requested": requested, "applied": applied}


def publish_in_place(directory):
    """Generate small summaries; archive, model and inputs stay in place."""
    from .. import managed
    from ..cli import report_summary
    from ..reporting import display_result, render_text
    from ..i18n import t
    directory = Path(directory).resolve()
    with run_lock(directory):
        root = task_root(directory)
        manifest = managed._read_json(directory / "full-run.json")
        report_path = directory / "results/validation.json"
        report = managed._read_json(report_path)
        _verify_report(directory, manifest, report)
        verify_model(full_archive_path(directory), root / "model")
        name = managed._job_metadata(directory).get("name", root.name)
        report_text = render_text(display_result(
            report_summary(report, name, report_path), {"name": name, "archive": str(full_archive_path(directory))},
            manifest, report))
        generated = {"report.txt": report_text,
                     "inputs/system-config.json": json.dumps(_system_configuration(directory, manifest), indent=2, ensure_ascii=False) + "\n",
                     "README.txt": t("delivery.single_layout", name=name) + "\n"}
        _publish_generated(directory, generated)
        log_path(directory).touch(mode=0o600, exist_ok=True)
        from ..archive_transport import cleanup_download_duplicates
        result = {"directory": str(root), "archive": str(full_archive_path(directory)),
                  "model": str(root / "model"), "report": str(root / "report.txt"),
                  "inputs": str(root / "inputs"), "status": report["status"], "passed": report["passed"],
                  "validation_tool_version": report["evidence"]["tool_version"],
                  "download_temporary_files_removed": cleanup_download_duplicates(full_archive_path(directory))}
        atomic_json(directory / "delivery.json", result)
        return result


def _publish_generated(directory, generated):
    """Preserve edited display documents; never overwrite edited input settings."""
    from .. import managed
    root = task_root(directory)
    marker = directory / "generated-files.json"
    if marker.is_symlink():
        raise _error("delivery.unsafe", marker)
    previous = managed._read_json(marker) if marker.is_file() else {}
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


def _verify_report(directory, manifest, report):
    """Preserve historical validation without treating it as a fresh audit.

    A client upgrade alone must not prevent exporting an old completed task.
    All other archive/input/config/compiler evidence still has to match.
    """
    from .. import managed
    try:
        stored = report["evidence"]
        current = managed._evidence(manifest, full_archive_path(directory), stored["grompp"], stored["gmx"])
        current["tool_version"] = stored["tool_version"]
        if not managed._cache_reusable(report, current):
            raise ValueError("stale evidence")
    except (ToolError, KeyError, TypeError, ValueError, OSError):
        raise ToolError(key="cli.stale_report") from None


def _export(directory, *, to=None):
    from .. import managed
    from ..i18n import t
    if modern(directory) and (to is None or Path(to).expanduser().resolve() == task_root(directory)):
        return publish_in_place(directory)
    with run_lock(directory):
        manifest = managed._read_json(directory / "full-run.json")
        report_path = directory / "results" / "validation.json"
        report = managed._read_json(report_path) if report_path.is_file() else {}
        if report.get("validation_complete") is not True or report.get("status") not in ("validated", "validation_failed"):
            raise ToolError(key="delivery.incomplete", next_step="charmm-gui-cli jobs status NAME")
        # Verify the internal archive/input binding, not a mutable export copy.
        _verify_report(directory, manifest, report)
        context = manifest.get("delivery_context")
        if not isinstance(context, dict) or not context.get("task_id"):
            context = creation_context()  # Old runs: explicit export uses today's directory.
            manifest["delivery_context"] = context
            atomic_json(directory / "full-run.json", manifest)
        if not isinstance(context.get("creation_directory"), str) or not Path(context["creation_directory"]).is_absolute():
            raise _error("delivery.unsafe", directory / "full-run.json")
        name = managed._job_metadata(directory).get("name", directory.name)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
            raise _error("delivery.unsafe", directory)
        requested = Path(to).expanduser() if to is not None else Path(context["creation_directory"]) / name
        if requested.is_symlink():
            raise _error("delivery.foreign", requested)
        target = requested.resolve()
        # No copying into the source, its parents, or any of its descendants.
        if target == directory or target in directory.parents or directory in target.parents:
            raise _error("delivery.unsafe", target)
        from ..cli import report_summary
        from ..reporting import display_result, render_text
        text_report = directory / "results" / "delivery-report.txt"
        atomic_write(text_report, render_text(display_result(report_summary(report, name, "reports/validation.json"),
                     {"name": name, "archive": "charmm-gui.tgz",
                      "compilation_status": "passed" if managed._grompp_artifacts(report) else
                                            "failed" if report["evidence"]["grompp"] else "not_run",
                      "source_job_id": manifest.get("source_job_id"), "build_job_id": manifest.get("build_job_id")}, manifest, report)))
        config_path = directory / "results" / "delivery-config.json"
        atomic_json(config_path, _system_configuration(directory, manifest))
        guide_path = directory / "results" / "delivery-readme.txt"
        atomic_write(guide_path, t("delivery.layout", name=name) + "\n")
        files = {"charmm-gui.tgz": full_archive_path(directory),
                 "README.txt": guide_path, "report.txt": text_report, "reports/validation.txt": text_report,
                 "inputs/system-config.json": config_path,
                 "reports/validation.json": report_path}
        extracted = Path(report["extracted_directory"])
        if not extracted.resolve().is_relative_to(task_root(directory)):
            raise _error("delivery.unsafe", extracted)
        files.update(_tree(Path(report["extracted_directory"]), "model"))
        files.update(_tree(inputs_path(directory), "inputs"))
        files["inputs/system-config.json"] = config_path
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
                  "inputs": str(target / "inputs"), "status": report["status"], "passed": report["passed"],
                  "validation_tool_version": report["evidence"]["tool_version"]}
        from ..archive_transport import cleanup_download_duplicates
        result["download_temporary_files_removed"] = cleanup_download_duplicates(full_archive_path(directory))
        atomic_json(directory / "delivery.json", result)
        return result
