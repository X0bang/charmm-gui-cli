"""Real RDKit/Meeko conversion, public CLI paths and chemical counterexamples."""

import hashlib
import importlib.util
import json
import shlex
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli.chem.inputs import read_ligand, _apply_hydrogen_policy
from charmm_gui_cli.chem.pdbqt import _rows
from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.store.paths import read_value
import test_input_policy as fixtures

MEEKO = importlib.util.find_spec('meeko') is not None


def row(serial, name, atom_type, xyz, charge=0.):
    return (f'ATOM  {serial:5d} {name:>4} LIG A   1    ' + ''.join(f'{value:8.3f}' for value in xyz)
            + f'  1.00  0.00    {charge:+6.3f} {atom_type:<2}\n')


def ethanol(*, metadata=True, conect=False, shift=0.):
    text = ('REMARK SMILES CCO\nREMARK SMILES IDX 1 1 2 2 3 3\nREMARK H PARENT 3 4\n' if metadata else '')
    text += 'ROOT\n'
    for serial, name, kind, xyz, charge in ((1, 'C1', 'C', (0., 4., 0.), .1), (2, 'C2', 'C', (1.4, 4., 0.), .2),
            (3, 'O1', 'OA', (2., 5.2, 0.), -.5), (4, 'H1', 'HD', (2.8, 5.2, 0.), .2)):
        text += row(serial, name, kind, (xyz[0] + shift, *xyz[1:]), charge)
    text += 'ENDROOT\nTORSDOF 0\n'
    if conect:
        text += 'CONECT    1    2\nCONECT    2    3\nCONECT    3    4\n'
    return text


class PdbqtInputTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.InputPolicyTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.root
        self.path = self.root / 'bound pose.pdbqt'
        self.f.ligand = self.path

    def write(self, **kwargs):
        self.path.write_text(ethanol(**kwargs))
        return self.path

    def assert_coordinates(self, source, ligand):
        rows, _, _ = _rows(source)
        conversion = ligand.provenance['conversion']
        for entry in conversion['atom_name_mapping']:
            atom = ligand.atoms[entry['index'] - 1]
            self.assertEqual(atom.xyz, rows[entry['source_serial']]['xyz'])
        self.assertTrue(conversion['input_atoms_retained'])
        self.assertEqual(conversion['added_coordinate_atoms'], 0)

    def test_user_serial_mapped_smiles_works_without_meeko_and_retains_existing_h(self):
        self.write(metadata=False)
        with patch.dict(sys.modules, {'meeko': None}):
            ligand = read_ligand(self.path, smiles='[CH3:1][CH2:2][O:3][H:4]')
        self.assertEqual(len(ligand.atoms), 4)
        self.assert_coordinates(self.path.read_text(), ligand)
        self.assertEqual(len(_apply_hydrogen_policy(ligand, 'preserve').atoms), 4)
        self.assertEqual(len(_apply_hydrogen_policy(ligand, 'add_missing').atoms), 9)
        self.assertEqual(ligand.provenance['conversion']['net_formal_charge'], 0)

    def test_plain_smiles_needs_complete_unique_conect_not_plausible_distances(self):
        self.write(metadata=False)
        with self.assertRaises(ToolError) as error:
            read_ligand(self.path, smiles='CCO')
        self.assertEqual(error.exception.key, 'input.pdbqt_mapping')
        self.write(metadata=False, conect=True)
        ligand = read_ligand(self.path, smiles='CCO')
        self.assert_coordinates(self.path.read_text(), ligand)
        self.assertEqual(ligand.provenance['conversion']['chemical_definition'], 'user_smiles_unique_conect')

    def test_pose_error_preserves_safe_explicit_smiles_in_executable_next_command(self):
        self.path.write_text('MODEL 1\n' + ethanol(metadata=False) + 'ENDMDL\nMODEL 2\n' + ethanol(metadata=False, shift=.3) + 'ENDMDL\n')
        code, report, _, _ = self.f.check('--ligand-smiles', '[CH3:1][CH2:2][O:3][H:4]')
        self.assertEqual(code, 2)
        args = shlex.split(report['error']['next_step'])
        self.assertIn('--ligand-smiles', args)
        code, output, errors = self.f.call([*args[1:], '--out', str(self.root / 'chosen-pose'), '--no-preview', '--json'])
        self.assertEqual(code, 0, errors)

    def test_ambiguous_symmetric_graph_and_incomplete_or_conflicting_maps_are_refused(self):
        self.write(metadata=False, conect=True)
        for smiles in ('[CH3:1][CH2:1][O:3][H:4]', '[CH3:1][CH2:2][OH:3]', '[CH3:1][CH2:2][N:3][H:4]', 'COC'):
            with self.assertRaises(ToolError):
                read_ligand(self.path, smiles=smiles)
        self.path.write_text('ROOT\n' + row(1, 'C1', 'C', (0., 4., 0.)) + row(2, 'C2', 'C', (1.5, 4., 0.)) +
                             'ENDROOT\nCONECT    1    2\n')
        with self.assertRaises(ToolError):
            read_ligand(self.path, smiles='CC')

    @unittest.skipUnless(MEEKO, 'Meeko chemistry extra is not installed')
    def test_real_meeko_export_keeps_original_atoms_partial_charges_and_bilingual_cli_repairs(self):
        original = self.write().read_bytes()
        for language, expected in (('en', 'PDBQT: restored 4'), ('zh', 'PDBQT：依据')):
            code, report, output, _ = self.f.check('--hydrogens', 'preserve', lang=language, tty=True)
            self.assertEqual(code, 0, report.get('error'))
            self.assertIn(expected, output)
            conversion = report['sources']['ligand_selection']['conversion']
            self.assertEqual(conversion['input_partial_charges'], [.1, .2, -.5, .2])
            self.assertEqual(conversion['net_formal_charge'], 0)
            self.assertTrue(conversion['graph_and_stereo_roundtrip_verified'])
            self.assertEqual(report['sources']['source_evidence']['ligand']['sha256'], hashlib.sha256(original).hexdigest())
            self.assertFalse(report['server_submitted'])
            self.assertEqual(self.path.read_bytes(), original)
        self.assert_coordinates(self.path.read_text(), read_ligand(self.path))

    @unittest.skipUnless(MEEKO, 'Meeko chemistry extra is not installed')
    def test_explicit_pose_selection_is_not_score_selection_and_scores_are_reported(self):
        first, second = ethanol(), ethanol(shift=.3)
        self.path.write_text('MODEL 1\nREMARK VINA RESULT: -9.5 0 0\n' + first + 'ENDMDL\nMODEL 2\nREMARK VINA RESULT: -4.0 0 0\n' + second + 'ENDMDL\n')
        original = self.path.read_bytes()
        for lang in ('zh', 'en'):
            code, report, output, _ = self.f.check(tty=True, lang=lang)
            self.assertEqual(code, 2)
            self.assertIn('-9.5', output)
            self.assertIn('--ligand-pose 1', report['error']['next_step'])
        selected = read_ligand(self.path, pose=2)
        self.assertEqual(selected.provenance['selected_pose'], 2)
        self.assertEqual(selected.atoms[0].xyz[0], .3)
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipUnless(MEEKO, 'Meeko chemistry extra is not installed')
    def test_chiral_ez_aromatic_and_charged_chemistry_survive_sdf_staging_and_both_h_policies(self):
        from rdkit import Chem
        from rdkit.Chem import AllChem
        from meeko import MoleculePreparation, PDBQTWriterLegacy
        for index, smiles in enumerate(('F[C@H](Cl)Br', 'F/C=C/Cl', 'F/C=C\\Cl', 'c1ccccc1', 'C[NH3+]')):
            original = Chem.AddHs(Chem.MolFromSmiles(smiles))
            self.assertEqual(AllChem.EmbedMolecule(original, randomSeed=7), 0)
            block, ok, _ = PDBQTWriterLegacy.write_string(MoleculePreparation().prepare(original)[0])
            self.assertTrue(ok)
            self.path.write_text(block)
            ligand = read_ligand(self.path)
            self.assert_coordinates(block, ligand)
            for policy in ('preserve', 'add_missing'):
                result = _apply_hydrogen_policy(ligand, policy)
                staged = self.root / (str(index) + '-' + policy + '.sdf')
                staged.write_text(result.sdf)
                reread = read_ligand(staged)
                checked = Chem.MolFromMolBlock(reread.sdf, removeHs=False)
                self.assertEqual(Chem.MolToSmiles(Chem.RemoveHs(checked)), Chem.MolToSmiles(Chem.RemoveHs(original)))
                self.assertFalse(any(a.GetNumRadicalElectrons() for a in checked.GetAtoms()))

    @unittest.skipUnless(MEEKO, 'Meeko chemistry extra is not installed')
    def test_bad_metadata_and_conflicting_definition_are_never_used(self):
        valid = ethanol()
        for block in (valid.replace('1 1 2 2 3 3', '1 1 2 1 3 3'), valid.replace('REMARK H PARENT 3 4\n', ''),
                      valid.replace('H PARENT 3 4', 'H PARENT 1 4'), valid + 'BRANCH 1 3\nENDBRANCH 1 3\n'):
            self.path.write_text(block)
            with self.assertRaises(ToolError):
                read_ligand(self.path)
        self.write()
        with self.assertRaises(ToolError) as error:
            read_ligand(self.path, smiles='CC[O-]')
        self.assertEqual(error.exception.key, 'input.pdbqt_conflict')

    def test_missing_dependency_is_actionable_and_parser_never_echoes_file_text(self):
        self.write()
        with patch.dict(sys.modules, {'meeko': None}):
            code, report, _, _ = self.f.check()
        self.assertEqual(code, 2)
        self.assertIn('-m pip install', report['error']['next_step'])
        self.path.write_text('REMARK password=PRIVATE_VALUE\nROOT\nENDROOT\n')
        code, report, output, errors = self.f.check(tty=True)
        self.assertEqual(code, 2)
        self.assertNotIn('PRIVATE_VALUE', json.dumps(report) + output + errors)

    def test_unsupported_types_flexres_invalid_serials_and_nonfinite_charges_are_refused(self):
        for block in (ethanol().replace(' OA', ' Zn'), ethanol() + 'BEGIN_RES LYS A 1\n',
                      ethanol().replace('ATOM      2', 'ATOM      1'), ethanol().replace('+0.100', '   nan')):
            self.path.write_text(block)
            with self.assertRaises(ToolError):
                read_ligand(self.path)

    def test_smiles_names_and_extraneous_definitions_are_not_silently_accepted(self):
        self.write(metadata=False)
        for smiles in ('CCO password=PRIVATE_VALUE', 'CCO |$secret$|', '[13CH3:1][CH2:2][O:3][H:4]', '[CH2:1][CH2:2][O:3][H:4]'):
            with self.assertRaises(ToolError) as error:
                read_ligand(self.path, smiles=smiles)
            self.assertNotIn('PRIVATE_VALUE', str(error.exception))
        sdf = self.root / 'ordinary.sdf'
        ligand = read_ligand(self.path, smiles='[CH3:1][CH2:2][O:3][H:4]')
        sdf.write_text(ligand.sdf)
        with self.assertRaises(ToolError):
            read_ligand(sdf, smiles='CC[O-]')

    @unittest.skipUnless(MEEKO, 'Meeko chemistry extra is not installed')
    def test_tampered_sdf_stereo_declaration_and_reversed_pose_are_rejected(self):
        from rdkit import Chem
        from rdkit.Chem import AllChem
        from meeko import MoleculePreparation, PDBQTWriterLegacy
        molecule = Chem.AddHs(Chem.MolFromSmiles('F/C=C/Cl'))
        self.assertEqual(AllChem.EmbedMolecule(molecule, randomSeed=7), 0)
        block, ok, _ = PDBQTWriterLegacy.write_string(MoleculePreparation().prepare(molecule)[0])
        self.assertTrue(ok)
        self.path.write_text(block)
        ligand = read_ligand(self.path)
        sdf = self.root / 'changed-stereo.sdf'
        sdf.write_text(ligand.sdf.replace('/[Cl:4]', '\\[Cl:4]'))
        self.assertNotEqual(sdf.read_text(), ligand.sdf)
        with self.assertRaises(ToolError) as error:
            read_ligand(sdf)
        self.assertEqual(error.exception.key, 'input.ligand_declared_stereo')
        import numpy as np
        rows, _, _ = _rows(block)
        carbon = [np.array(atom['xyz']) for atom in rows.values() if atom['element'] == 'C']
        fluorine = next(atom for atom in rows.values() if atom['element'] == 'F')
        axis = carbon[1] - carbon[0]
        offset = np.array(fluorine['xyz']) - carbon[0]
        projected = carbon[0] + axis * float(np.dot(offset, axis) / np.dot(axis, axis))
        reflected = 2 * projected - np.array(fluorine['xyz'])
        flipped = []
        for line in block.splitlines():
            if line.startswith(('ATOM  ', 'HETATM')) and int(line[6:11]) == fluorine['serial']:
                line = line[:30] + ''.join(f'{value:8.3f}' for value in reflected) + line[54:]
            flipped.append(line)
        self.path.write_text('\n'.join(flipped) + '\n')
        with self.assertRaises(ToolError):
            read_ligand(self.path)
        molecule = Chem.AddHs(Chem.MolFromSmiles('F[C@H](Cl)Br'))
        self.assertEqual(AllChem.EmbedMolecule(molecule, randomSeed=7), 0)
        block, ok, _ = PDBQTWriterLegacy.write_string(MoleculePreparation().prepare(molecule)[0])
        self.assertTrue(ok)
        mirrored = [line[:30] + f'{-float(line[30:38]):8.3f}' + line[38:] if line.startswith(('ATOM  ', 'HETATM')) else line
                    for line in block.splitlines()]
        self.path.write_text('\n'.join(mirrored) + '\n')
        with self.assertRaises(ToolError):
            read_ligand(self.path)
        flat = 'REMARK SMILES F[C@H](Cl)Br\nREMARK SMILES IDX 1 1 2 2 3 3 4 4\nROOT\n'
        for serial, symbol, xyz in ((1, 'F', (1., 4., 0.)), (2, 'C', (0., 4., 0.)),
                                   (3, 'Cl', (-1., 5., 0.)), (4, 'Br', (-1., 3., 0.))):
            flat += row(serial, symbol + str(serial), symbol, xyz)
        self.path.write_text(flat + 'ENDROOT\nTORSDOF 0\n')
        with self.assertRaises(ToolError) as error:
            read_ligand(self.path)
        self.assertEqual(error.exception.key, 'input.pdbqt_stereo_geometry')
        self.assertEqual(error.exception.params['serial'], 2)

    @unittest.skipUnless(MEEKO, 'Meeko chemistry extra is not installed')
    def test_cli_yaml_batch_sweep_prepare_same_pose_and_manual_smiles_is_supported_in_yaml(self):
        self.write()
        inputs = {'protein': str(self.f.protein), 'ligand': str(self.path), 'hydrogens': 'preserve'}
        hashes = []
        for command in ('direct', 'yaml', 'batch', 'sweep', 'manual-yaml'):
            out = self.root / command
            if command == 'direct':
                args = ['build', '--protein', str(self.f.protein), '--ligand', str(self.path), '--hydrogens', 'preserve']
            else:
                raw = ({'version': 2, 'protein': str(self.f.protein), 'ligand': str(self.path), 'ligand_hydrogens': 'preserve',
                        'membrane': {'upper': 'POPC=1', 'lower': 'POPC=1', 'margin_A': 20}} if 'yaml' in command else
                       {'version': 1, 'defaults': inputs, 'jobs': [{'name': 'a'}]} if command == 'batch' else
                       {'version': 1, 'inputs': inputs, 'axes': {'salt_concentration': [.1, .2]}})
                if command == 'manual-yaml':
                    self.write(metadata=False)
                    raw['ligand_smiles'] = '[CH3:1][CH2:2][O:3][H:4]'
                config = self.root / (command + '.yaml')
                config.write_text(yaml.safe_dump(raw))
                args = ['build' if 'yaml' in command else command, str(config)]
            code, output, errors = self.f.call([*args, '--out', str(out), '--dry-run', '--no-preview', '--json'])
            self.assertEqual(code, 0, errors)
            if command in ('batch', 'sweep'):
                paths = list(out.rglob('full-run.json'))
                self.assertEqual(len(paths), 1 if command == 'batch' else 2)
                directory = paths[0].parent
                manifest = read_value(directory / 'full-run.json')
            else:
                manifest = read_value(Path(json.loads(output)['directory']) / 'full-run.json')
            sdf = Path(manifest['inputs']['files']['ligand.sdf'])
            hashes.append(hashlib.sha256(sdf.read_bytes()).hexdigest())
            self.assertEqual(len(read_ligand(sdf).atoms), 4)
        self.assertEqual(len(set(hashes)), 1)


if __name__ == '__main__':
    unittest.main()
