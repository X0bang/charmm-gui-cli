"""Named task lookup, recovery evidence, and read-only status regression tests."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli import managed
from charmm_gui_cli.auth import ToolError, atomic_json, atomic_write
from charmm_gui_cli.cli import main


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-jobs-test-"))
        self.home = self.root / "home"
        self.home.mkdir()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch("pathlib.Path.home", return_value=self.home))
        self.stack.enter_context(patch("requests.Session.request", side_effect=AssertionError("No network in unit tests")))
        self.directory = self.root / "run"
        self.make_run(self.directory)

    def tearDown(self):
        self.stack.close()

    def make_run(self, directory):
        directory.mkdir()
        atomic_json(directory / "full-run.json", {"schema_version": 2, "state": "inputs_ready"})

    def call(self, args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(args)
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def test_name_is_unique_idempotent_and_resolves_outside_working_directory(self):
        managed.assign_name(self.directory, "protein-a")
        managed.assign_name(self.directory, "protein-a")
        self.assertEqual(managed.resolve_run("protein-a"), self.directory)
        self.assertEqual(managed.list_runs()[0]["name"], "protein-a")
        other = self.root / "other"
        self.make_run(other)
        with self.assertRaisesRegex(ToolError, "already registered"):
            managed.assign_name(other, "protein-a")
        with self.assertRaisesRegex(ToolError, "different name"):
            managed.assign_name(self.directory, "different")
        self.assertEqual(managed.resolve_run("protein-a"), self.directory)

    def test_names_cannot_escape_registry(self):
        for value in ("../escape", "/absolute", ".hidden", "", "a b", "a/b", "x" * 129):
            with self.subTest(value=value), self.assertRaises(ToolError):
                managed.assign_name(self.directory, value)
        self.assertFalse((self.home / ".config/charmm-gui-cli/names").exists())

    def test_unknown_or_moved_named_run_is_actionable_error(self):
        with self.assertRaisesRegex(ToolError, "Unknown job"):
            managed.resolve_run("missing")
        managed.assign_name(self.directory, "example")
        self.directory.rename(self.root / "retained-moved-run")
        with self.assertRaisesRegex(ToolError, "unavailable"):
            managed.resolve_run("example")

    def test_status_and_list_are_local_by_default_and_show_recovery(self):
        managed.assign_name(self.directory, "example")
        managed.record_recovery(self.directory, ToolError("Session expired", category="authentication"))
        code, result, _ = self.call(["jobs", "status", "example"])
        self.assertEqual(code, 0)
        self.assertFalse(result["server_checked"])
        self.assertIn("login", result["next_step"])
        self.assertIn("jobs resume example", result["next_step"])
        self.assertEqual(result["recovery"]["error"]["category"], "authentication")
        self.assertEqual(self.call(["jobs", "list"])[1]["jobs"][0]["name"], "example")

    def test_remote_status_does_not_write_or_claim_final_validation(self):
        managed.assign_name(self.directory, "example")
        (self.directory / "bilayer").mkdir()
        atomic_json(self.directory / "bilayer/run.json", {"job_id": "123", "state": "running"})
        before = (self.directory / "bilayer/run.json").read_bytes()
        with patch("charmm_gui_cli.auth.load_token", return_value=("test-token", "mock")), \
                patch("charmm_gui_cli.api.Client.status", return_value={"status": "done", "hasTarFile": True}) as status:
            result = managed.inspect_run("example", remote_check=True)
        status.assert_called_once_with("123")
        self.assertTrue(result["server_checked"])
        self.assertEqual(result["remote_status"], "done")
        self.assertEqual(result["state"], "running")
        self.assertNotIn("passed", result)
        self.assertEqual((self.directory / "bilayer/run.json").read_bytes(), before)

    def test_named_resume_routes_to_existing_directory(self):
        managed.assign_name(self.directory, "example")
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "running"}, 0)) as execute:
            self.assertEqual(self.call(["jobs", "resume", "example", "--no-wait"])[0], 0)
        self.assertEqual(execute.call_args.args[0].directory, self.directory)
        self.assertFalse(execute.call_args.args[0].wait)

    def test_recovery_history_is_retained_and_resolved_success_is_not_active(self):
        managed.assign_name(self.directory, "example")
        managed.record_recovery(self.directory, ToolError("Temporary network failure", category="transient_network", retryable=True))
        managed.record_recovery(self.directory, state="running")
        self.assertEqual(len(list((self.directory / "recovery-history").glob("*.json"))), 2)
        self.assertNotIn("recovery", managed.inspect_run("example"))
        self.assertFalse(json.loads((self.directory / "recovery.json").read_text())["active"])

    def test_offline_completed_archive_validation_needs_no_session_or_remote_query(self):
        manifest = {"schema_version": 2, "state": "ligands_present"}
        atomic_json(self.directory / "full-run.json", manifest)
        (self.directory / "bilayer").mkdir()
        atomic_write(self.directory / "bilayer/charmm-gui.tgz", "mock archive")
        managed.assign_name(self.directory, "example")
        with patch("charmm_gui_cli.build_command.load_token") as auth, \
                patch("charmm_gui_cli.full_build.advance") as advance, \
                patch.object(managed, "finalize_managed_build", return_value={"passed": True, "checks": {}}):
            code, result, _ = self.call(["jobs", "resume", "example", "--no-grompp"])
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "validated")
        auth.assert_not_called()
        advance.assert_not_called()

    def test_downloaded_archive_recovers_when_parent_state_sync_was_interrupted(self):
        atomic_json(self.directory / "full-run.json", {"schema_version": 2, "state": "bilayer_submitted"})
        (self.directory / "bilayer").mkdir()
        atomic_json(self.directory / "bilayer/run.json", {"schema_version": 1, "job_id": "123", "state": "ligands_present"})
        atomic_write(self.directory / "bilayer/charmm-gui.tgz", "mock archive")
        with patch("charmm_gui_cli.build_command.load_token") as auth, \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "ligands_present"}) as advance, \
                patch.object(managed, "finalize_managed_build", return_value={"passed": True, "checks": {}}):
            code, result, _ = self.call(["build-resume", str(self.directory), "--no-grompp"])
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "validated")
        auth.assert_not_called()
        self.assertIsNone(advance.call_args.args[1])

    def test_corrupt_name_record_is_controlled_error(self):
        path = self.home / ".config/charmm-gui-cli/names/broken.json"
        atomic_json(path, {"directory": None})
        with self.assertRaisesRegex(ToolError, "unreadable directory"):
            managed.resolve_run("broken")

    def test_report_rejects_stale_evidence_without_emitting_old_success(self):
        managed.assign_name(self.directory, "example")
        atomic_json(self.directory / "results/validation.json", {"passed": True, "status": "validated"})
        code, result, error = self.call(["jobs", "report", "example"])
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertIn("stale", error)

    def test_report_exposes_warnings_and_full_report_without_network(self):
        managed.assign_name(self.directory, "example")
        report = {"passed": True, "status": "validated", "review_required": True,
                  "acceptance_validation": {"passed": True, "warnings": ["Step limit reached"]},
                  "checks": {"grompp": {"passed": True}}, "compilation": {"status": "checked"}}
        atomic_json(self.directory / "results/validation.json", report)
        with patch.object(managed, "_final_summary", return_value={"state": "validated", "passed": True}):
            code, result, _ = self.call(["jobs", "report", "example"])
            self.assertEqual(code, 0)
            self.assertTrue(result["review_required"])
            self.assertEqual(result["acceptance"]["warnings"], ["Step limit reached"])
            self.assertEqual(self.call(["jobs", "report", "example", "--full"])[1], report)


if __name__ == "__main__":
    unittest.main()
