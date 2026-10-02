"""Private local run management and automatic, reproducible final validation."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from . import __version__
from .acceptance import assess_acceptance
from .auth import ToolError, atomic_json, default_token_path
from .full_build import initialize
from .pose_validation import validate_bound_pose
from .validation import extract_for_validation, inspect_environment, validate_system
from .workflow import run_lock


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolError(f"Cannot read local run metadata: {Path(path).name}.") from None
    if not isinstance(value, dict):
        raise ToolError("Local run metadata must contain a JSON object.")
    return value


def _new_json(path, value):
    """Publish a private JSON file atomically, without replacing another file."""
    path = Path(path)
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


def _registry():
    return default_token_path().parent


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise ToolError("Job name must contain 1-128 ASCII letters/digits, dots, underscores or hyphens, beginning with a letter/digit.")
    return name


def check_name_available(name, directory=None):
    validate_name(name)
    path = _registry() / "names" / (name + ".json")
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
    names = _registry() / "names"
    names.mkdir(mode=0o700, parents=True, exist_ok=True)
    with run_lock(names), run_lock(directory):
        check_name_available(name, directory)
        existing = directory / "job.json"
        if existing.exists() and _read_json(existing).get("name") != name:
            raise ToolError("This job already has a different name; existing names are retained.")
        record = {"name": name, "directory": str(directory), "created_at": datetime.now(timezone.utc).isoformat()}
        if not existing.exists():
            _new_json(existing, record)
        destination = names / (name + ".json")
        if not destination.exists():
            _new_json(destination, record)
    remember_run(directory)
    return record


def resolve_run(target):
    """Accept a named job or an explicit directory, without creating anything."""
    if target is None:
        return latest_run()
    path = Path(target).expanduser()
    if path.is_absolute() or len(path.parts) > 1 or path.exists():
        return path.resolve()
    validate_name(str(target))
    record_path = _registry() / "names" / (str(target) + ".json")
    if not record_path.is_file():
        raise ToolError(f"Unknown job {target}. Use charmm-gui-cli jobs, or supply its directory.")
    record = _read_json(record_path)
    if not isinstance(record.get("directory"), str) or not record["directory"]:
        raise ToolError("Named job has unreadable directory metadata; inspect its local name record.")
    directory = Path(record["directory"])
    if not (directory / "full-run.json").is_file():
        raise ToolError("The named job directory is unavailable; restore its original directory or supply a readable path.")
    return directory.resolve()


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
        event["error"] = error.as_dict() if hasattr(error, "as_dict") else {
            "category": "operation_error", "message": str(error), "retryable": False}
        category = event["error"]["category"]
        name = _job_metadata(directory).get("name")
        resume = f"charmm-gui-cli jobs resume {name}" if name else f"charmm-gui-cli build-resume {directory}"
        if category in ("authentication", "auth_required"):
            event["next_step"] = "charmm-gui-cli login; " + resume
        elif category == "submission_unknown":
            event["next_step"] = event["error"].get("next_step") or "Check the existing remote job; use jobs attach with its cloned build ID when available."
        else:
            event["next_step"] = event["error"].get("next_step") or resume
    with run_lock(directory):
        _new_json(directory / "recovery-history" / (uuid.uuid4().hex + ".json"), event)
        atomic_json(directory / "recovery.json", event)
    return event


def _recovery_summary(directory):
    path = directory / "recovery.json"
    if path.is_file():
        try:
            event = _read_json(path)
            if event.get("active"):
                return {"recovery": event, "next_step": event.get("next_step")}
        except ToolError:
            pass
    return {}


def inspect_run(target, *, remote_check=False, token_file=None):
    """Read local evidence and optionally make a read-only API status query."""
    directory = resolve_run(target)
    manifest = _read_json(directory / "full-run.json")
    result = {"directory": str(directory), "state": manifest.get("state", "unknown"),
              **_job_metadata(directory), **_bilayer_summary(directory), **_final_summary(directory),
              **_recovery_summary(directory), "server_checked": False}
    for key in ("source_job_id", "build_job_id"):
        if key in manifest and key not in result:
            result[key] = manifest[key]
    if remote_check and result.get("build_job_id"):
        from .api import Client
        from .auth import load_token
        from .workflow import status_summary
        token, _ = load_token(token_file)
        response = Client(token).status(result["build_job_id"])
        result.update(server_checked=True, remote_status=status_summary(response)["status"],
                      last_output_file=response.get("lastOutFile"), archive_available=response.get("hasTarFile") is True)
        if result["remote_status"] == "done" and result.get("state") not in ("validated", "validation_failed"):
            result["next_step"] = "Remote model is done. Use jobs resume to download and validate it."
    elif remote_check:
        result["remote_check_note"] = "No bilayer job exists yet; preparation state is recorded locally."
    return result


def new_run_path():
    """Return an unused default run path; create only its parent directory."""
    parent = Path.home() / ".local" / "share" / "charmm-gui-cli" / "runs"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for _ in range(10):
        directory = parent / f"{stamp}-{uuid.uuid4().hex[:12]}"
        if not directory.exists():
            return directory.resolve()
    raise ToolError("Could not allocate a new managed run path.")


def new_batch_path():
    parent = Path.home() / ".local" / "share" / "charmm-gui-cli" / "batches"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (parent / f"{stamp}-{uuid.uuid4().hex[:12]}").resolve()


def remember_run(directory):
    """Remember a successfully initialized run, without copying credentials."""
    directory = Path(directory).expanduser().resolve()
    manifest = _read_json(directory / "full-run.json")
    if manifest.get("schema_version") != 2:
        raise ToolError("Only full builds can be remembered.")
    record = {"directory": str(directory), "remembered_at": datetime.now(timezone.utc).isoformat(),
              "state": manifest.get("state", "unknown"), **_job_metadata(directory)}
    registry = _registry()
    _new_json(registry / "run-history" / f"{uuid.uuid4().hex}.json", record)
    atomic_json(registry / "latest-run.json", record)
    return record


def list_runs():
    """Return newest readable runs; stale/moved records are not usable runs."""
    records = []
    registry = _registry()
    files = list((registry / "run-history").glob("*.json"))
    if (registry / "latest-run.json").is_file():
        files.append(registry / "latest-run.json")
    for path in files:
        try:
            record = _read_json(path)
            directory = Path(record["directory"]).expanduser().resolve()
            manifest = _read_json(directory / "full-run.json")
        except (ToolError, KeyError, TypeError):
            # A history entry can outlive a mounted/moved directory. It does
            # not prevent resuming another readable initialized run.
            continue
        if manifest.get("schema_version") == 2:
            records.append({"directory": str(directory), "remembered_at": record.get("remembered_at", ""),
                            "state": manifest.get("state", "unknown")})
    records.sort(key=lambda item: item["remembered_at"], reverse=True)
    unique, seen = [], set()
    for record in records:
        if record["directory"] not in seen:
            record.update(_bilayer_summary(Path(record["directory"])))
            record.update(_final_summary(Path(record["directory"])))
            record.update(_job_metadata(Path(record["directory"])))
            record.update(_recovery_summary(Path(record["directory"])))
            unique.append(record)
            seen.add(record["directory"])
    return unique


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


def latest_run():
    records = list_runs()
    if not records:
        raise ToolError("No readable recent build is recorded. Start a build or supply its run directory.")
    return Path(records[0]["directory"])


def create_managed_build(*, protein=None, ligand=None, complex_pdb=None, ligand_resname="LIG",
                         ligand_smiles=None, accept_conect_bond_orders=False, output=None,
                         membrane=None, ions=None, preparation=None, hydrogens="add_missing"):
    """Prepare inputs and initialize one new run; never contact CHARMM-GUI."""
    from .input_sources import prepare_sources
    if output is None:
        directory = new_run_path()
    else:
        directory = Path(output).expanduser().resolve()
    if directory.exists():
        raise ToolError("Build directory already exists; resume it instead of overwriting it.")
    staging = directory.parent / f".{directory.name}.sources"
    if staging.exists():
        raise ToolError("Input staging for this build already exists; inspect it before starting a new run.")
    sources = prepare_sources(protein=protein, ligand=ligand, complex_pdb=complex_pdb,
                              ligand_resname=ligand_resname, ligand_smiles=ligand_smiles,
                              accept_conect_bond_orders=accept_conect_bond_orders, out=staging)
    config = {"version": 2, "protein": str(Path(sources["protein"]).resolve()),
              "ligand": str(Path(sources["ligand"]).resolve()), "ligand_resname": ligand_resname,
              "ligand_hydrogens": hydrogens,
              "membrane": membrane if membrane is not None else {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
              "ions": ions if ions is not None else {"type": "NaCl", "concentration_M": 0.15},
              "preparation": preparation if preparation is not None else {"orientation": "ppm", "n_terminal": "NTER", "c_terminal": "CTER"}}
    config_path = staging / "build-config.json"
    _new_json(config_path, config)  # JSON is valid YAML accepted by initialize.
    initialize(config_path, directory)
    _new_json(directory / "managed.json", {"version": 1, "sources_directory": str(staging.resolve()),
                                           "source_report": sources.get("report", {}), "config_file": str(config_path.resolve())})
    remember_run(directory)
    return directory.resolve()


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _compiler_identity(gmx):
    executable = shutil.which(str(gmx))
    if executable:
        try:
            path = Path(executable).resolve()
            info = path.stat()
            return {"available": True, "path": str(path), "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns}
        except OSError:
            pass
    return {"available": False, "requested": str(gmx)}


def _evidence(manifest, archive, grompp, gmx):
    return {"tool_version": __version__, "archive_sha256": _digest(archive),
            "inputs_sha256": {name: _digest(path) for name, path in sorted(manifest["inputs"]["files"].items())},
            "config_sha256": _canonical_hash(manifest["config"]), "grompp": bool(grompp),
            "gmx": str(gmx) if grompp else None,
            "gmx_identity": _compiler_identity(gmx) if grompp else None}


def _grompp_artifacts(report):
    check = report.get("checks", {}).get("grompp", {})
    try:
        paths = {"log": Path(check["log"]), "tpr": Path(check["output_dir"]) / "system.tpr"}
        hashes = report.get("compiled_artifacts_sha256", {})
        return check.get("passed") is True and all(path.is_file() and hashes.get(key) == _digest(path) for key, path in paths.items())
    except (KeyError, TypeError, OSError):
        return False


def _cache_reusable(report, evidence):
    if report.get("fingerprint") != _canonical_hash(evidence) or report.get("validation_complete") is not True:
        return False
    if evidence["grompp"]:
        if not evidence["gmx_identity"]["available"]:
            return False
        check = report.get("checks", {}).get("grompp", {})
        if check.get("passed") is False and report.get("status") == "validation_failed":
            try:
                return _digest(check["log"]) == report.get("compiled_artifacts_sha256", {}).get("log")
            except (KeyError, TypeError, OSError):
                return False
        return _grompp_artifacts(report)
    return True


def _final_summary(directory):
    """Expose final status only while its input/archive/compiler evidence holds."""
    path = directory / "results" / "validation.json"
    if not path.is_file():
        return {}
    try:
        report = _read_json(path)
        manifest = _read_json(directory / "full-run.json")
        stored = report["evidence"]
        archive = directory / "bilayer" / "charmm-gui.tgz"
        evidence = _evidence(manifest, archive, stored["grompp"], stored["gmx"])
        if _cache_reusable(report, evidence) and report.get("status") in ("validated", "validation_failed"):
            return {"state": report["status"], "passed": report.get("passed"),
                    "review_required": report.get("review_required", False),
                    "acceptance_status": report.get("acceptance_validation", {}).get("status"),
                    "gromacs_compiled": report.get("checks", {}).get("grompp", {}).get("passed") is True,
                    "compilation_status": ("passed" if _grompp_artifacts(report) else "failed") if stored["grompp"] else "not_run",
                    "archive": str(archive), "report_file": str(path)}
    except (ToolError, OSError, KeyError, TypeError, ValueError):
        pass
    return {"validation_status": "stale_or_unreadable", "report_file": str(path)}


def finalize_managed_build(directory, *, grompp=False, gmx="gmx"):
    """Validate a downloaded system, caching only matching immutable evidence."""
    directory = Path(directory).expanduser().resolve()
    with run_lock(directory):
        manifest = _read_json(directory / "full-run.json")
        archive = directory / "bilayer" / "charmm-gui.tgz"
        state = manifest.get("state", "unknown")
        if state in ("remote_error", "preparation_failed", "submission_unknown"):
            raise ToolError(f"Build state is {state}; final validation cannot replace recovery of this failure.")
        if not archive.is_file() or state not in ("ligands_present", "downloaded_unverified", "validation_failed", "complete", "completed", "validated"):
            return {"status": "pending", "passed": None, "directory": str(directory), "build_state": state}
        try:
            inputs, config = manifest["inputs"]["files"], manifest["config"]
            input_manifest = Path(inputs["input-manifest.json"])
            reference = Path(inputs["complex.pdb"])
            evidence = _evidence(manifest, archive, grompp, gmx)
        except (KeyError, TypeError, OSError):
            raise ToolError("Completed build is missing readable input files/configuration required for final validation.") from None
        fingerprint = _canonical_hash(evidence)
        results = directory / "results"
        report_path = results / "validation.json"
        if report_path.is_file():
            previous = _read_json(report_path)
            if _cache_reusable(previous, evidence):
                return previous
        results.mkdir(exist_ok=True)
        checks = results / "checks"
        checks.mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"{fingerprint[:12]}-", dir=checks))
        extracted = extract_for_validation(archive, work / "system")
        report = validate_system(extracted, input_manifest, run_grompp=grompp, gmx=gmx, output_parent=work)
        compiled = report.get("checks", {}).get("grompp", {})
        if "log" in compiled and "output_dir" in compiled:
            paths = {"log": Path(compiled["log"]), "tpr": Path(compiled["output_dir"]) / "system.tpr"}
            report["compiled_artifacts_sha256"] = {key: _digest(path) for key, path in paths.items() if path.is_file()}
        coordinates = report.get("files", {}).get("coordinates")
        if coordinates:
            expected_lipids = sorted({name for leaflet in ("upper", "lower")
                                     for name in config["membrane"][leaflet].split("=", 1)[0].split(":")})
            try:
                environment = inspect_environment(coordinates, expected_lipids=expected_lipids,
                                                  ion_type=config["ions"]["type"], concentration_M=config["ions"]["concentration_M"],
                                                  system_dir=extracted)
            except ToolError as exc:
                environment = {"passed": False, "error": str(exc)}
        else:
            environment = {"passed": False, "error": "No final coordinate file was identified."}
        pose = validate_bound_pose(extracted, input_manifest, reference)
        acceptance = assess_acceptance(extracted, membrane=config["membrane"], ligand_resname=config.get("ligand_resname", "LIG"))
        report.update(environment_validation=environment, bound_pose_validation=pose,
                      acceptance_validation=acceptance, review_required=acceptance.get("review_required", False),
                      review_warnings=acceptance.get("warnings", []),
                      passed=bool(report.get("passed") and environment["passed"] and pose["passed"] and acceptance["passed"]
                                  and (not grompp or _grompp_artifacts(report))),
                      status="validated", validation_complete=True, fingerprint=fingerprint, evidence=evidence,
                      report_file=str(report_path), directory=str(directory), archive=str(archive),
                      extracted_directory=str(extracted),
                      compilation={"status": ("checked" if _grompp_artifacts(report) else "failed") if grompp else "not_run",
                                   "requested": bool(grompp)})
        report["status"] = "validated" if report["passed"] else "validation_failed"
        _new_json(work / "validation.json", report)
        _new_json(work / "artifacts.json", {"archive": str(archive), "archive_sha256": evidence["archive_sha256"],
                                            "input_manifest": str(input_manifest), "reference_pdb": str(reference),
                                            "extracted_directory": str(extracted)})
        if report_path.exists():
            history = Path(tempfile.mkdtemp(prefix="previous-validation-", dir=results))
            report_path.rename(history / "validation.json")
        _new_json(report_path, report)
        return report
