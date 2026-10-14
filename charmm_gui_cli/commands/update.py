"""update command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.pipeline.updater import update
    result = update(check=args.check, prefix=args.prefix, bin_dir=args.bin_dir)
    emit(result, command="update")
    return 0
