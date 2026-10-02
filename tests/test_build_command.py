"""High-level user workflow tests; transport is always disabled."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.auth import atomic_json
from charmm_gui_cli.cli import main


class BuildCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-build-command-test-"))
        self.run = self.root / "run"
        self.cookies = self.root / "session.cookies.json"
        atomic_json(self.cookies, {"cookies": []})
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch("requests.Session.request", side_effect=AssertionError("No network in unit tests")))
        self.token = self.stack.enter_context(patch("charmm_gui_cli.build_command.load_token", return_value=("test-token", str(self.root / "session.token"))))

    def tearDown(self):
        self.stack.close()

    def call(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(arguments)
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def test_top_help_guides_user_before_advanced_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as error:
            main(["-h"])
        self.assertEqual(error.exception.code, 0)
        for command in ("charmm-gui-cli login", "--complex complex.pdb", "--protein protein.pdb", "build-resume", "--no-wait"):
            self.assertIn(command, output.getvalue())
        self.token.assert_not_called()

    def test_missing_inputs_has_actionable_error_without_authentication(self):
        code, _, errors = self.call(["build"])
        self.assertEqual(code, 2)
        self.assertIn("--protein", errors)
        self.assertIn("build -h", errors)
        self.token.assert_not_called()

    def test_dry_run_does_not_login_or_submit_and_propagates_model_options(self):
        with patch("charmm_gui_cli.managed.create_managed_build", return_value=self.run) as create, \
                patch("charmm_gui_cli.full_build.advance") as advance:
            code, result, _ = self.call(["build", "--complex", "bound.pdb", "--accept-conect-bond-orders",
                                        "--salt", "KCl", "--salt-concentration", "0.1", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(result["server_submitted"])
        self.assertEqual(create.call_args.kwargs["ions"], {"type": "KCl", "concentration_M": 0.1})
        self.assertTrue(create.call_args.kwargs["accept_conect_bond_orders"])
        advance.assert_not_called()
        self.token.assert_not_called()

    def test_direct_completed_build_automatically_validates(self):
        with patch("charmm_gui_cli.managed.create_managed_build", return_value=self.run), \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "ligands_present"}) as advance, \
                patch("charmm_gui_cli.managed.finalize_managed_build", return_value={"passed": True, "checks": {}}) as finalize, \
                patch("charmm_gui_cli.build_command.shutil.which", return_value=None):
            code, result, _ = self.call(["build", "--protein", "protein.pdb", "--ligand", "bound.sdf"])
        self.assertEqual(code, 0)
        self.assertTrue(advance.call_args.kwargs["wait"])
        self.assertEqual(result["state"], "validated")
        self.assertFalse(result["gromacs_compiled"])
        self.assertIn("not run", result["gromacs_note"])
        finalize.assert_called_once_with(self.run, grompp=False, gmx="gmx")

    def test_no_wait_does_not_mislabel_pending_as_complete(self):
        with patch("charmm_gui_cli.managed.create_managed_build", return_value=self.run), \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "preparation_running"}) as advance, \
                patch("charmm_gui_cli.managed.finalize_managed_build") as finalize:
            code, result, _ = self.call(["build", "--protein", "protein.pdb", "--ligand", "bound.sdf", "--no-wait"])
        self.assertEqual(code, 0)
        self.assertFalse(advance.call_args.kwargs["wait"])
        self.assertEqual(result["state"], "preparation_running")
        finalize.assert_not_called()

    def test_yaml_and_direct_flags_are_not_silently_mixed(self):
        code, _, error = self.call(["build", "system.yaml", "--salt", "NaCl"])
        self.assertEqual(code, 2)
        self.assertIn("not both", error)
        self.token.assert_not_called()

    def test_resume_without_path_uses_latest(self):
        with patch("charmm_gui_cli.managed.latest_run", return_value=self.run) as latest, \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "running"}) as advance:
            code, _, _ = self.call(["build-resume", "--no-wait"])
        self.assertEqual(code, 0)
        latest.assert_called_once()
        self.assertEqual(advance.call_args.args[0], self.run)

    def test_explicit_grompp_missing_executable_fails_before_login(self):
        with patch("charmm_gui_cli.build_command.shutil.which", return_value=None):
            code, _, error = self.call(["build", "--protein", "p.pdb", "--ligand", "l.sdf", "--grompp"])
        self.assertEqual(code, 2)
        self.assertIn("no job was submitted", error)
        self.token.assert_not_called()

    def test_failed_final_validation_is_nonzero(self):
        with patch("charmm_gui_cli.managed.create_managed_build", return_value=self.run), \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "ligands_present"}), \
                patch("charmm_gui_cli.managed.finalize_managed_build", return_value={"passed": False, "checks": {}}):
            code, result, _ = self.call(["build", "--protein", "p.pdb", "--ligand", "l.sdf", "--no-grompp"])
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
