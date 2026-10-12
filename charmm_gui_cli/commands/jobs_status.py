"""jobs status handler."""


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
    emit(managed.inspect_run(args.target, remote_check=args.remote_check, token_file=args.token_file), command="jobs.status")

    return 0
