"""jobs reconcile handler."""


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
    from ..background import metadata
    from ..reconcile import reconcile
    from ..web import WebClient
    directory = managed.resolve_run(args.target)
    if metadata(directory)["running"]:
        raise ToolError(key="background.running", next_step="charmm-gui-cli jobs list")
    _, source = load_token(args.token_file)
    from ..remote.sessions import bind, SessionManager, session_cookie_path
    cookies = session_cookie_path(args.cookies, source)
    web = WebClient(cookies)
    bind(web, source, cookies, SessionManager(source, cookies, allow_renew=False))
    result = reconcile(directory, web,
                       confirm_not_submitted=args.confirm_not_submitted, source_job_id=args.source_jobid)
    emit(result)
    return 0

    return 0
