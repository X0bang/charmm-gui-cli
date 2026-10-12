"""jobs resume handler."""


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
    from ..build_command import execute
    args.directory = managed.resolve_run(args.target)
    args.command = "build-resume"
    from ..background import launch
    result, code = execute(args) if args.foreground else launch(args)
    emit(result, command="jobs.resume")
    return code

    return 0
