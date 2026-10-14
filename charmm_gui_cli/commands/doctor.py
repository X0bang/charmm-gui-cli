"""doctor command handler."""


def execute(args):
    from ..cli import emit
    from ..store.doctor import inspect_installation

    report = inspect_installation(gmx=args.gmx, token_file=args.token_file)
    emit(report, command="doctor")
    return 0 if report["passed"] else 2
