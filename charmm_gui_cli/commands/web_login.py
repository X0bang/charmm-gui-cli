"""web-login command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.store.auth import ToolError
    from charmm_gui_cli.locales.i18n import t
    from getpass import getpass
    from charmm_gui_cli.remote.web import WebClient
    from ..store.auth import default_token_path
    from ..remote.sessions import SessionManager, bind
    manager = SessionManager(str(args.token_file or default_token_path()), args.cookies)
    cookies = manager.cookies
    paired = manager.profile and manager.profile["cookie_file"] == str(cookies.absolute())
    if args.command == "web-login" and paired:
        raise ToolError(key="session.web_only", category="authentication", next_step="charmm-gui-cli login")
    web_client = WebClient(None if args.command == "web-login" else cookies)
    if args.command != "web-login" and paired:
        bind(web_client, str(manager.path), cookies, manager)
    if args.command == "web-login":
        result = web_client.login(args.email or input(t("prompt.email")), getpass(t("prompt.password")))
        web_client.cookie_path = cookies
        web_client.save_cookies()
    elif args.command == "web-upload":
        result = web_client.upload_structure(args.pdb, args.out, args.project)
    elif args.command == "web-step":
        try:
            fields = dict(value.split("=", 1) for value in args.fields)
            files = dict(value.split("=", 1) for value in args.files)
        except ValueError:
            raise ToolError(key="cli.fields") from None
        result = web_client.submit_snapshot(args.snapshot, args.out, fields, files)
    else:
        result = web_client.inspect(args.doc, args.out)
    emit(result)
    return 0
