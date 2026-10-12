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
    token, _ = load_token(args.token_file)
    result = workflow.attach(Client(token), directory / "bilayer", args.jobid)
    managed.record_recovery(directory, state="submitted")
    emit({"directory": str(directory), **brief(result), "next_step": "charmm-gui-cli jobs resume " + args.target})

    return 0
