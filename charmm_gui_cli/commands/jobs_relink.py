"""Explicitly update the local task index after moving a task directory."""


def execute(args):
    from ..store.discovery import relink
    from ..cli import emit
    emit(relink(args.target, args.path), command="jobs.relink")
    return 0
