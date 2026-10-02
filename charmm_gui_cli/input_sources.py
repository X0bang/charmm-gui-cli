"""Resolve original structure inputs into explicit, coordinate-preserving sources.

No bond perception by distance, docking, or arbitrary symmetry matching is used.
Intermediate files are generated in a new directory and inputs are read-only.
"""

from dataclasses import replace
import json
from pathlib import Path
import re

from .auth import ToolError
from .inputs import (Ligand, _conect, _pdb, _sdf, _validate_ligand,
                     _validate_protein, read_ligand)


def _path(value, label):
    if value is None:
        return None
    path = Path(value).resolve()
    if not path.is_file():
        raise ToolError(f"{label} file does not exist: {path}.")
    return path


def _one_ligand(atoms, *, resname=None):
    selected = [a for a in atoms if resname is None or a.line[17:21].strip() == resname]
    residues = {(a.line[17:27], a.line[72:76]) for a in selected}
    if len(residues) != 1 or not selected:
        raise ToolError("Select exactly one ligand residue; split multiple copies first or specify the correct ligand_resname.")
    return selected


def _internal(atoms, bonds):
    index = {a.serial: i for i, a in enumerate(atoms)}
    if any((a in index) != (b in index) for a, b in bonds):
        raise ToolError("Covalently attached ligand requires an explicit specialized workflow.")
    return {tuple(sorted((index[a], index[b]))): order for (a, b), order in bonds.items() if a in index and b in index}


def _protein_identity(atoms):
    keys = [(a.line[21:27], a.line[17:21].strip(), a.name, a.element) for a in atoms]
    if len(set(keys)) != len(keys):
        raise ToolError("Protein atom identities are ambiguous; resolve repeated chains/residues/atom names.")
    return {key: atom.xyz for key, atom in zip(keys, atoms)}


def _check_protein(protein, complex_atoms):
    supplied, _ = _pdb(protein)
    _validate_protein(supplied)
    a, b = _protein_identity(supplied), _protein_identity(complex_atoms)
    if a.keys() != b.keys() or any(max(abs(x - y) for x, y in zip(a[key], b[key])) > 0.0001 for key in a):
        raise ToolError("Supplied protein does not match the complex protein atom identities and coordinates; provide matching structures in the same coordinate frame.")


def _from_smiles(atoms, bonds, smiles, resname):
    try:
        from rdkit import Chem
    except ImportError:
        raise ToolError("PDB plus SMILES conversion requires RDKit (the chem optional dependency).") from None
    template = Chem.MolFromSmiles(smiles)
    if template is None or len(Chem.GetMolFrags(template)) != 1:
        raise ToolError("ligand_smiles must define one valid connected molecule with explicit intended charges.")
    if any(atom.GetIsotope() for atom in template.GetAtoms()):
        raise ToolError("Isotopically labelled SMILES is not supported; isotope semantics must not be discarded during PDB/SDF conversion.")
    if any(atom.GetNumRadicalElectrons() for atom in template.GetAtoms()):
        raise ToolError("Radical SMILES is not supported; radical electron semantics must not be discarded during conversion.")
    allowed_bonds = {Chem.BondType.SINGLE, Chem.BondType.DOUBLE, Chem.BondType.TRIPLE, Chem.BondType.AROMATIC}
    if any(bond.GetBondType() not in allowed_bonds for bond in template.GetBonds()):
        raise ToolError("SMILES contains an unsupported bond type (such as dative/coordination); it cannot be converted to an ordinary covalent bond.")
    if any(a.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED for a in template.GetAtoms()) or any(
            b.GetStereo() != Chem.BondStereo.STEREONONE for b in template.GetBonds()):
        raise ToolError("Stereochemically specified SMILES needs an explicitly mapped bound SDF; this adapter does not infer/check stereochemistry from PDB coordinates.")
    template = Chem.RemoveHs(template)
    heavy = [a for a in atoms if a.element not in ("H", "D", "T")]
    old_to_new = {i: j for j, i in enumerate(i for i, a in enumerate(atoms) if a.element not in ("H", "D", "T"))}
    edges = {tuple(sorted((old_to_new[a], old_to_new[b]))) for a, b in bonds if a in old_to_new and b in old_to_new}
    if len(heavy) > 1 and not edges:
        raise ToolError("PDB lacks CONECT connectivity. Supply a bound SDF or explicit atom mapping/connectivity; SMILES alone cannot assign coordinates without guessing bonds.")
    if len(heavy) != template.GetNumAtoms():
        raise ToolError("SMILES and PDB heavy-atom counts differ.")
    source_graph, template_graph = Chem.RWMol(), Chem.RWMol()
    for atom in heavy:
        source_graph.AddAtom(Chem.Atom(atom.element))
    for atom in template.GetAtoms():
        template_graph.AddAtom(Chem.Atom(atom.GetAtomicNum()))
    for a, b in edges:
        source_graph.AddBond(a, b, Chem.BondType.SINGLE)
    for bond in template.GetBonds():
        template_graph.AddBond(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), Chem.BondType.SINGLE)
    if len(edges) != template.GetNumBonds():
        raise ToolError("PDB connectivity and SMILES molecular graph differ.")
    matches = source_graph.GetMol().GetSubstructMatches(template_graph.GetMol(), uniquify=False, maxMatches=2)
    if len(matches) != 1:
        raise ToolError("SMILES/PDB atom mapping is ambiguous or incompatible; provide a bound SDF with explicit atom names/mapping instead of selecting a symmetric match.")
    mapping = matches[0]  # A unique graph isomorphism, never an arbitrary match.
    prepared = list(heavy)
    for template_index, source_index in enumerate(mapping):
        charge = template.GetAtomWithIdx(template_index).GetFormalCharge()
        if prepared[source_index].charge and prepared[source_index].charge != charge:
            raise ToolError("PDB explicit charge conflicts with supplied SMILES.")
        prepared[source_index] = replace(prepared[source_index], charge=charge)
    chemical_bonds = {tuple(sorted((mapping[b.GetBeginAtomIdx()], mapping[b.GetEndAtomIdx()]))):
                      4 if b.GetIsAromatic() else int(b.GetBondTypeAsDouble()) for b in template.GetBonds()}
    sdf = _sdf(prepared, chemical_bonds, resname)
    _validate_ligand(Ligand(prepared, chemical_bonds, sdf, resname))
    return sdf, {"chemical_source": "user-supplied SMILES", "mapping": "unique element/connectivity graph isomorphism",
                 "discarded_coordinate_hydrogens": len(atoms) - len(heavy),
                 "note": "Existing heavy coordinates retained; downstream hydrogen policy supplies valence hydrogens. No pH state chosen."}


def _chemical_sdf_in_complex(path, atoms, bonds):
    molecule = read_ligand(path)
    if not re.search(r"<PDB_ATOM_NAMES>", molecule.sdf):
        raise ToolError("Complex plus SDF requires explicit PDB_ATOM_NAMES in SDF; generated/default sequential names are not an atom mapping.")
    coordinate_names = {a.name: a for a in atoms}
    if len(coordinate_names) != len(atoms) or set(coordinate_names) != {a.name for a in molecule.atoms}:
        raise ToolError("Complex/SDF atom names must map one-to-one, including explicit hydrogens. Supply a matching named SDF; unmatched hydrogen/atom sets are not guessed.")
    if any(coordinate_names[a.name].element != a.element for a in molecule.atoms):
        raise ToolError("Complex/SDF named atoms have different elements.")
    if any(coordinate_names[a.name].charge and coordinate_names[a.name].charge != a.charge for a in molecule.atoms):
        raise ToolError("Complex explicit PDB charge conflicts with supplied SDF chemistry.")
    source_edges = {tuple(sorted((atoms[a].name, atoms[b].name))) for a, b in bonds}
    chemistry_edges = {tuple(sorted((molecule.atoms[a].name, molecule.atoms[b].name))) for a, b in molecule.bonds}
    if source_edges and source_edges != chemistry_edges:
        raise ToolError("Complex CONECT connectivity must match the named SDF graph; provide explicit matching connectivity rather than guessing by distance.")
    lines = molecule.sdf.splitlines()
    n = len(molecule.atoms)
    if any(int(lines[4 + i][39:42].strip() or "0") for i in range(n)) or any(
            int(lines[4 + n + i][9:12].strip() or "0") for i in range(len(molecule.bonds))):
        raise ToolError("Stereo-labelled SDF coordinate transfer requires stereochemical verification; use its already bound coordinates as a standalone ligand SDF.")
    for i, atom in enumerate(molecule.atoms):
        xyz = coordinate_names[atom.name].xyz
        lines[4 + i] = "".join(f"{value:10.4f}" for value in xyz) + lines[4 + i][30:]
    sdf = "\n".join(lines) + "\n"
    return sdf, {"chemical_source": str(path), "mapping": "explicit unique PDB_ATOM_NAMES with matching elements",
                 "complex_connectivity_check": "matches SDF graph" if source_edges else
                    "Complex contains no CONECT graph; chemistry comes from the named SDF, with no independent connectivity evidence in the complex."}


def prepare_sources(*, out, protein=None, ligand=None, complex_pdb=None, ligand_resname="LIG",
                    ligand_smiles=None, accept_conect_bond_orders=False):
    """Materialize PDB/SDF sources without asking users to manage intermediates.

    PDB+SMILES requires encoded connectivity and a unique heavy graph mapping.
    Complex+SDF requires explicit atom names and identical atom sets/elements.
    Existing complex CONECT must match; when absent the named SDF supplies the
    chemical graph. Stereo-labelled transfer is intentionally refused.
    """
    destination = Path(out).resolve()
    if destination.exists():
        raise ToolError("prepare_sources out must be a new directory; existing outputs are never overwritten.")
    protein, ligand, complex_pdb = (_path(value, label) for value, label in
                                   ((protein, "Protein"), (ligand, "Ligand"), (complex_pdb, "Complex")))
    if ligand_smiles is not None and (not isinstance(ligand_smiles, str) or not ligand_smiles.strip()):
        raise ToolError("ligand_smiles must be a non-empty chemical SMILES string.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,2}", ligand_resname):
        raise ToolError("ligand_resname must be a 1-3 character PDB residue name beginning with a letter.")
    if not complex_pdb and (not protein or not ligand):
        raise ToolError("Provide protein plus bound ligand PDB/SDF, or one bound complex PDB.")
    if ligand and ligand.suffix.lower() not in (".pdb", ".sdf"):
        raise ToolError("Ligand source must be PDB or SDF V2000.")
    if ligand and ligand.suffix.lower() == ".sdf" and ligand_smiles:
        raise ToolError("Supply chemical SDF or ligand_smiles, not competing chemical definitions.")
    generated, report = {}, {"coordinate_transform": "none", "source_inputs": {
        "protein": str(protein) if protein else None, "ligand": str(ligand) if ligand else None,
        "complex": str(complex_pdb) if complex_pdb else None}}
    if complex_pdb:
        all_atoms, all_bonds = _pdb(complex_pdb)
        selected = _one_ligand(all_atoms, resname=ligand_resname)
        selected_ids = {a.serial for a in selected}
        protein_atoms = [a for a in all_atoms if a.serial not in selected_ids]
        _validate_protein(protein_atoms)
        bonds = _internal(selected, all_bonds)
        if protein:
            _check_protein(protein, protein_atoms)
            report["supplied_protein_checked_against_complex"] = True
        protein_ids = {a.serial for a in protein_atoms}
        generated["protein.pdb"] = "\n".join(a.line for a in protein_atoms) + "\n" + _conect(
            {edge: order for edge, order in all_bonds.items() if set(edge) <= protein_ids}) + "END\n"
        resolved_protein = destination / "protein.pdb"
        coordinate_source = complex_pdb
        if ligand and ligand.suffix.lower() == ".pdb":
            provided, provided_bonds = _pdb(ligand)
            provided = _one_ligand(provided)
            supplied_by_name = {a.name: a for a in provided}
            if (len(supplied_by_name) != len(provided) or len({a.name for a in selected}) != len(selected)
                    or {(a.name, a.element) for a in provided} != {(a.name, a.element) for a in selected}):
                raise ToolError("Supplied ligand PDB atom names/elements differ from complex ligand; provide explicitly matching atoms.")
            chemical = _internal(provided, provided_bonds)
            encoded_edges = {tuple(sorted((provided[a].name, provided[b].name))) for a, b in chemical}
            pose_edges = {tuple(sorted((selected[a].name, selected[b].name))) for a, b in bonds}
            if pose_edges and encoded_edges != pose_edges:
                raise ToolError("Supplied ligand PDB connectivity disagrees with the complex.")
            for atom in selected:
                supplied_atom = supplied_by_name[atom.name]
                if atom.line[78:80].strip() and supplied_atom.line[78:80].strip() and atom.charge != supplied_atom.charge:
                    raise ToolError("Complex and supplied ligand PDB contain conflicting explicit charges.")
            selected = [replace(a, charge=supplied_by_name[a.name].charge if supplied_by_name[a.name].line[78:80].strip()
                                else a.charge) for a in selected]
            lookup = {a.name: i for i, a in enumerate(selected)}
            bonds = {tuple(sorted((lookup[provided[a].name], lookup[provided[b].name]))): order for (a, b), order in chemical.items()}
            report["supplied_ligand_pdb_checked_against_complex"] = True
            report["independent_ligand_coordinates"] = "Not used: explicit complex supplies the bound pose; named atom elements/connectivity were checked."
            report["pdb_chemical_source"] = str(ligand)
            report["complex_connectivity_check"] = "matches supplied ligand PDB graph" if pose_edges else \
                "Complex contains no CONECT graph; connectivity comes from the supplied named ligand PDB."
    else:
        protein_atoms, _ = _pdb(protein)
        _validate_protein(protein_atoms)
        resolved_protein, coordinate_source = protein, ligand
        if ligand.suffix.lower() == ".sdf":
            molecule = read_ligand(ligand)
            resolved_ligand = ligand
            report.update({"coordinate_source": str(ligand), "chemical_source": str(ligand),
                           "mapping": "SDF atom order retained", "ligand_atoms": len(molecule.atoms)})
        else:
            atoms, encoded = _pdb(ligand)
            selected = _one_ligand(atoms)
            bonds = _internal(selected, encoded)
    if complex_pdb or ligand.suffix.lower() == ".pdb":
        if ligand and ligand.suffix.lower() == ".sdf":
            sdf, details = _chemical_sdf_in_complex(ligand, selected, bonds)
        elif ligand_smiles:
            sdf, details = _from_smiles(selected, bonds, ligand_smiles, ligand_resname)
        else:
            if not accept_conect_bond_orders:
                raise ToolError("PDB bond orders need --accept-conect-bond-orders or an explicit ligand_smiles/chemical SDF; connectivity/charges are never inferred by distance.")
            sdf = _sdf(selected, bonds, ligand_resname)
            _validate_ligand(Ligand(selected, bonds, sdf, ligand_resname))
            details = {"chemical_source": "user-acknowledged PDB CONECT bond multiplicity and charge fields",
                       "mapping": "PDB atom names/order retained"}
        generated["ligand.sdf"] = sdf
        resolved_ligand = destination / "ligand.sdf"
        report.update(details)
        report["coordinate_source"] = str(coordinate_source)
    report["limitations"] = ["No docking, distance-based bond guessing, pH-state assignment or energy minimization.",
                              "PDB CONECT chemistry is trusted only after explicit acknowledgement; stereochemistry is not invented."]
    destination.mkdir(parents=True)
    for name, contents in generated.items():
        with (destination / name).open("x") as handle:
            handle.write(contents)
    with (destination / "sources.json").open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return {"protein": str(resolved_protein), "ligand": str(resolved_ligand), "report": report}
