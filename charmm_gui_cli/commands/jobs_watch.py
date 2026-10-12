"""jobs watch handler."""


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
    from ..background import watch
    directory = managed.resolve_run(args.target)
    code = watch(directory)
    if code != 130:
        emit(managed.inspect_run(directory), command="jobs.status")
    return code

    return 0
