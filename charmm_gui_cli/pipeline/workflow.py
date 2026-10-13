"""Durable intent before submission, explicit recovery after ambiguous POSTs."""

from ..layout import archive_path as run_archive_path

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import time
import zlib

from .. import __version__
from ..api import SubmissionUnknown, job_id
from ..artifacts import inspect_archive
from ..auth import ToolError, atomic_json
from ..config import plan, validate_config


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def run_lock(directory):
    directory = Path(directory)
    if not directory.is_dir():
        raise ToolError("Run directory does not exist.")
    with (directory / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ToolError(key="run.lock_busy", category="lock_busy", retryable=True,
                            retry_after_seconds=10, next_step="charmm-gui-cli jobs list") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
            category="interrupted")
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


def read(directory):
    try:
        manifest = json.loads((Path(directory) / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolError("Cannot read run.json in the run directory.") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ToolError("Unsupported run manifest.")
    # New catalog coverage must not invalidate an existing recorded task.
    validate_config(manifest.get("config"), existing=True)
    if manifest.get("job_id") is not None:
        job_id(manifest["job_id"])
    return manifest


def verify_archive_provenance(directory, manifest=None, *, create_receipt=False):
    """Bind an immutable download to its build job; never repair mismatches.

    Legacy downloads need a previously recorded hash and a matching official
    archive root. An interrupted first download can be sealed only after its
    durable downloading intent after done or the observed compressing status.
    Read-only consumers never bootstrap it.
    """
    directory = Path(directory)
    manifest = read(directory) if manifest is None else manifest

    def reject(message):
        raise ToolError(message, category="archive_provenance",
                        next_step="Inspect this run's job ID, archive and download-receipt.json; retain all files and do not replace or resubmit automatically.")

    identifier = job_id(manifest.get("job_id"))
    archive_path = run_archive_path(directory)
    receipt_path = directory / "download-receipt.json"
    if archive_path.is_symlink() or receipt_path.is_symlink():
        reject("Archive and download receipt must be regular local files, not symlinks.")
    recorded_hash = manifest.get("archive_sha256")
    if "archive_sha256" in manifest and not (
            isinstance(recorded_hash, str) and re.fullmatch(r"[0-9a-f]{64}", recorded_hash)):
        reject("The recorded archive SHA256 is invalid; refusing to replace it.")
    try:
        digest = hashlib.sha256()
        with archive_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        actual_hash = digest.hexdigest()
        if recorded_hash is not None and recorded_hash != actual_hash:
            reject("Archive SHA256 mismatch: the downloaded archive has changed since it was recorded.")
        receipt = None
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                reject("Invalid download receipt.")
        elif recorded_hash is None and not (
                create_receipt and manifest.get("state") == "downloading"
                and (manifest.get("remote_status") == "done" or
                     (manifest.get("remote_status") == "archive_preparing" and manifest.get("raw_remote_status") == "compressing"))):
            reject("Existing archive has no download receipt or previously recorded SHA256; ownership cannot be established.")

        expected_root = "charmm-gui-" + identifier
        found_file = False
        # Exhaust gzip as well as tar to verify the CRC/trailer on legacy files.
        with gzip.open(archive_path, "rb") as decoded:
            with tarfile.open(fileobj=decoded, mode="r|") as archive:
                for index, member in enumerate(archive):
                    if index >= 50000:
                        reject("Archive has too many members to verify ownership.")
                    name = member.name
                    parts = PurePosixPath(name).parts
                    if (not name or name.startswith("/") or "\\" in name
                            or ".." in parts or not (member.isfile() or member.isdir())):
                        reject("Archive contains an unsafe path or unsupported member type.")
                    if not parts and member.isdir():
                        continue
                    if not parts or parts[0] != expected_root:
                        reject("Archive root does not match build job " + identifier + ".")
                    if member.isfile():
                        if len(parts) < 2:
                            reject("Archive root must be a directory containing result files.")
                        found_file = True
            for _ in iter(lambda: decoded.read(1024 * 1024), b""):
                pass
        if not found_file:
            reject("Archive contains no result files under its build job root.")
        expected = {"version": 1, "job_id": identifier,
                    "archive_sha256": actual_hash, "archive_root": expected_root}
        if receipt is not None and receipt != expected:
            reject("Download receipt does not match the current job and archive.")
        if receipt is None and create_receipt:
            # Exclusive hard-link publication cannot overwrite an old receipt.
            # Retain the staging file in accordance with no-deletion operation.
            descriptor, staged = tempfile.mkstemp(prefix=".download-receipt-", dir=directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(expected, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(staged, receipt_path)
            except FileExistsError:
                if json.loads(receipt_path.read_text(encoding="utf-8")) != expected:
                    reject("A different download receipt already exists; refusing to overwrite it.")
        return expected
    except (OSError, ValueError, EOFError, tarfile.TarError, zlib.error) as exc:
        reject("Cannot verify archive ownership/integrity: " + type(exc).__name__ + ".")


def start(client, config, directory, *, existing_configuration=False):
    config = validate_config(config, existing=existing_configuration)
    proposal = plan(config)
    if not proposal["ready_to_submit"]:
        raise ToolError("Preparation incomplete: " + " ".join(proposal["blockers"]))
    client.validate_auth()
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ToolError("Run directory already exists. Use resume; do not submit again.") from None
    with run_lock(directory):
        manifest = {
            "schema_version": 1, "tool_version": __version__, "created_at": now(),
            "state": "submitting", "job_id": None, "config": config, "plan": proposal,
        }
        save(directory, manifest)
        try:
            identifier = client.submit(proposal["request"]["form"])
            if identifier == config["source_job_id"]:
                raise SubmissionUnknown("Server returned the source job ID despite clone_job=true. Check website jobs before attaching.")
        except (SubmissionUnknown, KeyboardInterrupt) as exc:
            manifest["state"] = "submission_unknown"
            error = exc if isinstance(exc, SubmissionUnknown) else SubmissionUnknown("Interrupted during submission; inspect website jobs and attach before resuming.")
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
                        category="unexpected_response", next_step="Inspect the existing job and authenticate with login if needed; do not resubmit.")
    return {"status": status}


def attach(client, directory, identifier):
    identifier = job_id(identifier)
    with tracked_run(directory, read) as manifest:
        if manifest["state"] not in ("submission_unknown", "submitting") or manifest.get("job_id"):
            raise ToolError("attach is only for a run with an ambiguous submission and no recorded job ID.")
        if identifier == manifest["config"]["source_job_id"]:
            raise ToolError("Attach the new cloned build job, not the source preparation job.")
        status_summary(client.status(identifier))
        manifest.update(job_id=identifier, state="submitted")
        save(directory, manifest)
        return manifest


def _download_archive(client, directory, manifest, archive, *, wait, deadline, progress):
    """Wait for on-demand compression, respecting a durable GET-only backoff."""
    identifier = manifest["job_id"]
    while not archive.exists():
        retry_at = manifest.get("archive_retry_at_unix", 0)
        if isinstance(retry_at, bool) or not isinstance(retry_at, (int, float)) or not math.isfinite(retry_at):
            raise ToolError("Invalid saved archive retry time; inspect run.json before resuming.")
        delay = retry_at - time.time()
        if retry_at and wait and time.monotonic() >= deadline:
            raise ToolError("等待归档准备的本地时限已到；原任务和下载重试时间已保留。",
                            category="archive_preparing", retryable=True, retry_after_seconds=max(0, math.ceil(delay)))
        if delay > 0:
            if not wait or time.monotonic() >= deadline:
                raise ToolError("官网归档仍在准备；等待记录的重试时间后恢复同一任务。",
                                category="archive_preparing", retryable=True, retry_after_seconds=math.ceil(delay))
            time.sleep(min(30, delay, max(0, deadline - time.monotonic())))
            continue
        if progress:
            progress({"state": "downloading", "job_id": identifier, "build_job_id": identifier})
        manifest["state"] = "downloading"
        save(directory, manifest)
        try:
            client.download(identifier, archive)
        except ToolError as error:
            if error.category != "archive_preparing":
                raise
            manifest.update(state="archive_preparing", archive_retry_at_unix=time.time() + 120)
            save(directory, manifest)
            if progress:
                progress({"state": "archive_preparing", "job_id": identifier, "build_job_id": identifier})
            if not wait or time.monotonic() >= deadline:
                raise
        else:
            manifest.pop("archive_retry_at_unix", None)


def resume(client, directory, *, wait=False, interval=30, max_wait=21600, pdb_member=None, progress=None):
    if interval < 10 or max_wait <= 0:
        raise ToolError("Polling interval must be >= 10 seconds and max-wait must be positive.")
    directory = Path(directory)
    with tracked_run(directory, read) as manifest:
        identifier = manifest.get("job_id")
        if not identifier:
            raise SubmissionUnknown("Submission outcome is unknown. Check website jobs and use attach; resume never resubmits.")
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
                                    category="remote_failure", next_step="Inspect saved job output and the failed remote job; resume never resubmits.")
                if status == "done":
                    break
                if status == "archive_preparing":
                    manifest["archive_retry_at_unix"] = time.time() + 120
                    save(directory, manifest)
                    break
                if not wait:
                    return manifest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolError("Wait limit reached. The remote job is retained; run resume later.",
                                    category="wait_timeout", retryable=True)
                time.sleep(min(interval, remaining))
        if not archive.exists():
            _download_archive(client, directory, manifest, archive, wait=wait, deadline=deadline, progress=progress)
        receipt = verify_archive_provenance(directory, manifest, create_receipt=True)
        from ..archive_transport import cleanup_download_duplicates
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
                            category="validation_failure", next_step="Review validation.json and the retained archive; do not use this structure as validated.")
        return manifest
