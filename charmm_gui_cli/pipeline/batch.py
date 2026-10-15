"""Durable, bounded multi-build queue. Every job is prepared before networking."""

import charmm_gui_cli.store.tasks as _task_store

from argparse import Namespace
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import shlex
import time
from contextlib import nullcontext
from datetime import datetime, timezone

import yaml

from charmm_gui_cli.store.auth import ToolError, atomic_json, atomic_write, recovery_guidance
from charmm_gui_cli.pipeline.config import UniqueLoader
from charmm_gui_cli.store.configuration import mapping, composition, number
from charmm_gui_cli.store.locking import run_lock, now
from charmm_gui_cli.chem.pdb_normalization import N_TERMINALS, C_TERMINALS
from charmm_gui_cli.store.layout import inputs_path
from ..locales.i18n import t
from ..chem.residues import INPUT_FIELDS as INPUT_SELECTION, validate_input_options


FIELDS = ("protein", "ligand", "complex", "ligand_resname", "ligand_smiles", "terminals", "allow_unknown_lipids",
          "accept_conect_bond_orders", "hydrogens", "upper", "lower", "salt",
          "salt_concentration", "margin", "water_padding", "orientation", "n_terminal", "c_terminal") + INPUT_SELECTION
TERMINAL = {"validated", "validation_failed", "preparation_failed", "remote_error", "local_failed"}
DEFAULTS = {"ligand_resname": "LIG", "accept_conect_bond_orders": False, "hydrogens": "add_missing",
            "upper": "POPC=1", "lower": "POPC=1", "salt": "KCl", "salt_concentration": 0.15,
            "margin": 20, "water_padding": 22.5, "orientation": "ppm", "n_terminal": "NTER", "c_terminal": "CTER"}


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return _hash(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _named_job(directory, name):
    paths = (directory / "jobs" / name / ".charmm-gui" / "job.json", directory / "jobs" / name / "job.json")
    for local in paths:
        if not local.is_file():
            continue
        try:
            stored = json.loads(local.read_text())["name"]
            from charmm_gui_cli.store.tasks import validate_name
            return validate_name(stored)
        except (OSError, ValueError, KeyError, TypeError, ToolError):
            pass
    kind = "sweep" if (directory.parent / "sweep.json").is_file() else "batch"
    return kind + "-" + _hash(str(directory).encode())[:12] + "-" + name


def _options(raw, base):
    result = dict(raw)
    selected = {key: value for key, value in raw.items() if key in INPUT_SELECTION}
    validate_input_options(selected)
    for key, value in result.items():
        if key in INPUT_SELECTION:
            continue
        if key == "terminals":
            continue
        if key in ("accept_conect_bond_orders", "allow_unknown_lipids"):
            if type(value) is not bool:
                raise ToolError(key="batch.option_boolean", params={"option": key}, next_step="charmm-gui-cli batch -h")
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
               "orientation": ("ppm", "prepared")}
    for key, allowed in choices.items():
        if key in result and result[key] not in allowed:
            raise ToolError(f"Unsupported batch {key}.")
    from charmm_gui_cli.pipeline.build_config import validate_preparation
    validate_preparation({key: result[key] for key in ("n_terminal", "c_terminal", "terminals") if key in result})
    if "ligand_resname" in result and not re.fullmatch(r"[A-Z][A-Z0-9]{0,2}", result["ligand_resname"]):
        raise ToolError("ligand_resname must be 1-3 uppercase letters/digits beginning with a letter.")
    return result


def _v2_options(raw, base):
    """Flatten only at the CLI adapter; YAML uses build's nested v2 sections."""
    top = ("protein", "ligand", "complex", "ligand_resname", "ligand_smiles", "accept_conect_bond_orders",
           "ligand_hydrogens", "preparation", "membrane", "ions", "input_options")
    def checked(value, fields, section):
        try:
            return mapping(value, fields, section)
        except ToolError:
            raise ToolError(key="batch.option_fields", params={"section": section, "fields": ", ".join(fields)}, next_step="charmm-gui-cli batch -h") from None
    raw = checked(raw, top, "v2")
    result = {k: v for k, v in raw.items() if k in FIELDS}
    if "ligand_hydrogens" in raw:
        result["hydrogens"] = raw["ligand_hydrogens"]
    for section, fields, aliases in (
        ("preparation", ("orientation", "n_terminal", "c_terminal", "terminals"), {}),
        ("membrane", ("upper", "lower", "margin_A", "water_padding_A", "allow_unknown_lipids"), {"margin_A": "margin", "water_padding_A": "water_padding"}),
        ("ions", ("type", "concentration_M"), {"type": "salt", "concentration_M": "salt_concentration"}),
        ("input_options", INPUT_SELECTION, {}),
    ):
        if section in raw:
            selected = checked(raw[section], fields, section)
            result.update({aliases.get(k, k): v for k, v in selected.items()})
    return _options(result, base)


def load_batch(path):
    path = Path(path).expanduser().resolve()
    try:
        data = path.read_bytes()
        raw = yaml.load(data.decode("utf-8"), Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ToolError("Cannot read valid batch YAML.") from None
    mapping(raw, ("version", "defaults", "jobs", "reuse_preparation", "name"), "batch")
    if "reuse_preparation" in raw and type(raw["reuse_preparation"]) is not bool:
        raise ToolError("reuse_preparation must be boolean.")
    if type(raw.get("version")) is not int or raw["version"] not in (1, 2):
        raise ToolError(key="batch.config_version", next_step="charmm-gui-cli batch -h")
    if "name" in raw:
        from charmm_gui_cli.store.tasks import validate_name
        validate_name(raw["name"])
    adapter = _v2_options if raw["version"] == 2 else lambda value, base: _options(mapping(value, FIELDS, "batch defaults"), base)
    defaults = adapter(raw.get("defaults", {}), path.parent)
    if not isinstance(raw.get("jobs"), list) or not raw["jobs"]:
        raise ToolError("Batch jobs must be a nonempty list.")
    jobs, names = [], set()
    for raw_job in raw["jobs"]:
        if not isinstance(raw_job, dict):
            raise ToolError(key="batch.jobs_invalid", next_step="charmm-gui-cli batch -h")
        name = raw_job.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ToolError("Batch names must be 1-64 ASCII letters/digits, dots, underscores or hyphens, starting with a letter/digit.")
        if name in names:
            raise ToolError("Batch job names must be unique.")
        names.add(name)
        override = adapter({k: v for k, v in raw_job.items() if k != "name"}, path.parent)
        base_defaults = {**DEFAULTS, "hydrogens": "preserve"} if raw["version"] == 2 else DEFAULTS
        options = {**base_defaults, **defaults, **override}
        if "terminals" in defaults and "terminals" in override:
            options["terminals"] = {chain: {**defaults["terminals"].get(chain, {}), **override["terminals"].get(chain, {})}
                                    for chain in defaults["terminals"].keys() | override["terminals"].keys()}
        if not options.get("complex") and not (options.get("protein") and options.get("ligand")):
            raise ToolError("Each batch job needs protein + ligand, or complex.")
        jobs.append({"name": name, "options": options})
    return {"version": raw["version"], "source": str(path), "source_sha256": _hash(data), "jobs": jobs,
            **({"name": raw["name"]} if "name" in raw else {}),
            **({"reuse_preparation": raw["reuse_preparation"]} if "reuse_preparation" in raw else {})}


def _arguments(directory, options=None, **kwargs):
    fields = {key: None for key in FIELDS}
    fields.update(options or {})
    fields["complex_pdb"] = fields.pop("complex")
    for key in ("protein", "ligand", "complex_pdb"):
        if fields[key] is not None:
            fields[key] = Path(fields[key])
    return Namespace(**fields, command="build" if options is not None else "build-resume",
                     _progress_callback=kwargs.get("progress"),
                     config=None, out=directory, directory=directory, name=None, dry_run=options is not None,
                     wait=False, interval=kwargs.get("interval", 30), max_wait=kwargs.get("max_wait", 21600),
                     post_timeout=kwargs.get("post_timeout", 900),
                     token_file=kwargs.get("token_file"), cookies=kwargs.get("cookies"),
                     grompp=kwargs.get("grompp", False), no_grompp=kwargs.get("no_grompp", False),
                     gmx=kwargs.get("gmx", "gmx"), no_preview=kwargs.get("no_preview", False))


def _save(directory, manifest):
    manifest["updated_at"] = now()
    atomic_json(directory / "batch.json", manifest)
    summary = _summary(directory, manifest)
    atomic_json(directory / "summary.json", summary)
    atomic_write(directory / "summary.csv", export_csv(summary))
    from .batch_progress import current
    if current() is not None:
        current().update(summary)


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
        if "register_tasks" in manifest and type(manifest["register_tasks"]) is not bool:
            raise ValueError
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
            if any(key in job and type(job[key]) is not bool for key in ("indexed", "preparation_reused")):
                raise ValueError
            if "error_detail" in job and (not isinstance(job["error_detail"], dict)
                    or "message_params" in job["error_detail"] and not isinstance(job["error_detail"]["message_params"], dict)):
                raise ValueError
            if "scheduling_recoveries" in job and (not isinstance(job["scheduling_recoveries"], list)
                    or any(not isinstance(item, dict) or not isinstance(item.get("params"), dict)
                           for item in job["scheduling_recoveries"])):
                raise ValueError
            if "next_retry_unix" in job:
                number(job["next_retry_unix"], "saved next retry timestamp")
            expected = directory / "jobs" / job["name"]
            if Path(job["directory"]).resolve() not in (expected.resolve(), (expected / ".charmm-gui").resolve()):
                raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ToolError("Invalid batch manifest or fingerprint; refusing to guess task ownership.") from None
    return manifest


def initialize(config_path, directory, *, register=True, progress=None, name=None):
    """Prepare all entries locally; never authenticate or submit a remote job."""
    config = load_batch(config_path)
    directory = Path(directory).expanduser().resolve()
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError:
        raise ToolError("Batch directory already exists; use batch-resume.") from None
    manifest = {"version": 1, "created_at": now(), "state": "preparing", "config": config,
                "name": name or config.get("name", directory.name),
                "register_tasks": register,
                "config_fingerprint": _canonical(config), "last_start_unix": 0,
                "jobs": [{"name": job["name"], "directory": str(directory / "jobs" / job["name"] / ".charmm-gui"),
                          "state": "pending", "started": False} for job in config["jobs"]]}
    with run_lock(directory):
        _save(directory, manifest)
        _prepare(directory, manifest, progress=progress)
    return _summary(directory, manifest)


def _prepare(directory, manifest, *, progress=None):
    from ..store.indexing import local_plan
    with nullcontext() if manifest.get("register_tasks", True) else local_plan():
        _prepare_entries(directory, manifest, progress=progress)


def _prepare_entries(directory, manifest, *, progress=None):
    from charmm_gui_cli.pipeline import build_command
    from charmm_gui_cli.pipeline import managed
    for job, specification in zip(manifest["jobs"], manifest["config"]["jobs"]):
        if job["state"] not in ("pending", "preparing_local"):
            continue
        if time.time() < job.get("next_retry_unix", 0):
            continue
        output = Path(job["directory"])
        from ..store.local_build import _remote_evidence
        if (job.get("started") or job.get("source_job_id") or job.get("build_job_id")
                or _remote_evidence(output)):
            # "pending" is also the provider's queue state. Submitted children
            # must resume their existing task, never enter fresh input staging.
            _local(job)
            continue
        # A process may have stopped inside chemistry conversion. Never re-run
        # that conversion over existing files or infer that partial inputs passed.
        if job["state"] == "preparing_local" and (output.exists() or (output.parent / ("." + output.name + ".sources")).exists()):
            try:
                from ..store.paths import read_value
                complete = read_value(output / "full-run.json")
                files = complete["inputs"]["files"]
                required = ("protein.pdb", "ligand.pdb", "ligand.sdf", "complex.pdb", "input-manifest.json")
                if (complete.get("schema_version") != 2 or complete.get("state") != "inputs_ready"
                        or any(not isinstance(files.get(key), str)
                               or Path(files[key]).resolve() != (inputs_path(output) / key).resolve()
                               or not Path(files[key]).is_file() for key in required)):
                    raise ValueError
                _task_store.assign_name(output, _named_job(directory, job["name"]))
                job["state"] = "inputs_ready"
                if manifest["config"].get("reuse_preparation"):
                    from charmm_gui_cli.pipeline.sweep import seal_preparation
                    seal_preparation(output)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, ToolError) as error:
                if getattr(error, "category", None) == "lock_busy":
                    _local_wait(job, error)
                else:
                    job.update(state="local_failed", error="Local preparation was interrupted; retained partial files require inspection. No task was recreated.")
            else:
                for key in ("error", "error_detail", "error_category", "next_retry_unix"):
                    job.pop(key, None)
            _save(directory, manifest)
            continue
        else:
            job["state"] = "preparing_local"
            _save(directory, manifest)
            try:
                result, code = build_command.execute(_arguments(output, specification["options"], progress=progress.child(job) if progress else None))
                if code:
                    raise ToolError("Local preparation did not succeed.")
                # Prefix names with stable batch identity to avoid cross-batch collisions.
                _task_store.assign_name(Path(job["directory"]), _named_job(directory, job["name"]))
                job["state"] = result["state"]
                if manifest["config"].get("reuse_preparation"):
                    from charmm_gui_cli.pipeline.sweep import seal_preparation
                    seal_preparation(output)
            except (ToolError, OSError) as error:
                if getattr(error, "category", None) == "lock_busy":
                    _local_wait(job, error)
                else:
                    job.update(state="local_failed", error=str(error) if isinstance(error, ToolError) else "Local file operation failed.",
                               local_failure_phase="preparation")
                    if isinstance(error, ToolError):
                        job["error_detail"] = error.as_dict()
                    if progress:
                        progress.failure(job)
            _save(directory, manifest)
    if manifest["state"] != "running":
        manifest["state"] = "ready"
    _save(directory, manifest)


def _local_wait(job, error, *, state="preparing_local"):
    job.update(state=state, error=str(error), error_category="lock_busy", error_detail=error.as_dict(),
               next_retry_unix=time.time() + max(10, error.retry_after_seconds or 10))


def _register_children(directory, manifest):
    if manifest.get("register_tasks") is not False:
        return
    from charmm_gui_cli.store.tasks import assign_name
    for job in manifest["jobs"]:
        if job["state"] in ("local_failed", "pending", "preparing_local") or job.get("indexed") or time.time() < job.get("next_retry_unix", 0):
            continue
        try:
            assign_name(Path(job["directory"]), _named_job(directory, job["name"]))
        except ToolError as error:
            if error.category != "lock_busy":
                raise
            _local_wait(job, error, state="waiting_index")
        else:
            if job["state"] == "waiting_index":
                job["state"] = "inputs_ready"
            for key in ("error", "error_detail", "error_category", "next_retry_unix"):
                job.pop(key, None)
            job["indexed"] = True
        _save(directory, manifest)
    manifest["register_tasks"] = all(j.get("indexed") or j["state"] == "local_failed" for j in manifest["jobs"])


def _local(job):
    from charmm_gui_cli.pipeline import managed
    directory = Path(job["directory"])
    if job["state"] in ("preparing_local", "waiting_index") and job.get("error_category") == "lock_busy":
        return  # Local preparation/registration must finish before submission.
    if job["state"] == "local_failed":
        # Recover only the historical lock-conflict failure, not arbitrary local
        # preparation failures or unknown submissions.
        if job.get("error_category") == "lock_busy" or job.get("error") == "Another process is operating on this run.":
            job["state"] = "waiting_lock"
            job["error_category"] = "lock_busy"
        elif _recover_queued_child(job):
            pass
        else:
            return
    unknown = job.get("error_category") == "submission_unknown"
    if unknown:
        # API attach records a verified existing ID. Merely editing the outer
        # full-build state must never unblock an ambiguous submission.
        try:
            from ..store.paths import read_value
            attached = read_value(directory / "bilayer" / "run.json")
            recovered = (isinstance(attached, dict) and isinstance(attached.get("job_id"), str)
                         and re.fullmatch(r"[0-9]{1,32}", attached["job_id"])
                         and attached.get("state") in ("submitted", "pending", "running", "done", "remote_error",
                                                       "archive_preparing", "downloading", "downloaded_unverified", "ligands_present", "validation_failed"))
        except (OSError, ValueError):
            recovered = False
        if not recovered:
            job.update(state="blocked", started=True)
            return
        job.pop("error_category", None)
    prior_final = job["state"] in ("validated", "validation_failed")
    reused_source = False
    if prior_final:
        job["state"] = "validation_stale"
    for path in (directory / "full-run.json", directory / "bilayer" / "run.json"):
        if not path.is_file():
            continue
        try:
            from ..store.paths import read_value
            value = read_value(path)
            if path.name == "full-run.json":
                reused_source = bool(value.get("reused_preparation"))
                job["preparation_reused"] = reused_source
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
    if ((job.get("source_job_id") and not reused_source) or job.get("build_job_id")
            or (directory / "bilayer" / "run.json").is_file()
            or (directory / "preparation" / "web-run.json").is_file()
            or any((directory / "preparation").glob("*.intent.json"))
            or job["state"] not in ("pending", "preparing_local", "inputs_ready", "prepared_reused", "waiting_preparation")):
        job["started"] = True
    if job["state"] not in TERMINAL and time.time() < job.get("next_retry_unix", 0):
        job["state"] = ("waiting_lock" if job.get("error_category") == "lock_busy" else
                        "archive_preparing" if job.get("error_category") == "archive_preparing" else "waiting_network")
    elif job["state"] not in TERMINAL and job.get("error_category") == "transport_error":
        job["state"] = "paused_network"


def _recover_queued_child(job):
    """Repair the known scheduler misclassification, never retry failed inputs.

    Require matching persisted build IDs and a received remote response. Any
    ambiguous intent, unrelated failure or inconsistent record remains stopped.
    The caller only updates row metadata; no task evidence is rewritten.
    """
    if (job.get("local_failure_phase") != "preparation"
            or job.get("error") != "Input staging for this build already exists; inspect it before starting a new run."
            or job.get("error_category") == "submission_unknown"
            or job.get("error_detail", {}).get("category") == "submission_unknown"):
        return False
    from ..store.paths import read_value
    directory = Path(job["directory"])
    try:
        full = read_value(directory / "full-run.json")
        remote = read_value(directory / "bilayer/run.json")
        identifier = remote.get("job_id")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{1,32}", identifier)
                or job.get("build_job_id") != identifier or full.get("build_job_id") != identifier
                or remote.get("state") not in ("submitted", "pending", "running", "done", "archive_preparing",
                                               "downloading", "downloaded_unverified", "ligands_present")
                or full.get("state") in ("submission_unknown", "submitting", "remote_error", "preparation_failed", "validation_failed")):
            return False
        for path in [directory / "preparation/web-run.json", *directory.rglob("*.intent.json")]:
            if path.is_file() and read_value(path).get("state") in ("submitting", "submission_unknown", "upload_submitting"):
                return False
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    job.setdefault("scheduling_recoveries", []).append({"at": now(), "key": "batch.queued_recovered",
        "params": {"jobid": identifier}, "previous_error": job["error"], "previous_error_detail": job.get("error_detail")})
    job.update(state=remote["state"], started=True)
    for key in ("error", "error_detail", "error_category", "cause_category", "next_step", "next_retry_unix", "local_failure_phase"):
        job.pop(key, None)
    return True


def _summary(directory, manifest):
    jobs = [{key: job[key] for key in ("name", "directory", "state", "error", "error_category", "cause_category", "next_retry_unix", "next_step",
                                      "source_job_id", "build_job_id", "remote", "gromacs_compiled", "compilation_status",
                                      "gromacs_note", "review_required", "report", "archive", "validation", "error_detail", "local_failure_phase", "preparation_reused", "scheduling_recoveries") if key in job}
            for job in manifest["jobs"]]
    counts = {}
    for job in jobs:
        job["named_job"] = _named_job(directory, job["name"])
        counts[job["state"]] = counts.get(job["state"], 0) + 1
    resume = "charmm-gui-cli batch-resume " + shlex.quote(str(directory))
    sweep_path = directory.parent / "sweep.json"
    if sweep_path.is_file():
        try:
            saved = json.loads(sweep_path.read_text())
            if Path(saved["batch_directory"]).resolve() == directory.resolve():
                resume = "charmm-gui-cli sweep-resume " + shlex.quote(str(directory.parent))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    state = manifest["state"]
    unknown = any(job.get("error_category") == "submission_unknown" for job in jobs)
    if unknown:
        guidance = recovery_guidance("submission_unknown", cause_category="authentication" if state == "paused_auth" else None) + t("batch.next.inspection", resume=resume)
    elif state == "paused_auth":
        guidance = t("batch.next.authentication", resume=resume)
    elif state == "paused_network":
        guidance = t("batch.next.network", resume=resume)
    elif state == "waiting_network":
        guidance = t("batch.next.waiting", resume=resume)
    elif state == "archive_preparing":
        guidance = t("batch.next.archive", resume=resume)
    elif state == "interrupted":
        guidance = t("batch.next.interrupted", resume=resume)
    else:
        guidance = resume
    try:
        begin = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(manifest.get("finished_at", manifest.get("updated_at", "")).replace("Z", "+00:00")).timestamp() if state == "complete" else time.time()
        elapsed_seconds = max(0, end - begin)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
        elapsed_seconds = 0
    return {"directory": str(directory), "state": state, "counts": counts, "jobs": jobs,
            "name": manifest.get("name", manifest["config"].get("name", directory.name)), "elapsed_seconds": elapsed_seconds,
            "completed": sum(counts.get(s, 0) for s in TERMINAL),
            "active": sum(j.get("started", False) and j["state"] not in TERMINAL for j in manifest["jobs"]),
            "preparation_reused": sum(j.get("preparation_reused", False) for j in manifest["jobs"]),
            "total": len(jobs), "failed": sum(counts.get(state, 0) for state in TERMINAL - {"validated"}),
            "validated": counts.get("validated", 0),
            "reports": {"json": str(directory / "summary.json"), "csv": str(directory / "summary.csv")},
            "next_step": guidance}


def _auth_error(error):
    from charmm_gui_cli.remote.api import AuthError
    return isinstance(error, AuthError) or getattr(error, "category", None) in ("authentication", "auth_required") or getattr(error, "cause_category", None) in ("authentication", "auth_required") or any(word in str(error).lower() for word in
        ("session expired", "session is missing", "no api token", "token file", "jwt", "authentication", "web-login"))


def _retry_local(directory, manifest):
    """Retry only pre-upload failures whose failed local attempt left no root.

    Existing partial structures/foreign files are retained for manual review;
    neither unknown submissions nor remote failures become fresh local plans.
    """
    from ..store.layout import task_root
    from ..store.local_build import _remote_evidence
    candidates = [j for j in manifest["jobs"] if j["state"] == "local_failed"]
    for job in candidates:
        root = task_root(Path(job["directory"]))
        command = "charmm-gui-cli batch-status " + shlex.quote(str(directory))
        if (job.get("started") or job.get("source_job_id") or job.get("build_job_id")
                or job.get("error_category") == "submission_unknown" or _remote_evidence(Path(job["directory"]))):
            raise ToolError(key="batch.retry_remote", params={"name": job["name"]}, next_step=command)
        if root.exists() or root.is_symlink():
            raise ToolError(key="batch.retry_partial", params={"name": job["name"], "path": str(root)}, next_step=command)
    for job in candidates:
        job.setdefault("local_attempts", []).append({"at": now(), "error": job.get("error"), "error_detail": job.get("error_detail")})
        for key in ("error", "error_detail", "error_category", "cause_category", "next_step", "next_retry_unix", "local_failure_phase"):
            job.pop(key, None)
        job.update(state="pending", started=False)
    _save(directory, manifest)


@contextmanager
def _record_interruption(directory, manifest):
    try:
        yield
    except KeyboardInterrupt:
        for job in manifest["jobs"]:
            _local(job)
            uncertain = job["state"] in ("submission_unknown", "submitting", "upload_submitting")
            preparation = Path(job["directory"]) / "preparation"
            for path in [preparation / "web-run.json", *preparation.glob("*.intent.json")]:
                if path.is_file():
                    try:
                        from ..store.paths import read_value
                        value = read_value(path)
                        uncertain = uncertain or value.get("state") in ("submission_unknown", "submitting", "upload_submitting")
                    except (OSError, ValueError, AttributeError):
                        # An unreadable existing submission record cannot prove
                        # that the interrupted POST was never sent.
                        uncertain = True
            if uncertain:
                job.update(error_category="submission_unknown", state="blocked", started=True,
                           next_step=recovery_guidance("submission_unknown"))
        manifest["state"] = "interrupted"
        _save(directory, manifest)
        raise


def advance(directory, *, max_active=1, submit_interval=30, wait=True, interval=30, max_wait=21600,
            token_file=None, cookies=None, grompp=False, no_grompp=False, gmx="gmx", no_preview=False, post_timeout=900,
            progress=None, retry_failed_local=False):
    """Advance no-wait rounds; active covers preparation through final validation."""
    from charmm_gui_cli.pipeline import build_command
    if grompp and not shutil.which(gmx):
        raise ToolError("Batch --grompp requires a GROMACS executable; no remote tasks were advanced.", category="local_dependency")
    if type(max_active) is not int or not 1 <= max_active <= 4:
        raise ToolError("max-active must be an integer from 1 to 4.")
    for value, minimum, label in ((submit_interval, 0, "submit-interval"), (interval, 10, "interval"), (max_wait, 0, "max-wait")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum or (label == "max-wait" and value == 0):
            raise ToolError(f"Invalid batch {label}.")
    directory = Path(directory).expanduser().resolve()
    deadline = time.monotonic() + max_wait
    from .batch_progress import observe
    with observe(progress), run_lock(directory), _record_interruption(directory, manifest := _read(directory)):
        if retry_failed_local:
            _retry_local(directory, manifest)
        completed_at = (manifest.get("finished_at") or manifest.get("updated_at")) if manifest["state"] == "complete" else None
        manifest["state"] = "running"
        while True:
            if time.monotonic() >= deadline:
                manifest["state"] = ("waiting_network" if any(j["state"] == "waiting_network" for j in manifest["jobs"]) else
                                     "archive_preparing" if any(j["state"] == "archive_preparing" for j in manifest["jobs"]) else "waiting")
                _save(directory, manifest)
                return _summary(directory, manifest), 2 if manifest["state"] in ("waiting_network", "archive_preparing") else 0
            if any(j["state"] in ("pending", "preparing_local") for j in manifest["jobs"]):
                _prepare(directory, manifest, progress=progress)
            _register_children(directory, manifest)
            for job in manifest["jobs"]:
                _local(job)
            if any(j["state"] not in TERMINAL for j in manifest["jobs"]):
                completed_at = None
                manifest.pop("finished_at", None)
            if manifest["config"].get("reuse_preparation"):
                from charmm_gui_cli.pipeline.sweep import bind_ready_sources
                bind_ready_sources(directory, manifest)
            if any(j["state"] == "blocked" and j.get("error_category") == "submission_unknown" for j in manifest["jobs"]):
                manifest["state"] = "paused_inspection"
                _save(directory, manifest)
                return _summary(directory, manifest), 2
            active = sum(j.get("started", False) and j["state"] not in TERMINAL for j in manifest["jobs"])
            network_waiting = False
            for job in manifest["jobs"]:
                if job["state"] in TERMINAL:
                    continue
                if job["state"] in ("preparing_local", "waiting_index"):
                    continue
                if job.get("reuse_waiting"):
                    continue
                if time.time() < job.get("next_retry_unix", 0):
                    job["state"] = ("waiting_lock" if job.get("error_category") == "lock_busy" else
                                    "archive_preparing" if job.get("error_category") == "archive_preparing" else "waiting_network")
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
                        max_wait=max_wait, token_file=token_file, cookies=cookies, grompp=grompp, no_grompp=no_grompp, gmx=gmx, no_preview=no_preview, post_timeout=post_timeout,
                        progress=progress.child(job) if progress else None))
                    job.update({k: result[k] for k in ("state", "source_job_id", "build_job_id", "gromacs_compiled",
                                                       "gromacs_note", "report", "archive", "validation") if k in result})
                    if isinstance(result.get("validation"), dict):
                        job["review_required"] = result["validation"].get("review_required", False)
                    if "gromacs_compiled" in result:
                        job["compilation_status"] = "passed" if result["gromacs_compiled"] else "not_run" if result.get("gromacs_note") else "failed"
                    job.pop("error", None)
                    job.pop("error_detail", None)
                    job.pop("error_category", None)
                    job.pop("cause_category", None)
                    job.pop("next_retry_unix", None)
                    job.pop("next_step", None)
                    if code and job["state"] not in TERMINAL:
                        job.update(state="blocked", error="Build needs manual inspection before continuing.")
                except (ToolError, OSError) as error:
                    if getattr(error, "category", None) == "lock_busy" or getattr(error, "key", None) == "background.running":
                        # Lock contention says nothing about the remote result.
                        # Keep the child's pipeline state and slot; retry later.
                        job.update(state="waiting_lock", error=str(error), error_category="lock_busy",
                                   next_retry_unix=time.time() + max(10, getattr(error, "retry_after_seconds", None) or 10))
                        _save(directory, manifest)
                        continue
                    _local(job)
                    job["error"] = str(error) if isinstance(error, ToolError) else "Local file operation failed."
                    job["error_category"] = getattr(error, "category", "local_io")
                    job["next_step"] = getattr(error, "next_step", None) or "charmm-gui-cli jobs status " + shlex.quote(_named_job(directory, job["name"]))
                    if isinstance(error, ToolError):
                        job["error_detail"] = error.as_dict()
                    if progress:
                        progress.failure(job)
                    if getattr(error, "cause_category", None):
                        job["cause_category"] = error.cause_category
                    else:
                        job.pop("cause_category", None)
                    if _auth_error(error):
                        manifest["state"] = "paused_auth"
                        _save(directory, manifest)
                        return _summary(directory, manifest), 2
                    if job["error_category"] == "transport_error":
                        job["state"] = "paused_network"
                        manifest["state"] = "paused_network"
                        _save(directory, manifest)
                        return _summary(directory, manifest), 2
                    if (getattr(error, "retryable", False) and getattr(error, "category", "") in ("transient_network", "transient_http", "rate_limited", "archive_preparing")):
                        job["state"] = "archive_preparing" if error.category == "archive_preparing" else "waiting_network"
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
                if job["state"] == "blocked" or (not wait and job["state"] in ("waiting_network", "archive_preparing")):
                    break
            if all(j["state"] in TERMINAL for j in manifest["jobs"]):
                manifest["state"] = "complete"
                manifest["finished_at"] = completed_at or now()
            elif any(j["state"] == "blocked" for j in manifest["jobs"]):
                manifest["state"] = "paused_inspection"
            elif network_waiting and (not wait or time.monotonic() >= deadline):
                manifest["state"] = "waiting_network" if any(j["state"] == "waiting_network" for j in manifest["jobs"]) else "archive_preparing"
            elif time.monotonic() >= deadline:
                manifest["state"] = "waiting"
            _save(directory, manifest)
            if manifest["state"] != "running" or not wait:
                summary = _summary(directory, manifest)
                failed = any(j["state"] in TERMINAL - {"validated"} for j in manifest["jobs"])
                return summary, 2 if failed or manifest["state"].startswith("paused") or manifest["state"] in ("waiting_network", "archive_preparing") else 0
            time.sleep(min(interval, max(0, deadline - time.monotonic())))


def status(directory, *, remote_check=False, token_file=None):
    """Read local state; optional API GET only, never advance/save/download."""
    directory = Path(directory).expanduser().resolve()
    manifest = _read(directory, verify=False)
    client = None
    if remote_check:
        from charmm_gui_cli.store.auth import load_token
        from charmm_gui_cli.remote.api import Client
        token, source = load_token(token_file)
        client = Client(token)
        from ..remote.sessions import bind
        bind(client, source)
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
               "gromacs_compiled", "review_required", "report", "archive", "error", "error_category", "next_step")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for job in summary["jobs"]:
        # CSV consumers must not interpret any user-supplied value as a formula.
        writer.writerow({k: "'" + str(v) if str(v).startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in job.items()})
    return output.getvalue()
