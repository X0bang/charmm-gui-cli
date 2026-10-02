"""High-level CLI build orchestration; legacy YAML remains an advanced input."""

from pathlib import Path
import shutil
import sys

from .api import Client
from .auth import ToolError, load_token, resolve_cookie_path


def progress(event):
    """Keep machine-readable JSON on stdout; send safe progress to stderr."""
    state = event.get("state", "working")
    identifier = event.get("job_id")
    suffix = f" job={identifier}" if identifier else ""
    print(f"[charmm-gui-cli] {state}{suffix}", file=sys.stderr, flush=True)


def execute(args):
    from . import managed
    from .full_build import initialize, advance
    from .web import WebClient

    dry_run = getattr(args, "dry_run", False)
    if args.interval < 10 or args.max_wait <= 0:
        raise ToolError("--interval must be >=10 seconds and --max-wait must be positive.")
    if args.grompp and not shutil.which(args.gmx):
        raise ToolError("--grompp requires GROMACS. Install gmx or supply --gmx /path/to/gmx; no job was submitted.")
    if args.command == "build":
        source_fields = ("protein", "ligand", "complex_pdb", "ligand_resname", "ligand_smiles", "hydrogens")
        model_fields = ("upper", "lower", "salt", "salt_concentration", "margin", "water_padding",
                        "orientation", "n_terminal", "c_terminal")
        if args.config and (any(getattr(args, key) is not None for key in source_fields + model_fields)
                            or args.accept_conect_bond_orders):
            raise ToolError("Choose either a YAML configuration or direct structure/model options, not both. See build -h.")
        if not args.config and not (args.complex_pdb or (args.protein and args.ligand)):
            raise ToolError("Provide --protein protein.pdb --ligand bound.sdf, or --complex complex.pdb. See charmm-gui-cli build -h.")

    directory = None
    if args.command == "build-resume":
        directory = args.directory or managed.latest_run()
    token = cookies = None
    api_only = directory is not None and (directory / "bilayer" / "run.json").is_file()
    if not dry_run:
        token, source = load_token(args.token_file)
        cookies = resolve_cookie_path(args.cookies, source)
        if not api_only and not cookies.is_file():
            raise ToolError("The saved website session is missing. Run charmm-gui-cli login; custom token files need matching --cookies or a Cookie file beside the token.")

    if args.command == "build":
        progress({"state": "preparing_inputs (local)"})
        if args.config:
            directory = args.out or managed.new_run_path()
            initialize(args.config, directory)
        else:
            default = lambda value, fallback: fallback if value is None else value
            directory = managed.create_managed_build(
                protein=args.protein, ligand=args.ligand, complex_pdb=args.complex_pdb,
                ligand_resname=default(args.ligand_resname, "LIG"), ligand_smiles=args.ligand_smiles,
                accept_conect_bond_orders=args.accept_conect_bond_orders, output=args.out,
                membrane={"upper": default(args.upper, "POPC=1"), "lower": default(args.lower, "POPC=1"),
                          "margin_A": default(args.margin, 20), "water_padding_A": default(args.water_padding, 22.5)},
                ions={"type": default(args.salt, "KCl"), "concentration_M": default(args.salt_concentration, 0.15)},
                preparation={"orientation": default(args.orientation, "ppm"),
                             "n_terminal": default(args.n_terminal, "NTER"), "c_terminal": default(args.c_terminal, "CTER")},
                hydrogens=default(args.hydrogens, "add_missing"))
    directory = Path(directory).resolve()
    if (directory / "full-run.json").is_file():
        managed.remember_run(directory)
    if dry_run:
        return {"directory": str(directory), "state": "inputs_ready", "server_submitted": False,
                "next_step": "charmm-gui-cli login; charmm-gui-cli build-resume"}, 0
    print(f"[charmm-gui-cli] run={directory}", file=sys.stderr, flush=True)
    result = advance(directory, None if api_only else WebClient(cookies), Client(token), wait=args.wait,
                     interval=args.interval, max_wait=args.max_wait, progress=progress)
    summary = {"directory": str(directory), **{key: result[key] for key in
               ("state", "source_job_id", "build_job_id") if key in result}}
    if result.get("state") in ("ligands_present", "validation_failed", "downloaded_unverified"):
        progress({"state": "validating_final_archive (local)"})
        use_grompp = args.grompp or (not args.no_grompp and shutil.which(args.gmx) is not None)
        report = managed.finalize_managed_build(directory, grompp=use_grompp, gmx=args.gmx)
        summary.update(state="validated" if report.get("passed") else "validation_failed",
                       validation={"passed": report.get("passed"),
                                   "environment_passed": report.get("environment_validation", {}).get("passed"),
                                   "bound_pose_passed": report.get("bound_pose_validation", {}).get("passed"),
                                   "failed_checks": [key for key, value in report.get("checks", {}).items() if not value.get("passed")],
                                   "scientific_correctness_verified": False},
                       report=str(directory / "results" / "validation.json"),
                       archive=str(directory / "bilayer" / "charmm-gui.tgz"))
        summary["gromacs_compiled"] = bool(report.get("checks", {}).get("grompp", {}).get("passed"))
        if not use_grompp:
            summary["gromacs_note"] = "GROMACS compilation was not run; install gmx and build-resume --grompp to add it."
        return summary, 0 if report.get("passed") else 2
    summary["next_step"] = "charmm-gui-cli build-resume"
    return summary, 0
