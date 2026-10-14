

def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.presentation.summaries import brief
    from charmm_gui_cli.remote.api import Client
    from charmm_gui_cli.store.auth import ToolError, load_token, resolve_cookie_path, token_info
    from charmm_gui_cli.pipeline.config import load_config
    from charmm_gui_cli.pipeline import workflow
    token, source = load_token(args.token_file)
    client = Client(token)
    from ..remote.sessions import bind
    bind(client, source)
    if args.command == "auth":
        info = {"source": source, **token_info(token), "server_checked": False}
        cookies = resolve_cookie_path(token_source=source)
        info.update(cookie_file=str(cookies), web_session_saved=cookies.is_file())
        if args.check or args.jobid:
            data = client.status(args.jobid)
            # A newly uploaded web job has status=null before computation,
            # yet a matching job record already proves API access to it.
            if "jobid" in data and data["jobid"] != args.jobid:
                raise ToolError(key="cli.jobid_mismatch")
            if "jobid" not in data:
                workflow.status_summary(data)
            info["job_access_confirmed"] = args.jobid
            info["remote_status"] = data.get("status")
            info["server_checked"] = True
        emit(info)
        return 2 if info["expired"] else 0
    if args.command == "status":
        data = client.status(args.jobid)
        if args.diagnostic:
            emit({"job_id": args.jobid, "status": data.get("status"),
                  "last_output_file": data.get("lastOutFile"), "last_input_step": data.get("lastInpStep"),
                  "archive_available": data.get("hasTarFile"),
                  "response_shape": {key: type(value).__name__ for key, value in data.items()}})
        else:
            emit({"job_id": args.jobid, **workflow.status_summary(data)}, command="status")
    elif args.command == "download":
        if workflow.status_summary(client.status(args.jobid))["status"] != "done":
            raise ToolError(key="cli.not_done")
        emit({"archive": str(client.download(args.jobid, args.out))})
    elif args.command == "attach":
        emit(brief(workflow.attach(client, args.directory, args.jobid)))
    elif args.command in ("run", "resume"):
        if args.interval < 10 or args.max_wait <= 0:
            raise ToolError(key="cli.polling")
        directory = args.out if args.command == "run" else args.directory
        if args.command == "run":
            result = workflow.start(client, load_config(args.config), directory)
            if not args.wait:
                emit(brief(result))
                return 0
        emit(brief(workflow.resume(client, directory, wait=args.wait, interval=args.interval,
                                   max_wait=args.max_wait, pdb_member=args.pdb_member)))
    return 0
