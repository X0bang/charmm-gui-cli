"""plan command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.pipeline.config import load_config, plan
    proposal = plan(load_config(args.config))
    emit(proposal)
    return 0 if proposal["ready_to_submit"] else 2
