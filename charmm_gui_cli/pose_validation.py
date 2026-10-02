"""Conservative file selection and manifest mapping for bound-pose checks."""

import json
from pathlib import Path
import re

from .auth import ToolError
from .validation import compare_bound_pose


def validate_bound_pose(system_dir, input_manifest, reference_pdb):
    """Compare one final CHARMM assembly to a supplied bound-pose reference.

    Missing or ambiguous assembly files fail explicitly. No coordinate format
    fallback, atom-name guessing, periodic unwrapping, or ligand fitting occurs.
    """
    report = {"passed": False, "scientific_correctness_verified": False,
              "reference_pdb": str(Path(reference_pdb).resolve())}
    try:
        if not Path(reference_pdb).is_file():
            raise ToolError("Reference PDB file does not exist.")
        root = Path(system_dir).resolve()
        candidates = sorted(path for path in root.rglob("step5_assembly.pdb") if path.is_file())
        if not candidates:
            raise ToolError("No step5_assembly.pdb was found for bound-pose validation.")
        if len(candidates) != 1:
            raise ToolError("Multiple step5_assembly.pdb files were found; choose a system directory containing exactly one assembly.")
        final = candidates[0]
        report["assembly_pdb"] = str(final)
        manifest = input_manifest if isinstance(input_manifest, dict) else json.loads(Path(input_manifest).read_text())
        ligand = manifest["ligand"]
        resname = ligand["resname"]
        if not isinstance(resname, str) or not re.fullmatch(r"[A-Za-z0-9]{1,4}", resname):
            raise ToolError("Input manifest has an invalid ligand residue name.")
        mapping = ligand["atom_mapping"]
        if not isinstance(mapping, list) or not mapping:
            raise ToolError("Input manifest must include ligand atom_mapping for bound-pose validation.")
        names = []
        for atom in mapping:
            element = atom["element"]
            name = atom["pdb_atom_name"]
            if not isinstance(element, str) or not element.strip() or not isinstance(name, str) or not name.strip():
                raise ToolError("Input manifest ligand atom names and elements must be explicit.")
            if element.upper() not in ("H", "D", "T"):
                names.append(name)
        if not names or len(set(names)) != len(names):
            raise ToolError("Input manifest must provide unique ligand heavy-atom names.")
        report.update(compare_bound_pose(reference_pdb, final, ligand_resname=resname, ligand_atom_names=names))
        return report
    except ToolError as exc:
        report["error"] = str(exc)
    except (KeyError, TypeError, ValueError):
        report["error"] = "Input manifest or coordinate data are malformed for bound-pose validation."
    except OSError:
        report["error"] = "Could not read the reference, assembly, or input manifest for bound-pose validation."
    return report
