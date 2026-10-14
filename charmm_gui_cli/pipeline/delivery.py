"""In-place task results and explicitly requested independent exports."""

import charmm_gui_cli.store.result_files as _result_files
import charmm_gui_cli.store.tasks as _task_store

from charmm_gui_cli.store.layout import modern, task_root, inputs_path, log_path, full_archive_path

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import tarfile
import uuid

from charmm_gui_cli.store.auth import ToolError, atomic_json, atomic_write
from charmm_gui_cli.store.locking import run_lock, now


def export_run(directory, *, to=None):
    """Copy a completed managed run, never using hard links to user artifacts.

    A marker binds a destination to one run. Refreshes replace only unchanged,
    previously exported files; foreign directories and edited files are refused.
    Failed partial exports are retained and can be retried at a new destination.
    """
    from charmm_gui_cli.store.workers import metadata
    directory = Path(directory).expanduser().resolve()
    if metadata(directory)["running"]:
        raise ToolError(key="background.running", next_step="charmm-gui-cli jobs list")
    return _export(directory, to=to)


def auto_export(directory):
    # Called by the owning worker, which legitimately holds run.lock.
    if modern(directory):
        return publish_in_place(directory)
    return _export(Path(directory).resolve())


def publish_in_place(directory):
    """Generate small summaries; archive, model and inputs stay in place."""
    from charmm_gui_cli.pipeline import managed
    from charmm_gui_cli.presentation.summaries import report_summary
    from charmm_gui_cli.presentation.reporting import display_result, render_text
    from charmm_gui_cli.locales.i18n import t
    directory = Path(directory).resolve()
    with run_lock(directory):
        root = task_root(directory)
        manifest = _task_store._read_json(directory / "full-run.json")
        report_path = directory / "results/validation.json"
        report = _task_store._read_json(report_path)
        _verify_report(directory, manifest, report)
        _result_files.verify_model(full_archive_path(directory), root / "model")
        name = _task_store._job_metadata(directory).get("name", root.name)
        report_text = render_text(display_result(
            report_summary(report, name, report_path), {"name": name, "directory": str(directory), "archive": str(full_archive_path(directory))},
            manifest, report))
        generated = {"report.txt": report_text,
                     "inputs/system-config.json": json.dumps(_result_files._system_configuration(directory, manifest), indent=2, ensure_ascii=False) + "\n",
                     "README.txt": t("delivery.single_layout", name=name) + "\n"}
        _result_files._publish_generated(directory, generated)
        log_path(directory).touch(mode=0o600, exist_ok=True)
        from charmm_gui_cli.remote.archive_transport import cleanup_download_duplicates
        result = {"directory": str(root), "archive": str(full_archive_path(directory)),
                  "model": str(root / "model"), "report": str(root / "report.txt"),
                  "inputs": str(root / "inputs"), "status": report["status"], "passed": report["passed"],
                  "validation_tool_version": report["evidence"]["tool_version"],
                  "download_temporary_files_removed": cleanup_download_duplicates(full_archive_path(directory))}
        atomic_json(directory / "delivery.json", result)
        return result


def _verify_report(directory, manifest, report):
    """Preserve historical validation without treating it as a fresh audit.

    A client upgrade alone must not prevent exporting an old completed task.
    All other archive/input/config/compiler evidence still has to match.
    """
    from charmm_gui_cli.pipeline import managed
    try:
        stored = report["evidence"]
        current = managed._evidence(manifest, full_archive_path(directory), stored["grompp"], stored["gmx"])
        current["tool_version"] = stored["tool_version"]
        if not managed._cache_reusable(report, current):
            raise ValueError("stale evidence")
    except (ToolError, KeyError, TypeError, ValueError, OSError):
        value = managed._final_summary(directory)
        target = _task_store._job_metadata(directory).get("name") or str(task_root(directory))
        import shlex
        raise ToolError(key="cli.stale_report", details=value.get("validation_issues"),
                        next_step="charmm-gui-cli jobs resume " + shlex.quote(target)) from None


def _export(directory, *, to=None):
    from charmm_gui_cli.pipeline import managed
    from charmm_gui_cli.locales.i18n import t
    if modern(directory) and (to is None or Path(to).expanduser().resolve() == task_root(directory)):
        return publish_in_place(directory)
    with run_lock(directory):
        manifest = _task_store._read_json(directory / "full-run.json")
        report_path = directory / "results" / "validation.json"
        report = _task_store._read_json(report_path) if report_path.is_file() else {}
        if report.get("validation_complete") is not True or report.get("status") not in ("validated", "validation_failed"):
            raise ToolError(key="delivery.incomplete", next_step="charmm-gui-cli jobs status NAME")
        # Verify the internal archive/input binding, not a mutable export copy.
        _verify_report(directory, manifest, report)
        context = manifest.get("delivery_context")
        if not isinstance(context, dict) or not context.get("task_id"):
            context = _result_files.creation_context()  # Old runs: explicit export uses today's directory.
            from charmm_gui_cli.store.discovery import task_id
            identifier = task_id(directory)
            if identifier:
                context["task_id"] = identifier
            manifest["delivery_context"] = context
            atomic_json(directory / "full-run.json", manifest)
        if not isinstance(context.get("creation_directory"), str) or not Path(context["creation_directory"]).is_absolute():
            raise _result_files._error("delivery.unsafe", directory / "full-run.json")
        name = _task_store._job_metadata(directory).get("name", directory.name)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
            raise _result_files._error("delivery.unsafe", directory)
        requested = Path(to).expanduser() if to is not None else Path(context["creation_directory"]) / name
        if requested.is_symlink():
            raise _result_files._error("delivery.foreign", requested)
        target = requested.resolve()
        # No copying into the source, its parents, or any of its descendants.
        if target == directory or target in directory.parents or directory in target.parents:
            raise _result_files._error("delivery.unsafe", target)
        from charmm_gui_cli.presentation.summaries import report_summary
        from charmm_gui_cli.presentation.reporting import display_result, render_text
        text_report = directory / "results" / "delivery-report.txt"
        atomic_write(text_report, render_text(display_result(report_summary(report, name, "reports/validation.json"),
                     {"name": name, "directory": str(directory), "archive": "charmm-gui.tgz",
                      "compilation_status": "passed" if managed._grompp_artifacts(report) else
                                            "failed" if report["evidence"]["grompp"] else "not_run",
                      "source_job_id": manifest.get("source_job_id"), "build_job_id": manifest.get("build_job_id")}, manifest, report)))
        config_path = directory / "results" / "delivery-config.json"
        atomic_json(config_path, _result_files._system_configuration(directory, manifest))
        guide_path = directory / "results" / "delivery-readme.txt"
        atomic_write(guide_path, t("delivery.layout", name=name) + "\n")
        files = {"charmm-gui.tgz": full_archive_path(directory),
                 "README.txt": guide_path, "report.txt": text_report, "reports/validation.txt": text_report,
                 "inputs/system-config.json": config_path,
                 "reports/validation.json": report_path}
        extracted = Path(report["extracted_directory"])
        if not extracted.resolve().is_relative_to(task_root(directory)):
            raise _result_files._error("delivery.unsafe", extracted)
        files.update(_result_files._tree(Path(report["extracted_directory"]), "model"))
        files.update(_result_files._tree(inputs_path(directory), "inputs"))
        files["inputs/system-config.json"] = config_path
        compiled = report.get("checks", {}).get("grompp", {}).get("output_dir")
        if compiled and Path(compiled).resolve().is_relative_to(directory) and Path(compiled).is_dir():
            files.update(_result_files._tree(Path(compiled), "reports/grompp"))
        for path in files.values():
            if path.is_symlink() or not path.is_file():
                raise _result_files._error("delivery.unsafe", path)
        hashes = {name: _result_files._digest(path) for name, path in files.items()}
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
        marker_path = target / _result_files.MARKER
        previous = {}
        if target.exists():
            if not target.is_dir() or marker_path.is_symlink() or not marker_path.is_file():
                raise _result_files._error("delivery.foreign", target)
            try:
                previous = json.loads(marker_path.read_text())
            except (OSError, ValueError):
                raise _result_files._error("delivery.foreign", target) from None
            if (not isinstance(previous, dict) or previous.get("task_id") != context["task_id"]
                    or not modern(directory) and previous.get("run_directory") != str(directory)):
                raise _result_files._error("delivery.foreign", target)
            if previous.get("complete") is not True:
                raise _result_files._error("delivery.partial", target)
            if not isinstance(previous.get("files"), dict):
                raise _result_files._error("delivery.foreign", target)
            # Do not overwrite files the user changed, even in an owned export.
            for name, digest in previous.get("files", {}).items():
                path = target / name
                if (Path(name).is_absolute() or ".." in Path(name).parts or
                        any(parent.is_symlink() for parent in path.parents if parent != target) or
                        path.is_symlink() or not path.is_file() or _result_files._digest(path) != digest):
                    raise _result_files._error("delivery.modified", target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.mkdir(mode=0o700)
            except FileExistsError:
                raise _result_files._error("delivery.foreign", target) from None
        owned = previous.get("files", {})
        # Check every destination before changing any file or marker.
        for name in files:
            path = target / name
            if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != target)
                    or (path.exists() and name not in owned)):
                raise _result_files._error("delivery.modified", target)
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
                if _result_files._digest(Path(temporary)) != hashes[name]:
                    raise _result_files._error("delivery.changed_source", source)
                os.replace(temporary, path)
            marker.update(complete=True, exported_at=now())
            atomic_json(marker_path, marker)
        result = {"directory": str(target), "archive": str(target / "charmm-gui.tgz"),
                  "model": str(target / "model"), "report": str(target / "reports/validation.json"),
                  "inputs": str(target / "inputs"), "status": report["status"], "passed": report["passed"],
                  "validation_tool_version": report["evidence"]["tool_version"]}
        from charmm_gui_cli.remote.archive_transport import cleanup_download_duplicates
        result["download_temporary_files_removed"] = cleanup_download_duplicates(full_archive_path(directory))
        atomic_json(directory / "delivery.json", result)
        return result


# Backward-compatible function exports; internal code uses the owner module.
from charmm_gui_cli.store.result_files import MARKER, _digest, _error, _publish_generated, _system_configuration, _tree, check_destination, creation_context, verify_model
