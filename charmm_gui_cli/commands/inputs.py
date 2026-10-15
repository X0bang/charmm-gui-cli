"""inputs command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.chem.inputs import prepare_inputs
    from ..chem.residues import options_from_args
    options = options_from_args(args)
    emit(prepare_inputs(args.protein, args.ligand, args.out, ligand_resname=args.ligand_resname,
                        hydrogen_policy=args.hydrogens, **({'input_options': options} if options else {})))
    return 0
