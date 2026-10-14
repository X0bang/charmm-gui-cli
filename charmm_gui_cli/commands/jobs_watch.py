"""jobs watch handler."""

import charmm_gui_cli.store.tasks as _task_store


def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.pipeline import managed
    from charmm_gui_cli.pipeline.background import watch
    directory = _task_store.resolve_run(args.target)
    code = watch(directory)
    if code != 130:
        emit(managed.inspect_run(directory), command="jobs.status")
    return code

    return 0
