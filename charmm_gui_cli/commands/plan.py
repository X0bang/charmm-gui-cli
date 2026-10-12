"""plan command handler."""



def execute(args):
    from ..cli import (
        emit,
        load_config,
        plan,
    )
    proposal = plan(load_config(args.config))
    emit(proposal)
    return 0 if proposal["ready_to_submit"] else 2
