"""check command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.presentation.summaries import precheck_summary
    from charmm_gui_cli.store.auth import ToolError, atomic_json
    from pathlib import Path
    import sys
    from charmm_gui_cli.chem.precheck import check_inputs
    from charmm_gui_cli.chem.preview import render_model, new_preview_path
    from ..chem.residues import options_from_args
    report = check_inputs(out=args.out or new_preview_path("checks"), protein=args.protein,
        ligand=args.ligand, complex_pdb=args.complex_pdb, components=args.components,
        ligand_resname=args.ligand_resname, ligand_smiles=args.ligand_smiles,
        accept_conect_bond_orders=args.accept_conect_bond_orders, hydrogens=args.hydrogens, input_options=options_from_args(args))
    if report.get("passed") and not args.no_preview:
        try:
            report["preview"] = render_model(report["prepared_complex"], Path(report["directory"]) / "preview",
                ligand_resnames=report["component_resnames"], scope="prepared_input")
        except (ToolError, OSError) as exc:
            report["preview"] = {"status": "failed", "message": str(exc)}
        atomic_json(Path(report["check_file"]), report)
    emit(report if args.full else precheck_summary(report), command="check", full=args.full)
    if args.open:
        import shutil
        import subprocess
        opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
        images = report.get("preview", {}).get("images", [])
        if opener and images and images[0].get("file"):
            try:
                subprocess.Popen([opener, images[0]["file"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
    return 0 if report.get("passed") else 2
