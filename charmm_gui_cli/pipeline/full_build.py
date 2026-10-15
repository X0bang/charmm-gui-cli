"""End-to-end input preparation, authenticated HTTP forms, and API building."""

import json
import hashlib
from pathlib import Path
import re
import shlex
import time
import sys
from urllib.parse import parse_qs, urlparse

from charmm_gui_cli.remote.api import AuthError, SubmissionUnknown
from charmm_gui_cli.store.identifiers import job_id
from charmm_gui_cli.store.auth import ToolError, atomic_json, atomic_write
from charmm_gui_cli.pipeline.build_config import api_config, load_build_config
from charmm_gui_cli.store.configuration import validate_config
from charmm_gui_cli.chem.inputs import prepare_inputs
from charmm_gui_cli.remote.web import soup_for, assert_nonpolymer_parameterization, snapshot_error
from charmm_gui_cli.locales.i18n import t
from charmm_gui_cli.chem.pdb_normalization import finding
from charmm_gui_cli.remote.web_state import parse_page_state
from charmm_gui_cli.store.locking import now
from charmm_gui_cli.pipeline.workflow import tracked_run, start as start_api, resume as resume_api
from charmm_gui_cli.store.layout import modern, task_root, inputs_path
from ..store.paths import read_value


def _save(directory, manifest):
    manifest["updated_at"] = now()
    atomic_json(Path(directory) / "full-run.json", manifest)


def initialize(config_path, directory, *, _reserved=False, _source_report=None):
    config = load_build_config(config_path)
    directory = Path(directory).resolve()
    if not _reserved:
        from ..store.local_build import local_initialization
        with local_initialization(directory):
            return initialize(config_path, directory, _reserved=True, _source_report=_source_report)
    from charmm_gui_cli.store.result_files import creation_context
    manifest = {"schema_version": 2, "created_at": now(), "state": "preparing_inputs", "config": config,
                "delivery_context": creation_context()}
    _save(directory, manifest)
    prepared = prepare_inputs(config["protein"], config["ligand"], inputs_path(directory),
                              ligand_resname=config["ligand_resname"],
                              hydrogen_policy=config["ligand_hydrogens"],
                              **({"ligand_smiles": config["ligand_smiles"]} if "ligand_smiles" in config else {}),
                              input_options=({key: value for key, value in config.get("input_options", {}).items()
                                              if key == 'split_chain_breaks'} if _source_report is not None else config.get("input_options")),
                              **({'fragment_preparation': True} if _source_report is not None
                                 and _source_report.get('fragment_preparation_requested') else {}))
    if _source_report is not None:
        from ..chem.pdb_normalization import attach_source_normalization
        attach_source_normalization(prepared, _source_report)
    from charmm_gui_cli.pipeline.build_config import validate_terminal_residues
    from ..chem.fragment_inputs import validate_prepared_terminals
    if not validate_prepared_terminals(prepared, config['preparation']):
        validate_terminal_residues(config["preparation"], prepared.get("manifest", {}).get("input_normalization", {}).get("terminal_predictions", []))
    manifest.update(state="inputs_ready", inputs=prepared)
    from .fragments import seal_local_inputs
    seal_local_inputs(directory, manifest, create=True)
    _save(directory, manifest)
    return manifest


def _read(directory):
    try:
        manifest = read_value(Path(directory) / "full-run.json")
    except (ValueError, OSError):
        raise ToolError("Cannot read full-run.json.", key="build.manifest_unreadable",
                        next_step=shlex.join(['ls', '-l', str(Path(directory) / 'full-run.json')])) from None
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 2:
        raise ToolError("Unsupported full-build manifest version.", key="build.manifest_version",
                        next_step=shlex.join(['ls', '-l', str(Path(directory) / 'full-run.json')]))
    if not manifest.get("inputs"):
        raise ToolError("Input preparation did not complete; inspect the existing artifacts and correct the configuration.",
                        key="build.inputs_incomplete", next_step=shlex.join(['ls', '-ld', str(inputs_path(directory))]))
    return manifest


def _source_id(path):
    soup = soup_for(Path(path).read_text(encoding="utf-8"))
    field = soup.select_one('input[name="jobid"]')
    if not field:
        raise snapshot_error(path, "web.no_job")
    return job_id(field.get("value"))


def _submit(web, source, destination, fields, uploads=None):
    if Path(destination).is_file():
        return
    if Path(destination).with_suffix(".intent.json").exists():
        raise SubmissionUnknown("A previous modeling POST has an unresolved submission intent; inspect/recover it before continuing. No repeated POST was sent.",
                                key="build.post_unknown", next_step=shlex.join(['ls', '-l', str(Path(destination).with_suffix('.intent.json'))]))
    web.submit_snapshot(source, destination, fields, uploads)


def _chains(snapshot):
    soup = soup_for(Path(snapshot).read_text(encoding="utf-8"))
    selected = {field["name"]: "1" for field in soup.select('input[type="checkbox"][name]')
                if re.fullmatch(r"chains\[[^]]+\]\[checked\]", field["name"])}
    if not selected:
        raise snapshot_error(snapshot, "web.no_chains")
    return selected


def _remote_groups(manifest, *, check_files=True):
    """Validate the explicit CGenFF groups; trusted topology uploads are unsupported.

    The build adapter never converts a user-trusted RTF/PRM path into a CGenFF
    request. Such local preparations require a separately verified remote path.
    """
    config = manifest["config"]
    prepared = manifest["inputs"].get("manifest", {})
    groups = prepared.get("chemistry_groups") if isinstance(prepared, dict) else None
    if not config.get("components_mode") and groups is None:
        return None
    if not isinstance(groups, list) or not groups:
        raise ToolError("Multi-component build is missing explicit chemistry groups.",
                        key="build.groups_missing", next_step="charmm-gui-cli check --help")
    names, resolved = set(), []
    for group in groups:
        if not isinstance(group, dict):
            raise ToolError("Invalid multi-component chemistry group.", key="build.group_invalid", next_step="charmm-gui-cli check --help")
        name = group.get("resname")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,2}", name) or name in names:
            raise ToolError("Remote chemistry groups need unique explicit residue names.",
                            key="build.group_names_invalid", next_step="charmm-gui-cli check --help")
        names.add(name)
        if group.get("mode") == "trusted_charmm":
            raise ToolError("Remote upload of trusted CHARMM topology (trusted_charmm) is not supported; no new remote request was sent.",
                            key="build.trusted_upload_unsupported", category="unsupported_feature", next_step="charmm-gui-cli check --help")
        if group.get("mode") != "cgenff_required":
            raise ToolError("Remote multi-component preparation requires explicitly defined CGenFF chemistry for every group.",
                            key="build.group_mode_invalid", next_step="charmm-gui-cli check --help")
        if not isinstance(group.get("instances"), list) or len(group["instances"]) != 1:
            raise ToolError("Remote multi-component builds currently require one instance per distinct residue name.",
                            key="build.group_instances_unsupported", next_step="charmm-gui-cli check --help")
        files = group.get("files")
        sdf = files.get("sdf") if isinstance(files, dict) else None
        if not isinstance(sdf, str) or not sdf:
            raise ToolError("Each chemistry group requires its prepared SDF file.",
                            key="build.group_sdf_missing", next_step="charmm-gui-cli check --help")
        if check_files:
            allowed = {str(Path(value).resolve()) for value in manifest["inputs"]["files"].values() if isinstance(value, str)}
            if not Path(sdf).is_file() or str(Path(sdf).resolve()) not in allowed:
                raise ToolError("A component SDF is missing or is not a recorded prepared input file.",
                                key="build.group_sdf_unrecorded", next_step="charmm-gui-cli check --help")
        resolved.append({**group, "sdf": sdf})
    declared = config.get("component_resnames")
    if declared is not None and (not isinstance(declared, list) or any(not isinstance(name, str) for name in declared)
                                 or len(set(declared)) != len(declared) or set(declared) != names):
        raise ToolError("Configured component residue names disagree with the prepared chemistry groups.",
                        key="build.group_names_mismatch", next_step="charmm-gui-cli check --help")
    return resolved


def _preparation_fields(snapshot, config, groups=None, adjustments=None, *, state_intent=None, reference_pdb=None, state_mapping=None,
                        fragment_plan=None, fragment_source_pdb=None, fragment_protein_pdb=None, fragment_mapping=None):
    snapshot_content = Path(snapshot).read_bytes() if fragment_plan is not None else None
    soup = soup_for(snapshot_content.decode("utf-8") if snapshot_content is not None
                    else Path(snapshot).read_text(encoding="utf-8"))
    fragment_receipt = None
    if fragment_plan is not None:
        from .build_config import validate_preparation
        from ..remote.fragment_terminals import bind_fragment_terminals
        if fragment_source_pdb is None or fragment_protein_pdb is None:
            raise ToolError(key="fragment.evidence_changed", next_step=t("fragment.inspect_next"))
        try:
            source_lines = Path(fragment_source_pdb).read_text(encoding="utf-8").splitlines()
            prepared_lines = Path(fragment_protein_pdb).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            raise ToolError(key="fragment.evidence_changed", next_step=t("fragment.inspect_next")) from None
        preparation, fragment_receipt = bind_fragment_terminals(
            snapshot, validate_preparation(config["preparation"], allow_fragments=True), source_lines, prepared_lines, fragment_plan)
        if fragment_receipt["snapshot_sha256"] != hashlib.sha256(snapshot_content).hexdigest():
            raise ToolError(key="fragment.evidence_changed", next_step=t("fragment.inspect_next"))
        config = {**config, "preparation": preparation}
    elif fragment_source_pdb is not None or fragment_protein_pdb is not None or fragment_mapping is not None:
        raise ToolError(key="fragment.evidence_changed", next_step=t("fragment.inspect_next"))
    if groups is not None or fragment_receipt is not None:
        forms = [form for form in soup.find_all("form") if form.select_one('input[name="jobid"]')]
        if len(forms) != 1:
            raise snapshot_error(snapshot, "web.no_form")
        soup = forms[0]
    names = [group["resname"] for group in groups] if groups is not None else [config["ligand_resname"]]
    assert_nonpolymer_parameterization(soup, names)
    def available(name, *, value=None, kind=None):
        return any(not field.has_attr("disabled") and (value is None or field.get("value") == value)
                   and (kind is None or field.get("type", "text").lower() == kind)
                   for field in soup.find_all("input", {"name": name}))
    overrides = {"rename_checked": "1"}
    if groups is not None and not available("rename_checked"):
        raise snapshot_error(snapshot, "web.no_form")
    for name in names:
        if not available(f"sdf_cgenff[{name}]", kind="file"):
            raise snapshot_error(snapshot, "web.no_sdf_field", name=name)
        if groups is not None and not (
                available(f"rename[{name}]", value="cgenff", kind="radio")
                and available(f"rename_option[cgenff][{name}]", value="sdf", kind="radio")
                and available(f"newname[{name}]") and available(f"comment[{name}]")):
            raise snapshot_error(snapshot, "web.no_sdf_field", name=name)
        overrides.update({f"rename[{name}]": "cgenff", f"rename_option[cgenff][{name}]": "sdf",
                          f"newname[{name}]": name, f"comment[{name}]": "cgenff"})
    used_terminals = set()
    patches = config["preparation"].get("terminals", {})
    for field in soup.select("select[name]"):
        match = re.fullmatch(r"terminal\[([^]]+)\]\[(first|last)\]", field["name"])
        if match:
            chain, end = match.groups()
            preparation = config["preparation"]
            patches = preparation.get("terminals", {})
            key = "n_terminal" if end == "first" else "c_terminal"
            chain_key = chain if chain in patches else chain[-1:]
            value = patches.get(chain_key, {}).get(key, preparation[key])
            if key in patches.get(chain_key, {}):
                used_terminals.add((chain_key, key))
            options = [option.get("value", "") for option in field.find_all("option")
                       if not option.has_attr("disabled") and not option.find_parent("optgroup", disabled=True)]
            if value not in options and value == "NTER" and end == "first":
                first_residue = None
                if fragment_receipt:
                    first_residue = next(row["n_terminus"]["source"]["resname"]
                                         for row in fragment_receipt["fragments"] if row["server_chain"] == chain)
                replacement = next((patch for patch in ("GLYP", "PROP") if patch in options
                                    and (fragment_receipt is None or first_residue == {"GLYP": "GLY", "PROP": "PRO"}[patch])), None)
                if replacement:
                    entry = finding("terminal.adjusted", chain=chain, residue="Gly" if replacement == "GLYP" else "Pro", patch=replacement)
                    if adjustments is None or entry not in adjustments:
                        if adjustments is not None:
                            adjustments.append(entry)
                        print(t(entry["key"], **entry["params"]), file=sys.stderr, flush=True)
                    value = replacement
            if value not in options:
                raise ToolError(key="terminal.unavailable", params={"chain": chain, "end": "N" if end == "first" else "C",
                                "requested": value, "options": ", ".join(options)}, next_step=t("terminal.next"))
            overrides[field["name"]] = value
    missing = {(chain, key) for chain, values in patches.items() for key in values} - used_terminals
    if missing:
        raise ToolError(key="terminal.chain_missing", params={"chains": ", ".join(sorted(chain + ":" + key for chain, key in missing))},
                        next_step=t("terminal.next"))
    if state_intent:
        from ..remote.protein_states import state_fields
        extra, mapping = state_fields(snapshot, reference_pdb, state_intent)
        overrides.update(extra)
        if state_mapping is not None:
            state_mapping.update(mapping)
    if fragment_receipt is not None:
        for row in fragment_receipt["fragments"]:
            for entry in row["terminals"].values():
                entry["form_value"] = overrides[entry["field"]]
        if fragment_mapping is not None:
            fragment_mapping.update(fragment_receipt)
    return overrides


def require_protein_state_support(directory, manifest):
    """Shared foreground/worker gate; preserve uncertain-POST priority."""
    from ..chem.protein_states import intent_for, validate_intent
    directory = Path(directory)
    from .checked_inputs import require_requested
    require_requested(directory, manifest)
    local = manifest.get("inputs", {}).get("manifest", {})
    states = intent_for(local)
    fragment = local.get("input_normalization", {}).get("fragment_preparation")
    from .fragments import LOCAL_RECEIPT
    local_seal = (directory / LOCAL_RECEIPT).exists() or (directory / LOCAL_RECEIPT).is_symlink()
    split_requested = manifest.get('config', {}).get('input_options', {}).get('split_chain_breaks', False)
    if states or fragment is not None or split_requested or local_seal:
        preparation = directory / "preparation"
        unknown = (manifest.get("state") == "submission_unknown" or
                   preparation.exists() and not (preparation / "upload-response.html").is_file() or
                   any((preparation / (step + ".intent.json")).exists() and not (preparation / (step + ".html")).is_file()
                       for step in ("selection", "parameterization")))
        for step in ("selection", "parameterization"):
            intent = preparation / (step + ".intent.json")
            if intent.exists():
                try:
                    saved = read_value(intent)
                    if not isinstance(saved, dict) or saved.get("state") in ("submission_unknown", "submitting"):
                        unknown = True
                except (OSError, ValueError):
                    unknown = True
        bilayer = directory / "bilayer/run.json"
        if bilayer.is_file():
            try:
                saved = read_value(bilayer)
                if not isinstance(saved, dict) or saved.get("state") in ("submission_unknown", "submitting"):
                    unknown = True
            except (OSError, ValueError):
                unknown = True
        if unknown:
            raise SubmissionUnknown("A previous modeling POST has an unresolved submission intent; inspect/recover it before continuing. No repeated POST was sent.",
                                    key="build.post_unknown", next_step=shlex.join(['charmm-gui-cli', 'jobs', 'status', str(task_root(directory))]))
    if fragment is not None:
        from .fragments import validate_local
        validate_local(directory, manifest)
    if split_requested or local_seal:
        from .fragments import seal_local_inputs
        seal_local_inputs(directory, manifest)
    validate_intent(states)


def advance(directory, web, api, *, wait=False, interval=30, max_wait=21600, progress=None):
    """Advance a saved build without repeating any recorded submission."""
    if interval < 10 or max_wait <= 0:
        raise ToolError("Polling interval must be >=10 seconds and max-wait positive.",
                        key="build.poll_invalid", next_step="charmm-gui-cli jobs resume --help")
    directory = Path(directory).resolve()
    with tracked_run(directory, _read, _save) as manifest:
        from .checked_inputs import require_requested
        require_requested(directory, manifest)
        build_directory = directory / "bilayer"
        normalization = manifest.get("inputs", {}).get("manifest", {}).get("input_normalization", {})
        if isinstance(normalization, dict) and "fragment_preparation" in normalization:
            require_protein_state_support(directory, manifest)
        if (build_directory / "run.json").is_file():
            # Once an API build exists, website cookies and old preparation
            # snapshots are irrelevant. Ambiguous API jobs are also handed to
            # resume, which refuses to submit another job.
            return _resume_bilayer(directory, manifest, api, build_directory, wait, interval, max_wait, progress)
        require_protein_state_support(directory, manifest)
        if "reused_preparation" in manifest:
            from charmm_gui_cli.pipeline.sweep import reuse_api_config
            prepared_config = reuse_api_config(directory, manifest)
            return _start_prepared_bilayer(directory, manifest, api, prepared_config, wait, interval, max_wait, progress)
        config = manifest["config"]
        groups = _remote_groups(manifest)
        api.validate_auth()
        upload_dir = directory / "preparation"
        uploaded = upload_dir / "upload-response.html"
        selected = upload_dir / "selection.html"
        parameterized = upload_dir / "parameterization.html"
        if not uploaded.exists():
            if upload_dir.exists():
                raise SubmissionUnknown("Upload directory already exists without a response; inspect/recover the prior upload before continuing. No repeated upload was sent.",
                                        key="build.upload_unknown", next_step=shlex.join(['charmm-gui-cli', 'jobs', 'status', str(task_root(directory))]))
            if groups is None:
                from charmm_gui_cli.pipeline.sweep import seal_preparation
                seal_preparation(directory)
            from .protein_states import seal_request
            seal_request(directory, manifest, create=True)
            from .fragments import seal_request as seal_fragments
            seal_fragments(directory, manifest, create=True)
            manifest["state"] = "uploading"
            _save(directory, manifest)
            if progress:
                progress({"state": "uploading"})
            web.upload_structure(manifest["inputs"]["files"]["complex.pdb"], upload_dir)
        identifier = _source_id(uploaded)
        manifest.update(source_job_id=identifier, state="selecting_components")
        _save(directory, manifest)
        require_requested(directory, manifest)
        _submit(web, uploaded, selected, _chains(uploaded))
        manifest["state"] = "parameterizing"
        _save(directory, manifest)
        if progress:
            progress({"state": "parameterizing", "job_id": identifier, "source_job_id": identifier})
        uploads = ({f"sdf_cgenff[{group['resname']}]": group["sdf"] for group in groups} if groups is not None else
                   {f"sdf_cgenff[{config['ligand_resname']}]": manifest["inputs"]["files"]["ligand.sdf"]})
        adjustments = manifest.setdefault("terminal_adjustments", [])
        from ..chem.protein_states import intent_for
        plan = intent_for(manifest["inputs"].get("manifest", {}))
        if plan and _source_id(selected) != identifier:
            raise snapshot_error(selected, "state.form_missing")
        mapping, fragment_mapping = {}, {}
        from .fragments import request_kwargs
        fields = _preparation_fields(selected, config, groups, adjustments,
            state_intent=plan, reference_pdb=manifest["inputs"]["files"]["complex.pdb"], state_mapping=mapping,
            **request_kwargs(manifest, fragment_mapping))
        if plan:
            manifest["protein_state_mapping"] = mapping
        if fragment_mapping:
            manifest["fragment_mapping"] = fragment_mapping
        manifest["applied_terminals"] = {key: value for key, value in fields.items() if key.startswith("terminal[")}
        _save(directory, manifest)
        require_requested(directory, manifest)
        _submit(web, selected, parameterized, fields, uploads)
        # Completed-page parsing is kept separate from transport because API
        # status=null and monitor.php output cannot classify web preparation.
        _await_preparation(web, directory, manifest, parameterized, wait, interval, max_wait, progress)
        if manifest["state"] != "prepared":
            return manifest
        if fragment_mapping:
            from .fragments import ensure_prepared as ensure_fragments, source_binding
            receipt = ensure_fragments(directory, manifest, web)
            manifest["fragment_source_proof"] = source_binding(receipt)
            _save(directory, manifest)
        if plan:
            from .protein_states import ensure_prepared
            receipt = ensure_prepared(directory, manifest, web)
            manifest["protein_state_proof_fingerprint"] = receipt["fingerprint"]
            _save(directory, manifest)
        prepared_config = api_config(config, identifier, existing=True)
        if groups is not None:
            prepared_config["expected_ligands"] = [group["resname"] for group in groups]
            prepared_config = validate_config(prepared_config, existing=True)
        return _start_prepared_bilayer(directory, manifest, api, prepared_config, wait, interval, max_wait, progress)


def _start_prepared_bilayer(directory, manifest, api, prepared_config, wait, interval, max_wait, progress):
    """Common API clone path for original and evidence-verified reused sources."""
    from .checked_inputs import require_requested
    require_requested(directory, manifest)
    from ..chem.protein_states import intent_for
    normalization = manifest["inputs"].get("manifest", {}).get("input_normalization", {})
    if isinstance(normalization, dict) and "fragment_preparation" in normalization:
        require_protein_state_support(directory, manifest)
        from .fragments import require_prepared
        require_prepared(directory, manifest)
        if prepared_config.get("source_job_id") != manifest.get("source_job_id"):
            from .fragments import failure
            raise failure(directory)
    if intent_for(manifest["inputs"].get("manifest", {})):
        if manifest.get("reused_preparation"):
            from .sweep import validate_reuse
            validate_reuse(directory, manifest)
        else:
            from .protein_states import check_prepared
            check_prepared(directory, manifest)
    build_directory = directory / "bilayer"
    atomic_json(directory / "api-config.json", prepared_config)
    manifest["state"] = "submitting_bilayer"
    _save(directory, manifest)
    if progress:
        progress({"state": "submitting_bilayer", "job_id": manifest["source_job_id"], "source_job_id": manifest["source_job_id"]})
    # workflow.start creates its own durable intent and never retries a POST.
    result = start_api(api, prepared_config, build_directory, existing_configuration=True)
    manifest.update(build_job_id=result["job_id"], state="bilayer_submitted")
    _save(directory, manifest)
    if not wait:
        return manifest
    return _resume_bilayer(directory, manifest, api, build_directory, wait, interval, max_wait, progress)


def _resume_bilayer(directory, manifest, api, build_directory, wait, interval, max_wait, progress=None):
    result = resume_api(api, build_directory, wait=wait, interval=interval, max_wait=max_wait,
                        **({"progress": progress} if progress else {}))
    manifest.update(build_job_id=result["job_id"], state=result["state"])
    if "validation" in result:
        manifest["validation"] = result["validation"]
    _save(directory, manifest)
    return manifest


def _snapshot_path(directory, value):
    path = Path(value)
    if path.is_absolute():
        return path
    # Migrate older manifests which stored a path relative to the launch cwd.
    # Every generated preparation snapshot lives under this run's preparation.
    if "preparation" in path.parts:
        return Path(directory).resolve().joinpath(*path.parts[path.parts.index("preparation"):])
    return Path(directory).resolve() / path


def _await_preparation(web, directory, manifest, initial_page, wait, interval, max_wait, progress=None):
    """Check rendered preparation state and stage artifacts; poll only GETs."""
    current = _snapshot_path(directory, manifest.get("preparation_snapshot", initial_page))
    deadline = time.monotonic() + max_wait
    # A later --no-wait resume must still fetch one fresh page, otherwise it
    # would inspect the same saved pending response forever.
    refresh_pending = manifest.get("preparation_page_state") in ("running", "auth_required")
    groups = _remote_groups(manifest, check_files=False)
    names = [group["resname"].lower() for group in groups] if groups is not None else [manifest["config"]["ligand_resname"].lower()]
    while True:
        html = current.read_text(encoding="utf-8")
        page = parse_page_state(html)
        if progress:
            progress({"state": "preparation_" + page["state"], "job_id": manifest["source_job_id"], "source_job_id": manifest["source_job_id"]})
        if refresh_pending:
            refresh_pending = False
            current = _poll_preparation(web, directory, manifest, page)
            continue
        manifest.update(preparation_snapshot=str(current), preparation_page_state=page["state"],
                        preparation_artifacts=page["artifacts"])
        if page["job_id"] and page["job_id"] != manifest["source_job_id"]:
            manifest["state"] = "preparation_unrecognized"
            _save(directory, manifest)
            raise ToolError("Preparation response belongs to a different job; refusing to continue.",
                            key="build.preparation_wrong_job", next_step=shlex.join(['ls', '-l', str(current)]))
        if page["state"] == "auth_required":
            manifest["state"] = "preparation_auth_required"
            _save(directory, manifest)
            raise AuthError("Website session expired. Run charmm-gui-cli login and build-resume.", key="build.session_expired",
                            next_step=t("build.login_resume", command=shlex.join(['charmm-gui-cli', 'jobs', 'resume', str(task_root(directory))])))
        if page["state"] == "error":
            manifest.update(state="preparation_failed", preparation_errors=page["errors"])
            _save(directory, manifest)
            error = snapshot_error(current, "web.preparation_failed")
            error.category = "remote_failure"
            raise error
        next_query = parse_qs(urlparse(page["next_url"] or "").query)
        paths = {urlparse(url).path.lower() for url in page["artifacts"]}
        prefix = f"/uploaded_pdb/{manifest['source_job_id']}/"
        required = {prefix + "step1_pdbreader.pdb", prefix + "step1_pdbreader.psf"}
        required.update(prefix + f"{name}/{name}.{extension}" for name in names for extension in ("rtf", "prm"))
        if (page["state"] == "ready" and page["formstop"] == 0
                and next_query.get("doc") == ["input/membrane.bilayer"]
                and next_query.get("step") == ["2"] and required <= paths):
            manifest.update(state="prepared", preparation_errors=[])
            _save(directory, manifest)
            return
        if page["state"] != "running":
            manifest.update(state="preparation_unrecognized", preparation_missing_artifacts=sorted(required - paths))
            _save(directory, manifest)
            raise snapshot_error(current, "web.preparation_incomplete")
        manifest["state"] = "preparation_running"
        _save(directory, manifest)
        if not wait:
            return
        if time.monotonic() >= deadline:
            raise ToolError("Preparation wait limit reached; build-resume can continue later.",
                            key="build.preparation_wait_limit", category="wait_timeout", retryable=True,
                            next_step=shlex.join(['charmm-gui-cli', 'jobs', 'resume', str(task_root(directory))]))
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
        current = _poll_preparation(web, directory, manifest, page)


def _poll_preparation(web, directory, manifest, page):
    # The official bookmark is a read-only recovery endpoint. The Next form
    # action is deliberately never used for polling.
    target = page.get("recovery_url")
    if target:
        response = web.request("GET", target)
    else:
        response = web.request("GET", "", params={"doc": "input/pdbreader", "jobid": manifest["source_job_id"],
                                                    "project": "membrane_bilayer"})
    current = Path(directory) / "preparation" / f"poll-{time.time_ns()}.html"
    atomic_write(current, response.text)
    return current
