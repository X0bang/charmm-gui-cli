"""login command handler."""



def execute(args):
    from ..cli import (
        emit,
        Client,
        ToolError,
        atomic_write,
        default_token_path,
        resolve_cookie_path,
        t,
        getpass,
        os,
    )
    from ..web import WebClient
    destination = (args.save_to or args.token_file or default_token_path()).expanduser()
    cookies = resolve_cookie_path(args.cookies, str(destination))
    if destination.resolve() == cookies.resolve():
        raise ToolError(key="cli.session_paths")
    email = args.email or input(t("prompt.email"))
    from ..remote.sessions import command_password, login_session
    password = command_password() if os.environ.get("CHARMM_GUI_PASSWORD_COMMAND") else getpass(t("prompt.password"))
    web_result = login_session(destination, cookies, email, password, save_password=args.remember, api=Client(), web=WebClient(post_timeout=60))
    result = {"logged_in": True, "api_authenticated": True, **web_result,
              "saved_to": str(destination), "cookie_file": str(cookies), "permissions": "0600",
              "remembered": args.remember, "password_command": bool(os.environ.get("CHARMM_GUI_PASSWORD_COMMAND")),
              "next_step": "charmm-gui-cli build --protein protein.pdb --ligand bound-ligand.sdf --name receptor-a"}
    if os.environ.get("CHARMMGUI_TOKEN"):
        result["notice"] = "CHARMMGUI_TOKEN is set and overrides the saved login. Unset it to use this session by default."
    emit(result, command="login", email=email)
    return 0
