import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from charmm_gui_cli.auth import ToolError, atomic_json, atomic_write
from charmm_gui_cli.build_config import api_config, load_build_config
from charmm_gui_cli.cli import main


class BuildConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-build-config-test-"))
        atomic_write(self.root / "protein.pdb", "END\n")
        atomic_write(self.root / "ligand.sdf", "dummy test file\n")
        self.path = self.root / "build.yaml"
        self.raw = {"version": 2, "protein": "protein.pdb", "ligand": "ligand.sdf",
                    "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20}}

    def load(self, raw=None):
        atomic_write(self.path, yaml.safe_dump(self.raw if raw is None else raw))
        return load_build_config(self.path)

    def test_relative_inputs_resolve_against_yaml_and_defaults_are_explicit(self):
        config = self.load()
        self.assertEqual(config["protein"], str(self.root / "protein.pdb"))
        self.assertEqual(config["ligand"], str(self.root / "ligand.sdf"))
        self.assertEqual(config["preparation"], {"n_terminal": "NTER", "c_terminal": "CTER", "orientation": "ppm"})
        self.assertEqual(config["ligand_hydrogens"], "preserve")
        self.assertFalse(api_config(config, None)["preparation"]["ligand_parameters_ready"])
        self.assertTrue(api_config(config, "123")["preparation"]["ligand_parameters_ready"])

    def test_resname_matches_the_input_adapter_limits(self):
        for value in (None, 3, True, [], {}, "LONG", "2BC", "lig", "A B", ""):
            with self.subTest(value=value), self.assertRaises(ToolError):
                self.load({**self.raw, "ligand_resname": value})

    def test_terminal_hydrogen_and_orientation_enums_are_strict(self):
        for value in (None, True, 3, [], {}, "nter", "INVALID"):
            for field in ("n_terminal", "c_terminal", "orientation"):
                with self.subTest(field=field, value=value), self.assertRaises(ToolError):
                    self.load({**self.raw, "preparation": {field: value}})
            with self.subTest(hydrogens=value), self.assertRaises(ToolError):
                self.load({**self.raw, "ligand_hydrogens": value})

    def test_invalid_version_unknown_keys_and_missing_inputs_fail(self):
        cases = [{**self.raw, "version": True}, {**self.raw, "version": "2"},
                 {**self.raw, "allow_guessing": True}, {**self.raw, "protein": "missing.pdb"},
                 {**self.raw, "ligand": ["ligand.sdf"]}, {**self.raw, "preparation": None}]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ToolError):
                self.load(raw)

    def test_nonfinite_or_boolean_membrane_values_fail(self):
        for value in (float("nan"), float("inf"), True, 0, -1):
            with self.subTest(value=value), self.assertRaises(ToolError):
                self.load({**self.raw, "membrane": {**self.raw["membrane"], "margin_A": value}})

    def test_duplicate_yaml_keys_fail(self):
        atomic_write(self.path, "version: 2\nversion: 2\n")
        with self.assertRaisesRegex(ToolError, "unique"):
            load_build_config(self.path)

    def test_missing_config_is_controlled_error(self):
        with self.assertRaisesRegex(ToolError, "Cannot read"):
            load_build_config(self.root / "missing.yaml")

    def test_cli_resume_existing_bilayer_without_cookie(self):
        directory = self.root / "run"
        (directory / "bilayer").mkdir(parents=True)
        atomic_json(directory / "bilayer" / "run.json", {"job_id": "456"})
        output = io.StringIO()
        with patch("charmm_gui_cli.build_command.load_token", return_value=("secret", "mock")), \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "complete", "build_job_id": "456"}) as advance, \
                patch("charmm_gui_cli.web.WebClient") as web, contextlib.redirect_stdout(output):
            code = main(["build-resume", str(directory), "--cookies", str(self.root / "missing-cookie.json")])
        self.assertEqual(code, 0)
        web.assert_not_called()
        self.assertIsNone(advance.call_args.args[1])
        self.assertNotIn("secret", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["state"], "complete")

    def test_cli_fresh_build_requires_web_session_before_creating_run(self):
        self.load()
        directory = self.root / "not-created"
        errors = io.StringIO()
        with patch("charmm_gui_cli.build_command.load_token", return_value=("secret", "mock")), \
                patch("charmm_gui_cli.full_build.initialize") as initialize, contextlib.redirect_stderr(errors):
            code = main(["build", str(self.path), "--out", str(directory), "--cookies", str(self.root / "missing-cookie.json")])
        self.assertEqual(code, 2)
        initialize.assert_not_called()
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
