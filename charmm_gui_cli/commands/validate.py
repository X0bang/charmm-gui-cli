"""validate command handler."""



def execute(args):
    from ..cli import (
        emit,
        ToolError,
        atomic_json,
    )
    from ..validation import extract_for_validation, inspect_environment, validate_system
    build_config = None
    if args.build_config:
        from ..build_config import load_build_config
        build_config = load_build_config(args.build_config)
    if args.out.exists():
        raise ToolError(key="cli.validation_exists")
    args.out.mkdir(parents=True)
    system = args.system
    if system.is_file():
        system = extract_for_validation(system, args.out / "extracted")
    report = validate_system(system, args.input_manifest, run_grompp=args.grompp,
                             gmx=args.gmx, output_parent=args.out)
    if build_config is not None:
        coordinates = report.get("files", {}).get("coordinates")
        if not coordinates:
            environment = {"passed": False, "error": "No validated coordinate file was identified for environment checks."}
        else:
            lipids = sorted({name for leaflet in ("upper", "lower")
                             for name in build_config["membrane"][leaflet].split("=", 1)[0].split(":")})
            try:
                environment = inspect_environment(
                    coordinates, expected_lipids=lipids, ion_type=build_config["ions"]["type"],
                    concentration_M=build_config["ions"]["concentration_M"], system_dir=system)
            except ToolError as exc:
                environment = {"passed": False, "error": str(exc)}
        report["environment_validation"] = environment
        report["passed"] = bool(report["passed"] and environment["passed"])
        from ..acceptance import assess_acceptance
        acceptance = assess_acceptance(system, membrane=build_config["membrane"], ligand_resname=build_config["ligand_resname"])
        report.update(acceptance_validation=acceptance, review_required=acceptance.get("review_required", False),
                      review_warnings=acceptance.get("warnings", []))
        report["passed"] = bool(report["passed"] and acceptance["passed"])
    if args.reference_pdb is not None:
        from ..pose_validation import validate_bound_pose
        pose = validate_bound_pose(system, args.input_manifest, args.reference_pdb)
        report["bound_pose_validation"] = pose
        report["passed"] = bool(report["passed"] and pose["passed"])
    atomic_json(args.out / "validation.json", report)
    emit(report)
    return 0 if report["passed"] else 2
