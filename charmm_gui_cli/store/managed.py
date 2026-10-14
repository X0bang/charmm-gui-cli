"""Private local run management and automatic, reproducible final validation."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import tempfile
import uuid

from .. import __version__
from ..acceptance import assess_acceptance, extract_cgenff_penalties
from ..auth import ToolError, atomic_json, default_token_path
from ..full_build import initialize
from ..pose_validation import validate_bound_pose
from ..validation import extract_for_validation, inspect_environment, validate_system
from ..workflow import run_lock
from ..multicomponent import prepare_build_components, validate_components
from ..layout import modern, task_root, inputs_path, log_path, full_archive_path
from .paths import read_value, encode, identity


def _read_json(path):
    try:
        value = read_value(path)
    except (OSError, ValueError):
        raise ToolError(f"Cannot read local run metadata: {Path(path).name}.") from None
    if not isinstance(value, dict):
        raise ToolError("Local run metadata must contain a JSON object.")
    return value


def _new_json(path, value):
    """Publish a private JSON file atomically, without replacing another file."""
    path = Path(path)
    value = encode(path, value)
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
    finally:
        Path(name).unlink(missing_ok=True)


def _registry():
    # Task discovery is data, not authentication material.
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "charmm-gui-cli/index"


def _registries():
    return (_registry(), default_token_path().parent)


def validate_name(name):
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
            or name.lower().endswith((".pdb", ".sdf", ".yaml", ".yml", ".mol2", ".cif"))):
        raise ToolError(key="input.task_name", next_step="charmm-gui-cli build -h")
    return name


def check_name_available(name, directory=None):
    validate_name(name)
    path = next((registry / "names" / (name + ".json") for registry in _registries()
                 if (registry / "names" / (name + ".json")).exists()), _registry() / "names" / (name + ".json"))
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
    from .indexing import writes_allowed
    if not writes_allowed():
        # A dry-run may retain a local identity for later launch, but must not
        # mutate global names/history/latest-run or even create the index.
        with run_lock(directory):
            existing = directory / "job.json"
            if existing.exists():
                record = _read_json(existing)
                if record.get("name") != name:
                    raise ToolError("This job already has a different name; existing names are retained.")
                return record
            from .discovery import task_id
            record = {"name": name, "directory": str(directory), "task_id": task_id(directory) or uuid.uuid4().hex,
                      "created_at": datetime.now(timezone.utc).isoformat()}
            _new_json(existing, record)
            return record
    names = _registry() / "names"
    names.mkdir(mode=0o700, parents=True, exist_ok=True)
    with run_lock(names), run_lock(directory):
        check_name_available(name, directory)
        existing = directory / "job.json"
        if existing.exists() and _read_json(existing).get("name") != name:
            raise ToolError("This job already has a different name; existing names are retained.")
        from .discovery import task_id
        identifier = task_id(directory) or uuid.uuid4().hex
        record = {"name": name, "directory": str(directory), "task_id": identifier,
                  "created_at": datetime.now(timezone.utc).isoformat()}
        if not existing.exists():
            _new_json(existing, record)
        else:
            record = {**_read_json(existing), "directory": str(directory), "task_id": identifier}
            if "task_id" not in _read_json(existing):
                atomic_json(existing, record)
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
    explicit = path.is_absolute() or "/" in str(target) or str(target).startswith("~")
    if explicit:
        path = path.resolve()
        return path / ".charmm-gui" if (path / ".charmm-gui/full-run.json").is_file() else path
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(target)):
        raise ToolError(key="input.task_name")
    record_path = next((registry / "names" / (str(target) + ".json") for registry in _registries()
                        if (registry / "names" / (str(target) + ".json")).is_file()), _registry() / "names" / (str(target) + ".json"))
    if not record_path.is_file():
        if (path / ".charmm-gui/full-run.json").is_file():
            return (path / ".charmm-gui").resolve()
        if (path / "full-run.json").is_file():
            return path.resolve()
        raise ToolError(key="jobs.unknown", params={"name": target}, next_step="charmm-gui-cli jobs list")
    record = _read_json(record_path)
    if not isinstance(record.get("directory"), str) or not record["directory"]:
        raise ToolError("Named job has unreadable directory metadata; inspect its local name record.")
    from .discovery import locate
    return locate(record)


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
        if hasattr(error, "for_task"):
            error.for_task(_job_metadata(directory).get("name") or str(task_root(directory)))
        event["error"] = error.as_dict() if hasattr(error, "as_dict") else {
            "category": "operation_error", "message": str(error), "retryable": False}
        category = event["error"]["category"]
        manifest_path = directory / "full-run.json"
        manifest = _read_json(manifest_path)
        event["failed_stage"] = manifest.get("state", "unknown")
        ambiguous = category == "submission_unknown" or manifest.get("state") == "submission_unknown"
        event["execution_state"] = ("submission_unknown" if ambiguous else
                                    "interrupted" if category == "interrupted" else
                                    "failed_remote" if category in ("api_error", "http_error", "remote_error", "preparation_failed") or manifest.get("state") in ("remote_error", "preparation_failed") else
                                    "failed_local")
        name = _job_metadata(directory).get("name")
        resume = f"charmm-gui-cli jobs resume {name}" if name else f"charmm-gui-cli build-resume {directory}"
        if category in ("authentication", "auth_required"):
            event["next_step"] = "charmm-gui-cli login; " + resume
        elif category == "submission_unknown":
            event["next_step"] = event["error"].get("next_step") or "Check the existing remote job; use jobs attach with its cloned build ID when available."
        elif event["error"].get("retryable"):
            event["next_step"] = resume
        else:
            event["next_step"] = event["error"].get("next_step") or resume
    with run_lock(directory):
        _new_json(directory / "recovery-history" / (uuid.uuid4().hex + ".json"), event)
        atomic_json(directory / "recovery.json", event)
        # Keep the pipeline stage intact for safe recovery; execution outcome is
        # independent and durable rather than inventing a completed remote step.
        manifest = _read_json(directory / "full-run.json")
        manifest["execution_state"] = event.get("execution_state", state)
        if error is not None:
            manifest["failure"] = event
        else:
            manifest.pop("failure", None)
        atomic_json(directory / "full-run.json", manifest)
    return event


def _recovery_summary(directory):
    path = directory / "recovery.json"
    if path.is_file():
        try:
            event = _read_json(path)
            if event.get("active"):
                return {"recovery": event, "next_step": event.get("next_step"),
                        "execution_state": event.get("execution_state"), "failed_stage": event.get("failed_stage")}
        except ToolError:
            pass
    return {}


def inspect_run(target, *, remote_check=False, token_file=None):
    """Read local evidence and optionally make a read-only API status query."""
    directory = resolve_run(target)
    manifest = _read_json(directory / "full-run.json")
    result = {"directory": str(directory), "state": manifest.get("state", "unknown"),
              **({"execution_state": manifest["execution_state"]} if "execution_state" in manifest else {}),
              **_job_metadata(directory), **_bilayer_summary(directory), **_final_summary(directory),
              **_recovery_summary(directory), "server_checked": False}
    for key in ("source_job_id", "build_job_id"):
        if key in manifest and key not in result:
            result[key] = manifest[key]
    if remote_check and result.get("build_job_id"):
        from ..api import Client
        from ..auth import load_token
        from ..workflow import status_summary
        token, source = load_token(token_file)
        try:
            from ..remote.sessions import bind
            client = Client(token)
            bind(client, source)
            response = client.status(result["build_job_id"])
        except ToolError as error:
            error.for_task(result.get("name") or str(task_root(directory)))
            if error.category in ("api_error", "http_error", "unexpected_response"):
                # A local snapshot cannot repeat the failing remote GET.
                error.next_step = "charmm-gui-cli jobs status " + shlex.quote(result.get("name") or str(task_root(directory)))
            raise
        result.update(server_checked=True, remote_status=status_summary(response)["status"],
                      last_output_file=response.get("lastOutFile"), archive_available=response.get("hasTarFile") is True)
        if result["remote_status"] == "done" and result.get("state") not in ("validated", "validation_failed"):
            result["next_step"] = "Remote model is done. Use jobs resume to download and validate it."
        elif result["remote_status"] == "archive_preparing" and result.get("state") not in ("validated", "validation_failed"):
            from ..auth import recovery_guidance
            result["next_step"] = recovery_guidance("archive_preparing")
    elif remote_check:
        result["remote_check_note"] = "No bilayer job exists yet; preparation state is recorded locally."
    if isinstance(result.get("next_step"), str):
        result["next_step"] = re.sub(r"\bNAME\b", lambda _: shlex.quote(result.get("name") or str(task_root(directory))), result["next_step"])
    from ..progress import result_presentation
    result["presentation"] = result_presentation(result)
    result["official_links"] = result["presentation"]["official_links"]
    from ..background import metadata
    result["background"] = metadata(directory)
    if result["background"].get("running"):
        from .transfer_state import saved_transfer
        if (transfer := saved_transfer(directory)):
            result["download_progress"] = transfer
    wait = directory / "network-wait.json"
    if wait.is_file() and result["background"].get("running"):
        saved = _read_json(wait)
        if saved.get("active") is True:
            result["network_wait"] = {key: saved[key] for key in
                                      ("active", "updated_at", "retry_attempt", "retry_in_seconds", "waiting_elapsed_seconds", "next_retry_unix") if key in saved}
    log = log_path(directory)
    result["recent_stages"] = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
                               if re.match(r"\[\d{2}:\d{2}:\d{2}\]", line)][-5:] if log.is_file() else []
    preview = directory / "results" / "preview.json"
    if preview.is_file():
        result["preview_file"] = str(preview)
    delivery = directory / "delivery.json"
    if delivery.is_file():
        result["delivery"] = _read_json(delivery)
    return result


def new_run_path(name=None):
    """Return an unused default run path; create only its parent directory."""
    parent = Path.cwd()
    if name:
        return (parent / validate_name(name) / ".charmm-gui").resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for _ in range(10):
        directory = parent / f"{stamp}-{uuid.uuid4().hex[:12]}"
        if not directory.exists():
            return (directory / ".charmm-gui").resolve()
    raise ToolError("Could not allocate a new managed run path.")


def new_batch_path(name=None, *, kind="batch", source=None):
    if name:
        return (Path.cwd() / validate_name(name)).resolve()
    label = re.sub(r"[^A-Za-z0-9_.-]", "-", Path(source).stem if source else kind).strip(".-")[:40] or kind
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (Path.cwd() / f"{kind}-{label}-{stamp}-{uuid.uuid4().hex[:6]}").resolve()


def remember_run(directory):
    """Remember a successfully initialized run, without copying credentials."""
    directory = Path(directory).expanduser().resolve()
    manifest = _read_json(directory / "full-run.json")
    if manifest.get("schema_version") != 2:
        raise ToolError("Only full builds can be remembered.")
    record = {"directory": str(directory), "remembered_at": datetime.now(timezone.utc).isoformat(),
              "state": manifest.get("state", "unknown"), **_job_metadata(directory)}
    from .discovery import task_id
    identifier = task_id(directory)
    if identifier:
        record["task_id"] = identifier
    from .indexing import writes_allowed
    if not writes_allowed():
        return record
    registry = _registry()
    _new_json(registry / "run-history" / f"{uuid.uuid4().hex}.json", record)
    atomic_json(registry / "latest-run.json", record)
    return record


def list_runs():
    """Return newest readable runs; stale/moved records are not usable runs."""
    records = []
    files = []
    for registry in _registries():
        files.extend((registry / "run-history").glob("*.json"))
        if (registry / "latest-run.json").is_file():
            files.append(registry / "latest-run.json")
    for path in files:
        try:
            record = _read_json(path)
            from .discovery import locate
            directory = locate(record)
            manifest = _read_json(directory / "full-run.json")
        except (ToolError, KeyError, TypeError):
            # A history entry can outlive a mounted/moved directory. It does
            # not prevent resuming another readable initialized run.
            continue
        if manifest.get("schema_version") == 2:
            records.append({"directory": str(directory), "remembered_at": record.get("remembered_at", ""),
                            "state": manifest.get("state", "unknown"),
                            **{key: manifest[key] for key in ("execution_state", "last_error") if key in manifest}})
    records.sort(key=lambda item: item["remembered_at"], reverse=True)
    unique, seen = [], set()
    for record in records:
        if record["directory"] not in seen:
            record.update(_bilayer_summary(Path(record["directory"])))
            record.update(_final_summary(Path(record["directory"])))
            record.update(_job_metadata(Path(record["directory"])))
            record.update(_recovery_summary(Path(record["directory"])))
            from ..background import metadata
            record["background"] = metadata(record["directory"])
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
                         membrane=None, ions=None, preparation=None, hydrogens="add_missing", input_options=None):
    """Prepare inputs and initialize one new run; never contact CHARMM-GUI."""
    from ..input_sources import prepare_sources
    from ..build_config import validate_model_options
    from .local_build import local_initialization
    model = validate_model_options(
        membrane if membrane is not None else {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
        ions if ions is not None else {"type": "NaCl", "concentration_M": 0.15},
        preparation if preparation is not None else {"orientation": "ppm", "n_terminal": "NTER", "c_terminal": "CTER"},
        ligand_resname=ligand_resname, hydrogens=hydrogens)
    if output is None:
        directory = new_run_path()
    else:
        directory = Path(output).expanduser().resolve()
    staging = directory / "sources" if modern(directory) else directory.parent / f".{directory.name}.sources"
    if staging.exists():
        raise ToolError("Input staging for this build already exists; inspect it before starting a new run.")
    with local_initialization(directory):
        sources = prepare_sources(protein=protein, ligand=ligand, complex_pdb=complex_pdb,
                                  ligand_resname=ligand_resname, ligand_smiles=ligand_smiles,
                                  accept_conect_bond_orders=accept_conect_bond_orders, out=staging, input_options=input_options)
        config = {"version": 2, "protein": str(Path(sources["protein"]).resolve()),
                  "ligand": str(Path(sources["ligand"]).resolve()), "ligand_resname": ligand_resname,
                  "ligand_hydrogens": hydrogens, "membrane": model["membrane"],
                  "ions": model["ions"], "preparation": model["preparation"], "input_options": sources["report"]["input_options"]}
        config_path = staging / "build-config.json"
        _new_json(config_path, config)  # JSON is valid YAML accepted by initialize.
        initialize(config_path, directory, _reserved=True, _source_report=sources["report"])
        _new_json(directory / "managed.json", {"version": 1, "sources_directory": str(staging.resolve()),
                                               "source_report": sources.get("report", {}), "config_file": str(config_path.resolve())})
    remember_run(directory)
    return directory.resolve()


def create_component_build(*, components_config, complex_pdb, output=None, membrane=None, ions=None,
                           preparation=None, hydrogens="add_missing", allow_distant_pose=False, input_options=None):
    """Initialize one locally verified multicomponent run, without remote calls."""
    from ..build_config import load_build_config
    from ..build_config import validate_model_options
    from ..workflow import now

    directory = new_run_path() if output is None else Path(output).expanduser().resolve()
    model = validate_model_options(
        membrane if membrane is not None else {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
        ions if ions is not None else {"type": "NaCl", "concentration_M": 0.15},
        preparation if preparation is not None else {"orientation": "ppm", "n_terminal": "NTER", "c_terminal": "CTER"},
        hydrogens=hydrogens)
    from .local_build import local_initialization
    with local_initialization(directory):
        inputs = prepare_build_components(components_config=components_config, complex_pdb=complex_pdb,
                                          out=inputs_path(directory), hydrogens=hydrogens, allow_distant_pose=allow_distant_pose, input_options=input_options)
        component_manifest = inputs["manifest"]
        if any(group["mode"] != "cgenff_required" for group in component_manifest["chemistry_groups"]):
            raise ToolError(key="build.components_unsupported", next_step="charmm-gui-cli check --help")
        first = component_manifest["components"][0]
        raw = {"version": 2, "protein": inputs["files"]["protein.pdb"], "ligand": first["sdf"],
               "ligand_resname": first["resname"], "ligand_hydrogens": hydrogens,
               "membrane": model["membrane"], "ions": model["ions"], "preparation": model["preparation"]}
        config_path = directory / "component-build-config.json"
        _new_json(config_path, raw)
        config = load_build_config(config_path)
        from ..build_config import validate_terminal_residues
        from ..pdb_normalization import normalize_lines, terminal_predictions
        validate_terminal_residues(config["preparation"], terminal_predictions(normalize_lines(inputs["files"]["protein.pdb"])))
        config.update(components_mode=True, component_resnames=[group["resname"] for group in component_manifest["chemistry_groups"]],
                      components_config=str(Path(components_config).resolve()),
                      component_manifest=inputs["files"]["input-manifest.json"])
        from ..delivery import creation_context
        manifest = {"schema_version": 2, "created_at": now(), "updated_at": now(), "state": "inputs_ready", "config": config, "inputs": inputs,
                    "delivery_context": creation_context()}
        _new_json(directory / "full-run.json", manifest)
        _new_json(directory / "managed.json", {"version": 1, "components_mode": True,
                                                "sources_directory": str(inputs_path(directory) / "sources"),
                                                "source_report": component_manifest, "config_file": str(config_path)})
    remember_run(directory)
    return directory.resolve()


def _component_penalties(system_dir, resnames):
    """Discover actual residue-specific CGenFF files; never pick an ambiguous hit."""
    root = Path(system_dir).resolve()
    found = {name: [] for name in resnames}
    for path in root.rglob("*.rtf"):
        for line in path.read_text(errors="replace").splitlines():
            fields = line.split("!", 1)[0].split()
            if len(fields) >= 2 and fields[0].upper() == "RESI" and fields[1] in found:
                found[fields[1]].append(path)
    reports = {}
    for name in resnames:
        candidates = sorted(set(found[name]))
        # Coordinate-generation RTF derivatives need not have parameter pairs.
        # Count actual same-stem RTF/PRM pairs, not every RESI-bearing file.
        paths = [path for path in candidates if path.with_suffix(".prm").is_file()]
        if len(paths) != 1:
            reports[name] = {"passed": None, "status": "incomplete", "blocking": False, "review_required": True,
                             "warnings": [f"{name}: no unique residue-specific CGenFF RTF and same-stem PRM pair; review is incomplete."],
                             "evidence": {"candidate_topologies": [str(path) for path in candidates],
                                          "paired_topologies": [str(path) for path in paths]}}
        else:
            try:
                reports[name] = extract_cgenff_penalties(root, name, rtf_path=paths[0], prm_path=paths[0].with_suffix(".prm"))
                reports[name].setdefault("evidence", {})["unpaired_topologies"] = [str(path) for path in candidates if path not in paths]
            except (ToolError, OSError, ValueError) as exc:
                reports[name] = {"passed": None, "status": "incomplete", "blocking": False, "review_required": True,
                                 "warnings": [f"{name}: CGenFF review is incomplete: {exc}"], "evidence": {}}
    return reports


def _component_final_report(extracted, input_manifest, reference, *, grompp, gmx, work, membrane, archive_path=None):
    aggregate = validate_components(extracted, input_manifest, reference, run_grompp=grompp, gmx=gmx, output_parent=work,
                                    archive_path=archive_path)
    topologies = [entry["topology_validation"] for entry in aggregate.get("components", {}).values()]
    compiled = [entry["checks"]["grompp"] for entry in topologies if "grompp" in entry.get("checks", {})]
    report = {"passed": aggregate["passed"], "scientific_correctness_verified": False,
              "component_validation": aggregate, "checks": {"components": {"passed": aggregate["passed"]}},
              "files": topologies[0].get("files", {}) if topologies else {}, "errors": aggregate.get("errors", [])}
    counts = [entry.get("checks", {}).get("total_atom_count") for entry in topologies]
    if counts and all(isinstance(count, dict) for count in counts):
        if all(count == counts[0] for count in counts):
            report["checks"]["total_atom_count"] = counts[0]
        else:
            report["checks"]["total_atom_count"] = {"passed": False, "error": "Components disagree on shared system atom counts."}
            report["passed"] = False
    residue_counts = [entry.get("coordinate_residue_counts") for entry in topologies]
    if residue_counts and isinstance(residue_counts[0], dict) and all(count == residue_counts[0] for count in residue_counts):
        report["coordinate_residue_counts"] = residue_counts[0]
    if compiled:
        report["checks"]["grompp"] = compiled[0] if len(compiled) == 1 else {"passed": False, "error": "Expected a single shared grompp check."}
    components = _read_json(input_manifest)
    names = [group["resname"] for group in components["chemistry_groups"]]
    acceptance = assess_acceptance(extracted, membrane=membrane, ligand_resname=names[0])
    penalty_checks = _component_penalties(extracted, names)
    # Replace the single-ligand review; leaflet/log blocking decisions remain.
    checks = acceptance.get("checks", {})
    checks.pop("cgenff", None)
    for name, penalty in penalty_checks.items():
        checks[f"cgenff:{name}"] = penalty
    warnings = [warning for check in checks.values() for warning in check.get("warnings", [])]
    acceptance.update(checks=checks, component_penalties=penalty_checks, warnings=warnings,
                      review_required=any(check.get("review_required", False) for check in checks.values()) or bool(warnings),
                      evidence={name: check.get("evidence", {}) for name, check in checks.items()})
    if acceptance.get("passed"):
        acceptance["status"] = "passed_with_warnings" if acceptance["review_required"] else "passed"
    pose = {"passed": bool(aggregate.get("components")) and all(entry["bound_pose"]["passed"] and entry["same_protein_transform"] for entry in aggregate.get("components", {}).values()),
            "components": {name: entry["bound_pose"] for name, entry in aggregate.get("components", {}).items()},
            "scientific_correctness_verified": False}
    return report, pose, acceptance


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


def _archive_binding(manifest, archive, *, create_receipt=False):
    """Bind immutable archive provenance to both full-build and API job lineage."""
    from ..api import job_id
    from ..workflow import verify_archive_provenance

    archive = Path(archive)
    bilayer_directory = (archive.parent / ".charmm-gui/bilayer"
                         if (archive.parent / ".charmm-gui/full-run.json").is_file() else archive.parent)
    try:
        source_id = job_id(manifest["source_job_id"])
        build_id = job_id(manifest["build_job_id"])
        bilayer = _read_json(bilayer_directory / "run.json")
        bilayer_id = job_id(bilayer["job_id"])
        bilayer_source = job_id(bilayer["config"]["source_job_id"])
    except (KeyError, TypeError, ValueError):
        raise ToolError("Archive provenance requires complete source/build job IDs and readable bilayer job lineage.",
                        details=[{"key": "validation.lineage_changed", "params": {"path": str(bilayer_directory / "run.json")}}]) from None
    if build_id != bilayer_id or source_id != bilayer_source or source_id == build_id:
        raise ToolError("Archive provenance mismatch between full-build source/build IDs and the bilayer job; do not reuse another job's archive.",
                        details=[{"key": "validation.lineage_changed", "params": {"path": str(bilayer_directory / "run.json")}}])
    provenance = verify_archive_provenance(bilayer_directory, bilayer, create_receipt=create_receipt)
    receipt = bilayer_directory / "download-receipt.json"
    return {"source_job_id": source_id, "build_job_id": build_id,
            "bilayer_job_id": bilayer_id, "bilayer_source_job_id": bilayer_source,
            "archive_provenance": provenance,
            "download_receipt_sha256": _digest(receipt) if receipt.is_file() else None}


def _evidence(manifest, archive, grompp, gmx, *, create_receipt=False):
    binding = _archive_binding(manifest, archive, create_receipt=create_receipt)
    return {"tool_version": __version__, "archive_sha256": binding["archive_provenance"]["archive_sha256"],
            **({"component_validation_revision": 2} if manifest.get("config", {}).get("components_mode") else {}),
            "job_binding": binding,
            "inputs_sha256": {name: _digest(path) for name, path in sorted(manifest["inputs"]["files"].items())},
            "config_sha256": _canonical_hash(identity(manifest["config"], archive.parent / ".charmm-gui")), "grompp": bool(grompp),
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
    if (not isinstance(report.get("evidence"), dict)
            or report.get("fingerprint") != _canonical_hash(report["evidence"])
            or report.get("fingerprint") != _canonical_hash(evidence)
            or report.get("validation_complete") is not True):
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
    issues = []
    checked = path
    archive = full_archive_path(directory)
    try:
        report = _read_json(path)
        checked = directory / "full-run.json"
        manifest = _read_json(directory / "full-run.json")
        checked = path
        stored = report["evidence"]
        if not archive.is_file():
            raise FileNotFoundError(2, "Missing archive", str(archive))
        checked = archive
        evidence = _evidence(manifest, archive, stored["grompp"], stored["gmx"])
        # Reading a historical result preserves its validation version. A fresh
        # finalize still uses the current version and therefore revalidates it.
        evidence["tool_version"] = stored["tool_version"]
        if _cache_reusable(report, evidence) and report.get("status") in ("validated", "validation_failed"):
            _verify_cached_model(directory, archive, report)
            return {"state": report["status"], "passed": report.get("passed"),
                    "validation_tool_version": stored["tool_version"],
                    "review_required": report.get("review_required", False),
                    "review_warning_count": len(report.get("review_warnings", [])),
                    "acceptance_status": report.get("acceptance_validation", {}).get("status"),
                    "gromacs_compiled": report.get("checks", {}).get("grompp", {}).get("passed") is True,
                    "compilation_status": ("passed" if _grompp_artifacts(report) else "failed") if stored["grompp"] else "not_run",
                    "archive": str(archive), "report_file": str(path)}
        if report.get("fingerprint") != _canonical_hash(stored) or report.get("validation_complete") is not True:
            issues.append({"key": "validation.record_changed", "params": {"path": str(path)}})
        if stored.get("archive_sha256") != evidence["archive_sha256"]:
            issues.append({"key": "validation.archive_changed", "params": {"path": str(archive)}})
        for name in sorted(set(stored.get("inputs_sha256", {})) | set(evidence["inputs_sha256"])):
            if stored.get("inputs_sha256", {}).get(name) != evidence["inputs_sha256"].get(name):
                issues.append({"key": "validation.input_changed", "params": {"path": str(manifest["inputs"]["files"].get(name, inputs_path(directory) / name))}})
        for field, key in (("config_sha256", "validation.config_changed"), ("job_binding", "validation.lineage_changed"),
                           ("gmx_identity", "validation.compiler_changed")):
            if stored.get(field) != evidence.get(field):
                issues.append({"key": key, "params": {"path": str(directory / "full-run.json" if field == "config_sha256" else
                                                                      directory / "bilayer/download-receipt.json" if field == "job_binding" else stored.get("gmx") or "gmx")}})
        if stored.get("grompp") and not issues:
            check = report.get("checks", {}).get("grompp", {})
            issues.append({"key": "validation.compilation_changed", "params": {"path": str(check.get("output_dir") or check.get("log") or path)}})
    except FileNotFoundError as error:
        issues.append({"key": "validation.archive_missing" if Path(error.filename or checked) == archive else "validation.file_missing",
                       "params": {"path": str(error.filename or checked)}})
    except ToolError as error:
        if error.details:
            issues.extend(error.details)
        else:
            key = ("validation.model_changed" if error.key == "delivery.model_changed" else
                   "validation.model_path" if error.key == "preview.invalid_model_path" else
                   "validation.lineage_changed" if error.category == "archive_provenance" or error.key == "api.invalid_jobid" else "validation.metadata_unreadable")
            issues.append({"key": key, "params": {"path": str(task_root(directory) / "model" if key.startswith("validation.model") else checked)}})
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        issues.append({"key": "validation.metadata_unreadable", "params": {"path": str(checked)}})
    return {"validation_status": "stale_or_unreadable", "report_file": str(path),
            "validation_issues": issues or [{"key": "validation.record_changed", "params": {"path": str(path)}}]}


def _cached_model_missing(directory, report):
    """Only a missing owned tree is recoverable; edited/redirected trees are not."""
    try:
        model = Path(report["extracted_directory"])
    except (KeyError, TypeError):
        raise ToolError(key="preview.invalid_model_path") from None
    if modern(directory) and (model.is_symlink() or model.resolve() != task_root(directory) / "model"):
        raise ToolError(key="preview.invalid_model_path")
    if not model.resolve().is_relative_to(task_root(directory)):
        raise ToolError(key="preview.invalid_model_path")
    return not model.exists()


def _verify_cached_model(directory, archive, report):
    """Bind cached conclusions to model bytes, not just their source tarball."""
    from ..delivery import verify_model
    if _cached_model_missing(directory, report):
        raise FileNotFoundError(2, "Missing extracted model", report["extracted_directory"])
    model = Path(report["extracted_directory"])
    verify_model(archive, model)


def finalize_managed_build(directory, *, grompp=False, gmx="gmx"):
    """Validate a downloaded system, caching only matching immutable evidence."""
    directory = Path(directory).expanduser().resolve()
    with run_lock(directory):
        manifest = _read_json(directory / "full-run.json")
        archive = full_archive_path(directory)
        state = manifest.get("state", "unknown")
        if state in ("remote_error", "preparation_failed", "submission_unknown"):
            raise ToolError(f"Build state is {state}; final validation cannot replace recovery of this failure.")
        if not archive.is_file() or state not in ("ligands_present", "downloaded_unverified", "validation_failed", "complete", "completed", "validated"):
            return {"status": "pending", "passed": None, "directory": str(directory), "build_state": state}
        try:
            inputs, config = manifest["inputs"]["files"], manifest["config"]
            input_manifest = Path(inputs["input-manifest.json"])
            reference = Path(inputs["complex.pdb"])
            evidence = _evidence(manifest, archive, grompp, gmx, create_receipt=True)
        except (KeyError, TypeError, OSError):
            raise ToolError("Completed build is missing readable input files/configuration required for final validation.") from None
        fingerprint = _canonical_hash(evidence)
        results = directory / "results"
        report_path = results / "validation.json"
        if report_path.is_file():
            previous = _read_json(report_path)
            if _cache_reusable(previous, evidence) and not _cached_model_missing(directory, previous):
                _verify_cached_model(directory, archive, previous)
                return previous
        results.mkdir(exist_ok=True)
        checks = results / "checks"
        checks.mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"{fingerprint[:12]}-", dir=checks))
        model = task_root(directory) / "model" if modern(directory) else work / "system"
        # Retain one extracted model across compiler checks and client upgrades.
        # Verify its bytes against the archive before reusing user-visible files.
        if modern(directory) and model.exists():
            from ..delivery import verify_model
            verify_model(archive, model)
            extracted = model
        else:
            extracted = extract_for_validation(archive, model)
        if config.get("components_mode"):
            report, pose, acceptance = _component_final_report(extracted, input_manifest, reference,
                                                               grompp=grompp, gmx=gmx, work=work, membrane=config["membrane"], archive_path=archive)
        else:
            report = validate_system(extracted, input_manifest, run_grompp=grompp, gmx=gmx, output_parent=work)
            pose = validate_bound_pose(extracted, input_manifest, reference)
            acceptance = assess_acceptance(extracted, membrane=config["membrane"], ligand_resname=config.get("ligand_resname", "LIG"))
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
        report["terminal_adjustments"] = manifest.get("terminal_adjustments", [])
        report["input_normalization"] = manifest.get("inputs", {}).get("manifest", {}).get("input_normalization", {})
        _new_json(work / "validation.json", report)
        _new_json(work / "artifacts.json", {"archive": str(archive), "archive_sha256": evidence["archive_sha256"],
                                            "input_manifest": str(input_manifest), "reference_pdb": str(reference),
                                            "extracted_directory": str(extracted)})
        if report_path.exists():
            history = Path(tempfile.mkdtemp(prefix="previous-validation-", dir=results))
            report_path.rename(history / "validation.json")
        _new_json(report_path, report)
        return report
