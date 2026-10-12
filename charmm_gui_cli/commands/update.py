"""update command handler."""



def execute(args):
    from ..cli import (
        emit,
    )
    from ..updater import update
    result = update(check=args.check, prefix=args.prefix, bin_dir=args.bin_dir)
    emit(result, command="update")
    return 0
