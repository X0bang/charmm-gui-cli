"""inspect command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.pipeline.artifacts import inspect_archive
    report = inspect_archive(args.archive, args.ligand, args.pdb_member)
    emit(report)
    return 0 if report["passed"] else 2
