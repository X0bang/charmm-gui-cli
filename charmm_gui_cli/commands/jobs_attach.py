"""jobs attach handler."""


def execute(args):
    from ..cli import (
        emit,
        brief,
        report_summary,
        Client,
        ToolError,
        load_token,
        resolve_cookie_path,
        workflow,
        json,
    )
    from .. import managed
    directory = managed.resolve_run(args.target)
    token, source = load_token(args.token_file)
    client = Client(token)
    from ..remote.sessions import bind
    bind(client, source)
    result = workflow.attach(client, directory / "bilayer", args.jobid)
    managed.record_recovery(directory, state="submitted")
    emit({"directory": str(directory), **brief(result), "next_step": "charmm-gui-cli jobs resume " + args.target})

    return 0
