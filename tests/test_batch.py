"""Batch tests use synthetic inputs and mocked transport; scratch data retained."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli import batch
from charmm_gui_cli.api import AuthError, SubmissionUnknown
from charmm_gui_cli.auth import ToolError, atomic_json


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-batch-test-"))
        self.config = self.root / "batch.yaml"
        self.directory = self.root / "queue"
        self.raw = {"version": 1, "defaults": {"protein": "protein.pdb", "ligand": "ligand.sdf"},
                    "jobs": [{"name": "first"}, {"name": "second"}, {"name": "third"}]}
        self.write()

    def write(self):
        self.config.write_text(yaml.safe_dump(self.raw))

    def prepare(self, failure=None):
        def execute(args):
            self.assertTrue(args.dry_run)
            if args.out.name == failure:
                raise ToolError("Synthetic structure failed chemistry validation.")
            args.out.mkdir(parents=True)
            atomic_json(args.out / "full-run.json", {"state": "inputs_ready"})
            return {"state": "inputs_ready"}, 0
        with patch("charmm_gui_cli.build_command.execute", side_effect=execute), \
                patch("charmm_gui_cli.managed.assign_name", create=True):
            return batch.initialize(self.config, self.directory)

    def test_relative_paths_defaults_overrides_and_types(self):
        self.raw["jobs"][0]["salt_concentration"] = 0.1
        self.write()
        loaded = batch.load_batch(self.config)
        self.assertEqual(loaded["jobs"][0]["options"]["protein"], str(self.root / "protein.pdb"))
        self.assertEqual(loaded["jobs"][0]["options"]["salt_concentration"], 0.1)
        self.assertEqual(loaded["jobs"][1]["options"]["salt_concentration"], 0.15)

    def test_rejects_unknown_duplicate_and_unsafe_names(self):
        for value in ({"unknown": 1}, {"name": "first"}, {"name": "../escape"}):
            self.raw["jobs"][1] = value
            self.write()
            with self.assertRaises(ToolError):
                batch.load_batch(self.config)

    def test_rejects_duplicate_yaml_keys(self):
        self.config.write_text("version: 1\nversion: 1\njobs: []\n")
        with self.assertRaises(ToolError):
            batch.load_batch(self.config)

    def test_rejects_invalid_model_types_and_values(self):
        for key, value in (("salt_concentration", True), ("margin", 0), ("water_padding", float("nan")),
                           ("accept_conect_bond_orders", "yes"), ("salt", "MgCl2"), ("upper", "POPC=0"),
                           ("hydrogens", "guess"), ("orientation", "guess"), ("n_terminal", "bad")):
            self.raw["defaults"] = {"protein": "p.pdb", "ligand": "l.sdf", key: value}
            self.write()
            with self.subTest(key=key), self.assertRaises(ToolError):
                batch.load_batch(self.config)

    def test_all_local_preparation_finishes_despite_single_failure(self):
        result = self.prepare(failure="second")
        self.assertEqual([j["state"] for j in result["jobs"]], ["inputs_ready", "local_failed", "inputs_ready"])
        with self.assertRaisesRegex(ToolError, "already exists"):
            batch.initialize(self.config, self.directory)

    def test_default_active_limit_includes_preparation(self):
        self.prepare()
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "preparation_running"}, 0)) as execute:
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
            self.assertEqual(execute.call_count, 1)
            self.assertFalse(execute.call_args.args[0].wait)
            self.assertEqual(code, 0)
            self.assertEqual(result["jobs"][1]["state"], "inputs_ready")
            batch.advance(self.directory, wait=False, submit_interval=0)
            self.assertEqual(execute.call_count, 2)

    def test_concurrency_limited_entire_lifecycle_and_no_reprepare(self):
        self.prepare()
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "bilayer_submitted"}, 0)) as execute:
            batch.advance(self.directory, wait=False, max_active=2, submit_interval=0)
            self.assertEqual(execute.call_count, 2)
            batch.advance(self.directory, wait=False, max_active=2, submit_interval=0)
            self.assertEqual(execute.call_count, 4)
            self.assertTrue(all(call.args[0].command == "build-resume" for call in execute.call_args_list))

    def test_finished_jobs_release_slots(self):
        self.prepare()
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "validated"}, 0)) as execute, \
                patch("charmm_gui_cli.managed._final_summary", return_value={"state": "validated"}):
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
            self.assertEqual(execute.call_count, 3)
            self.assertEqual(result["state"], "complete")
            self.assertEqual(code, 0)
            batch.advance(self.directory, wait=False, submit_interval=0)
            self.assertEqual(execute.call_count, 3)

    def test_auth_failure_pauses_whole_batch(self):
        self.prepare()
        with patch("charmm_gui_cli.build_command.execute", side_effect=AuthError("Login expired")) as execute:
            result, code = batch.advance(self.directory, wait=False, max_active=4, submit_interval=0)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(result["state"], "paused_auth")
            self.assertEqual(code, 2)

    def test_uncertain_submission_preserves_active_slot(self):
        self.prepare()
        with patch("charmm_gui_cli.build_command.execute", side_effect=SubmissionUnknown("Submission uncertain")) as execute:
            result, code = batch.advance(self.directory, wait=False, max_active=4, submit_interval=0)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(result["state"], "paused_inspection")
            self.assertTrue(json.loads((self.directory / "batch.json").read_text())["jobs"][0]["started"])

    def test_terminal_failure_does_not_block_other_jobs(self):
        self.prepare(failure="second")
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "validation_failed"}, 2)) as execute:
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
            self.assertEqual(execute.call_count, 2)
            self.assertEqual(code, 2)
            self.assertEqual(result["state"], "complete")

    def test_submission_interval_survives_resume(self):
        self.prepare()
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "preparation_running"}, 0)) as execute:
            batch.advance(self.directory, wait=False, max_active=4, submit_interval=30)
            self.assertEqual(execute.call_count, 1)
            batch.advance(self.directory, wait=False, max_active=4, submit_interval=30)
            self.assertEqual(execute.call_count, 2)

    def test_source_fingerprint_detects_even_comment_changes(self):
        self.prepare()
        self.config.write_text(self.config.read_text() + "\n# change\n")
        with patch("charmm_gui_cli.build_command.execute") as execute, self.assertRaisesRegex(ToolError, "changed"):
            batch.advance(self.directory, wait=False)
        execute.assert_not_called()

    def test_manifest_config_tampering_rejected(self):
        self.prepare()
        path = self.directory / "batch.json"
        value = json.loads(path.read_text())
        value["config"]["jobs"][0]["options"]["salt"] = "NaCl"
        atomic_json(path, value)
        with self.assertRaisesRegex(ToolError, "fingerprint"):
            batch.advance(self.directory, wait=False)

    def test_status_is_read_only_and_csv_formula_safe(self):
        self.prepare()
        before = (self.directory / "batch.json").read_bytes()
        reports_before = {name: (self.directory / name).read_bytes() for name in ("summary.json", "summary.csv")}
        with patch("charmm_gui_cli.build_command.execute") as execute:
            result = batch.status(self.directory)
        execute.assert_not_called()
        self.assertEqual(before, (self.directory / "batch.json").read_bytes())
        for name, content in reports_before.items():
            self.assertEqual(content, (self.directory / name).read_bytes())
        self.assertTrue(result["jobs"][0]["named_job"].startswith("batch-"))
        self.assertEqual(result["reports"]["csv"], str(self.directory / "summary.csv"))
        result["jobs"][0]["error"] = "=BAD()"
        rows = list(csv.DictReader(io.StringIO(batch.export_csv(result))))
        self.assertEqual(rows[0]["error"], "'=BAD()")

    def test_remote_status_only_get_and_does_not_save(self):
        self.prepare()
        full = self.directory / "jobs" / "first" / "full-run.json"
        atomic_json(full, {"state": "preparation_running", "source_job_id": "123"})
        before = (self.directory / "batch.json").read_bytes()
        with patch("charmm_gui_cli.auth.load_token", return_value=("fake", "fake")), \
                patch("charmm_gui_cli.api.Client") as client:
            client.return_value.status.return_value = {"status": None, "hasTarFile": False, "private": "omit"}
            result = batch.status(self.directory, remote_check=True)
            client.return_value.status.assert_called_once_with("123")
            self.assertNotIn("private", result["jobs"][0]["remote"])
        self.assertEqual(before, (self.directory / "batch.json").read_bytes())

    def test_limits_checked_before_auth(self):
        self.prepare()
        for options in ({"max_active": 5}, {"max_active": True}, {"submit_interval": -1}, {"interval": 1}):
            with self.subTest(options=options), self.assertRaises(ToolError):
                batch.advance(self.directory, **options)

    def test_lock_prevents_second_scheduler(self):
        self.prepare()
        with batch.run_lock(self.directory), self.assertRaisesRegex(ToolError, "Another process"):
            batch.advance(self.directory, wait=False)

    def test_interrupted_preparation_never_recreates_existing_run(self):
        self.prepare()
        path = self.directory / "batch.json"
        value = json.loads(path.read_text())
        value["jobs"][0]["state"] = "preparing_local"
        atomic_json(path, value)
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "validated"}, 0)) as execute:
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result["jobs"][0]["state"], "local_failed")
        self.assertEqual(code, 2)

    def test_local_chemistry_preparation_never_authenticates(self):
        try:
            import rdkit  # noqa: F401
        except ImportError:
            self.skipTest("RDKit required for real synthetic preparation")
        from charmm_gui_cli.inputs import Atom, _sdf
        (self.root / "protein.pdb").write_text("ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\nEND\n")
        (self.root / "ligand.sdf").write_text(_sdf([Atom(1, "C1", "C", (3., 2., 1.)),
                                                   Atom(2, "O1", "O", (4.3, 2., 1.))], {(0, 1): 1}, "LIG"))
        with patch("charmm_gui_cli.managed._registry", return_value=self.root / "registry"), \
                patch("charmm_gui_cli.build_command.load_token", side_effect=AssertionError("No auth")), \
                patch("requests.Session.request", side_effect=AssertionError("No network")):
            result = batch.initialize(self.config, self.directory)
        self.assertEqual(result["counts"], {"inputs_ready": 3})

    def test_complete_interrupted_preparation_is_adopted_without_creation(self):
        self.prepare()
        path = self.directory / "batch.json"
        value = json.loads(path.read_text())
        value["jobs"][0]["state"] = "preparing_local"
        atomic_json(path, value)
        output = self.directory / "jobs" / "first"
        (output / "inputs").mkdir()
        files = {}
        for name in ("protein.pdb", "ligand.pdb", "ligand.sdf", "complex.pdb", "input-manifest.json"):
            file = output / "inputs" / name
            file.write_text("synthetic")
            files[name] = str(file)
        atomic_json(output / "full-run.json", {"schema_version": 2, "state": "inputs_ready", "inputs": {"files": files}})
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "validated"}, 0)) as execute, \
                patch("charmm_gui_cli.managed.assign_name") as assign:
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(execute.call_count, 3)
        self.assertTrue(all(call.args[0].command == "build-resume" for call in execute.call_args_list))
        assign.assert_called_once()
        self.assertEqual(result["validated"], 3)

    def test_retryable_get_exhaustion_keeps_slot_and_can_resume(self):
        self.prepare()
        with patch("charmm_gui_cli.build_command.execute", side_effect=ToolError("GET exhausted", category="transient_network", retryable=True)):
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(result["state"], "waiting_network")
        self.assertEqual(result["jobs"][0]["state"], "waiting_network")
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "preparation_running"}, 0)) as execute:
            batch.advance(self.directory, wait=False, submit_interval=0)
            self.assertEqual(execute.call_count, 1)

    def test_unknown_submission_requires_attach_before_resume(self):
        self.prepare()
        with patch("charmm_gui_cli.build_command.execute", side_effect=SubmissionUnknown("Uncertain")):
            batch.advance(self.directory, wait=False, submit_interval=0)
        with patch("charmm_gui_cli.build_command.execute") as execute:
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
            execute.assert_not_called()
            self.assertEqual(result["state"], "paused_inspection")
        bilayer = self.directory / "jobs" / "first" / "bilayer"
        bilayer.mkdir()
        atomic_json(bilayer / "run.json", {"state": "submitted", "job_id": "12345"})
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "running"}, 0)) as execute:
            batch.advance(self.directory, wait=False, submit_interval=0)
            self.assertEqual(execute.call_count, 1)

    def test_wrong_manifest_types_give_safe_errors(self):
        self.prepare()
        path = self.directory / "batch.json"
        original = json.loads(path.read_text())
        for damaged in ([], {**original, "jobs": "bad"}, {**original, "jobs": [None]},
                        {**original, "config": []}, {**original, "jobs": [{"name": "first", "directory": []}]}):
            atomic_json(path, damaged)
            with self.subTest(damaged=damaged), self.assertRaises(ToolError):
                batch.status(self.directory)

    def test_required_gromacs_checked_before_any_remote_progress(self):
        self.prepare()
        with patch("charmm_gui_cli.batch.shutil.which", return_value=None), \
                patch("charmm_gui_cli.build_command.execute") as execute, self.assertRaisesRegex(ToolError, "GROMACS"):
            batch.advance(self.directory, grompp=True)
        execute.assert_not_called()

    def test_retry_after_persisted_and_resume_cannot_retry_early(self):
        self.prepare()
        error = ToolError("Backoff required", category="rate_limited", retryable=True)
        error.retry_after_seconds = 600
        with patch("charmm_gui_cli.build_command.execute", side_effect=error), patch("charmm_gui_cli.batch.time.time", return_value=1000):
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(result["jobs"][0]["next_retry_unix"], 1600)
        with patch("charmm_gui_cli.build_command.execute") as execute, patch("charmm_gui_cli.batch.time.time", return_value=1200):
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
        execute.assert_not_called()
        self.assertEqual(result["state"], "waiting_network")
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "running"}, 0)) as execute, patch("charmm_gui_cli.batch.time.time", return_value=1601):
            batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(execute.call_count, 1)

    def test_independently_started_later_pending_job_occupies_slot(self):
        self.prepare()
        later = self.directory / "jobs" / "third"
        atomic_json(later / "full-run.json", {"state": "bilayer_submitted", "source_job_id": "123"})
        (later / "bilayer").mkdir()
        atomic_json(later / "bilayer" / "run.json", {"state": "pending", "job_id": "456"})
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "pending"}, 0)) as execute:
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execute.call_args.args[0].directory, later)
        self.assertEqual(result["jobs"][0]["state"], "inputs_ready")

    def test_upload_intent_occupies_slot_even_with_pending_display(self):
        self.prepare()
        later = self.directory / "jobs" / "third"
        atomic_json(later / "full-run.json", {"state": "pending"})
        (later / "preparation").mkdir()
        atomic_json(later / "preparation" / "web-run.json", {"state": "upload_submitting"})
        with patch("charmm_gui_cli.build_command.execute", return_value=({"state": "preparation_running"}, 0)) as execute:
            batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execute.call_args.args[0].directory, later)

    def test_final_status_requires_current_evidence_and_requeues_stale(self):
        self.prepare()
        path = self.directory / "batch.json"
        value = json.loads(path.read_text())
        value["jobs"][0].update(state="validated", started=True)
        value["jobs"][1].update(state="validation_failed", started=True)
        atomic_json(path, value)
        with patch("charmm_gui_cli.managed._final_summary", return_value={"validation_status": "stale_or_unreadable"}) as verify:
            result = batch.status(self.directory)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(result["jobs"][0]["state"], "validation_stale")
        self.assertEqual(result["jobs"][1]["state"], "validation_stale")
        self.assertEqual(result["validated"], 0)
        with patch("charmm_gui_cli.managed._final_summary", return_value={"validation_status": "stale_or_unreadable"}), \
                patch("charmm_gui_cli.build_command.execute", return_value=({"state": "validated"}, 0)) as execute:
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(result["validated"], 3)

    def test_current_evidence_preserves_failed_validation_terminal(self):
        self.prepare()
        path = self.directory / "batch.json"
        value = json.loads(path.read_text())
        value["jobs"][0].update(state="validation_failed", started=True)
        atomic_json(path, value)
        with patch("charmm_gui_cli.managed._final_summary", return_value={"state": "validation_failed"}):
            result = batch.status(self.directory)
        self.assertEqual(result["failed"], 1)

    def test_post_auth_failure_pauses_but_retains_uncertain_intent(self):
        self.prepare()
        error = SubmissionUnknown("POST outcome unknown")
        error.cause_category = "authentication"
        with patch("charmm_gui_cli.build_command.execute", side_effect=error) as execute:
            result, code = batch.advance(self.directory, wait=False, max_active=4, submit_interval=0)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(result["state"], "paused_auth")
        self.assertEqual(result["jobs"][0]["error_category"], "submission_unknown")
        self.assertEqual(result["jobs"][0]["cause_category"], "authentication")
        with patch("charmm_gui_cli.build_command.execute") as execute:
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
        execute.assert_not_called()
        self.assertEqual(result["state"], "paused_inspection")

    def test_summary_keeps_compilation_review_and_artifact_paths(self):
        self.prepare()
        result = {"state": "validated", "gromacs_compiled": False, "gromacs_note": "Not run",
                  "validation": {"passed": True, "review_required": True}, "report": "/tmp/report.json", "archive": "/tmp/system.tgz"}
        with patch("charmm_gui_cli.build_command.execute", return_value=(result, 0)):
            summary, code = batch.advance(self.directory, wait=False, submit_interval=0)
        row = summary["jobs"][0]
        self.assertEqual(row["compilation_status"], "not_run")
        self.assertTrue(row["review_required"])
        self.assertEqual(row["report"], "/tmp/report.json")
        rows = list(csv.DictReader(io.StringIO(batch.export_csv(summary))))
        self.assertEqual(rows[0]["compilation_status"], "not_run")
        self.assertEqual(rows[0]["gromacs_compiled"], "False")


if __name__ == "__main__":
    unittest.main()
