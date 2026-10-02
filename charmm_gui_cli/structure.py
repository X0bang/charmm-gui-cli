"""Local PDB inventory and geometry checks; no chemical parameter inference."""

from collections import Counter
import hashlib
import math
from pathlib import Path

from .auth import ToolError


def analyze_pdb(path, expected_ligands):
    data = Path(path).read_bytes()
    lines = data.decode("utf-8", errors="replace").splitlines()
    atoms = []
    first_model_finished = False
    connectivity = {}
    for line in lines:
        if line.startswith("ENDMDL"):
            first_model_finished = True
        if line.startswith("CONECT"):
            try:
                indices = [int(line[i:i + 5]) for i in range(6, len(line), 5) if line[i:i + 5].strip()]
            except ValueError:
                raise ToolError("Malformed CONECT record in local PDB.") from None
            if indices:
                counts = Counter(indices[1:])
                for target, multiplicity in counts.items():
                    key = tuple(sorted((indices[0], target)))
                    connectivity[key] = max(connectivity.get(key, 0), multiplicity)
        if not first_model_finished and line.startswith(("ATOM  ", "HETATM")):
            try:
                xyz = tuple(float(line[i:i + 8]) for i in (30, 38, 46))
                serial = int(line[6:11])
                residue_number = int(line[22:26])
                if not all(math.isfinite(x) for x in xyz):
                    raise ValueError
            except ValueError:
                raise ToolError("Malformed atom coordinates/numbering in local PDB.") from None
            atoms.append({
                "serial": serial, "name": line[12:16].strip(), "altloc": line[16:17].strip(),
                "resname": line[17:21].strip(), "chain": line[21:22].strip(),
                "residue_number": residue_number, "insertion": line[26:27].strip(),
                "segment": line[72:76].strip(), "record": line[:6].strip(),
                "element": line[76:78].strip(), "xyz": xyz,
            })
    if not atoms:
        raise ToolError("Local PDB contains no atom coordinates.")
    groups = {}
    for atom in atoms:
        key = (atom["chain"], atom["residue_number"], atom["insertion"], atom["segment"], atom["resname"])
        groups.setdefault(key, []).append(atom)
    counts = Counter(key[-1] for key in groups)
    chains = {}
    for atom in atoms:
        chains.setdefault(atom["chain"] or "(blank)", []).append(atom)
    inventory = []
    for chain, items in sorted(chains.items()):
        residues = {(a["residue_number"], a["insertion"], a["segment"], a["resname"]) for a in items}
        inventory.append({"chain": chain, "atoms": len(items), "residues": len(residues),
                          "record_types": dict(Counter(a["record"] for a in items)),
                          "residue_number_range": [min(a["residue_number"] for a in items), max(a["residue_number"] for a in items)]})
    protein = [a for a in atoms if a["record"] == "ATOM" and a["resname"] not in expected_ligands
               and a["element"] not in ("H", "D")]
    ligands = []
    warnings = []
    if not any(a["element"] in ("H", "D") for a in atoms):
        warnings.append("No explicit hydrogen elements found; protonation/hydrogen preparation is still required.")
    if any(not a["element"] for a in atoms):
        warnings.append("Some element columns are blank; element-based checks may be incomplete.")
    if any(a["altloc"] for a in atoms):
        warnings.append("Alternate locations are present; inventory includes all alternatives in the first model.")
    if len({a["serial"] for a in atoms}) != len(atoms):
        warnings.append("Duplicate atom serials; CONECT interpretation is ambiguous.")
    for key, items in groups.items():
        if key[-1] not in expected_ligands:
            continue
        serials = {a["serial"] for a in items}
        bonds = {edge: order for edge, order in connectivity.items() if set(edge) <= serials}
        closest = None
        heavy = [a for a in items if a["element"] not in ("H", "D")]
        for atom in heavy:
            for other in protein:
                distance_sq = sum((a - b) ** 2 for a, b in zip(atom["xyz"], other["xyz"]))
                if closest is None or distance_sq < closest[0]:
                    closest = (distance_sq, atom, other)
        minimum_distance = round(math.sqrt(closest[0]), 3) if closest else None
        if minimum_distance is not None and minimum_distance < 1.0:
            warnings.append(f"{key[-1]} has a protein/ligand heavy-atom distance < 1 A; inspect for overlap or an intended covalent bond.")
        if minimum_distance is not None and minimum_distance > 5.0:
            warnings.append(f"{key[-1]} is > 5 A from every ATOM protein heavy atom; verify the input binding pose.")
        if key[1] <= 0:
            warnings.append(f"{key[-1]} uses residue number {key[1]}; verify it is read correctly during website preparation.")
        if not bonds:
            warnings.append(f"{key[-1]} has no internal CONECT bonds; provide a chemically defined ligand file for preparation.")
        ligands.append({
            "resname": key[-1], "chain": key[0], "residue_number": key[1], "atoms": len(items),
            "elements": dict(Counter(a["element"] or "unknown" for a in items)),
            "atom_names_unique": len({a["name"] for a in items}) == len(items),
            "conect_internal_bonds": len(bonds),
            "conect_multiplicity_counts": dict(Counter(str(order) for order in bonds.values())),
            "minimum_protein_heavy_atom_distance_A": minimum_distance,
        })
    return {
        "sha256": hashlib.sha256(data).hexdigest(), "atoms_first_model": len(atoms),
        "chains": inventory, "residue_counts": dict(counts), "ligands": ligands,
        "missing_ligands": [name for name in expected_ligands if name not in counts],
        "coordinate_bounds_A": {axis: [min(a["xyz"][i] for a in atoms), max(a["xyz"][i] for a in atoms)]
                                for i, axis in enumerate("xyz")},
        "warnings": warnings,
        "limitations": ["CONECT multiplicity is reported as encoded, not a validated bond order or stereochemistry.",
                        "Distances do not establish a valid binding pose, chemical identity, or membrane orientation.",
                        "No ligand topology or force-field parameters are generated."],
    }
