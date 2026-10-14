"""Explicit local logout; never deletes tasks or sends remote requests."""


def execute(args):
    from charmm_gui_cli.cli import emit
    from ..remote.sessions import logout
    from ..store.auth import local_token_path
    emit(logout(args.token_file or local_token_path(), args.cookies), command="logout")
    return 0
