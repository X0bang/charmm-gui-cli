"""Durable intent before submission, explicit recovery after ambiguous POSTs."""

from charmm_gui_cli.store.layout import archive_path as run_archive_path

from contextlib import contextmanager
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile
import tempfile
import time
import zlib

from .. import __version__
from charmm_gui_cli.remote.api import SubmissionUnknown
from charmm_gui_cli.store.identifiers import job_id
from charmm_gui_cli.pipeline.artifacts import inspect_archive
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.pipeline.config import plan
from charmm_gui_cli.store.configuration import validate_config
from ..store.paths import read_value
from ..store.locking import now, run_lock


from charmm_gui_cli.store.run_manifest import read

from charmm_gui_cli.store.provenance import verify_archive_provenance


def _local_action(directory, *, resume=False):
    """Concrete recovery for a managed child or an advanced API-only run."""
    from ..store.paths import root_for
    directory = Path(directory).absolute()
    root = root_for(directory / 'run.json')
    if root is not None and (root / '.charmm-gui/full-run.json').is_file():
        return shlex.join(['charmm-gui-cli', 'jobs', 'resume' if resume else 'status', str(root)])
    if resume:
        return shlex.join(['charmm-gui-cli', 'resume', str(directory), '--wait'])
    return shlex.join(['ls', '-l', str(directory / 'run.json')])

def save(directory, manifest):
    manifest["updated_at"] = now()
    atomic_json(Path(directory) / "run.json", manifest)


@contextmanager
def record_errors(directory, manifest, saver=save):
    """Retain resumable state and safe error metadata, including Ctrl-C."""
    try:
        yield
    except (ToolError, KeyboardInterrupt) as exc:
        error = exc if isinstance(exc, ToolError) else ToolError(
            "Interrupted locally; the existing remote job and submission intents are retained.",
            key="workflow.interrupted", category="interrupted", next_step=_local_action(directory))
        manifest["last_error"] = {**error.as_dict(), "at": now(), "stage": manifest.get("state")}
        saver(directory, manifest)
        raise
    else:
        if manifest.pop("last_error", None) is not None:
            saver(directory, manifest)


@contextmanager
def tracked_run(directory, loader, saver=save):
    with run_lock(directory):
        manifest = loader(directory)
        with record_errors(directory, manifest, saver):
            yield manifest


def start(client, config, directory, *, existing_configuration=False):
    config = validate_config(config, existing=existing_configuration)
    proposal = plan(config)
    if not proposal["ready_to_submit"]:
        details = []
        if not config['source_job_id']:
            details.append({'key': 'workflow.source_required'})
        if not config['preparation']['ligand_parameters_ready']:
            details.append({'key': 'workflow.parameters_required'})
        if isinstance(proposal.get('local_pdb'), dict) and proposal['local_pdb'].get('missing_ligands'):
            details.append({'key': 'workflow.local_ligands_missing'})
        raise ToolError("Preparation incomplete: " + " ".join(proposal["blockers"]),
                        key="workflow.preparation_incomplete", details=details, next_step="charmm-gui-cli plan -h")
    client.validate_auth()
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ToolError("Run directory already exists. Use resume; do not submit again.",
                        key="workflow.directory_exists", params={"path": str(directory.absolute())},
                        next_step=_local_action(directory)) from None
    with run_lock(directory):
        manifest = {
            "schema_version": 1, "tool_version": __version__, "created_at": now(),
            "state": "submitting", "job_id": None, "config": config, "plan": proposal,
        }
        save(directory, manifest)
        try:
            identifier = client.submit(proposal["request"]["form"])
            if identifier == config["source_job_id"]:
                raise SubmissionUnknown("Server returned the source job ID despite clone_job=true. Check website jobs before attaching.",
                                        key="workflow.clone_identity_unknown", next_step=_local_action(directory))
        except (SubmissionUnknown, KeyboardInterrupt) as exc:
            manifest["state"] = "submission_unknown"
            error = exc if isinstance(exc, SubmissionUnknown) else SubmissionUnknown(
                "Interrupted during submission; inspect website jobs and attach before resuming.",
                key="workflow.post_interrupted", next_step=_local_action(directory))
            manifest["last_error"] = {**error.as_dict(), "at": now(), "stage": "submitting"}
            save(directory, manifest)
            raise
        manifest.update(job_id=identifier, state="submitted")
        save(directory, manifest)
        return manifest


def status_summary(data):
    status = data.get("status")
    if status == "compressing":
        return {"status": "archive_preparing", "raw_status": status}
    if status == "submitted":
        return {"status": "pending", "raw_status": status}
    if isinstance(status, str) and re.fullmatch(r"running [A-Za-z0-9_.-]+", status):
        return {"status": "running", "raw_status": status}
    if status not in ("pending", "running", "done", "error"):
        raise ToolError("Unrecognized job status; cannot infer completion or successful authentication.",
                        key="workflow.status_unknown", category="unexpected_response", next_step="charmm-gui-cli jobs list")
    return {"status": status}


def attach(client, directory, identifier):
    identifier = job_id(identifier)
    with tracked_run(directory, read) as manifest:
        if manifest["state"] not in ("submission_unknown", "submitting") or manifest.get("job_id"):
            raise ToolError("attach is only for a run with an ambiguous submission and no recorded job ID.",
                            key="workflow.attach_ineligible", next_step=_local_action(directory))
        if identifier == manifest["config"]["source_job_id"]:
            raise ToolError("Attach the new cloned build job, not the source preparation job.",
                            key="workflow.attach_source", next_step=_local_action(directory))
        status_summary(client.status(identifier))
        manifest.update(job_id=identifier, state="submitted")
        save(directory, manifest)
        return manifest


def _download_archive(client, directory, manifest, archive, *, wait, deadline, progress):
    """Wait for on-demand compression, respecting a durable GET-only backoff."""
    identifier = manifest["job_id"]
    from ..remote.transfers import observe, archive_delay
    while not archive.exists():
        retry_at = manifest.get("archive_retry_at_unix", 0)
        if isinstance(retry_at, bool) or not isinstance(retry_at, (int, float)) or not math.isfinite(retry_at):
            raise ToolError("Invalid saved archive retry time; inspect run.json before resuming.",
                            key="workflow.archive_retry_invalid", next_step=_local_action(directory))
        delay = retry_at - time.time()
        if retry_at and wait and time.monotonic() >= deadline:
            raise ToolError("等待归档准备的本地时限已到；原任务和下载重试时间已保留。",
                            key="workflow.archive_wait_limit", category="archive_preparing", retryable=True,
                            retry_after_seconds=max(0, math.ceil(delay)), next_step=_local_action(directory, resume=True))
        if delay > 0:
            if not wait or time.monotonic() >= deadline:
                raise ToolError("官网归档仍在准备；等待记录的重试时间后恢复同一任务。",
                                key="workflow.archive_waiting", category="archive_preparing", retryable=True,
                                retry_after_seconds=math.ceil(delay), next_step=_local_action(directory, resume=True))
            time.sleep(min(30, delay, max(0, deadline - time.monotonic())))
            continue
        if progress:
            progress({"state": "downloading", "job_id": identifier, "build_job_id": identifier})
        manifest["state"] = "downloading"
        save(directory, manifest)
        def received(event):
            record = {**event, "active": not event["transfer_complete"], "updated_at": now(), "job_id": identifier}
            atomic_json(directory / "download-progress.json", record)
            if progress:
                progress({"state": "downloading", "job_id": identifier, "build_job_id": identifier, **event})
        try:
            with observe(received):
                client.download(identifier, archive)
        except ToolError as error:
            atomic_json(directory / "download-progress.json", {"active": False, "updated_at": now(), "job_id": identifier})
            if error.category != "archive_preparing":
                raise
            prior = manifest.get("archive_poll_attempts", 0)
            archive_delay(prior + 1 if type(prior) is int else None)
            attempt = prior + 1
            delay = max(archive_delay(attempt), error.retry_after_seconds or 0)
            manifest.update(state="archive_preparing", archive_poll_attempts=attempt, archive_retry_at_unix=time.time() + delay)
            error.retry_after_seconds = delay
            save(directory, manifest)
            if progress:
                progress({"state": "archive_preparing", "job_id": identifier, "build_job_id": identifier})
            if not wait or time.monotonic() >= deadline:
                raise
        else:
            manifest.pop("archive_retry_at_unix", None)
        finally:
            # Viewer metadata must stop after TLS/filesystem failures or Ctrl-C;
            # retaining its byte counters is not evidence of a completed archive.
            path = directory / "download-progress.json"
            try:
                if path.is_file():
                    value = json.loads(path.read_text())
                    if isinstance(value, dict) and value.get("active"):
                        atomic_json(path, {**value, "active": False, "updated_at": now()})
            except (OSError, ValueError):
                pass  # Never mask the original transport/recovery exception.


def resume(client, directory, *, wait=False, interval=30, max_wait=21600, pdb_member=None, progress=None):
    if interval < 10 or max_wait <= 0:
        raise ToolError("Polling interval must be >= 10 seconds and max-wait must be positive.",
                        key="workflow.poll_invalid", next_step="charmm-gui-cli resume -h")
    directory = Path(directory)
    with tracked_run(directory, read) as manifest:
        identifier = manifest.get("job_id")
        if not identifier:
            raise SubmissionUnknown("Submission outcome is unknown. Check website jobs and use attach; resume never resubmits.",
                                    key="workflow.submission_unknown", next_step=_local_action(directory))
        deadline = time.monotonic() + max_wait
        archive = run_archive_path(directory)
        awaiting_archive = manifest.get("state") == "archive_preparing" and (
            manifest.get("remote_status") == "done" or
            (manifest.get("remote_status") == "archive_preparing" and manifest.get("raw_remote_status") == "compressing"))
        if not archive.exists() and not awaiting_archive:
            while True:
                data = client.status(identifier)
                status = status_summary(data)["status"]
                manifest["remote_status"] = status
                manifest["raw_remote_status"] = data.get("status")
                manifest["last_output_file"] = data.get("lastOutFile")
                manifest["state"] = "remote_error" if status == "error" else status
                save(directory, manifest)
                if progress:
                    event = {"state": status if status == "archive_preparing" else data.get("status", status), "job_id": identifier, "build_job_id": identifier}
                    output = data.get("lastOutFile")
                    # Expose recognized internal output basenames only, never
                    # raw log lines, URLs, arbitrary server text or credentials.
                    if isinstance(output, str) and re.fullmatch(
                            r"(?:ppm|step[1-9](?:\.[0-9]+)?_(?:pdbreader|orient|size|packing(?:_head)?|lipid(?:_checkchol)?|waterbox|water|ion|assembly|input))\.out", output):
                        event["last_output_file"] = output
                    progress(event)
                if status == "error":
                    raise ToolError("Remote building failed. Review this job in CHARMM-GUI; no resubmission was attempted.",
                                    key="workflow.remote_failed", category="remote_failure", next_step=_local_action(directory))
                if status == "done":
                    break
                if status == "archive_preparing":
                    from ..remote.transfers import archive_delay
                    prior = manifest.get("archive_poll_attempts", 0)
                    archive_delay(prior + 1 if type(prior) is int else None)
                    attempt = prior + 1
                    manifest.update(archive_poll_attempts=attempt, archive_retry_at_unix=time.time() + archive_delay(attempt))
                    save(directory, manifest)
                    break
                if not wait:
                    return manifest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolError("Wait limit reached. The remote job is retained; run resume later.",
                                    key="workflow.wait_limit", category="wait_timeout", retryable=True,
                                    next_step=_local_action(directory, resume=True))
                time.sleep(min(interval, remaining))
        if not archive.exists():
            _download_archive(client, directory, manifest, archive, wait=wait, deadline=deadline, progress=progress)
        receipt = verify_archive_provenance(directory, manifest, create_receipt=True)
        from charmm_gui_cli.remote.archive_transport import cleanup_download_duplicates
        cleanup_download_duplicates(archive)
        manifest["state"] = "downloaded_unverified"
        manifest["archive_sha256"] = receipt["archive_sha256"]
        save(directory, manifest)
        report = inspect_archive(archive, manifest["config"]["expected_ligands"], pdb_member)
        atomic_json(directory / "validation.json", report)
        manifest["validation"] = report
        manifest["state"] = "ligands_present" if report["passed"] else "validation_failed"
        save(directory, manifest)
        if not report["passed"]:
            raise ToolError("Expected ligand residues are missing from the assembled structure. See validation.json.",
                            key="workflow.ligands_missing", category="validation_failure",
                            next_step=shlex.join(['ls', '-l', str(directory / 'validation.json')]))
        return manifest
