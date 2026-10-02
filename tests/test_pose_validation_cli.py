import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.auth import atomic_json, atomic_write
from charmm_gui_cli.cli import main


def atom(serial, name, residue, resid, xyz, element="C"):
    kind = "HETATM" if residue == "ABC" else "ATOM  "
    return f"{kind}{serial:5d} {name:^4} {residue:>3} A{resid:4d}    {xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00  0.00          {element:>2}\n"


class PoseValidationCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-pose-cli-test-"))
        self.system = self.root / "system"
        self.system.mkdir()
        self.reference = self.root / "complex.pdb"
        self.manifest = self.root / "input-manifest.json"
        self.report_dir = self.root / "report"
        self.atoms = [(1, "CA", "ALA", 1, (0, 0, 0)), (2, "CA", "ALA", 2, (3, 0, 0)),
                      (3, "CA", "ALA", 3, (0, 4, 0)), (4, "CA", "ALA", 4, (0, 0, 5)),
                      (5, "C1", "ABC", 5, (1, 1, 2)), (6, "C2", "ABC", 5, (2, 1, 2))]
        atomic_write(self.reference, "".join(atom(*row) for row in self.atoms))
        atomic_json(self.manifest, {"ligand": {"resname": "ABC", "atom_mapping": [
            {"element": "C", "pdb_atom_name": "C1"}, {"element": "C", "pdb_atom_name": "C2"},
            {"element": "H", "pdb_atom_name": "H1"}]}})

    def assembly(self, ligand_drift=0, destination=None):
        lines = []
        for serial, name, residue, resid, xyz in self.atoms:
            # One 90-degree rotation plus translation for the entire complex.
            transformed = (-xyz[1] + 10, xyz[0] - 4, xyz[2] + 7)
            if residue == "ABC":
                transformed = (transformed[0] + ligand_drift, transformed[1], transformed[2])
            lines.append(atom(serial, name, residue, resid, transformed))
        destination = destination or self.system / "step5_assembly.pdb"
        atomic_write(destination, "".join(lines))

    def invoke(self, reference=True, base_passed=True):
        args = ["validate", str(self.system), "--input-manifest", str(self.manifest), "--out", str(self.report_dir)]
        if reference:
            args.extend(["--reference-pdb", str(self.reference)])
        with patch("charmm_gui_cli.validation.validate_system", return_value={"passed": base_passed}), \
                contextlib.redirect_stdout(io.StringIO()):
            status = main(args)
        return status, json.loads((self.report_dir / "validation.json").read_text())

    def test_global_rigid_transform_preserves_bound_pose(self):
        self.assembly()
        status, report = self.invoke()
        self.assertEqual(status, 0)
        pose = report["bound_pose_validation"]
        self.assertTrue(pose["passed"])
        self.assertLess(pose["ligand_rmsd_after_protein_fit_A"], 0.001)
        self.assertEqual(pose["ligand_heavy_atom_count"], 2)
        self.assertEqual(pose["thresholds_A"], {"protein_ca_rmsd": 1.0, "ligand_rmsd": 2.0})

    def test_independent_ligand_drift_fails(self):
        self.assembly(ligand_drift=4)
        status, report = self.invoke()
        self.assertEqual(status, 2)
        self.assertFalse(report["passed"])
        self.assertFalse(report["bound_pose_validation"]["passed"])

    def test_missing_assembly_is_clear_failure(self):
        status, report = self.invoke()
        self.assertEqual(status, 2)
        self.assertIn("No step5_assembly.pdb", report["bound_pose_validation"]["error"])

    def test_multiple_assemblies_are_not_guessed(self):
        self.assembly()
        other = self.system / "other"
        other.mkdir()
        self.assembly(destination=other / "step5_assembly.pdb")
        status, report = self.invoke()
        self.assertEqual(status, 2)
        self.assertIn("Multiple step5_assembly.pdb", report["bound_pose_validation"]["error"])

    def test_missing_reference_is_clear_failure(self):
        self.reference = self.root / "missing.pdb"
        self.assembly()
        status, report = self.invoke()
        self.assertEqual(status, 2)
        self.assertIn("Reference PDB file does not exist", report["bound_pose_validation"]["error"])

    def test_omitted_reference_preserves_existing_behavior(self):
        status, report = self.invoke(reference=False)
        self.assertEqual(status, 0)
        self.assertNotIn("bound_pose_validation", report)

    def test_pose_pass_cannot_override_topology_failure(self):
        self.assembly()
        status, report = self.invoke(base_passed=False)
        self.assertEqual(status, 2)
        self.assertTrue(report["bound_pose_validation"]["passed"])
        self.assertFalse(report["passed"])

    def test_duplicate_manifest_atom_mapping_fails(self):
        self.assembly()
        atomic_json(self.manifest, {"ligand": {"resname": "ABC", "atom_mapping": [
            {"element": "C", "pdb_atom_name": "C1"}, {"element": "C", "pdb_atom_name": "C1"}]}})
        status, report = self.invoke()
        self.assertEqual(status, 2)
        self.assertIn("unique", report["bound_pose_validation"]["error"])


if __name__ == "__main__":
    unittest.main()
