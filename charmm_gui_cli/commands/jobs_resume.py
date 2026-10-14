"""jobs resume handler."""


def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.store import managed
    from charmm_gui_cli.pipeline.build_command import execute
    args.directory = managed.resolve_run(args.target)
    args.command = "build-resume"
    from charmm_gui_cli.store.background import launch
    result, code = execute(args) if args.foreground else launch(args)
    emit(result, command="jobs.resume")
    return code

    return 0
