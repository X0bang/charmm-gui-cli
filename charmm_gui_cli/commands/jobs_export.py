"""jobs export handler."""


def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.store import managed
    from charmm_gui_cli.store.delivery import export_run
    directory = managed.resolve_run(args.target)
    result = export_run(directory, to=args.to)
    emit({"name": managed._job_metadata(directory).get("name", directory.name),
          "state": result["status"], "passed": result["passed"], "delivery": result}, command="build")
    return 0

    return 0
