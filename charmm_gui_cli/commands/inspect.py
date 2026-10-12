"""inspect command handler."""



def execute(args):
    from ..cli import (
        emit,
    )
    report = inspect_archive(args.archive, args.ligand, args.pdb_member)
    emit(report)
    return 0 if report["passed"] else 2
