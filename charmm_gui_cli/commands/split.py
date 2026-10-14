"""split command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.chem.inputs import split_complex
    emit(split_complex(args.pdb, args.out, ligand_resname=args.ligand, accept_conect_bond_orders=True))
    return 0
