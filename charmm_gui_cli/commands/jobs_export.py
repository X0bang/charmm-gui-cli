"""jobs export handler."""

import charmm_gui_cli.store.tasks as _task_store


def execute(args):
    from charmm_gui_cli.cli import emit
    pass
    from charmm_gui_cli.pipeline.delivery import export_run
    directory = _task_store.resolve_run(args.target)
    result = export_run(directory, to=args.to)
    emit({"name": _task_store._job_metadata(directory).get("name", directory.name),
          "state": result["status"], "passed": result["passed"], "delivery": result}, command="build")
    return 0

    return 0
