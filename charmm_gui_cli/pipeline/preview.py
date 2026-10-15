"""Preview orchestration for evidence-bound completed tasks."""

import charmm_gui_cli.chem.preview as _coordinate_views

import hashlib
from pathlib import Path
import shlex
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.layout import full_archive_path, previews_path


def preview_run(directory, *, report=None, out=None, width=1400, views=("side", "top", "ligand")):
    """Render the archived final input, never an arbitrary/stale extracted PDB."""
    import tarfile
    import uuid
    from charmm_gui_cli import __version__
    from charmm_gui_cli.store.tasks import _read_json
    from charmm_gui_cli.pipeline.managed import _final_summary
    from charmm_gui_cli.store.locking import run_lock
    directory = Path(directory).expanduser().resolve()
    with run_lock(directory):
        summary = _final_summary(directory)
        if summary.get("validation_status") == "stale_or_unreadable" or not summary.get("report_file"):
            raise ToolError("No current final validation evidence. Resume the task before requesting a model preview.")
        stored = _read_json(directory / "results" / "validation.json")
        if report is not None and report.get("fingerprint") != stored.get("fingerprint"):
            raise ToolError("Preview report does not match current final validation evidence.")
        extracted = Path(stored["extracted_directory"]).resolve()
        from charmm_gui_cli.store.layout import modern, task_root
        valid = (extracted == task_root(directory) / "model" if modern(directory)
                 else extracted.is_relative_to(directory / "results" / "checks"))
        if not valid:
            raise ToolError(key="preview.invalid_model_path")
        finals = [p for p in extracted.rglob("step5_input.pdb") if p.parent.name == "gromacs"]
        if len(finals) > 1:
            raise ToolError("Multiple final-input PDB files exist; refusing an ambiguous preview.")
        pdb = finals[0] if finals else extracted / "gromacs" / "step5_input.pdb"
        scope = "final_simulation_input"
        suffix = "gromacs/step5_input.pdb"
        if not pdb.is_file():
            assemblies = list(extracted.rglob("step5_assembly.pdb"))
            if len(assemblies) != 1:
                raise ToolError("No unique supported final-input or assembly PDB exists for preview.")
            pdb = assemblies[0]
            suffix, scope = "step5_assembly.pdb", "pre_input_assembly"
        if not pdb.is_file() or pdb.is_symlink():
            raise ToolError("No unique supported final-input or assembly PDB exists for preview.")
        source_hash = _coordinate_views._digest(pdb)
        archive = full_archive_path(directory)
        with tarfile.open(archive, "r:gz") as handle:
            matches = [m for m in handle.getmembers() if m.isfile() and
                       (m.name.lstrip("./") == suffix or m.name.endswith("/" + suffix))]
            if len(matches) != 1:
                raise ToolError("Archive has no unique matching preview PDB; refusing an ambiguous model.")
            stream = handle.extractfile(matches[0])
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            if digest.hexdigest() != source_hash:
                raise ToolError("Extracted preview coordinates differ from the verified archive.")
            psf = pdb.with_suffix(".psf")
            if psf.is_file():
                psf_suffix = suffix[:-4] + ".psf"
                psfs = [m for m in handle.getmembers() if m.isfile() and
                        (m.name.lstrip("./") == psf_suffix or m.name.endswith("/" + psf_suffix))]
                if len(psfs) != 1 or psf.is_symlink():
                    raise ToolError("Preview PSF has no unique verified archive member.")
                digest = hashlib.sha256()
                psf_stream = handle.extractfile(psfs[0])
                for block in iter(lambda: psf_stream.read(1024 * 1024), b""):
                    digest.update(block)
                if digest.hexdigest() != _coordinate_views._digest(psf):
                    raise ToolError("Extracted preview topology differs from the verified archive.")
        manifest = _read_json(directory / "full-run.json")
        names = manifest["config"].get("component_resnames") or [manifest["config"].get("ligand_resname", "LIG")]
        copy_model = not modern(directory) or out is not None
        identity = {"source_sha256": source_hash, "renderer_revision": 4 if modern(directory) else 3, "width": width, "views": list(views),
                    "scope": scope, "tool_version": __version__, "validation_fingerprint": stored["fingerprint"]}
        preview_root = previews_path(directory)
        cache = directory / "results" / "preview.json"
        if out is None and cache.is_file():
            try:
                previous = _read_json(cache)
            except ToolError:
                previous = {}
            if previous.get("identity") == identity:
                artifacts = [(item.get("file"), item.get("sha256")) for item in previous.get("images", [])]
                try:
                    model = Path(previous["model"])
                    model_valid = (model.resolve().is_relative_to(preview_root) if copy_model else model.resolve() == pdb.resolve())
                    if (artifacts and model_valid and not model.is_symlink() and _coordinate_views._digest(model) == source_hash
                            and all(not Path(p).is_symlink() and Path(p).resolve().is_relative_to(preview_root) and
                                    _coordinate_views._digest(p) == sha for p, sha in artifacts)):
                        return previous
                except (KeyError, TypeError, OSError):
                    pass
        if out is None and (preview_root.is_symlink() or not preview_root.resolve().is_relative_to(task_root(directory))):
            raise ToolError(key="preview.public_path_invalid", params={"path": str(preview_root)},
                            next_step=shlex.join(["charmm-gui-cli", "jobs", "status", str(task_root(directory))]))
        destination = Path(out) if out is not None else preview_root / uuid.uuid4().hex[:12]
        rendered = _coordinate_views.render_model(pdb, destination, ligand_resnames=names, width=width, views=views,
                                                   scope=scope, copy_model=copy_model)
        rendered["identity"] = identity
        from charmm_gui_cli.presentation.progress import official_links
        rendered["official_links"] = official_links(
            source_job_id=manifest.get("source_job_id"), build_job_id=manifest.get("build_job_id"))
        atomic_json(destination / "preview.json", rendered)
        if out is None:
            atomic_json(cache, rendered)
        return rendered


# Backward-compatible function exports; internal code uses the owner module.
from charmm_gui_cli.chem.preview import WATER, IONS, PROTEIN, TAIL_NAMES, ELEMENT_COLORS
from charmm_gui_cli.chem.preview import _dependencies, _digest, _font, _primitives, _psf_edges, _read_psf_edges, _read_scene, _render, new_preview_path, read_scene, render_model
