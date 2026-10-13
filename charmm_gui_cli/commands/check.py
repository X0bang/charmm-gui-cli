"""check command handler."""



def execute(args):
    from ..cli import (
        emit,
        precheck_summary,
        ToolError,
        atomic_json,
        Path,
        sys,
    )
    from ..precheck import check_inputs
    from ..preview import render_model, new_preview_path
    report = check_inputs(out=args.out or new_preview_path("checks"), protein=args.protein,
        ligand=args.ligand, complex_pdb=args.complex_pdb, components=args.components,
        ligand_resname=args.ligand_resname, ligand_smiles=args.ligand_smiles,
        accept_conect_bond_orders=args.accept_conect_bond_orders, hydrogens=args.hydrogens)
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
