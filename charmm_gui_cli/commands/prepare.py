"""prepare command handler."""



def execute(args):
    from charmm_gui_cli.cli import emit
    from charmm_gui_cli.store.auth import ToolError, atomic_json, atomic_write
    from charmm_gui_cli.pipeline.config import plan, validate_config
    import yaml
    config = validate_config({
        "version": 1, "source_job_id": None, "input_pdb": str(args.pdb.resolve()),
        "expected_ligands": args.ligand,
        "preparation": {"ligand_parameters_ready": False, "orientation": "ppm"},
        "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
        "ions": {"type": "NaCl", "concentration_M": 0.15},
    })
    proposal = plan(config)
    if proposal["local_pdb"]["missing_ligands"]:
        raise ToolError(key="cli.ligand_absent")
    try:
        args.out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ToolError(key="cli.preparation_exists") from None
    atomic_write(args.out / "system.yaml", yaml.safe_dump(config, sort_keys=False))
    atomic_json(args.out / "preparation.json", proposal)
    emit({"config": str(args.out / "system.yaml"), "report": str(args.out / "preparation.json"),
          "next_step": "Prepare the complex/ligand on the website, then fill source_job_id and preparation fields.",
          "defaults": "POPC, NaCl 0.15 M and PPM are editable placeholders, not recommendations for this protein."})
    return 0
