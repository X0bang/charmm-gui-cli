"""Evidence-bound state proof after preparation; only artifact GETs occur here."""

import hashlib
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import urlparse

from ..chem.protein_states import intent_for, validate_intent
from ..chem.protein_state_proof import verify_states, canonical, failure
from ..store.auth import atomic_json, atomic_write
from ..store.paths import read_value
from ..store.identifiers import job_id
from ..remote.web import soup_for, merged_form_data, modeling_target

RECEIPT = "preparation/protein-state-proof.json"
REQUEST = "protein-state-request.json"
ORIGINS = ("charmm-gui.org", "www.charmm-gui.org", "charmm-gui.org:443", "www.charmm-gui.org:443")


def _artifact_url(url, expected):
    try:
        parsed = urlparse(url) if isinstance(url, str) else None
        return (parsed is not None and parsed.scheme == "https" and parsed.netloc.lower() in ORIGINS
                and parsed.path == expected and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    try:
        value = read_value(path)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise failure() from None


def seal_request(directory, manifest, *, create=False):
    plan = validate_intent(intent_for(manifest["inputs"].get("manifest", {})))
    if not plan:
        return
    directory = Path(directory)
    actual = _json(manifest["inputs"]["files"]["input-manifest.json"])
    if canonical(intent_for(actual)) != canonical(plan):
        raise failure()
    body = {"schema_version": 1, "intent_sha256": canonical(plan),
            "input_sha256": {name: _digest(path) for name, path in manifest["inputs"]["files"].items()}}
    path = directory / REQUEST
    if path.is_symlink():
        raise failure()
    if path.exists():
        if _json(path) != body:
            raise failure()
    elif create and not (directory / "preparation").exists():
        atomic_json(path, body)
    else:
        raise failure()


def _request(directory, manifest):
    """Recompute fields from the actual saved form and sealed input recipe."""
    from .full_build import _preparation_fields, _remote_groups
    seal_request(directory, manifest)
    plan = validate_intent(intent_for(manifest["inputs"]["manifest"]))
    mapping = {}
    selected = Path(directory) / "preparation/selection.html"
    fields = _preparation_fields(selected, manifest["config"], _remote_groups(manifest),
        list(manifest.get("terminal_adjustments", [])), state_intent=plan,
        reference_pdb=manifest["inputs"]["files"]["complex.pdb"], state_mapping=mapping)
    soup = soup_for(selected.read_text(encoding="utf-8"))
    forms = [form for form in soup.find_all("form") if form.select_one('input[name="jobid"]')]
    if len(forms) != 1 or forms[0].select_one('input[name="jobid"]').get("value") != job_id(manifest["source_job_id"]):
        raise failure()
    names = ([group["resname"] for group in _remote_groups(manifest)] if _remote_groups(manifest) is not None else
             [manifest["config"]["ligand_resname"]])
    uploads = ["sdf_cgenff[" + name + "]" for name in names]
    intent = _json(Path(directory) / "preparation/parameterization.intent.json")
    expected = [list(pair) for pair in merged_form_data(forms[0], fields, uploads)]
    if (intent.get("state") != "response_received" or intent.get("fields") != expected or intent.get("upload_fields") != uploads
            or intent.get("target") != modeling_target(forms[0].get("action"))):
        raise failure()
    return plan, mapping


def check_prepared(directory, manifest):
    """Revalidate stored artifacts/POST receipt; never refresh a changed proof."""
    directory = Path(directory)
    plan = validate_intent(intent_for(manifest.get("inputs", {}).get("manifest", {})))
    if not plan:
        return None
    plan, mapping = _request(directory, manifest)
    path = directory / RECEIPT
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError
        receipt = _json(path)
        body = {key: value for key, value in receipt.items() if key != "fingerprint"}
        if (receipt.get("schema_version") != 1 or receipt.get("fingerprint") != canonical(body)
                or receipt.get("job_id") != job_id(manifest["source_job_id"])
                or receipt.get("intent_sha256") != canonical(plan) or receipt.get("mapping") != mapping
                or receipt.get("input_sha256") != _digest(manifest["inputs"]["files"]["complex.pdb"])):
            raise ValueError
        for name, digest in receipt["evidence_sha256"].items():
            relative = PurePosixPath(name)
            target = directory / name
            if (relative.is_absolute() or ".." in relative.parts or "\\" in name or not (name.startswith("preparation/") or name == REQUEST)
                    or target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != directory)
                    or _digest(target) != digest):
                raise ValueError
        pdb, psf = directory / receipt["pdb"], directory / receipt["psf"]
        if not {REQUEST, receipt["pdb"], receipt["psf"], "preparation/selection.html", "preparation/parameterization.intent.json"} <= receipt["evidence_sha256"].keys():
            raise ValueError
        current = verify_states(plan, mapping, pdb, psf, reference=manifest["inputs"]["files"]["complex.pdb"])
        if receipt.get("checks") != current:
            raise ValueError
        return receipt
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise failure() from None


def ensure_prepared(directory, manifest, web):
    directory = Path(directory)
    plan = validate_intent(intent_for(manifest.get("inputs", {}).get("manifest", {})))
    if not plan:
        return None
    if (directory / RECEIPT).exists():
        return check_prepared(directory, manifest)
    plan, mapping = _request(directory, manifest)
    identifier = job_id(manifest["source_job_id"])
    # This directory is exclusive; interrupted GET attempts never overwrite a
    # previous candidate or a user-edited proof. Resuming can safely repeat GET.
    work = Path(tempfile.mkdtemp(prefix="protein-state-", dir=directory / "preparation"))
    paths = {}
    for name in ("step1_pdbreader.pdb", "step1_pdbreader.psf"):
        expected = "/uploaded_pdb/" + identifier + "/" + name
        candidates = [url for url in manifest.get("preparation_artifacts", []) if _artifact_url(url, expected)]
        if len(set(candidates)) != 1:
            raise failure()
        target = candidates[0]
        response = web.request("GET", target)
        if not _artifact_url(response.url, expected):
            raise failure()
        text = response.text
        if not isinstance(text, str) or len(text.encode("utf-8")) > 128 * 1024 * 1024:
            raise failure()
        paths[name] = work / name
        atomic_write(paths[name], text)
    checks = verify_states(plan, mapping, paths["step1_pdbreader.pdb"], paths["step1_pdbreader.psf"],
                           reference=manifest["inputs"]["files"]["complex.pdb"])
    evidence = [*paths.values(), directory / REQUEST, directory / "preparation/selection.html", directory / "preparation/parameterization.intent.json"]
    snapshot = Path(manifest["preparation_snapshot"])
    if not snapshot.is_absolute():
        snapshot = directory / snapshot
    evidence.append(snapshot)
    receipt = {"schema_version": 1, "job_id": identifier, "intent_sha256": canonical(plan), "mapping": mapping,
        "input_sha256": _digest(manifest["inputs"]["files"]["complex.pdb"]), "checks": checks,
        "pdb": str(paths["step1_pdbreader.pdb"].relative_to(directory)),
        "psf": str(paths["step1_pdbreader.psf"].relative_to(directory)),
        "evidence_sha256": {str(path.relative_to(directory)): _digest(path) for path in evidence}}
    receipt["fingerprint"] = canonical(receipt)
    atomic_json(directory / RECEIPT, receipt)
    return check_prepared(directory, manifest)
