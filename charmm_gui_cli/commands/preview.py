"""preview command handler."""



def execute(args):
    from ..cli import (
        emit,
        ToolError,
        Path,
    )
    from ..preview import render_model, preview_run, new_preview_path
    if Path(args.target).is_file():
        report = render_model(args.target, args.out or new_preview_path(),
            ligand_resnames=args.ligand or ("LIG",), width=args.width,
            views=args.view or ("side", "top", "ligand"))
    else:
        if args.ligand:
            raise ToolError("Task previews use the recorded component names; --ligand is only for a PDB file.")
        from ..managed import resolve_run
        report = preview_run(resolve_run(args.target), out=args.out, width=args.width,
                             views=args.view or ("side", "top", "ligand"))
    emit(report)
    return 0
