"""Input tests retain scratch outputs because this work session forbids deletion."""

import json
import importlib.util
from pathlib import Path
import tempfile
import unittest

from charmm_gui_cli.auth import ToolError
from charmm_gui_cli.inputs import (Atom, Ligand, _pdb, _sdf, _validate_ligand,
                                   apply_hydrogen_policy, inspect_ligand, prepare_inputs, read_ligand, split_complex)


DATASET = Path(__file__).resolve().parents[1] / "test-dataset/CrtW_BetaCarotene_af3_fixed.pdb"


class InputsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="charmm-input-test-"))

    def split(self):
        if not DATASET.is_file():
            self.skipTest("Private integration fixture is not distributed")
        return split_complex(DATASET, self.root / "split", accept_conect_bond_orders=True)

    def sdf_file(self, atoms, bonds):
        path = self.root / "input.sdf"
        path.write_text(_sdf(atoms, bonds, "LIG"))
        return path

    def test_explicit_conect_acknowledgement_required(self):
        with self.assertRaisesRegex(ToolError, "explicitly"):
            split_complex(DATASET, self.root / "split")
        self.assertFalse((self.root / "split").exists())

    @unittest.skipUnless(DATASET.is_file(), "Private integration fixture is not distributed")
    def test_split_preserves_graph_coordinates_and_names(self):
        original = DATASET.read_bytes()
        result = self.split()
        ligand = read_ligand(result["files"]["ligand.sdf"])
        source, _ = _pdb(DATASET)
        selected = [a for a in source if a.line[17:21].strip() == "LIG"]
        self.assertEqual([a.xyz for a in ligand.atoms], [a.xyz for a in selected])
        self.assertEqual([a.name for a in ligand.atoms], [a.name for a in selected])
        self.assertEqual(len(ligand.atoms), 40)
        self.assertEqual(result["ligand"]["bond_orders"], {"1": 30, "2": 11})
        self.assertNotIn("HETATM", Path(result["files"]["protein.pdb"]).read_text())
        self.assertEqual(DATASET.read_bytes(), original)

    def test_merge_and_mapping_round_trip(self):
        split = self.split()["files"]
        result = prepare_inputs(split["protein.pdb"], split["ligand.sdf"], self.root / "merged")
        atoms, bonds = _pdb(result["files"]["complex.pdb"])
        ligand = [a for a in atoms if a.line.startswith("HETATM")]
        names = [a.name for a in read_ligand(split["ligand.sdf"]).atoms]
        self.assertEqual(len(atoms), 2584)
        self.assertEqual([a.name for a in ligand], names)
        self.assertEqual(len({a.serial for a in atoms}), 2584)
        self.assertTrue(all(a.line[21] == "L" and int(a.line[22:26]) == 1 for a in ligand))
        self.assertAlmostEqual(result["manifest"]["ligand"]["minimum_protein_ligand_heavy_distance_A"], 2.75267, places=4)
        for atom, mapping in zip(ligand, result["manifest"]["ligand"]["atom_mapping"]):
            self.assertEqual(atom.serial, mapping["pdb_serial"])
            self.assertEqual(list(atom.xyz), mapping["input_coordinates_A"])
        self.assertEqual(json.loads(Path(result["files"]["input-manifest.json"]).read_text())["coordinate_transform"], "none")

    def test_no_output_overwrite(self):
        self.split()
        output = self.root / "split/protein.pdb"
        original = output.read_bytes()
        with self.assertRaisesRegex(ToolError, "overwrite"):
            self.split()
        self.assertEqual(output.read_bytes(), original)

    def test_charge_roundtrip(self):
        atoms = [Atom(1, "N1", "N", (1., 2., 3.), 1)]
        path = self.sdf_file(atoms, {})
        self.assertEqual(read_ligand(path).atoms[0].charge, 1)
        self.assertEqual(inspect_ligand(path)["formal_charge"], 1)

    def test_disconnected_ligand_rejected(self):
        path = self.sdf_file([Atom(1, "C1", "C", (1., 2., 3.)), Atom(2, "C2", "C", (2., 3., 4.))], {})
        with self.assertRaisesRegex(ToolError, "no explicit bonds"):
            read_ligand(path)

    def test_bad_valence_rejected(self):
        atoms = [Atom(i + 1, f"C{i + 1}", "C", (float(i), 0., 0.)) for i in range(3)]
        path = self.sdf_file(atoms, {(0, 1): 3, (0, 2): 2})
        with self.assertRaisesRegex(ToolError, "valence"):
            read_ligand(path)

    def test_duplicate_atom_names_rejected(self):
        atoms = [Atom(1, "C1", "C", (1., 2., 3.)), Atom(2, "C1", "C", (2., 3., 4.))]
        with self.assertRaisesRegex(ToolError, "unique"):
            read_ligand(self.sdf_file(atoms, {(0, 1): 1}))

    def test_multiple_molecules_rejected(self):
        path = self.sdf_file([Atom(1, "C1", "C", (1., 2., 3.))], {})
        path.write_text(path.read_text() * 2)
        with self.assertRaisesRegex(ToolError, "exactly one"):
            read_ligand(path)

    def test_nonfinite_coordinate_rejected(self):
        path = self.sdf_file([Atom(1, "C1", "C", (1., 2., 3.))], {})
        path.write_text(path.read_text().replace("    1.0000", "       nan"))
        with self.assertRaisesRegex(ToolError, "finite"):
            read_ligand(path)

    def test_protein_must_not_include_ligand(self):
        split = self.split()["files"]
        with self.assertRaisesRegex(ToolError, "HETATM"):
            prepare_inputs(DATASET, split["ligand.sdf"], self.root / "merged")

    def test_distant_pose_rejected(self):
        split = self.split()["files"]
        mol = read_ligand(split["ligand.sdf"])
        for atom in mol.atoms:
            atom.xyz = tuple(v + 1000 for v in atom.xyz)
        path = self.sdf_file(mol.atoms, mol.bonds)
        with self.assertRaisesRegex(ToolError, ">6 A"):
            prepare_inputs(split["protein.pdb"], path, self.root / "merged")

    def test_overlap_rejected(self):
        split = self.split()["files"]
        protein, _ = _pdb(split["protein.pdb"])
        path = self.sdf_file([Atom(1, "C1", "C", protein[0].xyz)], {})
        with self.assertRaisesRegex(ToolError, "overlap"):
            prepare_inputs(split["protein.pdb"], path, self.root / "merged")

    def test_chain_collision_rejected(self):
        split = self.split()["files"]
        with self.assertRaisesRegex(ToolError, "unused"):
            prepare_inputs(split["protein.pdb"], split["ligand.sdf"], self.root / "merged", ligand_chain="A")

    @unittest.skipUnless(DATASET.is_file(), "Private integration fixture is not distributed")
    def test_covalent_ligand_rejected(self):
        text = DATASET.read_text().replace("END\n", "CONECT 2545    1\nEND\n")
        path = self.root / "covalent.pdb"
        path.write_text(text)
        with self.assertRaisesRegex(ToolError, "Covalently"):
            split_complex(path, self.root / "split", accept_conect_bond_orders=True)

    @unittest.skipUnless(DATASET.is_file(), "Private integration fixture is not distributed")
    def test_multi_model_rejected(self):
        path = self.root / "models.pdb"
        path.write_text("MODEL        1\nMODEL        2\n" + DATASET.read_text())
        with self.assertRaisesRegex(ToolError, "one PDB model"):
            split_complex(path, self.root / "split", accept_conect_bond_orders=True)

    def test_hydrogen_preserve_policy_changes_nothing(self):
        split = self.split()["files"]
        result = apply_hydrogen_policy(split["ligand.sdf"], self.root / "hydrogens", policy="preserve")
        self.assertEqual(Path(result["files"]["ligand.sdf"]).read_bytes(), Path(split["ligand.sdf"]).read_bytes())
        self.assertEqual(result["ligand"]["added_atoms"], 0)

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit optional dependency is absent")
    def test_add_missing_hydrogens_preserves_heavy_atoms(self):
        split = self.split()["files"]
        before = read_ligand(split["ligand.sdf"])
        result = apply_hydrogen_policy(split["ligand.sdf"], self.root / "hydrogens", policy="add_missing")
        after = read_ligand(result["files"]["ligand.sdf"])
        self.assertEqual(len(after.atoms), 96)
        self.assertEqual([a.xyz for a in after.atoms[:40]], [a.xyz for a in before.atoms])
        self.assertEqual([a.name for a in after.atoms[:40]], [a.name for a in before.atoms])
        self.assertEqual(result["ligand"]["elements"], {"C": 40, "H": 56})
        second = apply_hydrogen_policy(result["files"]["ligand.sdf"], self.root / "again", policy="add_missing")
        self.assertEqual(second["ligand"]["added_atoms"], 0)

    def test_invalid_hydrogen_policy_rejected(self):
        split = self.split()["files"]
        with self.assertRaisesRegex(ToolError, "hydrogen_policy"):
            apply_hydrogen_policy(split["ligand.sdf"], self.root / "hydrogens", policy="guess_ph")


if __name__ == "__main__":
    unittest.main()
