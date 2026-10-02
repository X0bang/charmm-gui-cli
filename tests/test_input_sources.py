import importlib.util
from pathlib import Path
import tempfile
import unittest

from charmm_gui_cli.auth import ToolError
from charmm_gui_cli.input_sources import prepare_sources
from charmm_gui_cli.inputs import Atom, _conect, _sdf, read_ligand, split_complex


DATASET = Path(__file__).resolve().parents[1] / "test-dataset/CrtW_BetaCarotene_af3_fixed.pdb"
requires_private_fixture = unittest.skipUnless(DATASET.is_file(), "Private integration fixture is not distributed")


class InputSourcesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="charmm-sources-test-"))
        self.protein = self.root / "protein.pdb"
        self.protein.write_text("ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\nEND\n")

    def pdb_ligand(self, elements=("C", "C", "O"), bonds=True):
        lines = []
        for i, element in enumerate(elements, 1):
            name = f"{element}{i}"
            lines.append(f"HETATM{i:5d} {name:>4} LIG L   1    {i * 1.3:8.3f}{2.0:8.3f}{3.0:8.3f}  1.00  0.00          {element:>2}")
        text = "\n".join(lines) + "\n"
        if bonds:
            text += _conect({(i, i + 1): 1 for i in range(1, len(elements))})
        path = self.root / "ligand.pdb"
        path.write_text(text + "END\n")
        return path

    def test_bound_sdf_paths_retained_without_copying(self):
        sdf = self.root / "bound.sdf"
        sdf.write_text(_sdf([Atom(1, "C1", "C", (1., 2., 3.))], {}, "LIG"))
        original = sdf.read_bytes()
        result = prepare_sources(out=self.root / "out", protein=self.protein, ligand=sdf)
        self.assertEqual(result["protein"], str(self.protein))
        self.assertEqual(result["ligand"], str(sdf))
        self.assertEqual(sdf.read_bytes(), original)
        self.assertTrue((self.root / "out/sources.json").is_file())

    @requires_private_fixture
    def test_raw_complex_prepared_without_user_managed_sdf(self):
        result = prepare_sources(out=self.root / "out", complex_pdb=DATASET, accept_conect_bond_orders=True)
        ligand = read_ligand(result["ligand"])
        self.assertEqual(len(ligand.atoms), 40)
        self.assertEqual(len(ligand.bonds), 41)
        self.assertEqual(result["report"]["coordinate_source"], str(DATASET))
        self.assertNotIn("HETATM", Path(result["protein"]).read_text())

    @requires_private_fixture
    def test_pdb_chemical_acknowledgement_required(self):
        with self.assertRaisesRegex(ToolError, "accept-conect"):
            prepare_sources(out=self.root / "out", complex_pdb=DATASET)
        self.assertFalse((self.root / "out").exists())

    def test_ligand_pdb_conect_conversion(self):
        path = self.pdb_ligand()
        result = prepare_sources(out=self.root / "out", protein=self.protein, ligand=path, accept_conect_bond_orders=True)
        ligand = read_ligand(result["ligand"])
        self.assertEqual([a.xyz for a in ligand.atoms], [(1.3, 2., 3.), (2.6, 2., 3.), (3.9, 2., 3.)])

    def test_existing_output_directory_refused(self):
        with self.assertRaisesRegex(ToolError, "new directory"):
            prepare_sources(out=self.root)

    @requires_private_fixture
    def test_all_inputs_must_be_consistent(self):
        with self.assertRaisesRegex(ToolError, "does not match"):
            prepare_sources(out=self.root / "out", protein=self.protein, complex_pdb=DATASET, accept_conect_bond_orders=True)

    @requires_private_fixture
    def test_complex_and_named_sdf_use_complex_coordinates(self):
        split = split_complex(DATASET, self.root / "split", accept_conect_bond_orders=True)
        sdf = Path(split["files"]["ligand.sdf"])
        molecule = read_ligand(sdf)
        original_coords = [a.xyz for a in molecule.atoms]
        for atom in molecule.atoms:
            atom.xyz = tuple(v + 100 for v in atom.xyz)
        independent = self.root / "independent.sdf"
        independent.write_text(_sdf(molecule.atoms, molecule.bonds, "LIG"))
        result = prepare_sources(out=self.root / "out", protein=split["files"]["protein.pdb"],
                                 complex_pdb=DATASET, ligand=independent)
        self.assertEqual([a.xyz for a in read_ligand(result["ligand"]).atoms], original_coords)
        self.assertTrue(result["report"]["supplied_protein_checked_against_complex"])

    @requires_private_fixture
    def test_complex_sdf_without_explicit_mapping_rejected(self):
        split = split_complex(DATASET, self.root / "split", accept_conect_bond_orders=True)
        path = self.root / "unnamed.sdf"
        text = Path(split["files"]["ligand.sdf"]).read_text()
        path.write_text(text.split(">  <PDB_ATOM_NAMES>")[0] + "$$$$\n")
        with self.assertRaisesRegex(ToolError, "explicit PDB_ATOM_NAMES"):
            prepare_sources(out=self.root / "out", complex_pdb=DATASET, ligand=path)

    @requires_private_fixture
    def test_complex_without_conect_accepts_explicitly_named_chemical_sdf(self):
        split = split_complex(DATASET, self.root / "split", accept_conect_bond_orders=True)
        complex_path = self.root / "without-bonds.pdb"
        complex_path.write_text("\n".join(line for line in DATASET.read_text().splitlines() if not line.startswith("CONECT")) + "\n")
        result = prepare_sources(out=self.root / "out", complex_pdb=complex_path, ligand=split["files"]["ligand.sdf"])
        self.assertEqual(len(read_ligand(result["ligand"]).bonds), 41)
        self.assertIn("no independent connectivity evidence", result["report"]["complex_connectivity_check"])

    @requires_private_fixture
    def test_complex_without_conect_accepts_named_ligand_pdb_graph(self):
        split = split_complex(DATASET, self.root / "split", accept_conect_bond_orders=True)
        complex_path = self.root / "without-bonds.pdb"
        complex_path.write_text("\n".join(line for line in DATASET.read_text().splitlines() if not line.startswith("CONECT")) + "\n")
        result = prepare_sources(out=self.root / "out", complex_pdb=complex_path, ligand=split["files"]["ligand.pdb"], accept_conect_bond_orders=True)
        self.assertEqual(len(read_ligand(result["ligand"]).bonds), 41)
        self.assertIn("no CONECT graph", result["report"]["complex_connectivity_check"])

    @requires_private_fixture
    def test_complex_named_sdf_rejects_ambiguous_atom_names(self):
        split = split_complex(DATASET, self.root / "split", accept_conect_bond_orders=True)
        lines = DATASET.read_text().splitlines()
        ligand_indices = [i for i, line in enumerate(lines) if line.startswith("HETATM") and line[17:21].strip() == "LIG"]
        index = ligand_indices[1]
        lines[index] = lines[index][:12] + lines[ligand_indices[0]][12:16] + lines[index][16:]
        path = self.root / "ambiguous.pdb"
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(ToolError, "one-to-one"):
            prepare_sources(out=self.root / "out", complex_pdb=path, ligand=split["files"]["ligand.sdf"])

    @requires_private_fixture
    def test_complex_existing_graph_conflict_is_not_ignored(self):
        split = split_complex(DATASET, self.root / "split", accept_conect_bond_orders=True)
        path = self.root / "wrong-bonds.pdb"
        path.write_text("\n".join(line for line in DATASET.read_text().splitlines() if not line.startswith("CONECT"))
                        + "\nCONECT 2545 2547\nCONECT 2547 2545\n")
        with self.assertRaisesRegex(ToolError, "connectivity must match"):
            prepare_sources(out=self.root / "out", complex_pdb=path, ligand=split["files"]["ligand.sdf"])

    @requires_private_fixture
    def test_complex_explicit_charge_conflict_is_not_ignored(self):
        split = split_complex(DATASET, self.root / "split", accept_conect_bond_orders=True)
        lines = DATASET.read_text().splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("HETATM") and line[17:21].strip() == "LIG")
        lines[index] = lines[index].ljust(80)[:78] + "1+"
        path = self.root / "charged.pdb"
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(ToolError, "charge conflicts"):
            prepare_sources(out=self.root / "out", complex_pdb=path, ligand=split["files"]["ligand.sdf"])

    @requires_private_fixture
    def test_all_three_pdb_inputs_use_complex_pose_and_check_chemistry(self):
        split = split_complex(DATASET, self.root / "split", accept_conect_bond_orders=True)
        original = Path(split["files"]["ligand.pdb"]).read_text().splitlines()
        changed = []
        for line in original:
            if line.startswith(("ATOM  ", "HETATM")):
                xyz = [float(line[i:i + 8]) + 100 for i in (30, 38, 46)]
                line = line[:30] + "".join(f"{v:8.3f}" for v in xyz) + line[54:]
            changed.append(line)
        other = self.root / "independent.pdb"
        other.write_text("\n".join(changed) + "\n")
        result = prepare_sources(out=self.root / "out", protein=split["files"]["protein.pdb"],
                                 ligand=other, complex_pdb=DATASET, accept_conect_bond_orders=True)
        self.assertEqual([a.xyz for a in read_ligand(result["ligand"]).atoms],
                         [a.xyz for a in read_ligand(split["files"]["ligand.sdf"]).atoms])
        self.assertTrue(result["report"]["supplied_ligand_pdb_checked_against_complex"])

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit optional dependency is absent")
    def test_unique_smiles_mapping_assigns_orders_without_moving_atoms(self):
        path = self.pdb_ligand()
        result = prepare_sources(out=self.root / "out", protein=self.protein, ligand=path, ligand_smiles="CC=O")
        ligand = read_ligand(result["ligand"])
        self.assertEqual(ligand.bonds, {(0, 1): 1, (1, 2): 2})
        self.assertEqual([a.xyz for a in ligand.atoms], [(1.3, 2., 3.), (2.6, 2., 3.), (3.9, 2., 3.)])

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit optional dependency is absent")
    def test_symmetric_smiles_mapping_rejected(self):
        path = self.pdb_ligand(elements=("C", "C"))
        with self.assertRaisesRegex(ToolError, "ambiguous"):
            prepare_sources(out=self.root / "out", protein=self.protein, ligand=path, ligand_smiles="CC")

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit optional dependency is absent")
    def test_smiles_without_connectivity_does_not_guess_by_distance(self):
        path = self.pdb_ligand(bonds=False)
        with self.assertRaisesRegex(ToolError, "lacks CONECT"):
            prepare_sources(out=self.root / "out", protein=self.protein, ligand=path, ligand_smiles="CCO")

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit optional dependency is absent")
    def test_isotope_smiles_is_rejected_without_losing_label(self):
        path = self.pdb_ligand()
        with self.assertRaisesRegex(ToolError, "Isotopically"):
            prepare_sources(out=self.root / "out", protein=self.protein, ligand=path, ligand_smiles="[13CH3]CO")
        self.assertFalse((self.root / "out").exists())

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit optional dependency is absent")
    def test_radical_smiles_is_rejected_without_changing_electrons(self):
        path = self.pdb_ligand()
        with self.assertRaisesRegex(ToolError, "Radical"):
            prepare_sources(out=self.root / "out", protein=self.protein, ligand=path, ligand_smiles="[CH2]CO")
        self.assertFalse((self.root / "out").exists())

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit optional dependency is absent")
    def test_dative_bond_is_not_silently_changed_to_single_bond(self):
        path = self.pdb_ligand(elements=("N", "Cu"))
        with self.assertRaisesRegex(ToolError, "unsupported bond type"):
            prepare_sources(out=self.root / "out", protein=self.protein, ligand=path, ligand_smiles="N->[Cu+2]")
        self.assertFalse((self.root / "out").exists())

    def test_multiple_ligand_residues_rejected(self):
        path = self.pdb_ligand()
        text = path.read_text()
        path.write_text(text.replace(" O3 LIG L   1", " O3 LIG L   2"))
        with self.assertRaisesRegex(ToolError, "exactly one"):
            prepare_sources(out=self.root / "out", protein=self.protein, ligand=path, accept_conect_bond_orders=True)


if __name__ == "__main__":
    unittest.main()
