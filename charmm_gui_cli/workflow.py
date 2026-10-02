"""Durable intent before submission, explicit recovery after ambiguous POSTs."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time

from . import __version__
from .api import SubmissionUnknown, job_id
from .artifacts import inspect_archive
from .auth import ToolError, atomic_json
from .config import plan, validate_config


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
            raise ToolError("Another process is operating on this run.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def save(directory, manifest):
    manifest["updated_at"] = now()
    atomic_json(Path(directory) / "run.json", manifest)


def read(directory):
    try:
        manifest = json.loads((Path(directory) / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolError("Cannot read run.json in the run directory.") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ToolError("Unsupported run manifest.")
    validate_config(manifest.get("config"))
    if manifest.get("job_id") is not None:
        job_id(manifest["job_id"])
    return manifest


def start(client, config, directory):
    config = validate_config(config)
    proposal = plan(config)
    if not proposal["ready_to_submit"]:
        raise ToolError("Preparation incomplete: " + " ".join(proposal["blockers"]))
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
        except SubmissionUnknown:
            manifest["state"] = "submission_unknown"
            save(directory, manifest)
            raise
        manifest.update(job_id=identifier, state="submitted")
        save(directory, manifest)
        return manifest


def status_summary(data):
    status = data.get("status")
    if status == "submitted":
        return {"status": "pending", "raw_status": status}
    if isinstance(status, str) and re.fullmatch(r"running [A-Za-z0-9_.-]+", status):
        return {"status": "running", "raw_status": status}
    if status not in ("pending", "running", "done", "error"):
        raise ToolError("Unrecognized job status; cannot infer completion or successful authentication.")
    return {"status": status}


def attach(client, directory, identifier):
    identifier = job_id(identifier)
    with run_lock(directory):
        manifest = read(directory)
        if manifest["state"] not in ("submission_unknown", "submitting") or manifest.get("job_id"):
            raise ToolError("attach is only for a run with an ambiguous submission and no recorded job ID.")
        if identifier == manifest["config"]["source_job_id"]:
            raise ToolError("Attach the new cloned build job, not the source preparation job.")
        status_summary(client.status(identifier))
        manifest.update(job_id=identifier, state="submitted")
        save(directory, manifest)
        return manifest


def resume(client, directory, *, wait=False, interval=30, max_wait=21600, pdb_member=None, progress=None):
    if interval < 10 or max_wait <= 0:
        raise ToolError("Polling interval must be >= 10 seconds and max-wait must be positive.")
    directory = Path(directory)
    with run_lock(directory):
        manifest = read(directory)
        identifier = manifest.get("job_id")
        if not identifier:
            raise ToolError("Submission outcome is unknown. Check website jobs and use attach; resume never resubmits.")
        deadline = time.monotonic() + max_wait
        archive = directory / "charmm-gui.tgz"
        if not archive.exists():
            while True:
                data = client.status(identifier)
                status = status_summary(data)["status"]
                manifest["remote_status"] = status
                manifest["raw_remote_status"] = data.get("status")
                manifest["last_output_file"] = data.get("lastOutFile")
                manifest["state"] = "remote_error" if status == "error" else status
                save(directory, manifest)
                if progress:
                    progress({"state": data.get("status", status), "job_id": identifier})
                if status == "error":
                    raise ToolError("Remote building failed. Review this job in CHARMM-GUI; no resubmission was attempted.")
                if status == "done":
                    break
                if not wait:
                    return manifest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolError("Wait limit reached. The remote job is retained; run resume later.")
                time.sleep(min(interval, remaining))
            if progress:
                progress({"state": "downloading", "job_id": identifier})
            client.download(identifier, archive)
        manifest["state"] = "downloaded_unverified"
        with archive.open("rb") as handle:
            digest = hashlib.sha256()
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        manifest["archive_sha256"] = digest.hexdigest()
        save(directory, manifest)
        report = inspect_archive(archive, manifest["config"]["expected_ligands"], pdb_member)
        atomic_json(directory / "validation.json", report)
        manifest["validation"] = report
        manifest["state"] = "ligands_present" if report["passed"] else "validation_failed"
        save(directory, manifest)
        if not report["passed"]:
            raise ToolError("Expected ligand residues are missing from the assembled structure. See validation.json.")
        return manifest
