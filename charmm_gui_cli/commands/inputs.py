"""inputs command handler."""



def execute(args):
    from ..cli import (
        emit,
    )
    from ..inputs import prepare_inputs
    emit(prepare_inputs(args.protein, args.ligand, args.out, ligand_resname=args.ligand_resname,
                        hydrogen_policy=args.hydrogens))
    return 0
