"""jobs resume handler."""

import charmm_gui_cli.store.tasks as _task_store


def execute(args):
    from charmm_gui_cli.cli import emit
    pass
    from charmm_gui_cli.pipeline.build_command import execute
    args.directory = _task_store.resolve_run(args.target)
    args.command = "build-resume"
    from charmm_gui_cli.pipeline.background import launch
    result, code = execute(args) if args.foreground else launch(args)
    emit(result, command="jobs.resume")
    return code

    return 0
