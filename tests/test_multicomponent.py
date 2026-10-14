import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.chem.inputs import Atom, _conect, _sdf, read_ligand
from charmm_gui_cli.chem.multicomponent import load_components_config, prepare_build_components, prepare_multicomponent, validate_components


def pdb_atom(serial, name, element, resname, chain, resid, xyz, record="HETATM", icode=" "):
    return f"{record:<6}{serial:5d} {name:>4} {resname:>3} {chain:1}{resid:4d}{icode}   {xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00  0.00          {element:>2}"


class MulticomponentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="charmm-multicomponent-")).resolve()
        self.source = self.root / "bound.pdb"
        self.sdf = self.root / "chemical.sdf"
        self.sdf.write_text(_sdf([Atom(1, "C1", "C", (99, 99, 99)), Atom(2, "O1", "O", (100, 99, 99))], {(0, 1): 2}, "LIG"))
        self.lines = [pdb_atom(1, "CA", "C", "ALA", "A", 1, (0, 0, 0), "ATOM"),
                      pdb_atom(2, "CA", "C", "GLY", "A", 2, (2, 0, 0), "ATOM"),
                      pdb_atom(3, "CA", "C", "SER", "A", 3, (0, 2, 0), "ATOM")]
        self.instances = []
        self.add_ligand("LIG", "B", 1, 10)
        self.add_ligand("COF", "C", 2, 20)
        self.write_source()

    def add_ligand(self, resname, chain, resid, serial, icode=" "):
        self.lines += [pdb_atom(serial, "C1", "C", resname, chain, resid, (serial, 1, 2), icode=icode),
                       pdb_atom(serial + 1, "O1", "O", resname, chain, resid, (serial + 1.2, 1, 2), icode=icode)]
        self.instances.append({"resname": resname, "chain": chain, "resid": resid, "sdf": str(self.sdf)})

    def write_source(self, extra=""):
        self.source.write_text("\n".join(self.lines) + "\n" + extra + "END\n")

    def prepare(self, **kwargs):
        return prepare_multicomponent(complex_pdb=self.source, components=self.instances,
                                      out=self.root / "output", water_policy=kwargs.pop("water_policy", "reject"),
                                      ion_policy=kwargs.pop("ion_policy", "reject"), **kwargs)

    def test_distinct_components_keep_bound_coordinates_and_all_atoms(self):
        original = self.source.read_bytes()
        result = self.prepare()
        self.assertEqual(Path(result["complex"]).read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), original)
        manifest = result["report"]
        self.assertEqual(manifest["discarded_atoms"], 0)
        self.assertTrue(manifest["remote_validation_supported"])
        self.assertEqual(len(manifest["chemistry_groups"]), 2)
        molecule = read_ligand(manifest["components"][0]["sdf"])
        self.assertEqual(molecule.atoms[0].xyz, (10, 1, 2))
        self.assertNotIn("HETATM", Path(result["protein"]).read_text())
        self.assertEqual(manifest["components"][0]["expected_ligand"]["heavy_atoms"], 2)

    def test_same_resname_copies_share_chemistry_not_coordinates(self):
        self.add_ligand("LIG", "B", 3, 30)
        self.write_source()
        report = self.prepare()["report"]
        group = next(g for g in report["chemistry_groups"] if g["resname"] == "LIG")
        self.assertEqual(len(group["instances"]), 2)
        self.assertFalse(report["remote_validation_supported"])
        self.assertEqual(report["components"][2]["atoms"][0]["xyz_A"], [30, 1, 2])

    def test_same_resname_different_bond_orders_refused(self):
        self.add_ligand("LIG", "B", 3, 30)
        path = self.root / "different.sdf"
        path.write_text(self.sdf.read_text().replace("  1  2  2", "  1  2  1"))
        self.instances[-1]["sdf"] = str(path)
        self.write_source()
        with self.assertRaisesRegex(ToolError, "Same-resname"):
            self.prepare()

    def test_unexplained_hetatm_is_not_dropped(self):
        self.lines.append(pdb_atom(50, "C1", "C", "UNK", "D", 1, (0, 0, 0)))
        self.write_source()
        with self.assertRaisesRegex(ToolError, "Unexplained"):
            self.prepare()

    def test_cross_component_conect_is_refused(self):
        self.write_source(_conect({(10, 20): 1}))
        with self.assertRaisesRegex(ToolError, "crosses"):
            self.prepare()

    def test_link_records_are_not_ignored(self):
        self.write_source("LINK        C1   LIG B   1                 CA  ALA A   1\n")
        with self.assertRaisesRegex(ToolError, "LINK"):
            self.prepare()

    def test_malformed_or_unmapped_disulfide_record_is_not_ignored(self):
        self.write_source("SSBOND   1 CYS A    1    CYS A    2\n")
        with self.assertRaisesRegex(ToolError, "SSBOND"):
            self.prepare()

    def test_water_requires_explicit_policy_and_is_retained(self):
        self.lines.append(pdb_atom(50, "O", "O", "HOH", "W", 1, (0, 0, 5)))
        self.write_source()
        with self.assertRaisesRegex(ToolError, "water_policy"):
            self.prepare()
        report = self.prepare(water_policy="retain")["report"]
        self.assertEqual(len(report["retained_waters"]), 1)
        self.assertEqual(report["source_atoms"], report["output_complex_atoms"])
        self.assertFalse(report["remote_validation_supported"])

    def test_spectator_ion_requires_explicit_policy(self):
        self.lines.append(pdb_atom(50, "NA", "Na", "NA", "I", 1, (0, 0, 8)))
        self.write_source()
        with self.assertRaisesRegex(ToolError, "ion_policy"):
            self.prepare()
        report = self.prepare(ion_policy="retain_noncoordinating")["report"]
        self.assertEqual(report["retained_ions"][0]["expected_formal_charge"], 1)

    def test_unknown_metal_not_treated_as_spectator(self):
        self.lines.append(pdb_atom(50, "ZN", "Zn", "ZN", "I", 1, (0, 0, 8)))
        self.write_source()
        with self.assertRaisesRegex(ToolError, "Unexplained"):
            self.prepare(ion_policy="retain_noncoordinating")

    def test_selector_ambiguity_requires_insertion_code(self):
        self.add_ligand("LIG", "B", 1, 30, icode="A")
        self.write_source()
        with self.assertRaisesRegex(ToolError, "exactly one"):
            self.prepare()

    def test_duplicate_component_rejected(self):
        self.instances.append(dict(self.instances[0]))
        with self.assertRaisesRegex(ToolError, "more than once"):
            self.prepare()

    def test_explicit_conect_chemistry_supported(self):
        for instance in self.instances:
            instance.pop("sdf")
            instance["accept_conect_bond_orders"] = True
        self.write_source(_conect({(10, 11): 2, (20, 21): 2}))
        report = self.prepare()["report"]
        self.assertEqual(report["components"][0]["chemical_graph"]["bonds"], [("C1", "O1", 2)])

    def test_chemical_source_is_required(self):
        self.instances[0].pop("sdf")
        with self.assertRaisesRegex(ToolError, "chemical orders are not guessed"):
            self.prepare()

    def test_named_sdf_mapping_required(self):
        self.sdf.write_text(self.sdf.read_text().replace("PDB_ATOM_NAMES", "UNRELATED_PROPERTY"))
        with self.assertRaisesRegex(ToolError, "PDB_ATOM_NAMES"):
            self.prepare()

    def test_trusted_topology_sidecar_atom_graph_and_charge_checked(self):
        rtf, prm = self.root / "lig.rtf", self.root / "lig.prm"
        rtf.write_text("* synthetic\n36 1\nRESI LIG 0.0\nGROUP\nATOM C1 CG 0.2\nATOM O1 OG -0.2\nDOUBLE C1 O1\nPATCHING FIRST NONE LAST NONE\nEND\n")
        prm.write_text("* trusted parameter fixture\nBONDS\nCG OG 300.0 1.2\nEND\n")
        self.instances[0]["topology"] = {"rtf": str(rtf), "prm": str(prm), "trusted": True}
        report = self.prepare()["report"]
        self.assertEqual(report["components"][0]["mode"], "trusted_charmm")
        self.assertFalse(report["components"][0]["parameter_coverage_verified"])

    def test_trusted_topology_bad_charge_rejected(self):
        rtf, prm = self.root / "lig.rtf", self.root / "lig.prm"
        rtf.write_text("RESI LIG 1.0\nATOM C1 CG 0.5\nATOM O1 OG 0.5\nBOND C1 O1\nEND\n")
        prm.write_text("BONDS\nCG OG 300 1.2\nEND\n")
        self.instances[0]["topology"] = {"rtf": str(rtf), "prm": str(prm), "trusted": True}
        with self.assertRaisesRegex(ToolError, "charge"):
            self.prepare()

    def test_existing_directory_is_never_overwritten(self):
        (self.root / "output").mkdir()
        with self.assertRaisesRegex(ToolError, "new directory"):
            self.prepare()

    def test_aggregate_calls_grompp_once_and_checks_exact_counts(self):
        manifest = self.prepare()["report"]
        validation = {"passed": True, "checks": {"ligand_topology_presence": {"copies": 1}, "ligand_coordinate_retention": {"copies": 1}}}
        pose = {"passed": True, "rotation_row_vector": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "translation_A": [1, 2, 3]}
        with patch("charmm_gui_cli.chem.validation.validate_system", return_value=validation) as check, patch("charmm_gui_cli.chem.pose_validation.validate_bound_pose", return_value=pose):
            result = validate_components(self.root, manifest, self.source, run_grompp=True)
        self.assertTrue(result["passed"])
        self.assertEqual([call.kwargs["run_grompp"] for call in check.call_args_list], [True, False])
        validation["checks"]["ligand_topology_presence"]["copies"] = 2
        with patch("charmm_gui_cli.chem.validation.validate_system", return_value=validation), patch("charmm_gui_cli.chem.pose_validation.validate_bound_pose", return_value=pose):
            self.assertFalse(validate_components(self.root, manifest, self.source)["passed"])

    def test_aggregate_refuses_repeated_copy_pose_ambiguity(self):
        self.add_ligand("LIG", "B", 3, 30)
        self.write_source()
        manifest = self.prepare()["report"]
        result = validate_components(self.root, manifest, self.source)
        self.assertFalse(result["passed"])
        self.assertIn("Repeated", result["errors"][0])

    def test_aggregate_rejects_different_protein_transform(self):
        manifest = self.prepare()["report"]
        validation = {"passed": True, "checks": {"ligand_topology_presence": {"copies": 1}, "ligand_coordinate_retention": {"copies": 1}}}
        poses = [{"passed": True, "rotation_row_vector": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "translation_A": [i, 0, 0]} for i in (0, 1)]
        with patch("charmm_gui_cli.chem.validation.validate_system", return_value=validation), patch("charmm_gui_cli.chem.pose_validation.validate_bound_pose", side_effect=poses):
            result = validate_components(self.root, manifest, self.source)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["same_protein_transform"]["passed"])

    def test_real_topology_and_pose_aggregate_without_mocks(self):
        manifest = self.prepare()["report"]
        system = self.root / "system"
        system.mkdir()
        (system / "step5_assembly.pdb").write_bytes(self.source.read_bytes())
        (system / "step5_input.pdb").write_bytes(self.source.read_bytes())
        (system / "topol.top").write_text(
            "[ defaults ]\n1 2 yes 1.0 1.0\n[ atomtypes ]\nC 6 12.011 0 A 0.3 0.2\nO 8 15.999 0 A 0.3 0.2\n"
            "[ moleculetype ]\nPROT 3\n[ atoms ]\n1 C 1 ALA CA 1 0 12.011\n2 C 2 GLY CA 2 0 12.011\n3 C 3 SER CA 3 0 12.011\n"
            "[ moleculetype ]\nLIG 3\n[ atoms ]\n1 C 1 LIG C1 1 0 12.011\n2 O 1 LIG O1 1 0 15.999\n[ bonds ]\n1 2 1 0.12 300\n"
            "[ moleculetype ]\nCOF 3\n[ atoms ]\n1 C 1 COF C1 1 0 12.011\n2 O 1 COF O1 1 0 15.999\n[ bonds ]\n1 2 1 0.12 300\n"
            "[ system ]\nSynthetic noncovalent components\n[ molecules ]\nPROT 1\nLIG 1\nCOF 1\n")
        result = validate_components(system, manifest, self.source)
        self.assertTrue(result["passed"], json.dumps(result, indent=2))
        self.assertEqual(set(result["components"]), {"LIG", "COF"})
        # A ligand moves but the protein remains fixed: no independent fit may hide it.
        changed = []
        for line in self.lines:
            if line[17:20] == "COF":
                line = line[:30] + f"{float(line[30:38]) + 5:8.3f}" + line[38:]
            changed.append(line)
        (system / "step5_assembly.pdb").write_text("\n".join(changed) + "\nEND\n")
        result = validate_components(system, manifest, self.source)
        self.assertFalse(result["passed"])
        self.assertTrue(result["components"]["LIG"]["passed"])
        self.assertFalse(result["components"]["COF"]["bound_pose"]["passed"])

    def config(self):
        path = self.root / "components.yaml"
        # JSON is a YAML subset and keeps the fixture readable without a writer dependency.
        path.write_text(json.dumps({"version": 1, "components": self.instances,
                                    "water_policy": "reject", "ion_policy": "reject"}))
        return path

    def test_build_preparation_preserves_every_component_and_identity(self):
        result = prepare_build_components(components_config=self.config(), complex_pdb=self.source,
                                          out=self.root / "build", hydrogens="preserve", allow_distant_pose=True)
        self.assertEqual(result["manifest"]["output_complex_atoms"], 7)
        self.assertEqual(result["manifest"]["hydrogens_added"], 0)
        from charmm_gui_cli.chem.inputs import _pdb
        atoms, bonds = _pdb(result["files"]["complex.pdb"])
        self.assertEqual([atom.serial for atom in atoms], list(range(1, 8)))
        self.assertEqual([(a.line[21], int(a.line[22:26])) for a in atoms[3:]], [("B", 1), ("B", 1), ("C", 2), ("C", 2)])
        self.assertEqual(bonds, {(4, 5): 2, (6, 7): 2})
        self.assertEqual(atoms[-1].xyz, (21.2, 1, 2))
        self.assertTrue(Path(result["files"]["input-manifest.json"]).is_file())
        self.assertEqual(result["manifest"]["components"][1]["expected_ligand"]["atom_mapping"][0]["pdb_serial"], 6)

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit optional dependency not installed")
    def test_build_adds_hydrogens_to_all_components_without_moving_heavy_atoms(self):
        result = prepare_build_components(components_config=self.config(), complex_pdb=self.source,
                                          out=self.root / "build", hydrogens="add_missing", allow_distant_pose=True)
        self.assertEqual(result["manifest"]["hydrogens_added"], 4)
        from charmm_gui_cli.chem.inputs import _pdb
        atoms, bonds = _pdb(result["files"]["complex.pdb"])
        self.assertEqual(len(atoms), 11)
        heavy = [a for a in atoms if a.element != "H"]
        source, _ = _pdb(self.source)
        self.assertEqual([a.xyz for a in heavy], [a.xyz for a in source])
        for component in result["manifest"]["components"]:
            self.assertEqual(component["hydrogens_added"], 2)
            self.assertEqual(component["expected_ligand"]["heavy_atoms"], 2)
            self.assertEqual(component["expected_ligand"]["atoms"], 4)

    def test_build_repeated_copies_blocked_without_losing_source_outputs(self):
        self.add_ligand("LIG", "B", 3, 30)
        self.write_source()
        with self.assertRaisesRegex(ToolError, "distinct"):
            prepare_build_components(components_config=self.config(), complex_pdb=self.source,
                                     out=self.root / "build", hydrogens="preserve")
        self.assertTrue((self.root / "build/sources/multicomponent-manifest.json").is_file())

    def test_config_paths_resolve_relative_and_unknown_fields_refused(self):
        self.instances[0]["sdf"] = "chemical.sdf"
        config = self.config()
        self.assertEqual(load_components_config(config)["components"][0]["sdf"], str(self.sdf))
        data = json.loads(config.read_text())
        data["drop_unknown_residues"] = True
        config.write_text(json.dumps(data))
        with self.assertRaisesRegex(ToolError, "unknown fields"):
            load_components_config(config)


if __name__ == "__main__":
    unittest.main()
