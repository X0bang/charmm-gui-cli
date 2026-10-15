"""Plan or explicitly quarantine an inactive local task without deletion."""


def execute(args):
    from ..cli import emit
    from ..pipeline.cleanup import clean
    emit(clean(args.target, yes=args.yes, trash_dir=args.trash_dir), command='jobs.clean')
    return 0
