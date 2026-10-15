"""Private local run management and automatic, reproducible final validation."""

import charmm_gui_cli.store.tasks as _task_store

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

from charmm_gui_cli import __version__
from charmm_gui_cli.pipeline.acceptance import assess_acceptance, extract_cgenff_penalties
from charmm_gui_cli.store.auth import ToolError, atomic_json, default_token_path
from charmm_gui_cli.pipeline.full_build import initialize
from charmm_gui_cli.chem.pose_validation import validate_bound_pose
from charmm_gui_cli.chem.validation import extract_for_validation, inspect_environment, validate_system
from charmm_gui_cli.store.locking import run_lock
from charmm_gui_cli.chem.multicomponent import prepare_build_components, validate_components
from charmm_gui_cli.store.layout import modern, task_root, inputs_path, log_path, full_archive_path
from charmm_gui_cli.store.paths import read_value, encode, identity


def inspect_run(target, *, remote_check=False, token_file=None):
    """Read local evidence and optionally make a read-only API status query."""
    directory = _task_store.resolve_run(target)
    manifest = _task_store._read_json(directory / "full-run.json")
    from ..store.settings import parameter_summary
    result = {"directory": str(directory), "state": manifest.get("state", "unknown"),
              "parameter_summary": parameter_summary(manifest, directory),
              **({"execution_state": manifest["execution_state"]} if "execution_state" in manifest else {}),
              **_task_store._job_metadata(directory), **_task_store._bilayer_summary(directory), **_final_summary(directory),
              **_task_store._recovery_summary(directory), "server_checked": False}
    for key in ("source_job_id", "build_job_id"):
        if key in manifest and key not in result:
            result[key] = manifest[key]
    if remote_check and result.get("build_job_id"):
        from charmm_gui_cli.remote.api import Client
        from charmm_gui_cli.store.auth import load_token
        from charmm_gui_cli.pipeline.workflow import status_summary
        token, source = load_token(token_file)
        try:
            from charmm_gui_cli.remote.sessions import bind
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
            from charmm_gui_cli.store.auth import recovery_guidance
            result["next_step"] = recovery_guidance("archive_preparing")
    elif remote_check:
        result["remote_check_note"] = "No bilayer job exists yet; preparation state is recorded locally."
    if isinstance(result.get("next_step"), str):
        result["next_step"] = re.sub(r"\bNAME\b", lambda _: shlex.quote(result.get("name") or str(task_root(directory))), result["next_step"])
    from charmm_gui_cli.presentation.progress import result_presentation
    result["presentation"] = result_presentation(result)
    result["official_links"] = result["presentation"]["official_links"]
    from charmm_gui_cli.store.workers import metadata
    result["background"] = metadata(directory)
    if result["background"].get("running"):
        from charmm_gui_cli.store.transfer_state import saved_transfer
        if (transfer := saved_transfer(directory)):
            result["download_progress"] = transfer
    wait = directory / "network-wait.json"
    if wait.is_file() and result["background"].get("running"):
        saved = _task_store._read_json(wait)
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
        result["delivery"] = _task_store._read_json(delivery)
    from ..presentation.execution import describe_execution
    from ..store.execution_evidence import submission_evidence
    result['submission_evidence'] = submission_evidence(directory)
    result['execution_summary'] = describe_execution(result, target=str(target) if target is not None else None,
                                                    legacy_error=manifest.get('last_error'))
    return result


def list_runs():
    """Enrich readable task records with current local validation evidence."""
    records = _task_store.list_records()
    for record in records:
        directory = Path(record["directory"])
        record.update(_task_store._bilayer_summary(directory))
        record.update(_final_summary(directory))
        record.update(_task_store._job_metadata(directory))
        record.update(_task_store._recovery_summary(directory))
        from charmm_gui_cli.store.workers import metadata
        record["background"] = metadata(directory)
        from ..presentation.execution import describe_execution
        from ..store.execution_evidence import submission_evidence
        record['submission_evidence'] = submission_evidence(directory)
        record['execution_summary'] = describe_execution(record)
    return records


def latest_run():
    records = list_runs()
    if not records:
        raise ToolError("No readable recent build is recorded. Start a build or supply its run directory.", key='jobs.no_recent',
                        next_step='charmm-gui-cli jobs list')
    return Path(records[0]["directory"])


def create_managed_build(*, protein=None, ligand=None, complex_pdb=None, ligand_resname="LIG",
                         ligand_smiles=None, accept_conect_bond_orders=False, output=None,
                         membrane=None, ions=None, preparation=None, hydrogens="add_missing", input_options=None):
    """Prepare inputs and initialize one new run; never contact CHARMM-GUI."""
    from charmm_gui_cli.chem.input_sources import prepare_sources
    from charmm_gui_cli.pipeline.build_config import validate_model_options
    from charmm_gui_cli.store.local_build import local_initialization
    model = validate_model_options(
        membrane if membrane is not None else {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
        ions if ions is not None else {"type": "NaCl", "concentration_M": 0.15},
        preparation if preparation is not None else {"orientation": "ppm", "n_terminal": "NTER", "c_terminal": "CTER"},
        ligand_resname=ligand_resname, hydrogens=hydrogens)
    if output is None:
        directory = _task_store.new_run_path()
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
        _task_store._new_json(config_path, config)  # JSON is valid YAML accepted by initialize.
        initialize(config_path, directory, _reserved=True, _source_report=sources["report"])
        _task_store._new_json(directory / "managed.json", {"version": 1, "sources_directory": str(staging.resolve()),
                                               "source_report": sources.get("report", {}), "config_file": str(config_path.resolve())})
    _task_store.remember_run(directory)
    return directory.resolve()


def create_component_build(*, components_config, complex_pdb, output=None, membrane=None, ions=None,
                           preparation=None, hydrogens="add_missing", allow_distant_pose=False, input_options=None):
    """Initialize one locally verified multicomponent run, without remote calls."""
    from charmm_gui_cli.pipeline.build_config import load_build_config
    from charmm_gui_cli.pipeline.build_config import validate_model_options
    from charmm_gui_cli.store.locking import now

    directory = _task_store.new_run_path() if output is None else Path(output).expanduser().resolve()
    model = validate_model_options(
        membrane if membrane is not None else {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
        ions if ions is not None else {"type": "NaCl", "concentration_M": 0.15},
        preparation if preparation is not None else {"orientation": "ppm", "n_terminal": "NTER", "c_terminal": "CTER"},
        hydrogens=hydrogens)
    from charmm_gui_cli.store.local_build import local_initialization
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
        _task_store._new_json(config_path, raw)
        config = load_build_config(config_path)
        from charmm_gui_cli.pipeline.build_config import validate_terminal_residues
        from charmm_gui_cli.chem.pdb_normalization import normalize_lines, terminal_predictions
        validate_terminal_residues(config["preparation"], terminal_predictions(normalize_lines(inputs["files"]["protein.pdb"])))
        config.update(components_mode=True, component_resnames=[group["resname"] for group in component_manifest["chemistry_groups"]],
                      components_config=str(Path(components_config).resolve()),
                      component_manifest=inputs["files"]["input-manifest.json"])
        from charmm_gui_cli.store.result_files import creation_context
        manifest = {"schema_version": 2, "created_at": now(), "updated_at": now(), "state": "inputs_ready", "config": config, "inputs": inputs,
                    "delivery_context": creation_context()}
        _task_store._new_json(directory / "full-run.json", manifest)
        _task_store._new_json(directory / "managed.json", {"version": 1, "components_mode": True,
                                                "sources_directory": str(inputs_path(directory) / "sources"),
                                                "source_report": component_manifest, "config_file": str(config_path)})
    _task_store.remember_run(directory)
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
    components = _task_store._read_json(input_manifest)
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
    from charmm_gui_cli.store.identifiers import job_id
    from charmm_gui_cli.store.provenance import verify_archive_provenance

    archive = Path(archive)
    bilayer_directory = (archive.parent / ".charmm-gui/bilayer"
                         if (archive.parent / ".charmm-gui/full-run.json").is_file() else archive.parent)
    try:
        source_id = job_id(manifest["source_job_id"])
        build_id = job_id(manifest["build_job_id"])
        bilayer = _task_store._read_json(bilayer_directory / "run.json")
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
    from ..chem.protein_states import intent_for, validate_intent
    state_intent = validate_intent(intent_for(manifest["inputs"].get("manifest", {})))
    if state_intent and not isinstance(manifest.get("protein_state_mapping"), dict):
        from ..chem.protein_state_proof import failure
        raise failure()
    return {"tool_version": __version__, "archive_sha256": binding["archive_provenance"]["archive_sha256"],
            **({"protein_state_revision": 1, "protein_state_request_sha256": _canonical_hash({"intent": state_intent,
                "mapping": manifest.get("protein_state_mapping")})} if state_intent else {}),
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
    if evidence.get("protein_state_revision") and report.get("protein_state_validation", {}).get("passed") is not True:
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
        report = _task_store._read_json(path)
        checked = directory / "full-run.json"
        manifest = _task_store._read_json(directory / "full-run.json")
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
                    **({"protein_state_validation": report["protein_state_validation"]} if "protein_state_validation" in report else {}),
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
    from charmm_gui_cli.store.result_files import verify_model
    if _cached_model_missing(directory, report):
        raise FileNotFoundError(2, "Missing extracted model", report["extracted_directory"])
    model = Path(report["extracted_directory"])
    verify_model(archive, model)


def finalize_managed_build(directory, *, grompp=False, gmx="gmx"):
    """Validate a downloaded system, caching only matching immutable evidence."""
    directory = Path(directory).expanduser().resolve()
    with run_lock(directory):
        manifest = _task_store._read_json(directory / "full-run.json")
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
            previous = _task_store._read_json(report_path)
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
            from charmm_gui_cli.store.result_files import verify_model
            verify_model(archive, model)
            extracted = model
        else:
            extracted = extract_for_validation(archive, model)
        from ..chem.protein_state_proof import verify_archive_states
        states = verify_archive_states(extracted, _task_store._read_json(input_manifest), reference=reference,
                                       mapping=manifest.get("protein_state_mapping"))
        if config.get("components_mode"):
            report, pose, acceptance = _component_final_report(extracted, input_manifest, reference,
                                                               grompp=grompp, gmx=gmx, work=work, membrane=config["membrane"], archive_path=archive)
        else:
            report = validate_system(extracted, input_manifest, run_grompp=grompp, gmx=gmx, output_parent=work,
                **({"protein_state_mapping": manifest.get("protein_state_mapping"), "protein_state_reference": reference}
                   if states["required"] else {}))
            pose = validate_bound_pose(extracted, input_manifest, reference)
            acceptance = assess_acceptance(extracted, membrane=config["membrane"], ligand_resname=config.get("ligand_resname", "LIG"))
        if states["required"]:
            report["protein_state_validation"] = states
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
        _task_store._new_json(work / "validation.json", report)
        _task_store._new_json(work / "artifacts.json", {"archive": str(archive), "archive_sha256": evidence["archive_sha256"],
                                            "input_manifest": str(input_manifest), "reference_pdb": str(reference),
                                            "extracted_directory": str(extracted)})
        if report_path.exists():
            history = Path(tempfile.mkdtemp(prefix="previous-validation-", dir=results))
            report_path.rename(history / "validation.json")
        _task_store._new_json(report_path, report)
        return report


# Backward-compatible function exports; internal code uses the owner module.
from charmm_gui_cli.store.tasks import _bilayer_summary, _job_metadata, _new_json, _read_json, _recovery_summary, _registries, _registry, assign_name, check_name_available, new_batch_path, new_run_path, record_recovery, remember_run, resolve_run, validate_name
