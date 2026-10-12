"""jobs list handler."""


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
    emit({"jobs": managed.list_runs(), "next_step": "charmm-gui-cli jobs resume NAME"}, command="jobs.list")

    return 0
