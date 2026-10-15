"""Plan or explicitly archive one task's local index records."""


def execute(args):
    from ..cli import emit
    from ..store.lifecycle import forget
    emit(forget(args.target, yes=args.yes, missing=args.missing), command='jobs.forget')
    return 0
