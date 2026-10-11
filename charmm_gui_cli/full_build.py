"""End-to-end input preparation, authenticated HTTP forms, and API building."""

import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlparse

from .api import AuthError, SubmissionUnknown, job_id
from .auth import ToolError, atomic_json, atomic_write
from .build_config import api_config, load_build_config
from .config import validate_config
from .inputs import prepare_inputs
from .web import soup_for, assert_nonpolymer_parameterization
from .web_state import parse_page_state
from .workflow import now, tracked_run, start as start_api, resume as resume_api


def _save(directory, manifest):
    manifest["updated_at"] = now()
    atomic_json(Path(directory) / "full-run.json", manifest)


def initialize(config_path, directory):
    config = load_build_config(config_path)
    directory = Path(directory).resolve()
    if directory.exists():
        raise ToolError("Build directory already exists; use build-resume.")
    directory.mkdir(parents=True)
    manifest = {"schema_version": 2, "created_at": now(), "state": "preparing_inputs", "config": config}
    _save(directory, manifest)
    prepared = prepare_inputs(config["protein"], config["ligand"], directory / "inputs",
                              ligand_resname=config["ligand_resname"],
                              hydrogen_policy=config["ligand_hydrogens"])
    manifest.update(state="inputs_ready", inputs=prepared)
    _save(directory, manifest)
    return manifest


def _read(directory):
    try:
        manifest = json.loads((Path(directory) / "full-run.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise ToolError("Cannot read full-run.json.") from None
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 2:
        raise ToolError("Unsupported full-build manifest version.")
    if not manifest.get("inputs"):
        raise ToolError("Input preparation did not complete; inspect the existing artifacts and correct the configuration.")
    return manifest


def _source_id(path):
    soup = soup_for(Path(path).read_text(encoding="utf-8"))
    field = soup.select_one('input[name="jobid"]')
    if not field:
        raise ToolError("Upload did not return a recognizable job ID. Inspect the saved page before retrying.")
    return job_id(field.get("value"))


def _submit(web, source, destination, fields, uploads=None):
    if Path(destination).is_file():
        return
    if Path(destination).with_suffix(".intent.json").exists():
        raise SubmissionUnknown("A previous modeling POST has an unresolved submission intent; inspect/recover it before continuing. No repeated POST was sent.")
    web.submit_snapshot(source, destination, fields, uploads)


def _chains(snapshot):
    soup = soup_for(Path(snapshot).read_text(encoding="utf-8"))
    selected = {field["name"]: "1" for field in soup.select('input[type="checkbox"][name]')
                if re.fullmatch(r"chains\[[^]]+\]\[checked\]", field["name"])}
    if not selected:
        raise ToolError("No protein/ligand chain choices found in the upload response.")
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
        raise ToolError("Multi-component build is missing explicit chemistry groups.")
    names, resolved = set(), []
    for group in groups:
        if not isinstance(group, dict):
            raise ToolError("Invalid multi-component chemistry group.")
        name = group.get("resname")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,2}", name) or name in names:
            raise ToolError("Remote chemistry groups need unique explicit residue names.")
        names.add(name)
        if group.get("mode") == "trusted_charmm":
            raise ToolError("Remote upload of trusted CHARMM topology (trusted_charmm) is not supported; no new remote request was sent.",
                            category="unsupported_feature", next_step="Keep the local topology checks; use an explicitly supported remote topology workflow, not automatic CGenFF substitution.")
        if group.get("mode") != "cgenff_required":
            raise ToolError("Remote multi-component preparation requires explicitly defined CGenFF chemistry for every group.")
        if not isinstance(group.get("instances"), list) or len(group["instances"]) != 1:
            raise ToolError("Remote multi-component builds currently require one instance per distinct residue name.")
        files = group.get("files")
        sdf = files.get("sdf") if isinstance(files, dict) else None
        if not isinstance(sdf, str) or not sdf:
            raise ToolError("Each chemistry group requires its prepared SDF file.")
        if check_files:
            allowed = {str(Path(value).resolve()) for value in manifest["inputs"]["files"].values() if isinstance(value, str)}
            if not Path(sdf).is_file() or str(Path(sdf).resolve()) not in allowed:
                raise ToolError("A component SDF is missing or is not a recorded prepared input file.")
        resolved.append({**group, "sdf": sdf})
    declared = config.get("component_resnames")
    if declared is not None and (not isinstance(declared, list) or any(not isinstance(name, str) for name in declared)
                                 or len(set(declared)) != len(declared) or set(declared) != names):
        raise ToolError("Configured component residue names disagree with the prepared chemistry groups.")
    return resolved


def _preparation_fields(snapshot, config, groups=None):
    soup = soup_for(Path(snapshot).read_text(encoding="utf-8"))
    if groups is not None:
        forms = [form for form in soup.find_all("form") if form.select_one('input[name="jobid"]')]
        if len(forms) != 1:
            raise ToolError("Multi-component parameterization needs exactly one identifiable modeling form.")
        soup = forms[0]
    names = [group["resname"] for group in groups] if groups is not None else [config["ligand_resname"]]
    assert_nonpolymer_parameterization(soup, names)
    def available(name, *, value=None, kind=None):
        return any(not field.has_attr("disabled") and (value is None or field.get("value") == value)
                   and (kind is None or field.get("type", "text").lower() == kind)
                   for field in soup.find_all("input", {"name": name}))
    overrides = {"rename_checked": "1"}
    if groups is not None and not available("rename_checked"):
        raise ToolError("The server page lacks an enabled hetero-residue parameterization control.")
    for name in names:
        if not available(f"sdf_cgenff[{name}]", kind="file"):
            raise ToolError(f"The expected ligand SDF field for {name} is absent or disabled; unsupported ligand-reading page.")
        if groups is not None and not (
                available(f"rename[{name}]", value="cgenff", kind="radio")
                and available(f"rename_option[cgenff][{name}]", value="sdf", kind="radio")
                and available(f"newname[{name}]") and available(f"comment[{name}]")):
            raise ToolError(f"The server page does not expose all required CGenFF/SDF controls for {name}; no parameterization POST was sent.")
        overrides.update({f"rename[{name}]": "cgenff", f"rename_option[cgenff][{name}]": "sdf",
                          f"newname[{name}]": name, f"comment[{name}]": "cgenff"})
    for field in soup.select("select[name]"):
        match = re.fullmatch(r"terminal\[[^]]+\]\[(first|last)\]", field["name"])
        if match:
            value = config["preparation"]["n_terminal" if match.group(1) == "first" else "c_terminal"]
            if not field.find("option", {"value": value}):
                raise ToolError("Requested terminal patch is unavailable in the server form.")
            overrides[field["name"]] = value
    return overrides


def advance(directory, web, api, *, wait=False, interval=30, max_wait=21600, progress=None):
    """Advance a saved build without repeating any recorded submission."""
    if interval < 10 or max_wait <= 0:
        raise ToolError("Polling interval must be >=10 seconds and max-wait positive.")
    directory = Path(directory).resolve()
    with tracked_run(directory, _read, _save) as manifest:
        build_directory = directory / "bilayer"
        if (build_directory / "run.json").is_file():
            # Once an API build exists, website cookies and old preparation
            # snapshots are irrelevant. Ambiguous API jobs are also handed to
            # resume, which refuses to submit another job.
            return _resume_bilayer(directory, manifest, api, build_directory, wait, interval, max_wait, progress)
        if "reused_preparation" in manifest:
            from .sweep import reuse_api_config
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
                raise SubmissionUnknown("Upload directory already exists without a response; inspect/recover the prior upload before continuing. No repeated upload was sent.")
            if groups is None:
                from .sweep import seal_preparation
                seal_preparation(directory)
            manifest["state"] = "uploading"
            _save(directory, manifest)
            if progress:
                progress({"state": "uploading"})
            web.upload_structure(manifest["inputs"]["files"]["complex.pdb"], upload_dir)
        identifier = _source_id(uploaded)
        manifest.update(source_job_id=identifier, state="selecting_components")
        _save(directory, manifest)
        _submit(web, uploaded, selected, _chains(uploaded))
        manifest["state"] = "parameterizing"
        _save(directory, manifest)
        if progress:
            progress({"state": "parameterizing", "job_id": identifier, "source_job_id": identifier})
        uploads = ({f"sdf_cgenff[{group['resname']}]": group["sdf"] for group in groups} if groups is not None else
                   {f"sdf_cgenff[{config['ligand_resname']}]": manifest["inputs"]["files"]["ligand.sdf"]})
        _submit(web, selected, parameterized, _preparation_fields(selected, config, groups), uploads)
        # Completed-page parsing is kept separate from transport because API
        # status=null and monitor.php output cannot classify web preparation.
        _await_preparation(web, directory, manifest, parameterized, wait, interval, max_wait, progress)
        if manifest["state"] != "prepared":
            return manifest
        prepared_config = api_config(config, identifier)
        if groups is not None:
            prepared_config["expected_ligands"] = [group["resname"] for group in groups]
            prepared_config = validate_config(prepared_config)
        return _start_prepared_bilayer(directory, manifest, api, prepared_config, wait, interval, max_wait, progress)


def _start_prepared_bilayer(directory, manifest, api, prepared_config, wait, interval, max_wait, progress):
    """Common API clone path for original and evidence-verified reused sources."""
    build_directory = directory / "bilayer"
    atomic_json(directory / "api-config.json", prepared_config)
    manifest["state"] = "submitting_bilayer"
    _save(directory, manifest)
    if progress:
        progress({"state": "submitting_bilayer", "job_id": manifest["source_job_id"], "source_job_id": manifest["source_job_id"]})
    # workflow.start creates its own durable intent and never retries a POST.
    result = start_api(api, prepared_config, build_directory)
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
            raise ToolError("Preparation response belongs to a different job; refusing to continue.")
        if page["state"] == "auth_required":
            manifest["state"] = "preparation_auth_required"
            _save(directory, manifest)
            raise AuthError("Website session expired. Run charmm-gui-cli login and build-resume.")
        if page["state"] == "error":
            manifest.update(state="preparation_failed", preparation_errors=page["errors"])
            _save(directory, manifest)
            raise ToolError("CHARMM-GUI preparation failed. See the saved HTML and full-run.json; no repeated POST was sent.",
                            category="remote_failure", next_step="Inspect the saved preparation HTML/errors; do not repeat this POST.")
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
            raise ToolError("Preparation completion was not established: expected orientation step and PDB/PSF/ligand parameter links. Inspect the saved page.")
        manifest["state"] = "preparation_running"
        _save(directory, manifest)
        if not wait:
            return
        if time.monotonic() >= deadline:
            raise ToolError("Preparation wait limit reached; build-resume can continue later.",
                            category="wait_timeout", retryable=True, next_step="Run build-resume on the existing run.")
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
