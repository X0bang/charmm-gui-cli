"""login command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.remote.api import Client
    from charmm_gui_cli.store.auth import ToolError, default_token_path, resolve_cookie_path
    from charmm_gui_cli.locales.i18n import t
    from getpass import getpass
    import os
    from charmm_gui_cli.remote.web import WebClient
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
