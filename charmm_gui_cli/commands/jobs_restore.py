"""Plan or explicitly restore a quarantined local task without modeling."""


def execute(args):
    from ..cli import emit
    from ..pipeline.cleanup import restore
    emit(restore(args.operation, yes=args.yes, to=args.to, register=not args.no_register), command='jobs.restore')
    return 0
