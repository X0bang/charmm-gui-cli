"""Bind explicit fragments to sealed inputs, successful forms and source GETs.

The chemistry checker deliberately does not assert server provenance. This
layer supplies the separate request/download chain. Rechecking is read-only;
interrupted GETs may resume, but a changed receipt is never refreshed and no
modeling POST occurs here. Public opt-in requires final/reuse integration too.
"""

import hashlib
from pathlib import Path, PurePosixPath
import re
import shlex
import tempfile
from urllib.parse import parse_qs, urlparse

from ..chem.fragment_inputs import verify_prepared_fragments
from ..chem.fragment_proof import verify_fragment_artifact
from ..chem.protein_state_proof import canonical
from ..chem.protein_states import intent_for
from ..locales.i18n import t
from ..remote.web import modeling_target, merged_form_data, soup_for
from ..remote.web_state import parse_page_state
from ..store.auth import ToolError, atomic_write
from ..store.identifiers import job_id
from ..store.paths import read_value


REQUEST = "fragment-request.json"
RECEIPT = "preparation/fragment-proof.json"
_ORIGINS = {"charmm-gui.org", "www.charmm-gui.org", "charmm-gui.org:443", "www.charmm-gui.org:443"}
_ARTIFACTS = {"pdb": "step1_pdbreader.pdb", "psf": "step1_pdbreader.psf", "topology": "top_all36_prot.rtf"}


def failure(directory):
    command = shlex.join(["charmm-gui-cli", "jobs", "status", str(Path(directory).absolute())])
    return ToolError(key="fragment.proof_invalid", category="fragment_topology_unverified",
                     next_step=t("fragment.proof_next", command=command))


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _json(path):
    value = read_value(path)
    if not isinstance(value, dict):
        raise ValueError
    return value


def _directory(value):
    directory = Path(value).expanduser().absolute()
    if directory.is_symlink():
        raise failure(directory)
    return directory.resolve()


def _path(directory, value, *, preparation=False):
    """Accept existing run-local regular files, not symlinked ancestors."""
    if not isinstance(value, (str, Path)):
        raise ValueError
    path = Path(value)
    path = path if path.is_absolute() else directory / path
    relative = path.relative_to(directory)
    if (not relative.parts or ".." in relative.parts or "\\" in str(relative)
            or preparation and relative.parts[0] != "preparation"
            or path.is_symlink() or not path.is_file()
            or any((directory / Path(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts)))
            or not path.resolve().is_relative_to(directory.resolve())):
        raise ValueError
    return path


def record_for(manifest):
    """Verify the actual staged files and on-disk/embedded fragment recipes."""
    try:
        inputs = manifest.get("inputs", {})
        local = inputs.get("manifest", {})
        normalization = local.get("input_normalization", {})
        if isinstance(normalization, dict) and "fragment_preparation" not in normalization:
            return None
        record = verify_prepared_fragments(inputs)
        if record is None:
            raise ValueError
        files = inputs["files"]
        root = Path(files["complex.pdb"]).parent.absolute()
        if root.is_symlink():
            raise ValueError
        for name, value in files.items():
            if not isinstance(name, str) or Path(name).name != name or "\\" in name:
                raise ValueError
            if _path(root, value) != root / name:
                raise ValueError
        actual = _json(files["input-manifest.json"])
        if (canonical(verify_prepared_fragments({"files": files, "manifest": actual})) != canonical(record)
                or canonical(intent_for(actual)) != canonical(intent_for(local))):
            raise ValueError
        return record
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        from ..chem.fragment_inputs import failure as input_failure
        raise input_failure() from None


def request_kwargs(manifest, mapping=None):
    record = record_for(manifest)
    if record is None:
        return {}
    files = manifest["inputs"]["files"]
    return {"fragment_plan": record["plan"], "fragment_source_pdb": files[record["source_file"]],
            # This older internal argument name denotes the full actual upload.
            "fragment_protein_pdb": files[record["prepared_file"]], "fragment_mapping": mapping}


def validate_local(directory, manifest):
    record = record_for(manifest)
    if record is None:
        return None
    try:
        from .build_config import validate_preparation
        from ..chem.fragment_terminals import resolve_fragment_terminals
        files = manifest["inputs"]["files"]
        resolve_fragment_terminals(validate_preparation(manifest["config"]["preparation"], allow_fragments=True),
            Path(files[record["source_file"]]).read_text(encoding="utf-8").splitlines(),
            Path(files[record["prepared_file"]]).read_text(encoding="utf-8").splitlines(), record["plan"])
        return record
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        raise failure(directory) from None


def seal_request(directory, manifest, *, create=False):
    record = validate_local(directory, manifest)
    if record is None:
        return None
    directory = _directory(directory)
    try:
        from .build_config import validate_preparation
        if directory.is_symlink():
            raise ValueError
        body = {"schema_version": 1, "fragment_record_sha256": canonical(record),
                "preparation_sha256": canonical(validate_preparation(manifest["config"]["preparation"], allow_fragments=True)),
                "input_sha256": {name: _digest(path) for name, path in manifest["inputs"]["files"].items()}}
        path = directory / REQUEST
        if path.is_symlink():
            raise ValueError
        if path.exists():
            if canonical(_json(path)) != canonical(body):
                raise ValueError
        elif (create and manifest.get("state") == "inputs_ready" and not manifest.get("source_job_id")
                and not manifest.get("reused_preparation") and not (directory / "preparation").exists()
                and not (directory / "bilayer").exists()):
            from ..store.tasks import _new_json
            _new_json(path, body)
        else:
            raise ValueError
        return body
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        raise failure(directory) from None


def _artifact_url(url, expected):
    try:
        parsed = urlparse(url) if isinstance(url, str) else None
        return (parsed is not None and parsed.scheme == "https" and parsed.netloc.lower() in _ORIGINS
                and parsed.path == expected and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _form(path, identifier):
    forms = [form for form in soup_for(path.read_text(encoding="utf-8")).find_all("form")
             if form.select_one('input[name="jobid"]')]
    if len(forms) != 1:
        raise ValueError
    ids = forms[0].find_all("input", {"name": "jobid"})
    if len(ids) != 1 or ids[0].get("value") != identifier or ids[0].has_attr("disabled"):
        raise ValueError
    return forms[0]


def _successful_intent(directory, source, target, fields, uploads, identifier):
    form = _form(source, identifier)
    intent_path = _path(directory, target.with_suffix(".intent.json"), preparation=True)
    intent = _json(intent_path)
    expected = [list(pair) for pair in merged_form_data(form, fields, uploads)]
    if (intent.get("state") != "response_received" or canonical(intent.get("fields")) != canonical(expected)
            or intent.get("upload_fields") != list(uploads) or intent.get("target") != modeling_target(form.get("action"))):
        raise ValueError
    if "snapshot" in intent and _path(directory, intent["snapshot"], preparation=True) != target:
        raise ValueError
    return intent_path


def _request(directory, manifest):
    from .build_config import validate_preparation
    from .full_build import _preparation_fields, _remote_groups, _chains
    record = record_for(manifest)
    if record is None:
        return None
    try:
        seal = seal_request(directory, manifest)
        identifier = job_id(manifest["source_job_id"])
        upload = _path(directory, "preparation/upload-response.html", preparation=True)
        selected = _path(directory, "preparation/selection.html", preparation=True)
        parameterized = _path(directory, "preparation/parameterization.html", preparation=True)
        web_run = _path(directory, "preparation/web-run.json", preparation=True)
        uploaded = _json(web_run)
        _form(upload, identifier)
        if (uploaded.get("state") != "upload_response_received" or uploaded.get("job_id") != identifier
                or Path(uploaded["input_pdb"]).absolute() != Path(manifest["inputs"]["files"]["complex.pdb"]).absolute()):
            raise ValueError
        evidence = {upload, selected, parameterized, web_run, directory / REQUEST}
        evidence.add(_successful_intent(directory, upload, selected, _chains(upload), (), identifier))
        groups, mapping, states = _remote_groups(manifest), {}, {}
        fields = _preparation_fields(selected, manifest["config"], groups, list(manifest.get("terminal_adjustments", [])),
            state_intent=intent_for(manifest["inputs"]["manifest"]), reference_pdb=manifest["inputs"]["files"]["complex.pdb"],
            state_mapping=states, **request_kwargs(manifest, mapping))
        if canonical(manifest.get("fragment_mapping")) != canonical(mapping):
            raise ValueError
        if states and canonical(manifest.get("protein_state_mapping")) != canonical(states):
            raise ValueError
        names = [row["resname"] for row in groups] if groups is not None else [manifest["config"]["ligand_resname"]]
        uploads = ["sdf_cgenff[" + name + "]" for name in names]
        evidence.add(_successful_intent(directory, selected, parameterized, fields, uploads, identifier))
        ready = _path(directory, manifest["preparation_snapshot"], preparation=True)
        page = parse_page_state(ready.read_text(encoding="utf-8"))
        query = parse_qs(urlparse(page.get("next_url") or "").query)
        _form(ready, identifier)
        prefix = "/uploaded_pdb/" + identifier + "/"
        required = {prefix + name for name in ("step1_pdbreader.pdb", "step1_pdbreader.psf")}
        required.update(prefix + name.lower() + "/" + name.lower() + "." + ext for name in names for ext in ("rtf", "prm"))
        if (page["state"] != "ready" or page["formstop"] != 0 or page["job_id"] != identifier
                or query.get("doc") != ["input/membrane.bilayer"] or query.get("step") != ["2"]
                or not required <= {urlparse(url).path.lower() for url in page["artifacts"]}
                or page["artifacts"] != manifest.get("preparation_artifacts")):
            raise ValueError
        urls = {}
        for key in ("pdb", "psf"):
            choices = {url for url in page["artifacts"] if _artifact_url(url, prefix + _ARTIFACTS[key])}
            if len(choices) != 1:
                raise ValueError
            urls[key] = choices.pop()
        origin = urlparse(urls["pdb"])
        urls["topology"] = origin.scheme + "://" + origin.netloc + prefix + "toppar/" + _ARTIFACTS["topology"]
        evidence.add(ready)
        return {"record": record, "seal": seal, "mapping": mapping, "job_id": identifier, "urls": urls,
                "preparation": validate_preparation(manifest["config"]["preparation"], allow_fragments=True),
                "evidence": {path.relative_to(directory).as_posix(): _digest(path) for path in evidence}}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        raise failure(directory) from None


def _checks(manifest, request, paths):
    files, record = manifest["inputs"]["files"], request["record"]
    return verify_fragment_artifact(request["preparation"], Path(files[record["source_file"]]).read_text(encoding="utf-8").splitlines(),
        Path(files[record["prepared_file"]]).read_text(encoding="utf-8").splitlines(), record["plan"], request["mapping"],
        paths["pdb"], paths["psf"], paths["topology"], state_intent=record["source_state_intent"])


def _receipt_body(request, paths, checks, directory):
    return {"schema_version": 1, "job_id": request["job_id"], "fragment_record_sha256": canonical(request["record"]),
            "request_sha256": canonical(request["seal"]), "mapping": request["mapping"], "checks": checks,
            "artifact_urls": request["urls"], "artifacts": {key: path.relative_to(directory).as_posix() for key, path in paths.items()},
            "evidence_sha256": {**request["evidence"], **{path.relative_to(directory).as_posix(): _digest(path) for path in paths.values()}},
            "scope": "successful_preparation_request_and_same_source_fragment_artifacts",
            "source_origin_verified": True, "scientific_correctness_verified": False}


def check_prepared(directory, manifest):
    """Recompute the exact successful request and check all retained evidence."""
    directory = _directory(directory)
    request = _request(directory, manifest)
    if request is None:
        return None
    try:
        receipt = _json(_path(directory, RECEIPT, preparation=True))
        stored = receipt["artifacts"]
        if not isinstance(stored, dict) or set(stored) != set(_ARTIFACTS):
            raise ValueError
        paths = {}
        for key, name in stored.items():
            relative = PurePosixPath(name)
            if (not isinstance(name, str) or relative.is_absolute() or len(relative.parts) != 3
                    or relative.parts[0] != "preparation" or not relative.parts[1].startswith("fragment-")
                    or relative.parts[2] != _ARTIFACTS[key]):
                raise ValueError
            paths[key] = _path(directory, name, preparation=True)
        if len({path.parent for path in paths.values()}) != 1:
            raise ValueError
        body = _receipt_body(request, paths, _checks(manifest, request, paths), directory)
        if canonical(receipt) != canonical({**body, "fingerprint": canonical(body)}):
            raise ValueError
        if request != _request(directory, manifest):
            raise ValueError
        return receipt
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        raise failure(directory) from None


def ensure_prepared(directory, manifest, web):
    """Fetch only read-only artifacts; retain interrupted candidates and receipts."""
    directory = _directory(directory)
    request = _request(directory, manifest)
    if request is None:
        return None
    receipt_path = directory / RECEIPT
    if receipt_path.exists() or receipt_path.is_symlink():
        return check_prepared(directory, manifest)
    work = Path(tempfile.mkdtemp(prefix="fragment-", dir=directory / "preparation"))
    paths = {}
    for key, target in request["urls"].items():
        response = web.request("GET", target)
        try:
            if not _artifact_url(response.url, urlparse(target).path):
                raise failure(directory)
            content = response.text
            limit = (4 if key == "topology" else 128) * 1024 * 1024
            if (not isinstance(content, str) or not 0 < len(content.encode("utf-8")) <= limit
                    or content.lstrip().lower().startswith(("<!doctype html", "<html", "<form"))):
                raise failure(directory)
            paths[key] = work / _ARTIFACTS[key]
            atomic_write(paths[key], content)
        finally:
            response.close()
    checks = _checks(manifest, request, paths)
    if request != _request(directory, manifest):
        raise failure(directory)
    receipt = _receipt_body(request, paths, checks, directory)
    receipt["fingerprint"] = canonical(receipt)
    from ..store.tasks import _new_json
    _new_json(receipt_path, receipt)
    return check_prepared(directory, manifest)


def source_binding(receipt):
    """Small portable source identity retained by an independently built child."""
    return {"fingerprint": receipt["fingerprint"], "job_id": receipt["job_id"],
            "fragment_record_sha256": receipt["fragment_record_sha256"], "mapping_sha256": canonical(receipt["mapping"]),
            "source_artifacts_sha256": receipt["checks"]["evidence_sha256"], "source_origin_verified": True}


def require_prepared(directory, manifest):
    """Called immediately before cloning; reused children recheck their source."""
    if record_for(manifest) is None:
        return
    if manifest.get("reused_preparation"):
        from .sweep import validate_reuse
        validate_reuse(directory, manifest)
    else:
        receipt = check_prepared(directory, manifest)
        if canonical(manifest.get("fragment_source_proof")) != canonical(source_binding(receipt)):
            raise failure(directory)


def validation_request(directory, manifest):
    """Keep completed child validation independent of a mounted source task.

    The source certificate was checked before cloning. A child's retained
    binding must still match that certificate AND its own preparation identity;
    final archive files must separately match these exact source artifact hashes.
    """
    record = record_for(manifest)
    if record is None:
        return None
    try:
        from .build_config import validate_preparation
        mapping = manifest["fragment_mapping"]
        binding = manifest["fragment_source_proof"]
        if (not isinstance(binding, dict) or set(binding) != {"fingerprint", "job_id", "fragment_record_sha256",
                "mapping_sha256", "source_artifacts_sha256", "source_origin_verified"}
                or binding["job_id"] != job_id(manifest["source_job_id"]) or binding["source_origin_verified"] is not True
                or binding["fragment_record_sha256"] != canonical(record) or binding["mapping_sha256"] != canonical(mapping)
                or not isinstance(binding["fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", binding["fingerprint"])
                or set(binding["source_artifacts_sha256"]) != set(_ARTIFACTS)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in binding["source_artifacts_sha256"].values())):
            raise ValueError
        if manifest.get("reused_preparation"):
            from .sweep import preparation_identity
            certificate = manifest["reused_preparation"]["certificate"]
            if (canonical(certificate.get("fragment_source_proof")) != canonical(binding)
                    or certificate.get("source_job_id") != binding["job_id"]
                    or canonical(certificate.get("identity")) != canonical(preparation_identity(directory, manifest))):
                raise ValueError
        elif canonical(source_binding(check_prepared(directory, manifest))) != canonical(binding):
            raise ValueError
        return {"record": record, "mapping": mapping, "source_binding": binding,
                "preparation": validate_preparation(manifest["config"]["preparation"], allow_fragments=True)}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        raise failure(directory) from None


def verify_archive(directory, manifest, root):
    """Require actual source AND assembly caps and the authenticated source bytes."""
    request = validation_request(directory, manifest)
    if request is None:
        return None
    from ..chem.fragment_proof import verify_fragment_archive
    files, record = manifest["inputs"]["files"], request["record"]
    checks = verify_fragment_archive(root, request["preparation"],
        Path(files[record["source_file"]]).read_text(encoding="utf-8").splitlines(),
        Path(files[record["prepared_file"]]).read_text(encoding="utf-8").splitlines(),
        record["plan"], request["mapping"], state_intent=record["source_state_intent"])
    if canonical(checks["source"]["evidence_sha256"]) != canonical(request["source_binding"]["source_artifacts_sha256"]):
        raise failure(directory)
    return {"required": True, "passed": True, "server_origin_verified": True,
            "request_sha256": canonical(request), "source_proof_fingerprint": request["source_binding"]["fingerprint"],
            "checks": checks, "scientific_correctness_verified": False}
