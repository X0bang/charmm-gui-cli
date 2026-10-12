import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from charmm_gui_cli.auth import ToolError
from charmm_gui_cli.build_config import load_build_config
from charmm_gui_cli.managed import new_run_path, validate_name
from charmm_gui_cli.preview import new_preview_path


class OutputLocationTests(unittest.TestCase):
    def test_paths_are_local(self):
        root = Path(tempfile.mkdtemp())
        with patch("pathlib.Path.cwd", return_value=root):
            self.assertEqual(new_run_path("crtw-bcar"), root / "charmm-gui-runs" / "crtw-bcar")
            self.assertEqual(new_preview_path("checks").parent, root / "charmm-gui-checks")

    def test_task_names_cannot_be_structure_filenames(self):
        for name in ("a/b", "ligand.sdf", "x.PDB", "x.yaml", "x.yml", "x.mol2", "x.cif"):
            with self.subTest(name=name), self.assertRaises(ToolError) as exc:
                validate_name(name)
            self.assertEqual(exc.exception.key, "input.task_name")
        self.assertEqual(validate_name("crtw-bcar"), "crtw-bcar")

    def test_missing_directory_and_permission_configuration_errors(self):
        root = Path(tempfile.mkdtemp())
        with self.assertRaises(ToolError) as exc:
            load_build_config(root / "missing.yaml")
        self.assertEqual(exc.exception.key, "config.missing")
        self.assertIn("curl -fL", exc.exception.next_step)
        with self.assertRaises(ToolError) as exc:
            load_build_config(root)
        self.assertEqual(exc.exception.key, "config.directory")
        with patch("pathlib.Path.read_text", side_effect=PermissionError), self.assertRaises(ToolError) as exc:
            load_build_config(root / "private.yaml")
        self.assertEqual(exc.exception.key, "config.permission")

    def test_every_error_has_an_action(self):
        self.assertTrue(ToolError("error").as_dict()["next_step"])
