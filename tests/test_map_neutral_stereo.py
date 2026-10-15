"""Correspondence labels cannot change chemical R/S or E/Z verification."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from charmm_gui_cli.chem.ligand_stereo import PROPERTY, annotation, declared
from charmm_gui_cli.chem.inputs import read_ligand, _apply_hydrogen_policy
from charmm_gui_cli.store.auth import ToolError


@unittest.skipUnless(importlib.util.find_spec('rdkit'), 'RDKit chemistry extra is not installed')
class MapNeutralStereoTests(unittest.TestCase):
    def setUp(self):
        from rdkit import Chem
        from rdkit.Chem import AllChem
        self.Chem, self.AllChem = Chem, AllChem
        self.root = Path(tempfile.mkdtemp(prefix='cgui-map-neutral-stereo-'))

    def molecule(self, smiles):
        mol = self.Chem.AddHs(self.Chem.MolFromSmiles(smiles))
        self.assertEqual(self.AllChem.EmbedMolecule(mol, randomSeed=17), 0)
        return mol

    def block(self, molecule, declaration=None):
        return self.Chem.MolToMolBlock(molecule) + annotation(declaration or molecule) + '$$$$\n'

    def test_carbon_priority_r_s_and_e_z_are_independent_of_transport_maps(self):
        for smiles in ('CC[C@H](F)C', 'CC[C@@H](F)C', 'CC/C=C(/CC)C', 'CC/C=C(\\CC)C', 'CC[C@H](F)/C=C(/CC)C'):
            with self.subTest(smiles=smiles):
                original = self.molecule(smiles)
                text = self.block(original)
                checked = self.Chem.MolFromMolBlock(text, removeHs=False)
                atoms, bonds = declared(text, checked)
                self.assertEqual((len(atoms), len(bonds)), (int('@' in smiles), int('/' in smiles)))
                path = self.root / 'bound.sdf'
                path.write_text(text)
                for policy in ('preserve', 'add_missing'):
                    ligand = _apply_hydrogen_policy(read_ligand(path), policy)
                    output = self.Chem.MolFromMolBlock(ligand.sdf, removeHs=False)
                    self.assertEqual(self.Chem.MolToSmiles(self.Chem.RemoveHs(output)),
                                     self.Chem.MolToSmiles(self.Chem.RemoveHs(original)))

    def test_validation_does_not_mutate_actual_maps_tags_or_coordinates(self):
        mol = self.molecule('CC[C@H](F)C')
        text = self.block(mol)
        checked = self.Chem.MolFromMolBlock(text, removeHs=False)
        for atom in checked.GetAtoms():
            atom.SetAtomMapNum(900 - atom.GetIdx())
        before = self.Chem.MolToMolBlock(checked)
        coordinates = [tuple(checked.GetConformer().GetAtomPosition(i)) for i in range(checked.GetNumAtoms())]
        atoms, _ = declared(text, checked)
        self.assertEqual(len(atoms), 1)
        self.assertEqual(self.Chem.MolToMolBlock(checked), before)
        self.assertEqual([tuple(checked.GetConformer().GetAtomPosition(i)) for i in range(checked.GetNumAtoms())], coordinates)

    def test_real_inverted_chirality_and_double_bond_still_fail(self):
        for smiles in ('CC[C@H](F)C', 'CC/C=C(/CC)C'):
            original = self.molecule(smiles)
            wrong = self.Chem.Mol(original)
            if '@' in smiles:
                next(a for a in wrong.GetAtoms() if a.GetChiralTag() != self.Chem.ChiralType.CHI_UNSPECIFIED).InvertChirality()
            else:
                bond = next(b for b in wrong.GetBonds() if b.GetStereo() != self.Chem.BondStereo.STEREONONE)
                bond.SetStereo(self.Chem.BondStereo.STEREOZ if bond.GetStereo() == self.Chem.BondStereo.STEREOE else self.Chem.BondStereo.STEREOE)
                self.Chem.SetDoubleBondNeighborDirections(wrong)
            self.Chem.AssignStereochemistry(wrong, cleanIt=True, force=True)
            text = self.block(original, wrong)
            checked = self.Chem.MolFromMolBlock(text, removeHs=False)
            with self.assertRaises(ToolError) as error:
                declared(text, checked)
            self.assertEqual(error.exception.key, 'input.ligand_declared_stereo')

    def test_map_only_pseudo_stereo_is_not_silently_accepted(self):
        for smiles, declaration in (
                ('ClC(C)C', '[Cl:1][C@H:2]([CH3:3])[CH3:4]'),
                ('CC=C(C)C', '[CH3:1]/[CH:2]=[C:3](/[CH3:4])[CH3:5]')):
            mol = self.Chem.RemoveHs(self.molecule(smiles))
            text = '\n> <' + PROPERTY + '>\n' + declaration + '\n\n'
            with self.assertRaises(ToolError) as error:
                declared(text, mol)
            self.assertEqual(error.exception.key, 'input.ligand_declared_stereo')

    def test_map_correspondence_graph_and_hydrogen_guards_remain_strict(self):
        mol = self.molecule('CC[C@H](F)C')
        text = self.block(mol)
        checked = self.Chem.MolFromMolBlock(text, removeHs=False)
        declaration = annotation(mol)
        for wrong in (declaration.replace(':2]', ':1]'), declaration.replace('[F:', '[Cl:'),
                      declaration.replace('[H:', '[2H:', 1)):
            with self.assertRaises(ToolError):
                declared(self.Chem.MolToMolBlock(mol) + wrong, checked)
