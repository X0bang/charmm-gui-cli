"""Explicit ligand-record selection and verified graph-preserving conversion."""

from collections import Counter
import hashlib
import math
from pathlib import Path
import re

from ..store.auth import ToolError
from .pdb_normalization import finding


def _error(key, **params):
    return ToolError(key="input." + key, params=params, next_step="charmm-gui-cli check -h")


def _visible(value):
    value = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x1f\x7f]", " ", value)
    value = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|(?i:password|token|cookie|authorization)\s*[:=]\s*\S+", "[redacted]", value)
    return value[:80]


def _properties(record):
    lines, props = record.splitlines(), {}
    for index, line in enumerate(lines):
        match = re.match(r"^>\s*<([^<>]+)>", line)
        if match and match[1] in ("minimizedAffinity", "vina_affinity", "docking_score", "score", "TotalEnergy") and index + 1 < len(lines):
            try:
                value = float(lines[index + 1])
                if math.isfinite(value):
                    props[match[1]] = value
            except ValueError:
                pass
    return props


def records(text, *, mol2=False):
    if mol2:
        starts = [m.start() for m in re.finditer(r"(?m)^@<TRIPOS>MOLECULE\s*$", text)]
        return [text[left:right] for left, right in zip(starts, starts[1:] + [len(text)])]
    result, current = [], []
    for line in text.splitlines(keepends=True):
        current.append(line)
        if line.strip() == "$$$$":
            result.append("".join(current))
            current = []
    if "".join(current).strip():
        result.append("".join(current))
    return result


def select(path, text, pose=None):
    mol2 = Path(path).suffix.lower() == ".mol2"
    blocks = records(text, mol2=mol2)
    if not blocks:
        raise _error("ligand_empty")
    inventory = []
    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        name = lines[1] if mol2 and len(lines) > 1 else lines[0] if lines else ""
        inventory.append({"index": index, "name": _visible(name), "scores": _properties(block)})
    details = [{"key": "input.ligand_pose_row", "params": {"index": item["index"], "name": item["name"] or "—",
                "scores": ", ".join(f"{name}={value:g}" for name, value in item["scores"].items()) or "—"}} for item in inventory[:20]]
    if len(blocks) > 1 and pose is None:
        raise ToolError(key="input.ligand_poses", params={"count": len(blocks)}, details=details,
                        next_step="charmm-gui-cli check --ligand-pose 1 -h")
    if pose is not None and (type(pose) is not int or not 1 <= pose <= len(blocks)):
        raise ToolError(key="input.ligand_pose_range", params={"count": len(blocks)}, details=details,
                        next_step="charmm-gui-cli check --ligand-pose 1 -h")
    index = pose or 1
    block = text if len(blocks) == 1 else blocks[index - 1]
    evidence = {"source_format": "mol2" if mol2 else "sdf", "pose_count": len(blocks), "selected_pose": index,
                "pose_inventory": inventory, "selected_record_sha256": hashlib.sha256(block.encode()).hexdigest(),
                "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "explicit_pose_selection": pose is not None}
    return block, evidence, len(blocks) > 1


def _rdkit():
    try:
        from rdkit import Chem, rdBase
    except ImportError:
        raise _error("ligand_rdkit") from None
    return Chem, rdBase


def _clear_inferred_stereo(mol, keep_atoms=(), keep_bonds=()):
    from rdkit import Chem
    for atom in mol.GetAtoms():
        if atom.GetIdx() not in keep_atoms:
            atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
            if atom.HasProp("_CIPCode"):
                atom.ClearProp("_CIPCode")
    for bond in mol.GetBonds():
        if bond.GetIdx() not in keep_bonds:
            bond.SetStereo(Chem.BondStereo.STEREONONE)
            bond.SetBondDir(Chem.BondDir.NONE)


def _signature(mol):
    from rdkit import Chem
    Chem.AssignStereochemistry(mol, cleanIt=False, force=True)
    return {"atoms": [(a.GetSymbol(), a.GetFormalCharge(), a.GetNumExplicitHs(), a.GetNumImplicitHs(),
                       a.GetProp("_CIPCode") if a.HasProp("_CIPCode") else "") for a in mol.GetAtoms()],
            "bonds": [(*sorted((b.GetBeginAtomIdx(), b.GetEndAtomIdx())), str(b.GetBondType()), str(b.GetStereo())) for b in mol.GetBonds()]}


def _depicted_double_bonds(mol):
    """2D molfile geometry encodes E/Z even when the double bond has no CFG.

    Do not infer new declarations from a 3D docking conformation.
    """
    from rdkit import Chem
    if mol.GetConformer().Is3D():
        return set()
    Chem.SetBondStereoFromDirections(mol)
    Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    return {b.GetIdx() for b in mol.GetBonds()
            if b.GetBondType() == Chem.BondType.DOUBLE and b.GetStereo() != Chem.BondStereo.STEREONONE}


def normalize(block, *, mol2=False):
    Chem, rdBase = _rdkit()
    try:
        with rdBase.BlockLogs():
            molecule = Chem.MolFromMol2Block(block, sanitize=False, removeHs=False, cleanupSubstructures=False) if mol2 else \
                Chem.MolFromMolBlock(block.split("$$$$")[0], sanitize=False, removeHs=False, strictParsing=True)
        if molecule is None or molecule.GetNumConformers() != 1 or not molecule.GetNumAtoms():
            raise ValueError
        if molecule.GetNumAtoms() > 999 or molecule.GetNumBonds() > 999:
            raise _error("ligand_size")
        if (any(a.HasQuery() or a.GetIsotope() for a in molecule.GetAtoms()) or any(b.HasQuery() for b in molecule.GetBonds())
                or len(Chem.GetMolSubstanceGroups(molecule)) or len(molecule.GetStereoGroups())):
            raise _error("ligand_semantics")
        supported = {Chem.BondType.SINGLE, Chem.BondType.DOUBLE, Chem.BondType.TRIPLE, Chem.BondType.AROMATIC}
        if any(b.GetBondType() not in supported for b in molecule.GetBonds()):
            raise _error("ligand_semantics")
        keep_atoms, keep_bonds = set(), set()
        if not mol2:
            keep_atoms.update(a.GetIdx() for a in molecule.GetAtoms() if a.HasProp("molParity") and a.GetIntProp("molParity"))
            for bond in molecule.GetBonds():
                if bond.HasProp("_MolFileBondCfg") and bond.GetIntProp("_MolFileBondCfg"):
                    keep_bonds.add(bond.GetIdx())
                    keep_atoms.add(bond.GetBeginAtomIdx())
        if mol2:
            # Mol2 readers disable implicit Hs. Represent ordinary valence Hs
            # exactly as in a supplied SDF; never treat partial charges as pH.
            for atom in molecule.GetAtoms():
                atom.SetNoImplicit(False)
        coordinates = [tuple(molecule.GetConformer().GetAtomPosition(i)) for i in range(molecule.GetNumAtoms())]
        if any(not math.isfinite(value) for xyz in coordinates for value in xyz):
            raise ValueError
        with rdBase.BlockLogs():
            Chem.SanitizeMol(molecule)
        if not mol2:
            keep_bonds.update(_depicted_double_bonds(molecule))
        _clear_inferred_stereo(molecule, keep_atoms, keep_bonds)
        if any(a.GetNumRadicalElectrons() for a in molecule.GetAtoms()):
            raise _error("ligand_semantics")
        counts, names = Counter(), []
        source_names = [a.GetProp("_TriposAtomName") for a in molecule.GetAtoms()] if mol2 else []
        explicit = bool(source_names) and len(set(source_names)) == len(source_names) and all(re.fullmatch(r"[A-Za-z0-9]{1,4}", name) for name in source_names)
        if not mol2:
            match = re.search(r"(?m)^>\s*<PDB_ATOM_NAMES>[^\n]*\n([^\n]+)", block)
            if match:
                source_names = match[1].split()
                if len(source_names) != molecule.GetNumAtoms() or len(set(source_names)) != len(source_names):
                    raise ValueError
                explicit = True
        for atom in molecule.GetAtoms():
            counts[atom.GetSymbol()] += 1
            names.append(source_names[atom.GetIdx()] if explicit else atom.GetSymbol().upper() + str(counts[atom.GetSymbol()]))
        if any(not re.fullmatch(r"[A-Za-z0-9]{1,4}", name) for name in names):
            raise _error("ligand_names")
        before = _signature(molecule)
        converted = Chem.MolToMolBlock(molecule, includeStereo=True, kekulize=False, forceV3000=False)
        if "V2000" not in converted.splitlines()[3]:
            raise _error("ligand_size")
        with rdBase.BlockLogs():
            roundtrip = Chem.MolFromMolBlock(converted, removeHs=False, strictParsing=True)
        if roundtrip is None:
            raise ValueError
        _clear_inferred_stereo(roundtrip, keep_atoms, keep_bonds)
        if _signature(roundtrip) != before:
            raise _error("ligand_roundtrip")
        deviation = max(abs(value - other) for i, xyz in enumerate(coordinates)
                        for value, other in zip(xyz, tuple(roundtrip.GetConformer().GetAtomPosition(i))))
        # Keep original SDF properties, replacing only the atom-name mapping.
        properties = block.split("M  END", 1)[1].split("$$$$")[0] if not mol2 and "M  END" in block else ""
        properties = re.sub(r"(?ms)^>\s*<PDB_ATOM_NAMES>[^\n]*\n.*?(?=^>\s*<|\Z)", "", properties)
        text = converted.rstrip() + "\n" + properties.rstrip() + "\n\n> <PDB_ATOM_NAMES>\n" + " ".join(names) + "\n\n$$$$\n"
        evidence = {"input_format": "mol2" if mol2 else "sdf_v3000", "output_format": "sdf_v2000",
                    "explicit_atom_names": explicit, "coordinates_transformed": False,
                    "maximum_coordinate_roundoff_A": deviation, "graph_and_stereo_roundtrip_verified": True,
                    "atom_name_mapping": [{"index": i + 1, "source_name": source_names[i] if source_names else None, "pdb_atom_name": name} for i, name in enumerate(names)]}
        if mol2:
            partial = [a.GetDoubleProp("_TriposPartialCharge") if a.HasProp("_TriposPartialCharge") else None for a in molecule.GetAtoms()]
            if any(value is not None and not math.isfinite(value) for value in partial):
                raise ValueError
            evidence.update(input_partial_charges=partial, partial_charge_sum=sum(value for value in partial if value is not None),
                            net_formal_charge=sum(a.GetFormalCharge() for a in molecule.GetAtoms()), review_required=True,
                            implicit_valence_hydrogens=sum(a.GetNumImplicitHs() for a in molecule.GetAtoms()),
                            input_atom_types=[a.GetProp("_TriposAtomType") if a.HasProp("_TriposAtomType") else None for a in molecule.GetAtoms()])
        return text, evidence
    except ToolError:
        raise
    except (ValueError, IndexError, RuntimeError, OverflowError):
        raise _error("ligand_parse", format="mol2" if mol2 else "V3000") from None


def ligand_text(path, *, pose=None, report=None):
    text = Path(path).read_bytes().decode("utf-8")
    block, evidence, selected = select(path, text, pose)
    lines = block.splitlines()
    mol2 = Path(path).suffix.lower() == ".mol2"
    if mol2 or len(lines) > 3 and "V3000" in lines[3]:
        block, conversion = normalize(block, mol2=mol2)
        evidence["conversion"] = conversion
        evidence["explicit_atom_names"] = conversion["explicit_atom_names"]
    else:
        evidence["explicit_atom_names"] = bool(re.search(r"<PDB_ATOM_NAMES>", block))
    if report is not None:
        report.update(evidence)
        repairs = report.setdefault("repairs", [])
        if selected or pose is not None:
            repairs.append(finding("input.ligand_pose_selected", index=evidence["selected_pose"], count=evidence["pose_count"], name=evidence["pose_inventory"][evidence["selected_pose"] - 1]["name"] or "—"))
        if "conversion" in evidence:
            repairs.append(finding("input.ligand_converted", format=evidence["conversion"]["input_format"]))
            if evidence["conversion"]["maximum_coordinate_roundoff_A"]:
                repairs.append(finding("input.ligand_precision", delta=f"{evidence['conversion']['maximum_coordinate_roundoff_A']:.8g}"))
    return block, evidence
