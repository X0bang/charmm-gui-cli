"""Carry explicitly supplied mapped chemistry through 3D SDF intermediates."""

import re

from ..store.auth import ToolError
from ..locales.i18n import t

PROPERTY = 'CHARMM_GUI_DECLARED_SMILES'


def annotation(mol):
    from rdkit import Chem
    molecule = Chem.Mol(mol)
    for atom in molecule.GetAtoms():
        atom.SetAtomMapNum(atom.GetIdx() + 1)
    return '\n> <' + PROPERTY + '>\n' + Chem.MolToSmiles(molecule, isomericSmiles=True) + '\n\n'


def declared(text, molecule):
    """Validate graph and declared stereo against coordinates; return keep masks."""
    from rdkit import Chem, rdBase
    matches = re.findall(r'(?m)^>\s*<' + PROPERTY + r'>[^\n]*\n([^\n]+)', text)
    if not matches:
        return set(), set()
    try:
        if len(matches) != 1 or len(matches[0]) > 100000:
            raise ValueError
        params = Chem.SmilesParserParams()
        params.removeHs = False
        params.parseName = False
        params.allowCXSMILES = False
        with rdBase.BlockLogs():
            template = Chem.MolFromSmiles(matches[0], params)
        if (template is None or len(template.GetStereoGroups()) or template.GetNumAtoms() != molecule.GetNumAtoms()
                or template.GetNumBonds() != molecule.GetNumBonds()):
            raise ValueError
        numbers = [a.GetAtomMapNum() for a in template.GetAtoms()]
        if set(numbers) != set(range(1, molecule.GetNumAtoms() + 1)):
            raise ValueError
        wanted_atoms = {a.GetIdx() for a in template.GetAtoms() if a.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED}
        wanted_bonds = {b.GetIdx() for b in template.GetBonds() if b.GetStereo() != Chem.BondStereo.STEREONONE}
        # Maps encode correspondence only. RDKit's legacy stereo ranking also
        # uses map labels, so compare copies without them; never rewrite the
        # caller's atoms, coordinates or declared mapping.
        actual_molecule = Chem.Mol(molecule)
        for candidate in (template, actual_molecule):
            for atom in candidate.GetAtoms():
                atom.SetAtomMapNum(0)
                for name in ('_CIPCode', '_CIPRank'):
                    if atom.HasProp(name):
                        atom.ClearProp(name)
            # A staged molecule can retain explicit double-bond stereo while
            # its neighboring direction flags were cleared. Re-express that
            # same state before clean reassignment; do not infer a new state.
            Chem.SetDoubleBondNeighborDirections(candidate)
            Chem.AssignStereochemistry(candidate, cleanIt=True, force=True)
        if (any(template.GetAtomWithIdx(i).GetChiralTag() == Chem.ChiralType.CHI_UNSPECIFIED for i in wanted_atoms)
                or any(template.GetBondWithIdx(i).GetStereo() == Chem.BondStereo.STEREONONE for i in wanted_bonds)):
            raise ValueError  # Map-only pseudo-stereo is not a verified state.
        atoms, bonds = set(), set()
        for atom in template.GetAtoms():
            index = numbers[atom.GetIdx()] - 1
            actual = actual_molecule.GetAtomWithIdx(index)
            if (atom.GetAtomicNum() != actual.GetAtomicNum() or atom.GetFormalCharge() != actual.GetFormalCharge()
                    or atom.GetIsotope() or atom.GetNumRadicalElectrons()
                    or atom.GetTotalNumHs() != actual.GetTotalNumHs()):
                raise ValueError
            if atom.HasProp('_CIPCode'):
                if not actual.HasProp('_CIPCode') or actual.GetProp('_CIPCode') != atom.GetProp('_CIPCode'):
                    raise ValueError
                atoms.add(index)
        for bond in template.GetBonds():
            left, right = (numbers[i] - 1 for i in (bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))
            actual = actual_molecule.GetBondBetweenAtoms(left, right)
            if actual is None or actual.GetBondType() != bond.GetBondType():
                raise ValueError
            if bond.GetStereo() != Chem.BondStereo.STEREONONE:
                if actual.GetStereo() != bond.GetStereo():
                    raise ValueError
                bonds.add(actual.GetIdx())
        return atoms, bonds
    except (ValueError, RuntimeError, IndexError):
        raise ToolError(key='input.ligand_declared_stereo', next_step=t('input.pdbqt_next')) from None
