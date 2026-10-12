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
    password = getpass(t("prompt.password"))
    token = Client().login(email, password)
    # Authenticate both transports before touching the saved credentials.
    # A failed website login must not leave a new JWT paired with old cookies.
    web_client = WebClient()
    web_result = web_client.login(email, password)
    web_client.cookie_path = cookies
    web_client.save_cookies()
    atomic_write(destination, token + "\n")
    result = {"logged_in": True, "api_authenticated": True, **web_result,
              "saved_to": str(destination), "cookie_file": str(cookies), "permissions": "0600",
              "next_step": "charmm-gui-cli build --protein protein.pdb --ligand bound-ligand.sdf --name receptor-a"}
    if os.environ.get("CHARMMGUI_TOKEN"):
        result["notice"] = "CHARMMGUI_TOKEN is set and overrides the saved login. Unset it to use this session by default."
    emit(result, command="login", email=email)
    return 0
