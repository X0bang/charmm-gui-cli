"""jobs status handler."""


def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.pipeline import managed
    emit(managed.inspect_run(args.target, remote_check=args.remote_check, token_file=args.token_file), command="jobs.status")

    return 0
