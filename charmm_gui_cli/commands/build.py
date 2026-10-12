"""build command handler."""



def execute(args):
    from ..cli import (
        emit,
    )
    args._managed_cli = True
    from ..build_command import execute
    from ..background import launch
    result, code = execute(args) if args.foreground or getattr(args, "dry_run", False) else launch(args)
    emit(result, command=args.command)
    return code
