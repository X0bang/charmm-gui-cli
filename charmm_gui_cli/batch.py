"""Durable, bounded multi-build queue. Every job is prepared before networking."""

from argparse import Namespace
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import time

import yaml

from .auth import ToolError, atomic_json, atomic_write
from .config import UniqueLoader, mapping, composition, number
from .workflow import run_lock, now


FIELDS = ("protein", "ligand", "complex", "ligand_resname", "ligand_smiles",
          "accept_conect_bond_orders", "hydrogens", "upper", "lower", "salt",
          "salt_concentration", "margin", "water_padding", "orientation", "n_terminal", "c_terminal")
TERMINAL = {"validated", "validation_failed", "preparation_failed", "remote_error", "local_failed"}
DEFAULTS = {"ligand_resname": "LIG", "accept_conect_bond_orders": False, "hydrogens": "add_missing",
            "upper": "POPC=1", "lower": "POPC=1", "salt": "KCl", "salt_concentration": 0.15,
            "margin": 20, "water_padding": 22.5, "orientation": "ppm", "n_terminal": "NTER", "c_terminal": "CTER"}


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return _hash(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _named_job(directory, name):
    return "batch-" + _hash(str(directory).encode())[:12] + "-" + name


def _options(raw, base):
    result = dict(raw)
    for key, value in result.items():
        if key == "accept_conect_bond_orders":
            if type(value) is not bool:
                raise ToolError("accept_conect_bond_orders must be boolean.")
        elif key in ("salt_concentration", "margin", "water_padding"):
            number(value, key, positive=key != "salt_concentration")
        elif not isinstance(value, str) or not value or any(c in value for c in "\n\r\x00"):
            raise ToolError(f"{key} must be a nonempty single-line string.")
        if key in ("protein", "ligand", "complex"):
            result[key] = str((base / Path(value).expanduser()).resolve())
    for key in ("upper", "lower"):
        if key in result:
            composition(result[key])
    choices = {"salt": ("KCl", "NaCl"), "hydrogens": ("add_missing", "preserve"),
               "orientation": ("ppm", "prepared"), "n_terminal": ("NTER", "NNEU", "ACE", "ACP", "NONE"),
               "c_terminal": ("CTER", "CNEU", "CT1", "CT2", "CT3", "NONE")}
    for key, allowed in choices.items():
        if key in result and result[key] not in allowed:
            raise ToolError(f"Unsupported batch {key}.")
    if "ligand_resname" in result and not re.fullmatch(r"[A-Z][A-Z0-9]{0,2}", result["ligand_resname"]):
        raise ToolError("ligand_resname must be 1-3 uppercase letters/digits beginning with a letter.")
    return result


def load_batch(path):
    path = Path(path).expanduser().resolve()
    try:
        data = path.read_bytes()
        raw = yaml.load(data.decode("utf-8"), Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ToolError("Cannot read valid batch YAML.") from None
    mapping(raw, ("version", "defaults", "jobs"), "batch")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ToolError("Batch YAML requires version: 1.")
    defaults = _options(mapping(raw.get("defaults", {}), FIELDS, "batch defaults"), path.parent)
    if not isinstance(raw.get("jobs"), list) or not raw["jobs"]:
        raise ToolError("Batch jobs must be a nonempty list.")
    jobs, names = [], set()
    for raw_job in raw["jobs"]:
        mapping(raw_job, ("name",) + FIELDS, "batch job")
        name = raw_job.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ToolError("Batch names must be 1-64 ASCII letters/digits, dots, underscores or hyphens, starting with a letter/digit.")
        if name in names:
            raise ToolError("Batch job names must be unique.")
        names.add(name)
        options = {**DEFAULTS, **defaults, **_options({k: v for k, v in raw_job.items() if k != "name"}, path.parent)}
        if not options.get("complex") and not (options.get("protein") and options.get("ligand")):
            raise ToolError("Each batch job needs protein + ligand, or complex.")
        jobs.append({"name": name, "options": options})
    return {"version": 1, "source": str(path), "source_sha256": _hash(data), "jobs": jobs}


def _arguments(directory, options=None, **kwargs):
    fields = {key: None for key in FIELDS}
    fields.update(options or {})
    fields["complex_pdb"] = fields.pop("complex")
    for key in ("protein", "ligand", "complex_pdb"):
        if fields[key] is not None:
            fields[key] = Path(fields[key])
    return Namespace(**fields, command="build" if options is not None else "build-resume",
                     config=None, out=directory, directory=directory, name=None, dry_run=options is not None,
                     wait=False, interval=kwargs.get("interval", 30), max_wait=kwargs.get("max_wait", 21600),
                     token_file=kwargs.get("token_file"), cookies=kwargs.get("cookies"),
                     grompp=kwargs.get("grompp", False), no_grompp=kwargs.get("no_grompp", False),
                     gmx=kwargs.get("gmx", "gmx"))


def _save(directory, manifest):
    manifest["updated_at"] = now()
    atomic_json(directory / "batch.json", manifest)
    summary = _summary(directory, manifest)
    atomic_json(directory / "summary.json", summary)
    atomic_write(directory / "summary.csv", export_csv(summary))


def _read(directory, verify=True):
    try:
        manifest = json.loads((directory / "batch.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
            raise ValueError
        config = manifest["config"]
        if (not isinstance(config, dict) or not isinstance(config.get("source"), str)
                or not isinstance(config.get("jobs"), list) or not config["jobs"]
                or not isinstance(manifest.get("jobs"), list) or not isinstance(manifest.get("state"), str)):
            raise ValueError
        number(manifest.get("last_start_unix"), "saved submission timestamp")
        if manifest["version"] != 1 or _canonical(config) != manifest["config_fingerprint"]:
            raise ValueError
        if verify and load_batch(config["source"]) != config:
            raise ToolError("Batch source changed since preparation; refusing resume. Use a new batch directory.")
        if [j["name"] for j in manifest["jobs"]] != [j["name"] for j in config["jobs"]]:
            raise ValueError
        for job in manifest["jobs"]:
            if (not isinstance(job, dict) or not isinstance(job.get("name"), str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", job["name"])
                    or not isinstance(job.get("state"), str) or not job["state"]
                    or type(job.get("started")) is not bool or not isinstance(job.get("directory"), str)
                    or any(key in job and not isinstance(job[key], str) for key in ("error", "error_category", "source_job_id", "build_job_id"))):
                raise ValueError
            if "next_retry_unix" in job:
                number(job["next_retry_unix"], "saved next retry timestamp")
            expected = directory / "jobs" / job["name"]
            if Path(job["directory"]).resolve() != expected.resolve():
                raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ToolError("Invalid batch manifest or fingerprint; refusing to guess task ownership.") from None
    return manifest


def initialize(config_path, directory):
    """Prepare all entries locally; never authenticate or submit a remote job."""
    config = load_batch(config_path)
    directory = Path(directory).expanduser().resolve()
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError:
        raise ToolError("Batch directory already exists; use batch-resume.") from None
    manifest = {"version": 1, "created_at": now(), "state": "preparing", "config": config,
                "config_fingerprint": _canonical(config), "last_start_unix": 0,
                "jobs": [{"name": job["name"], "directory": str(directory / "jobs" / job["name"]),
                          "state": "pending", "started": False} for job in config["jobs"]]}
    with run_lock(directory):
        _save(directory, manifest)
        _prepare(directory, manifest)
    return _summary(directory, manifest)


def _prepare(directory, manifest):
    from . import build_command, managed
    for job, specification in zip(manifest["jobs"], manifest["config"]["jobs"]):
        if job["state"] not in ("pending", "preparing_local"):
            continue
        output = Path(job["directory"])
        # A process may have stopped inside chemistry conversion. Never re-run
        # that conversion over existing files or infer that partial inputs passed.
        if job["state"] == "preparing_local" and (output.exists() or (output.parent / ("." + output.name + ".sources")).exists()):
            try:
                complete = json.loads((output / "full-run.json").read_text(encoding="utf-8"))
                files = complete["inputs"]["files"]
                required = ("protein.pdb", "ligand.pdb", "ligand.sdf", "complex.pdb", "input-manifest.json")
                if (complete.get("schema_version") != 2 or complete.get("state") != "inputs_ready"
                        or any(not isinstance(files.get(key), str)
                               or Path(files[key]).resolve() != (output / "inputs" / key).resolve()
                               or not Path(files[key]).is_file() for key in required)):
                    raise ValueError
                managed.assign_name(output, _named_job(directory, job["name"]))
                job["state"] = "inputs_ready"
            except (OSError, ValueError, KeyError, TypeError, AttributeError, ToolError):
                job.update(state="local_failed", error="Local preparation was interrupted; retained partial files require inspection. No task was recreated.")
            _save(directory, manifest)
            continue
        else:
            job["state"] = "preparing_local"
            _save(directory, manifest)
            try:
                result, code = build_command.execute(_arguments(output, specification["options"]))
                if code:
                    raise ToolError("Local preparation did not succeed.")
                # Prefix names with stable batch identity to avoid cross-batch collisions.
                managed.assign_name(Path(job["directory"]), _named_job(directory, job["name"]))
                job["state"] = result["state"]
            except (ToolError, OSError) as error:
                job.update(state="local_failed", error=str(error) if isinstance(error, ToolError) else "Local file operation failed.")
            _save(directory, manifest)
    manifest["state"] = "ready"
    _save(directory, manifest)


def _local(job):
    from . import managed
    directory = Path(job["directory"])
    if job["state"] == "local_failed":
        return
    unknown = job.get("error_category") == "submission_unknown"
    if unknown:
        # API attach records a verified existing ID. Merely editing the outer
        # full-build state must never unblock an ambiguous submission.
        try:
            attached = json.loads((directory / "bilayer" / "run.json").read_text())
            recovered = (isinstance(attached, dict) and isinstance(attached.get("job_id"), str)
                         and re.fullmatch(r"[0-9]{1,32}", attached["job_id"])
                         and attached.get("state") in ("submitted", "pending", "running", "done", "remote_error",
                                                       "downloaded_unverified", "ligands_present", "validation_failed"))
        except (OSError, ValueError):
            recovered = False
        if not recovered:
            job.update(state="blocked", started=True)
            return
        job.pop("error_category", None)
    prior_final = job["state"] in ("validated", "validation_failed")
    if prior_final:
        job["state"] = "validation_stale"
    for path in (directory / "full-run.json", directory / "bilayer" / "run.json"):
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            # Final validation is accepted only after evidence is checked below.
            if job["state"] not in TERMINAL and isinstance(value.get("state"), str):
                job["state"] = value["state"]
            for key in ("source_job_id", "build_job_id"):
                if value.get(key):
                    job[key] = value[key]
            if path.name == "run.json" and value.get("job_id"):
                job["build_job_id"] = value["job_id"]
        except (OSError, ValueError, AttributeError):
            job.update(state="blocked", error="Unreadable local task state; inspect before resuming.")
    report_exists = (directory / "results" / "validation.json").is_file()
    if prior_final or job["state"] in ("validated", "validation_failed") or report_exists:
        final = managed._final_summary(directory)
        if final.get("state") in ("validated", "validation_failed"):
            job["state"] = final["state"]
            for key in ("review_required", "archive", "gromacs_compiled", "compilation_status"):
                if key in final:
                    job[key] = final[key]
                else:
                    job.pop(key, None)
            if final.get("report_file"):
                job["report"] = final["report_file"]
            job.setdefault("compilation_status", "unknown")
        else:
            job["state"] = "validation_stale"
            job["compilation_status"] = "stale"
            for key in ("validation", "gromacs_compiled", "gromacs_note", "review_required"):
                job.pop(key, None)
    # pending is also a real remote status. A user may have advanced a later
    # named job independently of this batch, so infer occupancy from durable
    # submission evidence, never from the display status alone.
    if (job.get("source_job_id") or job.get("build_job_id")
            or (directory / "bilayer" / "run.json").is_file()
            or (directory / "preparation" / "web-run.json").is_file()
            or any((directory / "preparation").glob("*.intent.json"))
            or job["state"] not in ("pending", "preparing_local", "inputs_ready")):
        job["started"] = True
    if job["state"] not in TERMINAL and time.time() < job.get("next_retry_unix", 0):
        job["state"] = "waiting_network"


def _summary(directory, manifest):
    jobs = [{key: job[key] for key in ("name", "directory", "state", "error", "error_category", "cause_category", "next_retry_unix",
                                      "source_job_id", "build_job_id", "remote", "gromacs_compiled", "compilation_status",
                                      "gromacs_note", "review_required", "report", "archive", "validation") if key in job}
            for job in manifest["jobs"]]
    counts = {}
    for job in jobs:
        job["named_job"] = _named_job(directory, job["name"])
        counts[job["state"]] = counts.get(job["state"], 0) + 1
    return {"directory": str(directory), "state": manifest["state"], "counts": counts, "jobs": jobs,
            "total": len(jobs), "failed": sum(counts.get(state, 0) for state in TERMINAL - {"validated"}),
            "validated": counts.get("validated", 0),
            "reports": {"json": str(directory / "summary.json"), "csv": str(directory / "summary.csv")},
            "next_step": "charmm-gui-cli batch-resume " + str(directory)}


def _auth_error(error):
    from .api import AuthError
    return isinstance(error, AuthError) or getattr(error, "category", None) == "authentication" or getattr(error, "cause_category", None) in ("authentication", "auth_required") or any(word in str(error).lower() for word in
        ("session expired", "session is missing", "no api token", "token file", "jwt", "authentication", "web-login"))


def advance(directory, *, max_active=1, submit_interval=30, wait=True, interval=30, max_wait=21600,
            token_file=None, cookies=None, grompp=False, no_grompp=False, gmx="gmx"):
    """Advance no-wait rounds; active covers preparation through final validation."""
    from . import build_command
    if grompp and not shutil.which(gmx):
        raise ToolError("Batch --grompp requires a GROMACS executable; no remote tasks were advanced.", category="local_dependency")
    if type(max_active) is not int or not 1 <= max_active <= 4:
        raise ToolError("max-active must be an integer from 1 to 4.")
    for value, minimum, label in ((submit_interval, 0, "submit-interval"), (interval, 10, "interval"), (max_wait, 0, "max-wait")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum or (label == "max-wait" and value == 0):
            raise ToolError(f"Invalid batch {label}.")
    directory = Path(directory).expanduser().resolve()
    deadline = time.monotonic() + max_wait
    with run_lock(directory):
        manifest = _read(directory)
        if any(j["state"] in ("pending", "preparing_local") for j in manifest["jobs"]):
            _prepare(directory, manifest)
        manifest["state"] = "running"
        while True:
            if time.monotonic() >= deadline:
                manifest["state"] = "waiting_network" if any(j["state"] == "waiting_network" for j in manifest["jobs"]) else "waiting"
                _save(directory, manifest)
                return _summary(directory, manifest), 2 if manifest["state"] == "waiting_network" else 0
            for job in manifest["jobs"]:
                _local(job)
            if any(j["state"] == "blocked" and j.get("error_category") == "submission_unknown" for j in manifest["jobs"]):
                manifest["state"] = "paused_inspection"
                _save(directory, manifest)
                return _summary(directory, manifest), 2
            active = sum(j.get("started", False) and j["state"] not in TERMINAL for j in manifest["jobs"])
            network_waiting = False
            for job in manifest["jobs"]:
                if job["state"] in TERMINAL:
                    continue
                if time.time() < job.get("next_retry_unix", 0):
                    job["state"] = "waiting_network"
                    network_waiting = True
                    continue
                if not job.get("started"):
                    if active >= max_active:
                        continue
                    remaining = submit_interval - (time.time() - manifest.get("last_start_unix", 0))
                    if remaining > 0:
                        continue
                    job["started"] = True
                    manifest["last_start_unix"] = time.time()
                    active += 1
                # Persist slot/intent before entering a potentially submitting operation.
                _save(directory, manifest)
                try:
                    result, code = build_command.execute(_arguments(Path(job["directory"]), interval=interval,
                        max_wait=max_wait, token_file=token_file, cookies=cookies, grompp=grompp, no_grompp=no_grompp, gmx=gmx))
                    job.update({k: result[k] for k in ("state", "source_job_id", "build_job_id", "gromacs_compiled",
                                                       "gromacs_note", "report", "archive", "validation") if k in result})
                    if isinstance(result.get("validation"), dict):
                        job["review_required"] = result["validation"].get("review_required", False)
                    if "gromacs_compiled" in result:
                        job["compilation_status"] = "passed" if result["gromacs_compiled"] else "not_run" if result.get("gromacs_note") else "failed"
                    job.pop("error", None)
                    job.pop("error_category", None)
                    job.pop("cause_category", None)
                    job.pop("next_retry_unix", None)
                    if code and job["state"] not in TERMINAL:
                        job.update(state="blocked", error="Build needs manual inspection before continuing.")
                except (ToolError, OSError) as error:
                    _local(job)
                    job["error"] = str(error) if isinstance(error, ToolError) else "Local file operation failed."
                    job["error_category"] = getattr(error, "category", "local_io")
                    if getattr(error, "cause_category", None):
                        job["cause_category"] = error.cause_category
                    if _auth_error(error):
                        manifest["state"] = "paused_auth"
                        _save(directory, manifest)
                        return _summary(directory, manifest), 2
                    if (getattr(error, "retryable", False) and getattr(error, "category", "") in ("transient_network", "transient_http", "rate_limited")):
                        job["state"] = "waiting_network"
                        network_waiting = True
                        delay = getattr(error, "retry_after_seconds", None)
                        if isinstance(delay, (int, float)) and not isinstance(delay, bool) and math.isfinite(delay) and delay > 0:
                            job["next_retry_unix"] = time.time() + delay
                    elif job["state"] not in TERMINAL:
                        # Unknown remote lifetime remains active. Do not free its slot.
                        job["state"] = "blocked"
                if job["state"] in TERMINAL:
                    active -= 1
                _save(directory, manifest)
                if job["state"] == "blocked" or (not wait and job["state"] == "waiting_network"):
                    break
            if all(j["state"] in TERMINAL for j in manifest["jobs"]):
                manifest["state"] = "complete"
            elif any(j["state"] == "blocked" for j in manifest["jobs"]):
                manifest["state"] = "paused_inspection"
            elif network_waiting and (not wait or time.monotonic() >= deadline):
                manifest["state"] = "waiting_network"
            elif time.monotonic() >= deadline:
                manifest["state"] = "waiting"
            _save(directory, manifest)
            if manifest["state"] != "running" or not wait:
                summary = _summary(directory, manifest)
                failed = any(j["state"] in TERMINAL - {"validated"} for j in manifest["jobs"])
                return summary, 2 if failed or manifest["state"].startswith("paused") or manifest["state"] == "waiting_network" else 0
            time.sleep(min(interval, max(0, deadline - time.monotonic())))


def status(directory, *, remote_check=False, token_file=None):
    """Read local state; optional API GET only, never advance/save/download."""
    directory = Path(directory).expanduser().resolve()
    manifest = _read(directory, verify=False)
    client = None
    if remote_check:
        from .auth import load_token
        from .api import Client
        client = Client(load_token(token_file)[0])
    for job in manifest["jobs"]:
        _local(job)
        identifier = job.get("build_job_id") or job.get("source_job_id")
        if client is not None and identifier:
            try:
                result = client.status(identifier)
                job["remote"] = {key: result[key] for key in ("status", "hasTarFile") if key in result}
            except ToolError as error:
                if _auth_error(error):
                    raise
                job["error"] = str(error)
    return _summary(directory, manifest)


def export_csv(summary):
    output = io.StringIO()
    columns = ("name", "named_job", "state", "directory", "source_job_id", "build_job_id", "compilation_status",
               "gromacs_compiled", "review_required", "report", "archive", "error")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for job in summary["jobs"]:
        # CSV consumers must not interpret any user-supplied value as a formula.
        writer.writerow({k: "'" + str(v) if str(v).startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in job.items()})
    return output.getvalue()
