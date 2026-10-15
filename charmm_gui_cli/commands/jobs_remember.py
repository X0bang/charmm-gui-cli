"""Re-register a retained task locally without running its pipeline."""


def execute(args):
    from ..cli import emit
    from ..store.lifecycle import remember
    emit(remember(args.path), command='jobs.remember')
    return 0
