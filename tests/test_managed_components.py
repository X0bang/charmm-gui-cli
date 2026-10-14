import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.store import managed
from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.chem.inputs import Atom, _sdf


class ManagedComponentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-managed-components-")).resolve()
        self.home_patch = patch("pathlib.Path.home", return_value=self.root / "home")
        self.home_patch.start()
        # Pipeline mocks use non-tar bytes; actual provenance has a separate suite.
        self.provenance_patch = patch.object(managed, "_archive_binding", side_effect=lambda manifest, archive, **kwargs:
                                             {"archive_provenance": {"archive_sha256": managed._digest(archive)}})
        self.provenance_patch.start()
        self.directory = self.root / "run"
        self.source = self.root / "bound.pdb"
        lines = []
        records = [("ATOM", "CA", "C", "ALA", "A", 1, (0, 0, 0)),
                   ("ATOM", "CA", "C", "GLY", "A", 2, (2, 0, 0)),
                   ("ATOM", "CA", "C", "SER", "A", 3, (0, 2, 0)),
                   ("HETATM", "C1", "C", "LIG", "B", 1, (3, 0, 2)),
                   ("HETATM", "O1", "O", "LIG", "B", 1, (4.2, 0, 2)),
                   ("HETATM", "C1", "C", "COF", "C", 1, (0, 3, 2)),
                   ("HETATM", "O1", "O", "COF", "C", 1, (0, 4.2, 2))]
        for i, (record, name, element, residue, chain, resid, xyz) in enumerate(records, 1):
            lines.append(f"{record:<6}{i:5d} {name:>4} {residue:>3} {chain}{resid:4d}    {xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00  0.00          {element:>2}")
        self.source.write_text("\n".join(lines) + "\nEND\n")
        self.sdf = self.root / "chemical.sdf"
        self.sdf.write_text(_sdf([Atom(1, "C1", "C", (0, 0, 0)), Atom(2, "O1", "O", (1.2, 0, 0))], {(0, 1): 2}, "LIG"))
        self.config = self.root / "components.yaml"
        self.component_config = {"version": 1, "water_policy": "reject", "ion_policy": "reject", "components": [
            {"resname": "LIG", "chain": "B", "resid": 1, "sdf": "chemical.sdf"},
            {"resname": "COF", "chain": "C", "resid": 1, "sdf": "chemical.sdf", "role": "cofactor"}]}
        self.config.write_text(json.dumps(self.component_config))

    def tearDown(self):
        self.provenance_patch.stop()
        self.home_patch.stop()  # All test artifacts are retained.

    def create(self):
        return managed.create_component_build(components_config=self.config, complex_pdb=self.source,
                                              output=self.directory, hydrogens="preserve")

    def done(self):
        directory = self.create()
        manifest = json.loads((directory / "full-run.json").read_text())
        manifest["state"] = "ligands_present"
        (directory / "full-run.json").write_text(json.dumps(manifest))
        (directory / "bilayer").mkdir()
        (directory / "bilayer/charmm-gui.tgz").write_bytes(b"local synthetic archive")
        return directory

    def test_initialize_persists_component_inputs_without_single_ligand_initializer(self):
        with patch.object(managed, "initialize") as single:
            directory = self.create()
        single.assert_not_called()
        manifest = json.loads((directory / "full-run.json").read_text())
        self.assertTrue(manifest["config"]["components_mode"])
        self.assertEqual(manifest["config"]["component_resnames"], ["LIG", "COF"])
        self.assertEqual(manifest["state"], "inputs_ready")
        self.assertIn("component-002.sdf", manifest["inputs"]["files"])
        self.assertEqual(managed.latest_run(), directory)

    def test_trusted_parameters_are_locally_retained_but_remote_initialization_refused(self):
        (self.root / "lig.rtf").write_text("RESI LIG 0\nATOM C1 CG 0\nATOM O1 OG 0\nDOUBLE C1 O1\nEND\n")
        (self.root / "lig.prm").write_text("BONDS\nCG OG 300 1.2\nEND\n")
        self.component_config["components"][0]["topology"] = {"rtf": "lig.rtf", "prm": "lig.prm", "trusted": True}
        self.config.write_text(json.dumps(self.component_config))
        with self.assertRaisesRegex(ToolError, "trusted CHARMM topology upload"):
            self.create()
        self.assertFalse((self.directory / "full-run.json").exists())
        self.assertTrue((self.directory / "inputs/input-manifest.json").is_file())

    def test_invalid_build_parameters_cannot_create_submittable_run(self):
        with self.assertRaises(ToolError):
            managed.create_component_build(components_config=self.config, complex_pdb=self.source,
                                           output=self.directory, hydrogens="preserve", ions={"type": "KCl", "concentration_M": -0.1})
        self.assertFalse((self.directory / "full-run.json").exists())

    def penalty_fixture(self, root, name, penalty):
        root.mkdir(parents=True, exist_ok=True)
        (root / "step3_size.str").write_text("SET NLIPTOP = 2\nSET NLIPBOT = 2\n")
        folder = root / "lig"
        folder.mkdir(exist_ok=True)
        (folder / (name.lower() + ".rtf")).write_text(f"RESI {name} 0.0 ! param penalty= {penalty}; charge penalty= 1.2\n")
        (folder / (name.lower() + ".prm")).write_text(f"! penalty= {penalty}\n")

    def test_penalty_discovery_is_residue_bound_and_ambiguity_is_incomplete(self):
        system = self.root / "system"
        self.penalty_fixture(system, "LIG", 1.4)
        self.penalty_fixture(system, "COF", 75)
        reports = managed._component_penalties(system, ["LIG", "COF"])
        self.assertEqual(reports["COF"]["parameter_max"], 75)
        self.assertTrue(reports["COF"]["review_required"])
        self.assertEqual(reports["LIG"]["parameter_max"], 1.4)
        (system / "duplicate.rtf").write_text("RESI COF 0.0\n")
        reports = managed._component_penalties(system, ["COF"])
        self.assertEqual(reports["COF"]["parameter_max"], 75)
        self.assertIn(str(system / "duplicate.rtf"), reports["COF"]["evidence"]["unpaired_topologies"])
        (system / "duplicate.prm").write_text("! penalty= 0\n")
        reports = managed._component_penalties(system, ["COF"])
        self.assertEqual(reports["COF"]["status"], "incomplete")
        self.assertTrue(reports["COF"]["review_required"])

    def mock_validators(self, failed_component=False):
        def extract(archive, destination):
            destination.mkdir(parents=True)
            self.penalty_fixture(destination, "LIG", 1.4)
            self.penalty_fixture(destination, "COF", 75)
            return destination

        def validate(system, manifest, reference, **kwargs):
            components = {}
            for index, name in enumerate(("LIG", "COF")):
                checks = {"total_atom_count": {"passed": True, "coordinates": 120, "topology": 120}}
                if kwargs["run_grompp"] and index == 0:
                    output = Path(kwargs["output_parent"]) / "grompp-test"
                    output.mkdir()
                    (output / "system.tpr").write_bytes(b"synthetic tpr")
                    (output / "grompp.log").write_text("No warnings\n")
                    checks["grompp"] = {"passed": True, "log": str(output / "grompp.log"), "output_dir": str(output)}
                components[name] = {"passed": not (failed_component and name == "COF"),
                                    "topology_validation": {"passed": True, "checks": checks,
                                        "coordinate_residue_counts": {"LIG": 1, "COF": 1}, "files": {"coordinates": str(system / "step5_input.pdb")}},
                                    "bound_pose": {"passed": not (failed_component and name == "COF")}, "same_protein_transform": True}
            return {"passed": not failed_component, "components": components, "errors": []}

        acceptance = {"passed": True, "checks": {"leaflets": {"passed": True, "blocking": True},
                      "charmm_minimization": {"passed": True, "blocking": True},
                      "cgenff": {"passed": True, "warnings": []}}, "warnings": []}
        return (patch.object(managed, "extract_for_validation", side_effect=extract),
                patch.object(managed, "validate_components", side_effect=validate),
                patch.object(managed, "assess_acceptance", return_value=acceptance),
                patch.object(managed, "inspect_environment", return_value={"passed": True}))

    def test_final_aggregate_compilation_cache_and_each_penalty_review(self):
        boundary = patch("charmm_gui_cli.store.delivery.verify_model")
        boundary.start()  # This fixture uses non-tar mock bytes.
        self.addCleanup(boundary.stop)
        directory = self.done()
        gmx = self.root / "gmx-test"
        gmx.write_text("compiler identity fixture")
        gmx.chmod(0o700)
        a, b, c, d = self.mock_validators()
        with a as extract, b as validate, c, d, patch("charmm_gui_cli.store.delivery.verify_model"):
            report = managed.finalize_managed_build(directory, grompp=True, gmx=str(gmx))
            reused = managed.finalize_managed_build(directory, grompp=True, gmx=str(gmx))
        self.assertTrue(report["passed"])
        self.assertEqual(report, reused)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(validate.call_count, 1)
        self.assertTrue(report["checks"]["grompp"]["passed"])
        self.assertEqual(report["checks"]["total_atom_count"]["coordinates"], 120)
        self.assertEqual(report["coordinate_residue_counts"], {"LIG": 1, "COF": 1})
        self.assertEqual(report["evidence"]["component_validation_revision"], 2)
        old_evidence = dict(report["evidence"])
        old_evidence.pop("component_validation_revision")
        self.assertFalse(managed._cache_reusable({**report, "fingerprint": managed._canonical_hash(old_evidence)}, report["evidence"]))
        self.assertTrue(report["review_required"])
        self.assertEqual(report["acceptance_validation"]["component_penalties"]["COF"]["parameter_max"], 75)
        self.assertTrue(managed.inspect_run(directory)["gromacs_compiled"])

    def test_one_bad_component_prevents_overall_success(self):
        directory = self.done()
        a, b, c, d = self.mock_validators(failed_component=True)
        with a, b, c, d:
            report = managed.finalize_managed_build(directory)
        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "validation_failed")
        self.assertFalse(report["bound_pose_validation"]["passed"])


if __name__ == "__main__":
    unittest.main()
