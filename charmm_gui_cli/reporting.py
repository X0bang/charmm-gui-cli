"""Offline, escaped result presentation; never submits or repairs a model."""

import base64
import hashlib
import html
import json
from pathlib import Path
import tarfile

from .auth import ToolError
from .progress import official_links
from .i18n import get_language, t


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_images(directory, report):
    """Read existing previews only; reject stale, changed or unrelated files."""
    directory = Path(directory).resolve()
    cache = directory / "results/preview.json"
    if not cache.is_file():
        return [], t("report.no_preview")
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        identity = saved["identity"]
        if not report.get("fingerprint") or identity["validation_fingerprint"] != report["fingerprint"]:
            raise ValueError("stale preview")
        source = Path(saved["source"])
        model = Path(saved["model"])
        roots = (directory / "results/checks", directory / "results/previews")
        if (source.is_symlink() or model.is_symlink() or
                not source.resolve().is_relative_to(roots[0]) or
                not model.resolve().is_relative_to(roots[1])):
            raise ValueError("unrelated model")
        expected = saved["source_sha256"]
        if identity["source_sha256"] != expected or _hash(source) != expected or _hash(model) != expected:
            raise ValueError("changed model")
        if saved["scope"] not in ("final_simulation_input", "pre_input_assembly"):
            raise ValueError("unexpected preview scope")
        # Bind the extracted coordinate file to the actual downloaded member,
        # not merely to a user-editable preview manifest.
        suffix = "/gromacs/step5_input.pdb" if saved["scope"] == "final_simulation_input" else "/step5_assembly.pdb"
        with tarfile.open(directory / "bilayer/charmm-gui.tgz", "r:gz") as archive:
            members = [m for m in archive.getmembers() if m.isfile() and m.name.endswith(suffix)]
            if len(members) != 1:
                raise ValueError("ambiguous archive model")
            digest = hashlib.sha256()
            with archive.extractfile(members[0]) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError("archive model mismatch")
        images = []
        for entry in saved["images"]:
            path = Path(entry["file"])
            if (entry["view"] not in ("side", "top", "ligand") or path.is_symlink() or
                    not path.resolve().is_relative_to(roots[1]) or path.stat().st_size > 20 * 1024 * 1024):
                raise ValueError("unsafe preview image")
            data = path.read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("changed preview image")
            images.append({"view": entry["view"], "file": str(path), "data": data})
        return images, t("report.final_preview" if saved["scope"] == "final_simulation_input" else "report.assembly_preview")
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        return [], t("report.stale_preview")


def display_result(summary, context, manifest, report):
    """A fixed-schema display model, excluding credentials and raw server data."""
    config = manifest.get("config", {})
    return {**summary, "directory": context.get("directory"),
            "source_job_id": context.get("source_job_id"), "build_job_id": context.get("build_job_id"),
            "parameters": {"membrane": config.get("membrane"), "ions": config.get("ions"),
                           "orientation": config.get("preparation", {}).get("orientation")},
            "compilation_status": context.get("compilation_status", "unknown"),
            "atom_counts": report.get("checks", {}).get("total_atom_count"),
            "residue_counts": report.get("coordinate_residue_counts"),
            "warnings": report.get("review_warnings", []),
            "archive": context.get("archive"),
            "official_links": official_links(source_job_id=context.get("source_job_id"),
                                             build_job_id=context.get("build_job_id")),
            "simulation_note": "结构验收和 GROMACS 输入编译不代表已执行平衡或生产 MD，也不证明充分最小化。"}


def _value(value):
    if value is None:
        return t("output.unconfirmed")
    if value is True:
        return t("output.passed")
    if value is False:
        return t("output.failed")
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)


def render_text(result):
    passed = result.get("passed")
    state = t("report.review_pass") if passed is True and result.get("review_required") else _value(passed)
    compilation = {"passed": t("output.passed"), "failed": t("output.failed"),
                   "not_run": t("output.not_run"), "unknown": t("output.unconfirmed")}
    parameters = result.get("parameters", {})
    membrane, ions = parameters.get("membrane") or {}, parameters.get("ions") or {}
    environment = result.get("environment") or {}
    leaflets = result.get("acceptance", {}).get("leaflets") or {}
    atoms, pose = result.get("atom_counts") or {}, result.get("bound_pose") or {}
    lines = [t("report.title"), t("report.task", name=result.get("name") or result.get("directory")),
             t("report.validation", state=state),
             t("output.compilation", state=compilation.get(result.get("compilation_status"), t("output.unconfirmed"))),
             t("report.ids", source=_value(result.get("source_job_id")), build=_value(result.get("build_job_id"))),
             "", t("report.parameters"),
             t("report.leaflets", upper=_value(membrane.get("upper")), lower=_value(membrane.get("lower"))),
             t("report.salt", salt=_value(ions.get("type")), concentration=_value(ions.get("concentration_M")), orientation=_value(parameters.get("orientation"))),
             t("report.padding", margin=_value(membrane.get("margin_A")), water=_value(membrane.get("water_padding_A"))),
             "", t("report.system"),
             t("report.atoms", coordinates=_value(atoms.get("coordinates")), topology=_value(atoms.get("topology"))),
             t("report.solvent", lipids=_value(environment.get("lipids")), waters=_value(environment.get("water_molecules"))),
             t("report.lipids", upper=_value(leaflets.get("upper", {}).get("lipid_counts")), lower=_value(leaflets.get("lower", {}).get("lipid_counts"))),
             t("report.ions", ions=_value(environment.get("ions"))),
             t("report.concentration", state=_value(environment.get("concentration_verified"))),
             "", t("report.pose")]
    if result.get("components"):
        for name, component in result["components"].items():
            copies, penalty = component.get("copies") or {}, component.get("cgenff") or {}
            lines.extend([t("report.component", name=name, state=_value(component.get("passed")), coordinates=_value(copies.get("coordinates")), topology=_value(copies.get("topology"))),
                          t("report.displacement", rmsd=_value(component.get("ligand_rmsd_A")), maximum=_value(component.get("ligand_max_displacement_A"))),
                          t("report.penalty", parameter=_value(penalty.get("parameter_max")), charge=_value(penalty.get("charge_max")), state=_value(penalty.get("status")))])
            if component.get("pose_error"):
                lines.append(t("report.pose_error", error=_value(component["pose_error"])))
    else:
        lines.extend([t("report.ligand_rmsd", rmsd=_value(pose.get("ligand_rmsd_after_protein_fit_A"))),
                      t("report.ligand_max", maximum=_value(pose.get("ligand_max_displacement_A")))])
    lines.extend(["", t("report.warnings")])
    warnings = result.get("warnings", [])
    lines.extend("- " + _value(item) for item in warnings)
    if not warnings:
        lines.append(t("report.no_warnings"))
    lines.extend(["", t("report.archive", path=_value(result.get("archive"))),
                  t("report.path", path=_value(result.get("report"))), t("report.simulation")])
    return "\n".join(lines) + "\n"


def render_html(result, images=(), preview_note=""):
    escape = lambda value: html.escape(str(value), quote=True)
    labels = {"side": t("report.side"), "top": t("report.top"), "ligand": t("report.ligand")}
    figures = "".join('<figure><figcaption>' + labels[item["view"]] + '</figcaption><img alt="' +
                      labels[item["view"]] + '" src="data:image/png;base64,' +
                      base64.b64encode(item["data"]).decode("ascii") + '"></figure>' for item in images)
    links = "".join('<li><a rel="noreferrer" href="' + escape(url) + '">' + escape(role) + '</a></li>'
                    for role, url in result.get("official_links", {}).items())
    return ('<!doctype html><html lang="' + ("zh-CN" if get_language() == "zh" else "en") + '"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; '
            'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            '<title>' + escape(t("report.title")) + '</title><style>body{font-family:system-ui,sans-serif;'
            'max-width:1100px;margin:2rem auto;padding:0 1rem;color:#20303d}pre{white-space:pre-wrap;'
            'overflow-wrap:anywhere;background:#f4f7fa;padding:1rem}img{width:100%;height:auto}'
            'figure{margin:1rem 0}figcaption{font-weight:600}a{color:#156781}</style>'
            '<h1>' + escape(t("report.title")) + '</h1><pre>' + escape(render_text(result)) + '</pre>'
            '<h2>' + escape(t("report.previews")) + '</h2><p>' + escape(preview_note) + '</p>' + figures +
            '<h2>' + escape(t("report.links")) + '</h2><ul>' + links + '</ul></html>')


def write_new(path, content):
    """Explicit export only, exclusively create; never overwrite an old report."""
    path = Path(path).expanduser()
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        raise ToolError("Cannot create report; choose a new filename in an existing writable directory.") from exc
    return str(path.resolve())
