import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli.auth import ToolError, atomic_write
from charmm_gui_cli.cli import main


class ValidationCliTests(unittest.TestCase):
    def setUp(self):
        # Preserve all test directories under the user's no-deletion policy.
        self.root = Path(tempfile.mkdtemp(prefix="cgui-validation-cli-test-"))
        self.system = self.root / "system"
        self.system.mkdir()
        self.out = self.root / "report"
        self.config = self.root / "build.yaml"
        atomic_write(self.root / "protein.pdb", "END\n")
        atomic_write(self.root / "ligand.sdf", "placeholder\n")
        atomic_write(self.config, yaml.safe_dump({
            "version": 2, "protein": "protein.pdb", "ligand": "ligand.sdf",
            "membrane": {"upper": "POPC:CHL1=3:1", "lower": "POPC:POPE=1:1", "margin_A": 20},
            "ions": {"type": "KCl", "concentration_M": 0.2}}))
        self.base = ["validate", str(self.system), "--input-manifest", str(self.root / "manifest.json"), "--out", str(self.out)]

    def invoke(self, options=(), report=None, environment=None, side_effect=None):
        if report is None:
            report = {"passed": True, "files": {"coordinates": str(self.system / "step5_input.pdb")}}
        if environment is None:
            environment = {"passed": True, "checks": {}}
        with patch("charmm_gui_cli.validation.validate_system", return_value=report), \
                patch("charmm_gui_cli.acceptance.assess_acceptance", return_value={"passed": True, "review_required": False, "warnings": []}), \
                patch("charmm_gui_cli.validation.inspect_environment", return_value=environment, side_effect=side_effect) as inspect, \
                contextlib.redirect_stdout(io.StringIO()):
            status = main([*self.base, *options])
        return status, inspect, json.loads((self.out / "validation.json").read_text())

    def test_default_validation_keeps_existing_behavior(self):
        status, inspect, report = self.invoke()
        self.assertEqual(status, 0)
        inspect.assert_not_called()
        self.assertNotIn("environment_validation", report)

    def test_build_config_passes_leaflet_union_salt_and_system_root(self):
        status, inspect, report = self.invoke(["--build-config", str(self.config)])
        self.assertEqual(status, 0)
        self.assertTrue(report["environment_validation"]["passed"])
        kwargs = inspect.call_args.kwargs
        self.assertEqual(kwargs["expected_lipids"], ["CHL1", "POPC", "POPE"])
        self.assertEqual(kwargs["ion_type"], "KCl")
        self.assertEqual(kwargs["concentration_M"], 0.2)
        self.assertEqual(kwargs["system_dir"], self.system)

    def test_environment_mismatch_fails_combined_report(self):
        status, _, report = self.invoke(["--build-config", str(self.config)], environment={"passed": False, "checks": {"ion_species": {"passed": False}}})
        self.assertEqual(status, 2)
        self.assertFalse(report["passed"])

    def test_topology_failure_is_not_overridden_by_environment_success(self):
        status, _, report = self.invoke(["--build-config", str(self.config)], report={"passed": False, "files": {"coordinates": "some.pdb"}})
        self.assertEqual(status, 2)
        self.assertFalse(report["passed"])

    def test_missing_coordinates_records_environment_failure(self):
        status, inspect, report = self.invoke(["--build-config", str(self.config)], report={"passed": False, "errors": ["No topology"]})
        self.assertEqual(status, 2)
        inspect.assert_not_called()
        self.assertIn("coordinate", report["environment_validation"]["error"])

    def test_environment_parse_error_is_preserved_in_report(self):
        status, _, report = self.invoke(["--build-config", str(self.config)], side_effect=ToolError("Unsupported salt"))
        self.assertEqual(status, 2)
        self.assertEqual(report["environment_validation"]["error"], "Unsupported salt")

    def test_archive_validation_scans_extracted_root_for_ion_input(self):
        archive = self.root / "system.tgz"
        atomic_write(archive, "mock archive")
        self.base[1] = str(archive)
        extracted = self.out / "extracted"
        with patch("charmm_gui_cli.validation.extract_for_validation", return_value=extracted) as extract:
            status, inspect, _ = self.invoke(["--build-config", str(self.config)])
        self.assertEqual(status, 0)
        extract.assert_called_once_with(archive, extracted)
        self.assertEqual(inspect.call_args.kwargs["system_dir"], extracted)

    def test_invalid_build_yaml_does_not_create_output(self):
        atomic_write(self.config, "version: 1\n")
        with contextlib.redirect_stderr(io.StringIO()):
            status = main([*self.base, "--build-config", str(self.config)])
        self.assertEqual(status, 2)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
