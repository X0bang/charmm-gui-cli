"""jobs attach handler."""


def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.presentation.summaries import brief
    from charmm_gui_cli.remote.api import Client
    from charmm_gui_cli.store.auth import load_token
    from charmm_gui_cli.pipeline import workflow
    from charmm_gui_cli.store import managed
    directory = managed.resolve_run(args.target)
    token, source = load_token(args.token_file)
    client = Client(token)
    from ..remote.sessions import bind
    bind(client, source)
    result = workflow.attach(client, directory / "bilayer", args.jobid)
    managed.record_recovery(directory, state="submitted")
    emit({"directory": str(directory), **brief(result), "next_step": "charmm-gui-cli jobs resume " + args.target})

    return 0
