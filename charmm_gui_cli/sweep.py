"""Deterministic parameter scans and evidence-bound preparation reuse."""

from contextlib import ExitStack
import hashlib
import itertools
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from . import batch
from .api import job_id
from .auth import ToolError, atomic_json
from .config import UniqueLoader, mapping, payload_for
from .workflow import run_lock, now


AXES = ("upper", "lower", "salt", "salt_concentration", "margin", "water_padding")
INPUT_FIELDS = tuple(key for key in batch.FIELDS if key not in AXES)
INPUT_FILES = ("protein.pdb", "ligand.pdb", "ligand.sdf", "complex.pdb")
MAX_CONDITIONS = 256


def _digest(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        raise ToolError("Cannot read required preparation evidence.") from None
    return digest.hexdigest()


def _json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolError("Cannot read preparation/sweep metadata.") from None
    if not isinstance(value, dict):
        raise ToolError("Preparation/sweep metadata must be an object.")
    return value


def expand(path):
    """Validate a scan and expand a deterministic Cartesian product, offline."""
    path = Path(path).expanduser().resolve()
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ToolError("Cannot read valid sweep YAML.") from None
    mapping(raw, ("version", "inputs", "defaults", "axes", "source_run"), "sweep")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ToolError("Sweep YAML requires version: 1.")
    inputs = batch._options(mapping(raw.get("inputs"), INPUT_FIELDS, "sweep inputs"), path.parent)
    if not inputs.get("complex") and not (inputs.get("protein") and inputs.get("ligand")):
        raise ToolError("Sweep inputs require protein + ligand, or complex.")
    defaults = batch._options(mapping(raw.get("defaults", {}), AXES, "sweep defaults"), path.parent)
    axes = mapping(raw.get("axes"), AXES, "sweep axes")
    if not axes:
        raise ToolError("Sweep axes must contain at least one nonempty list.")
    size, normalized = 1, {}
    for key in sorted(axes):
        values = axes[key]
        if not isinstance(values, list) or not values:
            raise ToolError("Each sweep axis must be a nonempty list.")
        cleaned = [batch._options({key: value}, path.parent)[key] for value in values]
        if any(value in cleaned[:index] for index, value in enumerate(cleaned)):
            raise ToolError("Duplicate values in a sweep axis are not allowed.")
        normalized[key] = cleaned
        size *= len(cleaned)
        if size > MAX_CONDITIONS:
            raise ToolError(f"Sweep exceeds the {MAX_CONDITIONS}-condition safety limit.")
    names = list(normalized)
    jobs = [{"name": f"scan-{index:04d}", **dict(zip(names, values))}
            for index, values in enumerate(itertools.product(*(normalized[key] for key in names)), 1)]
    source = raw.get("source_run")
    if source is not None and (not isinstance(source, str) or not source.strip()):
        raise ToolError("source_run must name an evidence-bearing local run directory, not a job ID.")
    if source is not None and source.isdigit():
        raise ToolError("A bare remote job ID is not preparation reuse evidence.")
    return {"version": 1, "source": str(path), "source_sha256": _digest(path),
            "conditions": size, "axes": normalized,
            "source_run": str((path.parent / Path(source).expanduser()).resolve()) if source else None,
            "batch": {"version": 1, "reuse_preparation": True, "defaults": {**inputs, **defaults}, "jobs": jobs}}


def prepare(path, directory):
    """Create all local child inputs and seals; no credentials/network required."""
    expanded = expand(path)
    directory = Path(directory).expanduser().resolve()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        raise ToolError("Sweep directory already exists; resume it rather than rebuilding inputs.") from None
    with run_lock(directory):
        expanded_path = directory / "expanded-batch.json"
        atomic_json(expanded_path, expanded["batch"])
        metadata = {"version": 1, "created_at": now(), "expanded": expanded,
                    "fingerprint": batch._canonical(expanded), "state": "preparing_local",
                    "batch_directory": str(directory / "batch")}
        atomic_json(directory / "sweep.json", metadata)
        summary = batch.initialize(expanded_path, directory / "batch")
        for row in summary["jobs"]:
            if row["state"] == "inputs_ready":
                seal_preparation(row["directory"])
        if expanded["source_run"]:
            # Validate all candidates before binding any, avoiding a half-bound
            # sweep when one condition unexpectedly changed its chemistry.
            source = certify_source(expanded["source_run"])
            for row in summary["jobs"]:
                if row["state"] == "inputs_ready" and preparation_identity(row["directory"]) != source["identity"]:
                    raise ToolError("Prepared source does not match the sweep protein, ligand chemistry or preparation settings.")
            for row in summary["jobs"]:
                if row["state"] == "inputs_ready":
                    bind_source(row["directory"], expanded["source_run"])
        metadata["state"] = "local_ready"
        atomic_json(directory / "sweep.json", metadata)
    return {"directory": str(directory), "batch_directory": str(directory / "batch"),
            "conditions": expanded["conditions"], "failed": summary["failed"], "state": "local_ready",
            "source_run": expanded["source_run"], "leader": summary["jobs"][0]["directory"],
            "server_submitted": False, "batch": summary}


def read(directory):
    """Validate saved source fingerprints before a resume; never creates jobs."""
    directory = Path(directory).expanduser().resolve()
    metadata = _json(directory / "sweep.json")
    try:
        expanded = metadata["expanded"]
        if (metadata.get("version") != 1 or batch._canonical(expanded) != metadata["fingerprint"]
                or expand(expanded["source"]) != expanded
                or Path(metadata["batch_directory"]).resolve() != directory / "batch"
                or _json(directory / "expanded-batch.json") != expanded["batch"]):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ToolError("Sweep source or saved expansion changed; refusing to recreate or redirect jobs.") from None
    if expanded.get("source_run"):
        # Never turn a failed request to reuse an explicit source into a fresh
        # upload when the user subsequently resumes the saved scan directory.
        if metadata.get("state") != "local_ready":
            raise ToolError("Explicit-source sweep preparation/binding did not complete; inspect the retained files. No fallback upload is allowed.")
        saved_batch = batch._read(directory / "batch")
        for row in saved_batch["jobs"]:
            if row["state"] != "local_failed":
                child = _json(Path(row["directory"]) / "full-run.json")
                record = child.get("reused_preparation")
                certificate = record.get("certificate") if isinstance(record, dict) else None
                if not isinstance(certificate, dict) or certificate.get("source_directory") != expanded["source_run"]:
                    raise ToolError("Explicit-source sweep has an unbound or differently bound child; refusing fallback upload.")
                if not (Path(row["directory"]) / "bilayer/run.json").is_file():
                    validate_reuse(row["directory"], child)
    return metadata


def _file(directory, path, expected=None):
    if not isinstance(path, (str, Path)) or not str(path):
        raise ToolError("Preparation evidence path must be a nonempty local file path.")
    path = Path(path)
    if not path.is_absolute():
        path = directory / path
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_relative_to(directory) or not resolved.is_file():
        raise ToolError("Preparation evidence must be an existing regular file inside its run.")
    if expected is not None and resolved != directory / expected:
        raise ToolError("Preparation input path differs from its fixed run-local location.")
    return resolved


def preparation_identity(directory, manifest=None):
    """Exact coordinate/chemistry/settings identity, independent of membrane axes."""
    from .inputs import read_ligand
    directory = Path(directory).expanduser().resolve()
    manifest = _json(directory / "full-run.json") if manifest is None else manifest
    try:
        if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 2:
            raise ValueError
        config = manifest["config"]
        if (config.get("components_mode") or config.get("components") or config.get("chemistry_groups")
                or manifest.get("components") or manifest.get("chemistry_groups")
                or manifest.get("inputs", {}).get("components") or manifest.get("inputs", {}).get("chemistry_groups")):
            raise ToolError("Preparation reuse currently supports one ligand chemistry only; multi-component builds remain supported without sweep reuse.")
        mapping(config, ("version", "protein", "ligand", "ligand_resname", "ligand_hydrogens", "preparation", "membrane", "ions"), "sealed preparation configuration")
        preparation = mapping(config["preparation"], ("orientation", "n_terminal", "c_terminal"), "preparation identity")
        settings = {key: preparation[key] for key in ("orientation", "n_terminal", "c_terminal")}
        settings.update(ligand_resname=config["ligand_resname"], hydrogens=config["ligand_hydrogens"])
        settings = batch._options(settings, directory)
        files = {name: _file(directory, manifest["inputs"]["files"][name], "inputs/" + name) for name in INPUT_FILES}
        ligand = read_ligand(files["ligand.sdf"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ToolError("Missing/unknown preparation identity; reuse requires fully prepared local inputs and explicit settings.") from None
    chemical_graph = {"atoms": [{"name": atom.name, "element": atom.element, "charge": atom.charge} for atom in ligand.atoms],
                      "bonds": [[a, b, order] for (a, b), order in sorted(ligand.bonds.items())]}
    return {"version": 1, "settings": settings, "input_sha256": {name: _digest(path) for name, path in files.items()},
            "ligand_chemistry_sha256": batch._canonical(chemical_graph)}


def seal_preparation(directory):
    """Freeze inputs BEFORE first upload. Existing seals are verified, not replaced.

    Caller holds the full-build lock when integrated into normal submission.
    An old run without a pre-upload seal is intentionally ineligible for reuse.
    """
    directory = Path(directory).expanduser().resolve()
    identity = preparation_identity(directory)
    seal_path = directory / "preparation-seal.json"
    if seal_path.exists():
        seal = _json(seal_path)
        if type(seal.get("version")) is not int or seal["version"] != 1 or seal.get("identity") != identity or seal.get("fingerprint") != batch._canonical(identity):
            raise ToolError("Preparation inputs/settings changed after sealing; refusing reuse or upload.")
        return seal
    manifest = _json(directory / "full-run.json")
    if ((directory / "preparation").exists() or (directory / "bilayer").exists()
            or manifest.get("source_job_id") or manifest.get("reused_preparation")
            or manifest.get("state") != "inputs_ready"):
        raise ToolError("Cannot retroactively seal an uploaded/unknown preparation; start a new evidence-tracked source run.")
    seal = {"version": 1, "created_at": now(), "identity": identity, "fingerprint": batch._canonical(identity)}
    from .managed import _new_json
    _new_json(seal_path, seal)
    return seal


def _check_intent(directory, source_name, destination_name, overrides, upload_fields=()):
    from .web import soup_for, form_data, modeling_target
    source = _file(directory, "preparation/" + source_name)
    target = _file(directory, "preparation/" + destination_name)
    intent_path = _file(directory, str(target.with_suffix(".intent.json")))
    intent = _json(intent_path)
    forms = [form for form in soup_for(source.read_text()).find_all("form") if form.select_one('input[name="jobid"]')]
    if len(forms) != 1:
        raise ToolError("Preparation form evidence is ambiguous.")
    expected = [[key, value] for key, value in form_data(forms[0]) if key not in overrides and key not in upload_fields]
    expected.extend([key, value] for key, value in overrides.items())
    if (intent.get("state") != "response_received" or intent.get("target") != modeling_target(forms[0].get("action"))
            or intent.get("fields") != expected or intent.get("upload_fields") != list(upload_fields)):
        raise ToolError("Unknown/changed preparation POST settings; refusing source reuse.")
    return (source, target, intent_path)


def certify_source(directory):
    """Read-only verification of a finished PDB Reader/CGenFF source run."""
    from .full_build import _source_id, _chains, _preparation_fields
    from .web_state import parse_page_state
    directory = Path(directory).expanduser().resolve()
    manifest = _json(directory / "full-run.json")
    if manifest.get("reused_preparation"):
        raise ToolError("Use the original preparation source, not a chain of reused sources.")
    identity = preparation_identity(directory, manifest)
    if not (directory / "preparation-seal.json").is_file():
        raise ToolError("Source has no pre-upload seal; old or untracked preparation cannot be reused.")
    seal = _json(directory / "preparation-seal.json")
    if type(seal.get("version")) is not int or seal["version"] != 1 or seal.get("identity") != identity or seal.get("fingerprint") != batch._canonical(identity):
        raise ToolError("Source inputs/preparation changed or lack a matching pre-upload seal.")
    identifier = job_id(manifest.get("source_job_id"))
    try:
        ready = _file(directory, manifest["preparation_snapshot"])
    except (KeyError, TypeError):
        raise ToolError("Source has no verified preparation completion snapshot.") from None
    page = parse_page_state(ready.read_text(encoding="utf-8"))
    query = parse_qs(urlparse(page.get("next_url") or "").query)
    ligand = identity["settings"]["ligand_resname"].lower()
    required = {f"/uploaded_pdb/{identifier}/{name}" for name in
                ("step1_pdbreader.pdb", "step1_pdbreader.psf", f"{ligand}/{ligand}.rtf", f"{ligand}/{ligand}.prm")}
    if (page.get("state") != "ready" or page.get("job_id") != identifier or page.get("formstop") != 0
            or query.get("doc") != ["input/membrane.bilayer"] or query.get("step") != ["2"]
            or not required <= {urlparse(url).path.lower() for url in page["artifacts"]}):
        raise ToolError("Source preparation is not proven complete with matching PDB/PSF/ligand parameters.")
    upload = _file(directory, "preparation/upload-response.html")
    web_run = _file(directory, "preparation/web-run.json")
    uploaded = _json(web_run)
    if (_source_id(upload) != identifier or uploaded.get("job_id") != identifier
            or uploaded.get("state") != "upload_response_received"
            or not isinstance(uploaded.get("input_pdb"), str)
            or Path(uploaded.get("input_pdb", "")).resolve() != directory / "inputs/complex.pdb"):
        raise ToolError("Source upload receipt does not match the sealed input and source ID.")
    selected = _file(directory, "preparation/selection.html")
    evidence = [ready, upload, web_run, directory / "preparation-seal.json"]
    evidence.extend(_check_intent(directory, "upload-response.html", "selection.html", _chains(upload)))
    evidence.extend(_check_intent(directory, "selection.html", "parameterization.html",
                                  _preparation_fields(selected, manifest["config"]), (f"sdf_cgenff[{identity['settings']['ligand_resname']}]",)))
    certificate = {"version": 1, "source_directory": str(directory), "source_job_id": identifier,
                   "identity": identity, "evidence_sha256": {str(path.relative_to(directory)): _digest(path) for path in evidence}}
    return {**certificate, "fingerprint": batch._canonical(certificate)}


def bind_source(directory, source_directory):
    """Bind an untouched locally prepared child to an evidence-bearing source."""
    directory, source_directory = Path(directory).resolve(), Path(source_directory).resolve()
    if directory == source_directory:
        raise ToolError("A source cannot reuse itself.")
    with ExitStack() as stack:
        for path in sorted((directory, source_directory)):
            stack.enter_context(run_lock(path))
        source = certify_source(source_directory)
        manifest = _json(directory / "full-run.json")
        identity = preparation_identity(directory, manifest)
        if identity != source["identity"]:
            raise ToolError("Reuse refused: protein/coordinates/ligand chemistry/preparation settings differ.")
        if manifest.get("reused_preparation"):
            if (validate_reuse(directory, manifest) != source["source_job_id"]
                    or manifest["reused_preparation"]["certificate"]["source_directory"] != str(source_directory)):
                raise ToolError("Child is already bound to another source.")
            return manifest["reused_preparation"]
        if (manifest.get("state") != "inputs_ready" or (directory / "preparation").exists()
                or (directory / "bilayer").exists() or manifest.get("source_job_id")):
            raise ToolError("Reuse binding requires an untouched inputs_ready child; no existing job will be replaced.")
        record = {"version": 1, "certificate": source, "identity_fingerprint": batch._canonical(identity)}
        manifest.update(reused_preparation=record, source_job_id=source["source_job_id"], state="prepared_reused")
        atomic_json(directory / "full-run.json", manifest)
        return record


def validate_reuse(directory, manifest=None):
    """Recheck child, source, original POST evidence and ready snapshot; return ID.

    Integrators must call this immediately before constructing the API clone,
    never fall back to uploading a child when verification fails.
    """
    directory = Path(directory).resolve()
    manifest = _json(directory / "full-run.json") if manifest is None else manifest
    try:
        record = manifest["reused_preparation"]
        if record.get("version") != 1:
            raise ValueError
        stored = record["certificate"]
        current = certify_source(stored["source_directory"])
        identity = preparation_identity(directory, manifest)
        if (stored != current or identity != current["identity"]
                or record["identity_fingerprint"] != batch._canonical(identity)
                or manifest.get("source_job_id") != current["source_job_id"]):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ToolError("Reuse certificate/source/input changed; refusing upload or clone.") from None
    return current["source_job_id"]


def reuse_api_config(directory, manifest=None):
    """Validated API config; clone_job=true is enforced by existing payload code."""
    from .build_config import api_config
    directory = Path(directory).resolve()
    manifest = _json(directory / "full-run.json") if manifest is None else manifest
    identifier = validate_reuse(directory, manifest)
    config = api_config(manifest["config"], identifier)
    if payload_for(config).get("clone_job") != "true":
        raise ToolError("Preparation reuse requires clone_job=true.")
    return config


def bind_ready_sources(batch_directory, manifest):
    """Elect one preparation leader per identity and bind untouched followers.

    Called with the batch lock held, before counting active jobs each round.
    Mutates only batch row metadata and verified child full-run manifests.
    ``reuse_waiting`` rows must not be submitted by the scheduler. Already
    attempted children are never rebound and retain their independent jobs.
    """
    if not manifest.get("config", {}).get("reuse_preparation", False):
        return {"bound": 0, "waiting": 0}
    # Existing clones are independent of later source changes. Do not make
    # their status/download/validation depend on re-certifying a source when
    # no untouched follower needs a new binding.
    candidates = []
    for row in manifest["jobs"]:
        directory = Path(row["directory"])
        if row["state"] in batch.TERMINAL:
            continue
        current = _json(directory / "full-run.json")
        if (not current.get("reused_preparation") and not current.get("source_job_id")
                and not (directory / "preparation").exists() and not (directory / "bilayer").exists()):
            candidates.append(row["directory"])
    if not candidates:
        for row in manifest["jobs"]:
            row["reuse_waiting"] = False
        return {"bound": 0, "waiting": 0}
    groups = {}
    for row in manifest["jobs"]:
        if row["state"] == "local_failed":
            continue
        directory = Path(row["directory"]).resolve()
        identity = preparation_identity(directory)
        groups.setdefault(batch._canonical(identity), []).append(row)
    bound = waiting = 0
    for rows in groups.values():
        if not any(row["directory"] in candidates for row in rows):
            for row in rows:
                row["reuse_waiting"] = False
            continue
        source = None
        for row in rows:
            current = _json(Path(row["directory"]) / "full-run.json")
            if current.get("reused_preparation"):
                validate_reuse(row["directory"], current)
                source = current["reused_preparation"]["certificate"]["source_directory"]
                break
            if current.get("source_job_id") and current.get("preparation_page_state") == "ready":
                certify_source(row["directory"])
                source = row["directory"]
                break
        leader = next((row for row in rows if row["state"] not in batch.TERMINAL), None)
        for row in rows:
            row["reuse_waiting"] = False
            directory = Path(row["directory"])
            current = _json(directory / "full-run.json")
            if (row["state"] in batch.TERMINAL or current.get("reused_preparation")
                    or current.get("source_job_id") or (directory / "preparation").exists()
                    or (directory / "bilayer").exists()):
                continue
            if source is not None:
                bind_source(directory, source)
                row.update(state="prepared_reused", source_job_id=certify_source(source)["source_job_id"], started=False)
                bound += 1
            elif row is not leader:
                row.update(state="waiting_preparation", reuse_waiting=True)
                waiting += 1
    return {"bound": bound, "waiting": waiting}
