"""Blocking input checks; no topology, bond or protonation state is invented."""

from collections import defaultdict
import math

from ..store.auth import ToolError
from .pdb_normalization import finding
from .residues import SIDECHAINS, ALIASES, MODIFIED, METALS, WATERS, ADDITIVES, DETERGENTS, CGENFF_ELEMENTS


def atom_label(atom):
    line = atom.line
    return " ".join((line[17:21].strip(), line[21:22].strip() or line[72:76].strip() or "-",
                     line[22:27].strip(), atom.name)) if line else atom.name


def heterogen_inventory(atoms):
    residues = defaultdict(list)
    protein = [atom for atom in atoms if atom.line.startswith("ATOM  ") and atom.line[17:21].strip() not in MODIFIED]
    for atom in atoms:
        if atom.line.startswith("HETATM") or atom.line[17:21].strip() in MODIFIED:
            residues[(atom.polymer_block, atom.line[17:27], atom.line[72:76])].append(atom)
    entries = []
    for group in residues.values():
        first = group[0]
        line, name = first.line, first.line[17:21].strip()
        identity = {"resname": name, "chain": line[21:22].strip(), "resid": int(line[22:26]),
                    "icode": line[26:27].strip(), "segment": line[72:76].strip(), "polymer_block": first.polymer_block}
        kind = "modified" if name in MODIFIED else "metal" if len(group) == 1 and first.element in METALS | {"Cl"} else \
            "additive" if name in ADDITIVES else "detergent" if name in DETERGENTS else "water" if name in WATERS else "cofactor"
        label = " ".join((name, identity["chain"] or identity["segment"] or "-", str(identity["resid"]) + identity["icode"]))
        entry = {"key": "input.heterogen_" + kind, "params": {"residue": label}, **identity, "kind": kind, "atoms": len(group)}
        if kind == "metal":
            neighbors = [(math.dist(first.xyz, atom.xyz), atom) for atom in protein
                         if atom.element in ("N", "O", "S") and math.dist(first.xyz, atom.xyz) <= 2.8]
            entry["coordination_atoms"] = [{"atom": atom_label(atom), "distance_A": distance} for distance, atom in sorted(neighbors, key=lambda pair: pair[0])]
            entry["params"]["neighbors"] = ", ".join(f"{atom_label(atom)} ({distance:.2f} Å)" for distance, atom in sorted(neighbors, key=lambda pair: pair[0])) or "—"
        elif protein:
            distance, left, right = min(((math.dist(a.xyz, b.xyz), a, b) for a in group for b in protein), key=lambda item: item[0])
            entry["nearest_pair"] = {"component_atom": atom_label(left), "protein_atom": atom_label(right), "distance_A": distance}
            entry["params"].update(distance=f"{distance:.2f}", pair=f"{left.name}–{atom_label(right)}")
        else:
            entry["params"].update(distance="—", pair="—")
        # Populate the stable English message only after all placeholders exist.
        from ..locales.i18n import MESSAGES
        entry["message"] = MESSAGES[entry["key"]]["en"].format(**entry["params"])
        entries.append(entry)
    order = {kind: index for index, kind in enumerate(("metal", "cofactor", "modified", "additive", "detergent", "water"))}
    return sorted(entries, key=lambda entry: order[entry["kind"]])


def validate_protein(atoms):
    from .structure_inventory import NUCLEIC_NAMES
    nucleic = list(dict.fromkeys(atom_label(atom).rsplit(" ", 1)[0] for atom in atoms
                                if atom.line[17:21].strip() in NUCLEIC_NAMES))
    if nucleic:
        raise ToolError(key="input.nucleic_chains", params={"count": len(nucleic), "residues": ", ".join(nucleic[:20])},
                        next_step="charmm-gui-cli check --help")
    heterogens = heterogen_inventory(atoms)
    if heterogens:
        raise ToolError(key="input.protein_hetero", details=heterogens, next_step="charmm-gui-cli check -h")
    unknown = list(dict.fromkeys(atom_label(atom).rsplit(" ", 1)[0] for atom in atoms
                                if atom.line[17:21].strip() not in set(SIDECHAINS) | set(ALIASES)))
    if unknown:
        raise ToolError(key="input.unknown_residues", params={"residues": ", ".join(unknown[:20]), "count": len(unknown)},
                        next_step="charmm-gui-cli check -h")
    if not atoms or not any(a.name == "CA" for a in atoms):
        raise ToolError(key="input.no_protein", next_step="charmm-gui-cli check -h")
    chains = defaultdict(dict)
    for atom in atoms:
        line = atom.line
        chain = (atom.polymer_block, line[21:22], line[72:76])
        identity = (line[22:27], line[17:21])
        names = chains[chain].setdefault(identity, {})
        if atom.name in names:
            raise ToolError(key="input.atom_identity", params={"atom": atom_label(atom)}, next_step="charmm-gui-cli check -h")
        names[atom.name] = atom
    breaks = []
    for residues in chains.values():
        for left, right in zip(list(residues.values()), list(residues.values())[1:]):
            if "C" in left and "N" in right:
                distance = math.dist(left["C"].xyz, right["N"].xyz)
                if distance > 2.0:
                    entry = finding("input.chain_break", left=atom_label(left["C"]), right=atom_label(right["N"]), distance=f"{distance:.2f}")
                    entry["distance_A"] = distance
                    breaks.append(entry)
    if breaks:
        raise ToolError(key="input.chain_breaks", params={"count": len(breaks)}, details=breaks,
                        next_step="charmm-gui-cli check -h")


def validate_cgenff(molecule):
    unsupported = sorted({atom.element for atom in molecule.atoms} - CGENFF_ELEMENTS)
    if unsupported:
        raise ToolError(key="input.cgenff_elements", params={"elements": ", ".join(unsupported)},
                        next_step="charmm-gui-cli check --help")


def validate_link_records(path):
    from pathlib import Path
    from .structure_inventory import link_records, endpoint_label
    links = link_records(Path(path).read_text().splitlines())
    if links:
        # LINK encodes linkage/coordination absent from many CONECT graphs.
        # Removing its other endpoint cannot certify the remaining chemistry.
        details = [finding("input.glycan_link" if link["glycan_name_hint"] else "input.link_record",
                           line=link["line"], left=endpoint_label(link["endpoints"][0]),
                           right=endpoint_label(link["endpoints"][1])) for link in links[:20]]
        raise ToolError(key="input.linkage", params={"count": len(links)}, details=details,
                        next_step="charmm-gui-cli check -h")


def validate_ligand_name(name):
    from .lipids import KNOWN_LIPIDS
    reserved = set(SIDECHAINS) | set(ALIASES) | set(MODIFIED) | set(WATERS) | {"CLA", "SOD", "POT", "CAL", "NA", "CL", "ZN", "MG", "FE"}
    reserved.update(lipid[:3] for lipid in KNOWN_LIPIDS)
    if name.upper() in reserved:
        raise ToolError(key="input.reserved_name", params={"name": name}, next_step="charmm-gui-cli check --ligand-resname LIG --help")


def validate_contact(protein, ligand):
    heavy_protein = [atom for atom in protein if atom.element not in ("H", "D", "T")]
    heavy_ligand = [atom for atom in ligand if atom.element not in ("H", "D", "T")]
    if not heavy_protein or not heavy_ligand:
        raise ToolError(key="input.no_heavy_atoms", next_step="charmm-gui-cli check -h")
    distance, left, right = min(((math.dist(a.xyz, b.xyz), a, b) for a in heavy_protein for b in heavy_ligand), key=lambda item: item[0])
    if distance < 2.2:
        raise ToolError(key="input.overlap" if distance < .8 else "input.covalent_contact",
                        params={"left": atom_label(left), "right": atom_label(right), "distance": f"{distance:.2f}"},
                        next_step="charmm-gui-cli check -h")
    return distance
