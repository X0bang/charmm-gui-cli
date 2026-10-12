"""High-level user workflow tests; transport is always disabled."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.auth import ToolError, atomic_json
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
        self.preview = self.stack.enter_context(patch("charmm_gui_cli.preview.preview_run", return_value={"status": "generated"}, create=True))

    def tearDown(self):
        self.stack.close()

    def call(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(["--foreground", *arguments])
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

    def test_reused_preparation_needs_api_token_but_no_cookie(self):
        self.run.mkdir()
        atomic_json(self.run / "full-run.json", {"schema_version": 2, "state": "prepared_reused", "reused_preparation": {"certificate": "mocked"}})
        with patch("charmm_gui_cli.build_command.resolve_cookie_path", return_value=self.root / "missing.cookies"), \
                patch("charmm_gui_cli.web.WebClient", side_effect=AssertionError("No website session")), \
                patch("charmm_gui_cli.managed.remember_run"), patch("charmm_gui_cli.managed.record_recovery"), \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "bilayer_submitted"}) as advance:
            code, result, error = self.call(["build-resume", str(self.run), "--no-wait"])
        self.assertEqual(code, 0, error)
        self.token.assert_called_once()
        self.assertIsNone(advance.call_args.args[1])

    def test_preview_failure_does_not_invalidate_model_acceptance(self):
        self.preview.side_effect = ToolError("Preview conversion unavailable")
        with patch("charmm_gui_cli.managed.create_managed_build", return_value=self.run), \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "ligands_present"}), \
                patch("charmm_gui_cli.managed.finalize_managed_build", return_value={"passed": True, "checks": {}}):
            code, result, error = self.call(["build", "--protein", "protein.pdb", "--ligand", "bound.sdf"])
        self.assertEqual(code, 0, error)
        self.assertEqual(result["state"], "validated")
        self.assertEqual(result["preview"]["status"], "failed")

    def test_no_preview_disables_optional_generation(self):
        with patch("charmm_gui_cli.managed.create_managed_build", return_value=self.run), \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "ligands_present"}), \
                patch("charmm_gui_cli.managed.finalize_managed_build", return_value={"passed": True, "checks": {}}):
            code, result, error = self.call(["build", "--protein", "p.pdb", "--ligand", "l.sdf", "--no-preview"])
        self.assertEqual(code, 0, error)
        self.preview.assert_not_called()
        self.assertEqual(result["preview"]["status"], "disabled")

    def test_components_dry_run_uses_component_builder_without_auth(self):
        with patch("charmm_gui_cli.managed.create_component_build", return_value=self.run) as create, \
                patch("charmm_gui_cli.managed.create_managed_build") as single:
            code, result, error = self.call(["build", "--complex", "complex.pdb", "--components", "components.yaml",
                                             "--salt", "KCl", "--salt-concentration", "0.1", "--dry-run"])
        self.assertEqual(code, 0, error)
        self.assertFalse(result["server_submitted"])
        self.assertEqual(create.call_args.kwargs["components_config"], Path("components.yaml"))
        self.assertEqual(create.call_args.kwargs["complex_pdb"], Path("complex.pdb"))
        self.assertEqual(create.call_args.kwargs["ions"]["concentration_M"], 0.1)
        single.assert_not_called()
        self.token.assert_not_called()

    def test_component_conflicts_rejected_before_auth(self):
        cases = (["--components", "c.yaml"],
                 ["--complex", "complex.pdb", "--components", "c.yaml", "--ligand", "ligand.sdf"],
                 ["--complex", "complex.pdb", "--components", "c.yaml", "--ligand-resname", "LIG"],
                 ["--complex", "complex.pdb", "--components", "c.yaml", "--accept-conect-bond-orders"],
                 ["system.yaml", "--components", "c.yaml"])
        for arguments in cases:
            with self.subTest(arguments=arguments):
                code, result, error = self.call(["build", *arguments, "--dry-run"])
                self.assertEqual(code, 2)
                self.assertTrue("components" in error or "not both" in error)
        self.token.assert_not_called()

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
