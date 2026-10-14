"""build command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    args._managed_cli = True
    from charmm_gui_cli.pipeline.build_command import execute
    from charmm_gui_cli.pipeline.background import launch
    result, code = execute(args) if args.foreground or getattr(args, "dry_run", False) else launch(args)
    emit(result, command=args.command)
    return code
