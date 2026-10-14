"""preview command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.store.auth import ToolError
    from pathlib import Path
    from charmm_gui_cli.chem.preview import render_model, new_preview_path
    from charmm_gui_cli.pipeline.preview import preview_run
    if Path(args.target).is_file():
        report = render_model(args.target, args.out or new_preview_path(),
            ligand_resnames=args.ligand or ("LIG",), width=args.width,
            views=args.view or ("side", "top", "ligand"))
    else:
        if args.ligand:
            raise ToolError("Task previews use the recorded component names; --ligand is only for a PDB file.")
        from charmm_gui_cli.store.tasks import resolve_run
        report = preview_run(resolve_run(args.target), out=args.out, width=args.width,
                             views=args.view or ("side", "top", "ligand"))
    emit(report, command="preview")
    return 0
