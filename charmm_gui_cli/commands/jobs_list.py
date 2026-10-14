"""jobs list handler."""


def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.pipeline import managed
    emit({"jobs": managed.list_runs(), "next_step": "charmm-gui-cli jobs resume NAME"}, command="jobs.list")

    return 0
