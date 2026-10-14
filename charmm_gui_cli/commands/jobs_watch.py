"""jobs watch handler."""


def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.store import managed
    from charmm_gui_cli.store.background import watch
    directory = managed.resolve_run(args.target)
    code = watch(directory)
    if code != 130:
        emit(managed.inspect_run(directory), command="jobs.status")
    return code

    return 0
