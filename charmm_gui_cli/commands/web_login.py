"""web-login command handler."""



def execute(args):
    from ..cli import (
        emit,
        ToolError,
        resolve_cookie_path,
        t,
        getpass,
    )
    from ..web import WebClient
    cookies = resolve_cookie_path(args.cookies)
    web_client = WebClient(None if args.command == "web-login" else cookies)
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
