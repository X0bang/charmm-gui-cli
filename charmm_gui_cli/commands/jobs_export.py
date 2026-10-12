"""jobs export handler."""


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
    from ..delivery import export_run
    directory = managed.resolve_run(args.target)
    result = export_run(directory, to=args.to)
    emit({"name": managed._job_metadata(directory).get("name", directory.name),
          "state": result["status"], "passed": result["passed"], "delivery": result}, command="build")
    return 0

    return 0
