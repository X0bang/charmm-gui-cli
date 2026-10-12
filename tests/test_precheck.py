"""Local preflight fixtures; generated artifacts are retained after each test."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli.auth import ToolError
from charmm_gui_cli.inputs import Atom, _sdf, read_ligand
from charmm_gui_cli.precheck import check_inputs, inspect_protein, inspect_spatial_review


def atom_line(serial, name, resname="ALA", resid=1, chain="A", insertion=" ", altloc=" ",
              xyz=None, record="ATOM", element=None):
    xyz = xyz or (serial * 0.2, 0.0, 0.0)
    element = element or name[0]
    return (f"{record:<6}{serial:5d} {name:>4}{altloc}{resname:>3} {chain}{resid:4d}{insertion}   "
            f"{xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00  0.00          {element:>2}")


class PrecheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="charmm-precheck-")).resolve()
        self.protein, self.ligand = self.root / "protein.pdb", self.root / "ligand.sdf"
        self.protein.write_text("\n".join(atom_line(i, name) for i, name in enumerate(("N", "CA", "C", "O", "CB"), 1)) + "\nEND\n")
        self.ligand.write_text(_sdf([Atom(1, "C1", "C", (2., 3., 0.)), Atom(2, "O1", "O", (3.2, 3., 0.))], {(0, 1): 2}, "LIG"))
        self.out = self.root / "check"

    def check(self, **kwargs):
        values = {"out": self.out, "protein": self.protein, "ligand": self.ligand, "hydrogens": "preserve"}
        values.update(kwargs)
        return check_inputs(**values)

    def test_standard_complete_residue_passes_without_claiming_structural_perfection(self):
        original = self.protein.read_bytes(), self.ligand.read_bytes()
        report = self.check()
        self.assertTrue(report["passed"])
        self.assertEqual(report["protein_inventory"]["missing_heavy_atoms"], [])
        self.assertFalse(report["structure_complete_verified"])
        self.assertFalse(report["scientific_correctness_verified"])
        self.assertFalse(report["server_submitted"])
        self.assertFalse(report["task_registered"])
        self.assertTrue(Path(report["prepared_complex"]).is_file())
        self.assertEqual(report["component_resnames"], ["LIG"])
        self.assertEqual((self.protein.read_bytes(), self.ligand.read_bytes()), original)
        self.assertEqual(json.loads((self.out / "check.json").read_text()), report)

    def test_no_registration_or_login_calls(self):
        with patch("charmm_gui_cli.managed.remember_run") as remember, \
                patch("charmm_gui_cli.auth.load_token") as credentials, \
                patch("requests.sessions.Session.request") as network:
            self.assertTrue(self.check()["passed"])
        remember.assert_not_called()
        credentials.assert_not_called()
        network.assert_not_called()

    def test_repairs_are_persisted_in_check_json(self):
        lines = self.protein.read_text().splitlines()
        repaired_input = []
        for line in lines:
            if line.startswith("ATOM"):
                line = line[:6] + "    1" + line[11:21] + " " + line[22:72].ljust(50) + "PROA" + "  "
            repaired_input.append(line)
        self.protein.write_text("\n".join(repaired_input) + "\n")
        report = self.check()
        self.assertTrue(report["passed"])
        keys = {entry["key"] for entry in report["automatic_repairs"]}
        self.assertTrue({"input.renumbered", "input.elements_added", "input.chains_added"} <= keys)
        persisted = json.loads(Path(report["check_file"]).read_text())
        self.assertEqual(persisted["automatic_repairs"], report["automatic_repairs"])
        atoms = [line for line in Path(report["prepared_files"]["protein.pdb"]).read_text().splitlines() if line.startswith("ATOM")]
        self.assertEqual(len({line[6:11] for line in atoms}), len(atoms))
        self.assertTrue(all(line[21] == "A" and line[76:78].strip() for line in atoms))

    def test_missing_sidechain_and_backbone_are_review_not_repaired(self):
        self.protein.write_text(atom_line(1, "CA", resname="LYS") + "\nEND\n")
        report = self.check()
        self.assertTrue(report["passed"])
        self.assertTrue(report["review_required"])
        self.assertEqual(report["status"], "passed_with_review")
        missing = report["protein_inventory"]["missing_heavy_atoms"][0]
        self.assertEqual(missing["missing_backbone_atoms"], ["C", "N", "O"])
        self.assertEqual(missing["missing_sidechain_atoms"], ["CB", "CD", "CE", "CG", "NZ"])
        self.assertFalse(missing["repaired"])
        self.assertEqual(Path(report["prepared_files"]["protein.pdb"]).read_text().count("ATOM"), 1)

    def test_numbering_gap_is_not_a_claim_of_missing_residues(self):
        self.protein.write_text(atom_line(1, "CA", resid=10) + "\n" + atom_line(2, "CA", resid=20) + "\nEND\n")
        report = self.check()
        inventory = report["protein_inventory"]
        self.assertTrue(report["passed"])
        self.assertTrue(report["review_required"])
        self.assertEqual(inventory["numbering_gaps"][0]["unused_integer_numbers"], 9)
        self.assertFalse(inventory["numbering_gaps"][0]["missing_residues_inferred"])
        self.assertIsNone(inventory["missing_residues_inferred"])
        self.assertFalse(inventory["sequence_alignment_performed"])
        self.assertIn("不保证", inventory["interpretation"])

    def test_gaps_do_not_cross_ter_chains_or_insertions(self):
        self.protein.write_text("\n".join([
            atom_line(1, "CA", resid=10), atom_line(2, "CA", resid=10, insertion="A"),
            atom_line(3, "CA", resid=11), "TER", atom_line(4, "CA", resid=30),
            atom_line(5, "CA", resid=99, chain="B")]) + "\nEND\n")
        self.assertEqual(inspect_protein(self.protein)["numbering_gaps"], [])

    def test_terminal_oxygen_and_ile_alias_not_false_missing(self):
        names = ("N", "CA", "C", "OT1", "OT2", "CB", "CG1", "CG2", "CD")
        self.protein.write_text("\n".join(atom_line(i, name, resname="ILE") for i, name in enumerate(names, 1)) + "\nEND\n")
        self.assertEqual(inspect_protein(self.protein)["missing_heavy_atoms"], [])

    def test_altloc_report_retained_when_strict_preparation_rejects(self):
        self.protein.write_text(atom_line(1, "CA", altloc="A") + "\nEND\n")
        report = self.check()
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"]["stage"], "source_preparation")
        self.assertEqual(report["protein_inventory"]["alternate_locations"][0]["altloc"], "A")
        self.assertTrue(report["review_required"])
        self.assertTrue((self.out / "check.json").is_file())

    def test_multiple_models_reported_and_not_silently_selected(self):
        self.protein.write_text("MODEL        1\n" + atom_line(1, "CA") + "\nENDMDL\nMODEL        2\n" + atom_line(2, "CA") + "\nENDMDL\n")
        report = self.check()
        self.assertFalse(report["passed"])
        self.assertEqual(report["protein_inventory"]["models"], 2)
        self.assertEqual(report["protein_inventory"]["protein_residues_all_models"], 2)
        self.assertIn("one PDB model", report["error"]["message"])

    def test_graph_and_net_formal_charge_reported_not_total_system_charge(self):
        self.ligand.write_text(_sdf([Atom(1, "N1", "N", (2., 3., 0.), charge=1)], {}, "LIG"))
        report = self.check()
        self.assertTrue(report["passed"])
        chemistry = report["chemistry"][0]
        self.assertEqual(chemistry["net_formal_charge"], 1)
        self.assertTrue(chemistry["graph_connected"])
        self.assertEqual(chemistry["chemical_graph"]["atoms"], [{"name": "N1", "element": "N", "formal_charge": 1}])
        self.assertEqual(len(chemistry["graph_sha256"]), 64)
        self.assertNotIn("system_formal_charge", report)

    def test_original_input_hashes_not_only_generated_files(self):
        report = self.check()
        for role, path in (("protein", self.protein), ("ligand", self.ligand)):
            self.assertEqual(report["input_evidence"][role]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(report["input_evidence"][role]["path"], str(path))

    def test_preparation_failure_preserves_sources_and_report_stage(self):
        self.ligand.write_text(_sdf([Atom(1, "C1", "C", (100., 100., 100.))], {}, "LIG"))
        report = self.check()
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"]["stage"], "input_preparation")
        self.assertIn(">6 A", report["error"]["message"])
        self.assertTrue((self.out / "sources/sources.json").is_file())
        self.assertIsNone(report["prepared_complex"])
        self.assertEqual(report["spatial_review"]["summary"]["distant_component_ids"], ["ligand"])
        self.assertGreater(report["spatial_review"]["components"][0]["protein_contacts"]["minimum_distance_A"], 100)

    def test_short_contact_is_review_without_automatic_coordinate_repair(self):
        self.ligand.write_text(_sdf([Atom(1, "C1", "C", (1., 1.5, 0.))], {}, "LIG"))
        original = self.protein.read_bytes(), self.ligand.read_bytes()
        report = self.check()
        self.assertTrue(report["passed"])
        self.assertEqual(report["status"], "passed_with_review")
        spatial = report["spatial_review"]
        self.assertGreater(spatial["summary"]["near_pair_count"], 0)
        self.assertEqual(spatial["summary"]["severe_overlap_pair_count"], 0)
        self.assertFalse(spatial["blocking"])
        self.assertFalse(spatial["coordinates_modified"])
        self.assertFalse(spatial["bonds_inferred"])
        self.assertFalse(spatial["membrane_orientation_verified"])
        self.assertFalse(spatial["equilibration_verified"])
        self.assertEqual((self.protein.read_bytes(), self.ligand.read_bytes()), original)

    def test_severe_overlap_keeps_metrics_but_existing_preparation_still_fails(self):
        self.ligand.write_text(_sdf([Atom(1, "C1", "C", (1., 0.2, 0.))], {}, "LIG"))
        report = self.check()
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"]["stage"], "input_preparation")
        self.assertGreater(report["spatial_review"]["summary"]["severe_overlap_pair_count"], 0)
        self.assertAlmostEqual(report["spatial_review"]["components"][0]["protein_contacts"]["minimum_distance_A"], 0.2)

    def test_spatial_excludes_protein_and_ligand_internal_bonds(self):
        protein = [Atom(1, "C", "C", (0, 0, 0)), Atom(2, "N", "N", (1.3, 0, 0))]
        ligand = [Atom(3, "C1", "C", (0, 3, 0)), Atom(4, "O1", "O", (1.2, 3, 0))]
        spatial = inspect_spatial_review(protein, [{"id": "bound", "resname": "LIG", "atoms": ligand}])
        self.assertEqual(spatial["summary"]["near_pair_count"], 0)
        self.assertFalse(spatial["review_required"])
        self.assertEqual(spatial["components"][0]["protein_contacts"]["contact_pair_count"], 4)
        self.assertEqual(spatial["components"][0]["extent"]["centroid_A"], [0.6, 3., 0.])
        self.assertEqual(spatial["components"][0]["extent"]["minimum_A"], [0, 3, 0])
        self.assertEqual(spatial["components"][0]["extent"]["maximum_A"], [1.2, 3, 0])

    def test_hydrogens_and_isotope_hydrogens_are_not_heavy_contacts(self):
        protein = [Atom(1, "CA", "C", (0, 0, 0)), Atom(2, "H", "H", (0, 3, 0))]
        ligand = [Atom(3, "C1", "C", (0, 3, 0)), Atom(4, "H1", "H", (0, 0, 0)),
                  Atom(5, "D1", "D", (0, 0, 0)), Atom(6, "T1", "T", (0, 0, 0))]
        spatial = inspect_spatial_review(protein, [{"id": "bound", "resname": "LIG", "atoms": ligand}])
        self.assertEqual(spatial["summary"]["near_pair_count"], 0)
        self.assertEqual(spatial["protein_extent"]["heavy_atoms"], 1)
        self.assertEqual(spatial["components"][0]["extent"]["heavy_atoms"], 1)

    def test_sample_cap_does_not_truncate_counts_or_hide_closest_pair(self):
        protein = [Atom(1, "CA", "C", (0, 0, 0))]
        ligand = [Atom(i, f"C{i}", "C", (0, 0, z)) for i, z in enumerate((1.5, 1.4, 0.5), 2)]
        spatial = inspect_spatial_review(protein, [{"id": "bound", "resname": "LIG", "atoms": ligand}], sample_limit=1)
        contacts = spatial["components"][0]["protein_contacts"]
        self.assertEqual(contacts["near_pair_count"], 3)
        self.assertEqual(contacts["severe_overlap_pair_count"], 1)
        self.assertEqual(len(contacts["near_pair_examples"]), 1)
        self.assertTrue(contacts["examples_truncated"])
        self.assertEqual(contacts["minimum_distance_A"], 0.5)
        self.assertEqual(contacts["closest_pair"]["right"]["atom_name"], "C4")

    def test_distinct_components_are_compared_without_internal_pair_false_positives(self):
        protein = [Atom(1, "CA", "C", (0, 0, 0))]
        groups = [{"id": "ligand", "resname": "LIG", "atoms": [Atom(2, "C1", "C", (0, 3, 0))]},
                  {"id": "cofactor", "resname": "COF", "atoms": [Atom(3, "C1", "C", (0, 3.5, 0))]}]
        spatial = inspect_spatial_review(protein, groups)
        self.assertEqual(spatial["summary"]["near_pair_count"], 1)
        self.assertEqual(spatial["summary"]["severe_overlap_pair_count"], 1)
        self.assertEqual(spatial["component_pairs"][0]["left"], "ligand")
        self.assertEqual(spatial["component_pairs"][0]["right"], "cofactor")

    def test_cutoffs_have_explicit_boundary_semantics(self):
        protein = [Atom(1, "CA", "C", (0, 0, 0))]
        for distance, near, severe, contact, distant in ((0.8, 1, 0, 1, False), (2., 0, 0, 1, False),
                                                       (4., 0, 0, 1, False), (6., 0, 0, 0, False), (6.01, 0, 0, 0, True)):
            with self.subTest(distance=distance):
                spatial = inspect_spatial_review(protein, [{"id": "lig", "resname": "LIG", "atoms": [Atom(2, "C1", "C", (0, distance, 0))]}])
                details = spatial["components"][0]
                self.assertEqual(details["protein_contacts"]["near_pair_count"], near)
                self.assertEqual(details["protein_contacts"]["severe_overlap_pair_count"], severe)
                self.assertEqual(details["protein_contacts"]["contact_pair_count"], contact)
                self.assertEqual(details["beyond_distant_threshold"], distant)

    def test_spatial_parameter_and_atom_validation(self):
        protein = [Atom(1, "CA", "C", (0, 0, 0))]
        groups = [{"id": "lig", "resname": "LIG", "atoms": [Atom(2, "C1", "C", (0, 3, 0))]}]
        for kwargs in ({"near_A": float("nan")}, {"contact_A": 1}, {"sample_limit": -1}, {"sample_limit": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ToolError):
                inspect_spatial_review(protein, groups, **kwargs)
        with self.assertRaisesRegex(ToolError, "uniquely"):
            inspect_spatial_review(protein, groups + groups)

    def test_unknown_residue_template_requests_review(self):
        self.protein.write_text(atom_line(1, "CA", resname="UNK") + "\nEND\n")
        inventory = inspect_protein(self.protein)
        self.assertTrue(inventory["review_required"])
        self.assertEqual(inventory["unknown_residue_templates"][0]["resname"], "UNK")
        self.assertEqual(inventory["missing_heavy_atoms"], [])

    def test_existing_output_never_overwritten(self):
        self.out.mkdir()
        sentinel = self.out / "check.json"
        sentinel.write_text("KEEP")
        with self.assertRaisesRegex(ToolError, "new directory"):
            self.check()
        self.assertEqual(sentinel.read_text(), "KEEP")

    def test_missing_file_becomes_failed_json(self):
        report = self.check(ligand=self.root / "absent.sdf")
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"]["stage"], "input_evidence")
        self.assertTrue((self.out / "check.json").is_file())

    def test_strict_option_types_fail_without_traceback(self):
        report = self.check(accept_conect_bond_orders="yes")
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"]["stage"], "options")

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit is required for valence hydrogen addition")
    def test_add_missing_policy_really_adds_ligand_hydrogens(self):
        report = self.check(hydrogens="add_missing")
        self.assertTrue(report["passed"])
        self.assertEqual(report["hydrogen_policy"], "add_missing")
        self.assertGreater(report["chemistry"][0]["elements"].get("H", 0), 0)
        original = read_ligand(self.ligand)
        prepared = read_ligand(report["prepared_files"]["ligand.sdf"])
        self.assertEqual([a.xyz for a in original.atoms], [a.xyz for a in prepared.atoms[:len(original.atoms)]])

    def component_inputs(self, repeated=False):
        source = self.root / "complex.pdb"
        lines = self.protein.read_text().splitlines()[:-1]
        selectors = []
        for index, (resname, chain, xyz) in enumerate((("LIG", "L", (2., 3., 0.)), ("LIG" if repeated else "COF", "M", (2., -3., 0.))), 1):
            lines.extend([atom_line(10 * index, "C1", resname, chain=chain, xyz=xyz, record="HETATM"),
                          atom_line(10 * index + 1, "O1", resname, chain=chain, xyz=(xyz[0] + 1.2, xyz[1], xyz[2]), record="HETATM")])
            selectors.append({"resname": resname, "chain": chain, "resid": 1, "sdf": self.ligand.name})
        source.write_text("\n".join(lines) + "\nEND\n")
        config = self.root / "components.yaml"
        config.write_text(yaml.safe_dump({"version": 1, "water_policy": "reject", "ion_policy": "reject", "components": selectors}))
        return source, config

    def test_component_config_uses_build_path_and_preserves_all_instances(self):
        source, config = self.component_inputs()
        report = check_inputs(out=self.out, complex_pdb=source, components=config, hydrogens="preserve")
        self.assertTrue(report["passed"], report.get("error"))
        self.assertEqual(report["component_resnames"], ["COF", "LIG"])
        self.assertEqual(len(report["chemistry"]), 2)
        self.assertTrue(report["component_preparation"]["remote_validation_supported"])
        self.assertEqual(report["input_evidence"]["components_config"]["sha256"], hashlib.sha256(config.read_bytes()).hexdigest())
        self.assertEqual(report["hydrogen_policy"], "preserve")
        self.assertEqual(report["spatial_review"]["summary"]["component_count"], 2)
        centroids = [item["extent"]["centroid_A"] for item in report["spatial_review"]["components"]]
        self.assertEqual(centroids, [[2.6, 3., 0.], [2.6, -3., 0.]])
        self.assertEqual(report["spatial_review"]["component_pairs"][0]["minimum_distance_A"], 6.)

    @unittest.skipUnless(importlib.util.find_spec("rdkit"), "RDKit is required for component hydrogen addition")
    def test_component_default_add_missing_is_not_silently_preserve(self):
        source, config = self.component_inputs()
        report = check_inputs(out=self.out, complex_pdb=source, components=config)
        self.assertTrue(report["passed"], report.get("error"))
        self.assertGreater(report["component_preparation"]["hydrogens_added"], 0)
        self.assertTrue(all(item["elements"].get("H", 0) > 0 for item in report["chemistry"]))

    def test_component_layout_without_final_validation_support_fails(self):
        source, config = self.component_inputs(repeated=True)
        report = check_inputs(out=self.out, complex_pdb=source, components=config, hydrogens="preserve")
        self.assertFalse(report["passed"])
        self.assertIn("one copy each", report["error"]["message"])
        self.assertTrue((self.out / "prepared/sources/complex.pdb").is_file())


if __name__ == "__main__":
    unittest.main()
