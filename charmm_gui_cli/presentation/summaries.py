"""Stable JSON summaries shared by command, delivery and report views."""

from ..locales.i18n import t


def brief(manifest):
    return {key: manifest[key] for key in ("state", "job_id", "remote_status", "validation") if key in manifest}



def precheck_summary(report):
    result = {key: report[key] for key in ("passed", "status", "directory", "check_file", "review_required",
        "server_submitted", "task_registered", "scientific_correctness_verified", "prepared_complex", "preview", "error") if key in report}
    inventory = report.get("protein_inventory", {})
    result["protein"] = {"residues": inventory.get("protein_residues_all_models"),
        "residues_missing_heavy_atoms": len(inventory.get("missing_heavy_atoms", [])),
        "numbering_gaps": len(inventory.get("numbering_gaps", [])),
        "sequence_alignment_performed": False, "repaired": False}
    result["chemistry"] = [{key: component[key] for key in ("resname", "atoms", "heavy_atoms", "bonds",
        "molecular_formula", "net_formal_charge", "rdkit_sanitized", "graph_sha256", "implicit_hydrogen_atoms",
        "explicit_hydrogen_atoms", "chemistry_warnings", "ligand_source") if key in component}
        for component in report.get("chemistry", [])]
    for key in ("automatic_repairs", "terminal_predictions", "input_normalization"):
        if key in report:
            result[key] = report[key]
    if "structure_inventory" in inventory:
        result["structure_inventory"] = inventory["structure_inventory"]
    if "spatial_review" in report:
        result["spatial_review"] = report["spatial_review"].get("summary", {})
    result["next_step"] = report.get("error", {}).get("next_step") or t("check.next_passed" if report.get("passed") else "check.next_failed")
    return result



def report_summary(report, name, path):
    """Keep the everyday report readable; --full retains all evidence paths."""
    acceptance = report.get("acceptance_validation", {})
    checks = acceptance.get("checks", {})
    penalty = checks.get("cgenff", {})
    minimization = checks.get("charmm_minimization", {})
    pose = report.get("bound_pose_validation", {})
    environment = report.get("environment_validation", {})
    environment_checks = environment.get("checks", {})
    concentration = environment_checks.get("concentration_direct_evidence", {})
    return {"state": report.get("status"), "passed": report.get("passed"), "name": name,
            "compilation": report.get("compilation"),
            "environment": {"passed": environment.get("passed"),
                "lipids": environment_checks.get("requested_lipids_present", {}).get("counts"),
                "water_molecules": environment_checks.get("water_present", {}).get("molecules"),
                "ions": environment_checks.get("ion_species"),
                "concentration_M": concentration.get("requested_M"),
                "concentration_verified": concentration.get("passed")},
            "bound_pose": {key: pose[key] for key in ("passed", "protein_ca_rmsd_A",
                "ligand_rmsd_after_protein_fit_A", "ligand_max_displacement_A") if key in pose},
            "components": {resname: {"passed": entry.get("passed"), "copies": entry.get("exact_copy_count"),
                "ligand_rmsd_A": entry.get("bound_pose", {}).get("ligand_rmsd_after_protein_fit_A"),
                "ligand_max_displacement_A": entry.get("bound_pose", {}).get("ligand_max_displacement_A"),
                "pose_error": entry.get("bound_pose", {}).get("error"),
                "cgenff": {key: acceptance.get("component_penalties", {}).get(resname, {}).get(key)
                    for key in ("status", "parameter_max", "charge_max", "review_required")}}
                for resname, entry in report.get("component_validation", {}).get("components", {}).items()},
            "acceptance": {"passed": acceptance.get("passed"), "status": acceptance.get("status"),
                "leaflets": checks.get("leaflets", {}).get("leaflets"),
                "cgenff": {key: penalty[key] for key in ("parameter_max", "charge_max", "review_required") if key in penalty},
                "charmm": {key: minimization[key] for key in ("normal_termination", "warning_counts",
                    "most_severe_warning_level", "minimization_convergence_verified", "equilibration_verified") if key in minimization},
                "warnings": acceptance.get("warnings", [])},
            "review_required": report.get("review_required", False),
            "scientific_correctness_verified": report.get("scientific_correctness_verified", False),
            "report": str(path)}
