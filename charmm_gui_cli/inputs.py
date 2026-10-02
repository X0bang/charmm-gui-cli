"""Coordinate-preserving input preparation; never docks or parameterizes a ligand.

The dependency-free path validates file structure, connectivity and common valences.
If RDKit is installed it additionally sanitizes the supplied molecular graph.
PDB CONECT conversion requires explicit acknowledgement of the bond-order source.
All output files are created exclusively: an existing output is never overwritten.
"""

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re

from .auth import ToolError


@dataclass
class Atom:
    serial: int
    name: str
    element: str
    xyz: tuple
    charge: int = 0
    line: str = ""


@dataclass
class Ligand:
    atoms: list
    bonds: dict
    sdf: str
    name: str = "LIG"


def _xyz(values):
    try:
        values = tuple(float(x) for x in values)
        if len(values) != 3 or not all(math.isfinite(x) for x in values):
            raise ValueError
        return values
    except ValueError:
        raise ToolError("Coordinates must be three finite numbers.") from None


def _pdb(path):
    lines = Path(path).read_text().splitlines()
    atoms, bonds, directed = [], {}, {}
    if sum(line.startswith("MODEL ") for line in lines) > 1:
        raise ToolError("Select one PDB model before input preparation.")
    for line in lines:
        if line.startswith(("ATOM  ", "HETATM")):
            if line[16:17].strip():
                raise ToolError("Resolve alternate atom locations before input preparation.")
            element = line[76:78].strip().title()
            if not element:
                raise ToolError("PDB atoms need explicit element columns 77-78.")
            try:
                serial = int(line[6:11])
                charge_field = line[78:80].strip()
                charge = (int(charge_field[0]) * (1 if charge_field[1] == "+" else -1)
                          if re.fullmatch(r"[1-9][+-]", charge_field) else 0)
                if charge_field and not re.fullmatch(r"[1-9][+-]", charge_field):
                    raise ValueError
                int(line[22:26])
            except ValueError:
                raise ToolError("Malformed PDB atom serial, residue number, or charge.") from None
            atoms.append(Atom(serial, line[12:16].strip(), element,
                              _xyz(line[i:i + 8] for i in (30, 38, 46)), charge, line))
        elif line.startswith("CONECT"):
            try:
                ids = [int(line[i:i + 5]) for i in range(6, len(line), 5) if line[i:i + 5].strip()]
            except ValueError:
                raise ToolError("Malformed CONECT record.") from None
            if ids:
                for target, order in Counter(ids[1:]).items():
                    key = (ids[0], target)
                    directed[key] = order + directed.get(key, 0)
    if not atoms or len({a.serial for a in atoms}) != len(atoms):
        raise ToolError("PDB must contain atoms with unique serial numbers.")
    serials = {a.serial for a in atoms}
    for (a, b), order in directed.items():
        if a == b or a not in serials or b not in serials:
            raise ToolError("CONECT references a missing atom or self bond.")
        if (b, a) in directed and directed[b, a] != order:
            raise ToolError("Reciprocal CONECT records disagree on bond multiplicity.")
        bonds[tuple(sorted((a, b)))] = order
    return atoms, bonds


def _sdf(atoms, bonds, name):
    if len(atoms) > 999 or len(bonds) > 999:
        raise ToolError("Initial input preparation supports SDF V2000 with at most 999 atoms/bonds.")
    lines = [name, "  charmm-gui-cli    3D", "Coordinate-preserving; no stereochemistry inferred",
             f"{len(atoms):3d}{len(bonds):3d}  0  0  0  0            999 V2000"]
    for atom in atoms:
        if any(len(f"{value:10.4f}") != 10 for value in atom.xyz):
            raise ToolError("Ligand coordinate exceeds SDF V2000 width.")
        lines.append("".join(f"{v:10.4f}" for v in atom.xyz)
                     + f" {atom.element:<3} 0  0  0  0  0  0  0  0  0  0  0  0")
    for (a, b), order in sorted(bonds.items()):
        lines.append(f"{a + 1:3d}{b + 1:3d}{order:3d}  0  0  0  0")
    charges = [(i + 1, atom.charge) for i, atom in enumerate(atoms) if atom.charge]
    for offset in range(0, len(charges), 8):
        chunk = charges[offset:offset + 8]
        lines.append(f"M  CHG{len(chunk):3d}" + "".join(f"{i:4d}{charge:4d}" for i, charge in chunk))
    lines.extend(["M  END", ">  <PDB_ATOM_NAMES>", " ".join(a.name for a in atoms), "",
                  ">  <PDB_RESIDUE_NAME>", name, "", "$$$$"])
    return "\n".join(lines) + "\n"


def read_ligand(path):
    """Read exactly one SDF V2000 molecule with supplied 3D coordinates and bonds."""
    text = Path(path).read_text()
    records = [record for record in text.split("$$$$") if record.strip()]
    if len(records) != 1:
        raise ToolError("Ligand SDF must contain exactly one molecule.")
    lines = records[0].splitlines()
    if len(lines) < 5 or "V2000" not in lines[3]:
        raise ToolError("Ligand input must be SDF V2000; convert other formats explicitly first.")
    try:
        n, nb = int(lines[3][:3]), int(lines[3][3:6])
        if n < 1 or nb < 0 or len(lines) < 5 + n + nb:
            raise ValueError
        atoms, counts = [], Counter()
        charge_codes = {0: 0, 1: 3, 2: 2, 3: 1, 5: -1, 6: -2, 7: -3}
        for i, line in enumerate(lines[4:4 + n]):
            element = line[31:34].strip().title()
            if not re.fullmatch(r"[A-Z][a-z]?", element):
                raise ValueError
            counts[element] += 1
            if int(line[34:36].strip() or "0") != 0:
                raise ToolError("Isotopically labelled ligands need an explicit specialized workflow.")
            charge_code = int(line[36:39].strip() or "0")
            if charge_code not in charge_codes:
                raise ToolError("Radical ligands are not supported by initial input preparation.")
            atoms.append(Atom(i + 1, f"{element.upper()}{counts[element]}", element,
                              _xyz(line[j:j + 10] for j in (0, 10, 20)), charge_codes[charge_code]))
        bonds = {}
        for line in lines[4 + n:4 + n + nb]:
            a, b, order = int(line[:3]) - 1, int(line[3:6]) - 1, int(line[6:9])
            edge = tuple(sorted((a, b)))
            if a == b or min(a, b) < 0 or max(a, b) >= n or edge in bonds or order not in (1, 2, 3, 4):
                raise ValueError
            bonds[edge] = order
        trailer = lines[4 + n + nb:]
        if "M  END" not in trailer:
            raise ValueError
        for line in trailer[:trailer.index("M  END")]:
            if line.startswith("M  CHG"):
                entries = list(map(int, line[6:].split()))
                if len(entries) != 1 + entries[0] * 2:
                    raise ValueError
                for i, charge in zip(entries[1::2], entries[2::2]):
                    if not 1 <= i <= n:
                        raise ValueError
                    atoms[i - 1].charge = charge
            elif line.startswith(("M  RAD", "M  ISO")):
                raise ToolError("Radical/isotopically labelled ligands need an explicit specialized workflow.")
        for i, line in enumerate(trailer):
            if re.search(r"<PDB_ATOM_NAMES>", line):
                names = trailer[i + 1].split()
                if len(names) != n:
                    raise ValueError
                for atom, name in zip(atoms, names):
                    atom.name = name
    except (ValueError, IndexError):
        raise ToolError("Malformed SDF atom, bond, charge, or atom-name records.") from None
    ligand = Ligand(atoms, bonds, text, lines[0].strip() or "LIG")
    _validate_ligand(ligand)
    return ligand


def _validate_ligand(ligand):
    atoms, bonds = ligand.atoms, ligand.bonds
    if len({a.name for a in atoms}) != len(atoms) or any(not re.fullmatch(r"[A-Za-z0-9]{1,4}", a.name) for a in atoms):
        raise ToolError("Ligand atom names must be unique 1-4 character alphanumeric PDB names.")
    if len(atoms) > 1 and not bonds:
        raise ToolError("Ligand has no explicit bonds; connectivity will not be guessed from coordinates.")
    graph = {i: set() for i in range(len(atoms))}
    valences = Counter()
    aromatic = set()
    for (a, b), order in bonds.items():
        if order not in (1, 2, 3, 4):
            raise ToolError("Unsupported ligand bond order.")
        graph[a].add(b)
        graph[b].add(a)
        if order == 4:
            aromatic.update((a, b))
        valences[a] += 1 if order == 4 else order
        valences[b] += 1 if order == 4 else order
    seen, pending = set(), [0]
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(graph[current] - seen)
    if len(seen) != len(atoms):
        raise ToolError("Ligand has disconnected fragments; provide a single connected molecule.")
    max_valence = {"C": 4, "N": 3, "O": 2, "F": 1, "Cl": 1, "Br": 1, "I": 1, "H": 1, "P": 5, "S": 6}
    for i, atom in enumerate(atoms):
        maximum = max_valence.get(atom.element)
        if maximum is not None and valences[i] > maximum + abs(atom.charge):
            raise ToolError(f"Impossible common valence at ligand atom {atom.name}; verify supplied bond orders.")
    report = {"atoms": len(atoms), "heavy_atoms": sum(a.element not in ("H", "D") for a in atoms),
              "bonds": len(bonds), "elements": dict(Counter(a.element for a in atoms)),
              "bond_orders": dict(Counter(str(x) for x in bonds.values())),
              "formal_charge": sum(a.charge for a in atoms), "rdkit_sanitized": False,
              "warnings": ["Coordinates must already represent a bound pose in the protein coordinate frame.",
                           "Input preparation does not produce ligand force-field parameters."]}
    try:
        from rdkit import Chem
        from rdkit.Chem import rdMolDescriptors
    except ImportError:
        report["warnings"].append("RDKit is unavailable: only structural, connectivity and common-valence checks were performed.")
    else:
        mol = Chem.MolFromMolBlock(ligand.sdf.split("$$$$")[0], sanitize=True, removeHs=False)
        if mol is None:
            raise ToolError("RDKit could not sanitize the ligand; verify bonds, charges and atom types.")
        report["rdkit_sanitized"] = True
        report["molecular_formula"] = rdMolDescriptors.CalcMolFormula(mol)
        report["warnings"].append("RDKit sanitization is not a force-field quality or protonation-state assessment.")
    return report


def inspect_ligand(path):
    """Return chemistry checks for one SDF; does not modify the input."""
    return _validate_ligand(read_ligand(path))


def _apply_hydrogen_policy(ligand, policy):
    if policy not in ("preserve", "add_missing"):
        raise ToolError("hydrogen_policy must be preserve or add_missing.")
    if policy == "preserve":
        return ligand
    try:
        from rdkit import Chem
    except ImportError:
        raise ToolError("hydrogen_policy=add_missing requires RDKit; install the chem optional dependency.") from None
    mol = Chem.MolFromMolBlock(ligand.sdf.split("$$$$")[0], sanitize=True, removeHs=False)
    if mol is None:
        raise ToolError("RDKit could not sanitize ligand before adding hydrogens.")
    before = [tuple(mol.GetConformer().GetAtomPosition(i)) for i in range(mol.GetNumAtoms())]
    hydrogenated = Chem.AddHs(mol, addCoords=True)
    for i, xyz in enumerate(before):
        if tuple(hydrogenated.GetConformer().GetAtomPosition(i)) != xyz:
            raise ToolError("Adding hydrogens unexpectedly changed an input atom coordinate.")
    names = [atom.name for atom in ligand.atoms]
    count = 0
    for _ in range(len(names), hydrogenated.GetNumAtoms()):
        while True:
            count += 1
            name = f"H{count}"
            if name not in names:
                break
        names.append(name)
    original = ligand.sdf.splitlines()
    block = Chem.MolToMolBlock(hydrogenated).splitlines()
    n_old, nb_old = len(ligand.atoms), len(ligand.bonds)
    n_new = hydrogenated.GetNumAtoms()
    # Keep the input's explicit stereo declarations; do not add inferred labels
    # derived from the coordinate geometry during RDKit's MolBlock read/write.
    for i in range(n_old):
        block[4 + i] = block[4 + i][:39] + original[4 + i][39:42] + block[4 + i][42:]
    for i in range(nb_old):
        block[4 + n_new + i] = (block[4 + n_new + i][:9] + original[4 + n_old + i][9:12]
                                  + block[4 + n_new + i][12:])
    sdf = "\n".join(block) + "\n>  <PDB_ATOM_NAMES>\n" + " ".join(names) + "\n\n$$$$\n"
    atoms = []
    for i, atom in enumerate(hydrogenated.GetAtoms()):
        atoms.append(Atom(i + 1, names[i], atom.GetSymbol(),
                          tuple(hydrogenated.GetConformer().GetAtomPosition(i)), atom.GetFormalCharge()))
    bonds = {}
    for bond in hydrogenated.GetBonds():
        order = 4 if bond.GetIsAromatic() else int(bond.GetBondTypeAsDouble())
        bonds[tuple(sorted((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())))] = order
    return Ligand(atoms, bonds, sdf, ligand.name)


def apply_hydrogen_policy(ligand_sdf, output_dir, *, policy="add_missing"):
    """Optionally add valence hydrogens without moving input atoms or choosing pH.

    Existing formal charges and explicit stereo declarations are retained.
    Added hydrogen coordinates are geometric estimates, not energy minimization.
    """
    ligand = read_ligand(ligand_sdf)
    prepared = _apply_hydrogen_policy(ligand, policy)
    report = _validate_ligand(prepared)
    report.update({"hydrogen_policy": policy, "added_atoms": len(prepared.atoms) - len(ligand.atoms),
                   "existing_atom_coordinates_preserved": True,
                   "limitations": ["Adds hydrogens from supplied valences/formal charges; does not select a pH-dependent protonation state.",
                                   "Added hydrogen coordinates are geometric estimates; no geometry optimization is performed."]})
    files = _write_outputs(output_dir, {"ligand.sdf": prepared.sdf,
                                       "hydrogen-report.json": json.dumps(report, indent=2) + "\n"})
    return {"files": files, "ligand": report}


def _write_outputs(output_dir, files):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    existing = [name for name in files if (directory / name).exists()]
    if existing:
        raise ToolError("Refusing to overwrite existing input-preparation output: " + ", ".join(existing))
    for name, content in files.items():
        with (directory / name).open("x", encoding="utf-8") as handle:
            handle.write(content)
    return {name: str((directory / name).resolve()) for name in files}


def split_complex(complex_pdb, output_dir, *, ligand_resname="LIG", ligand_chain=None,
                  ligand_resid=None, accept_conect_bond_orders=False):
    """Split one selected ligand from a PDB, preserving coordinates and atom names.

    Repeated CONECT targets are interpreted as bond multiplicity only with an
    explicit acknowledgement. No E/Z or chiral labels are inferred from geometry.
    """
    if not accept_conect_bond_orders:
        raise ToolError("PDB CONECT is not a chemical definition: explicitly accept_conect_bond_orders or supply a trusted SDF.")
    atoms, bonds = _pdb(complex_pdb)
    selected = [a for a in atoms if a.line[17:21].strip() == ligand_resname
                and (ligand_chain is None or a.line[21:22].strip() == ligand_chain)
                and (ligand_resid is None or int(a.line[22:26]) == ligand_resid)]
    residues = {(a.line[17:27], a.line[72:76]) for a in selected}
    if len(residues) != 1:
        raise ToolError("Ligand selector must identify exactly one residue; specify chain/residue number.")
    ids = {a.serial for a in selected}
    if any((a in ids) != (b in ids) for a, b in bonds):
        raise ToolError("Covalently connected protein/ligand or other cross-residue bonds require a specialized workflow.")
    protein = [a for a in atoms if a.serial not in ids]
    _validate_protein(protein)
    mapping = {a.serial: i for i, a in enumerate(selected)}
    internal = {tuple(sorted((mapping[a], mapping[b]))): order for (a, b), order in bonds.items() if a in ids and b in ids}
    sdf = _sdf(selected, internal, ligand_resname)
    ligand = Ligand(selected, internal, sdf, ligand_resname)
    report = _validate_ligand(ligand)
    report.update({"source": str(Path(complex_pdb).resolve()), "source_sha256": _hash(complex_pdb),
                   "bond_order_source": "User-acknowledged PDB CONECT multiplicity",
                   "stereochemistry": "No explicit stereochemical labels inferred; input coordinates preserved"})
    protein_ids = {a.serial for a in protein}
    protein_bonds = {edge: order for edge, order in bonds.items() if set(edge) <= protein_ids}
    files = {"protein.pdb": "\n".join(a.line for a in protein) + "\n" + _conect(protein_bonds) + "END\n",
             "ligand.sdf": sdf,
             "ligand.pdb": "\n".join(a.line for a in selected) + "\n" + _conect({edge: order for edge, order in bonds.items() if set(edge) <= ids}) + "END\n",
             "split-report.json": json.dumps(report, indent=2) + "\n"}
    paths = _write_outputs(output_dir, files)
    return {"files": paths, "ligand": report}


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _validate_protein(atoms):
    if not atoms or not any(a.line[12:16].strip() == "CA" for a in atoms):
        raise ToolError("Protein input must contain protein ATOM records including alpha carbons.")
    if any(not a.line.startswith("ATOM  ") for a in atoms):
        raise ToolError("Protein input contains HETATM records; resolve cofactors, waters and ligands explicitly first.")


def _conect(bonds):
    neighbors = {}
    for (a, b), order in sorted(bonds.items()):
        # Aromatic SDF remains authoritative; PDB CONECT carries connectivity only.
        multiplicity = order if order in (1, 2, 3) else 1
        neighbors.setdefault(a, []).extend([b] * multiplicity)
        neighbors.setdefault(b, []).extend([a] * multiplicity)
    return "".join(f"CONECT{a:5d}" + "".join(f"{b:5d}" for b in targets[i:i + 4]) + "\n"
                   for a, targets in sorted(neighbors.items()) for i in range(0, len(targets), 4))


def prepare_inputs(protein_pdb, ligand_sdf, output_dir, *, ligand_resname="LIG", ligand_chain=None,
                   ligand_resid=1, allow_distant_pose=False, hydrogen_policy="preserve"):
    """Merge a protein PDB and chemically defined ligand SDF in their current frame.

    Outputs include a mapping between SDF atom index, ligand PDB names and complex
    serials. No coordinate transform, docking, protonation or stereochemistry is
    invented. Very distant/overlapping poses fail before upload.
    """
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,2}", ligand_resname):
        raise ToolError("ligand_resname must be a 1-3 character PDB residue name beginning with a letter.")
    if type(ligand_resid) is not int or not 1 <= ligand_resid <= 9999:
        raise ToolError("ligand_resid must be an integer between 1 and 9999.")
    protein, protein_bonds = _pdb(protein_pdb)
    _validate_protein(protein)
    ligand = _apply_hydrogen_policy(read_ligand(ligand_sdf), hydrogen_policy)
    report = _validate_ligand(ligand)
    chains = {a.line[21:22] for a in protein}
    if ligand_chain is None:
        ligand_chain = next((c for c in "LABCDEFGHIJKMNOPQRSTUVWXYZ0123456789" if c not in chains), None)
    if not isinstance(ligand_chain, str) or not re.fullmatch(r"[A-Za-z0-9]", ligand_chain) or ligand_chain in chains:
        raise ToolError("Ligand chain must be one unused alphanumeric character.")
    if ligand_resname in {a.line[17:21].strip() for a in protein}:
        raise ToolError("Ligand residue name collides with a residue in the protein input.")
    if len(protein) + len(ligand.atoms) > 99999:
        raise ToolError("Combined PDB would exceed 99999 atom serials.")
    heavy_protein = [a for a in protein if a.element not in ("H", "D")]
    heavy_ligand = [a for a in ligand.atoms if a.element not in ("H", "D")]
    if not heavy_protein or not heavy_ligand:
        raise ToolError("Protein and ligand must both have heavy atoms.")
    distance = min(math.dist(a.xyz, b.xyz) for a in heavy_protein for b in heavy_ligand)
    if distance < 0.8:
        raise ToolError("Protein/ligand heavy atoms overlap (<0.8 A); verify coordinate frame and binding pose.")
    if distance > 6 and not allow_distant_pose:
        raise ToolError("Ligand is >6 A from protein; supply a bound pose in the same coordinate frame or explicitly allow_distant_pose.")
    protein_map = {a.serial: i + 1 for i, a in enumerate(protein)}
    protein_lines = [a.line[:6] + f"{i + 1:5d}" + a.line[11:] for i, a in enumerate(protein)]
    ligand_lines, atom_map = [], []
    for i, atom in enumerate(ligand.atoms):
        serial = len(protein) + i + 1
        if any(len(f"{value:8.3f}") != 8 for value in atom.xyz):
            raise ToolError("Ligand coordinate exceeds PDB field width.")
        name = f" {atom.name:<3}" if len(atom.element) == 1 and len(atom.name) < 4 else f"{atom.name:<4}"
        charge = f"{abs(atom.charge)}{'+' if atom.charge > 0 else '-'}" if atom.charge else "  "
        if len(charge) != 2:
            raise ToolError("Ligand charge exceeds PDB charge field width.")
        line = (f"HETATM{serial:5d} {name} {ligand_resname:>3} {ligand_chain}{ligand_resid:4d}    "
                + "".join(f"{v:8.3f}" for v in atom.xyz) + f"  1.00  0.00          {atom.element:>2}{charge}")
        ligand_lines.append(line)
        atom_map.append({"sdf_index": i + 1, "pdb_serial": serial, "pdb_atom_name": atom.name,
                         "element": atom.element, "input_coordinates_A": list(atom.xyz)})
    mapped_protein = {tuple(protein_map[i] for i in edge): order for edge, order in protein_bonds.items()}
    mapped_ligand = {tuple(len(protein) + i + 1 for i in edge): order for edge, order in ligand.bonds.items()}
    report.update({"minimum_protein_ligand_heavy_distance_A": distance, "resname": ligand_resname,
                   "chain": ligand_chain, "residue_number": ligand_resid, "atom_mapping": atom_map,
                   "hydrogen_policy": hydrogen_policy})
    report["heavy_atom_bonds"] = [
        {"atoms": [ligand.atoms[a].name, ligand.atoms[b].name], "input_order": order}
        for (a, b), order in sorted(ligand.bonds.items())
        if ligand.atoms[a].element not in ("H", "D", "T") and ligand.atoms[b].element not in ("H", "D", "T")]
    if hydrogen_policy == "add_missing":
        report["warnings"].append("Added valence hydrogens without selecting a pH-dependent protonation state or optimizing geometry.")
    if 4 in ligand.bonds.values():
        report["warnings"].append("Aromatic bonds remain in SDF; combined PDB CONECT encodes their connectivity as single edges.")
    if distance > 6:
        report["warnings"].append("Distant ligand pose was explicitly allowed; check the binding pose before submitting.")
    manifest = {"protein": {"path": str(Path(protein_pdb).resolve()), "sha256": _hash(protein_pdb), "atoms": len(protein)},
                "ligand_input": {"path": str(Path(ligand_sdf).resolve()), "sha256": _hash(ligand_sdf)},
                "ligand": report, "coordinate_transform": "none", "force_field_parameters_generated": False}
    files = {"protein.pdb": "\n".join(protein_lines) + "\n" + _conect(mapped_protein) + "END\n",
             "ligand.pdb": "\n".join(ligand_lines) + "\n" + _conect(mapped_ligand) + "END\n",
             "ligand.sdf": ligand.sdf,
             "complex.pdb": "\n".join(protein_lines) + "\nTER\n" + "\n".join(ligand_lines) + "\n"
                            + _conect({**mapped_protein, **mapped_ligand}) + "END\n",
             "input-manifest.json": json.dumps(manifest, indent=2) + "\n"}
    return {"files": _write_outputs(output_dir, files), "manifest": manifest}
