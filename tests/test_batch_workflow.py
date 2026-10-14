"""Public batch/sweep plans, nested configuration, retries and terminal logs."""

import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli.pipeline import batch
from charmm_gui_cli.pipeline.batch_progress import BatchReporter
from charmm_gui_cli.pipeline import build_command
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.paths import read_value
from charmm_gui_cli.locales.i18n import set_language
import test_input_policy as fixtures


class BatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InputPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.path = self.root / "queue.yaml"
        self.directory = self.root / "queue"
        self.raw = {"version": 2, "defaults": {
            "protein": str(self.fixture.protein), "ligand": str(self.fixture.ligand), "ligand_hydrogens": "preserve",
            "preparation": {"terminals": {"A": {"n_terminal": "NTER", "c_terminal": "CTER"}}},
            "membrane": {"upper": "POPC:POPE=7:3", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
            "ions": {"type": "KCl", "concentration_M": .15}},
            "jobs": [{"name": "first"}, {"name": "second", "ions": {"concentration_M": .10}}]}
        self.write()

    def write(self):
        self.path.write_text(yaml.safe_dump(self.raw))

    def plan(self, **kwargs):
        args = ["batch", str(self.path), "--out", str(self.directory), "--dry-run"]
        return self.fixture.call(args, **kwargs)

    def test_nested_v2_matches_build_prepared_bytes_and_terminal_identity(self):
        code, output, error = self.plan()
        self.assertEqual(code, 0, error)
        loaded = batch.load_batch(self.path)
        self.assertEqual(loaded["version"], 2)
        self.assertEqual(loaded["jobs"][1]["options"]["salt_concentration"], .10)
        direct = self.root / "system.yaml"
        direct.write_text(yaml.safe_dump({"version": 2, **self.raw["defaults"]}))
        target = self.root / "baseline"
        code, _, error = self.fixture.call(["build", str(direct), "--out", str(target), "--dry-run"])
        self.assertEqual(code, 0, error)
        child = self.directory / "jobs/first"
        for name in ("protein.pdb", "ligand.sdf", "complex.pdb"):
            self.assertEqual((child / "inputs" / name).read_bytes(), (target / "inputs" / name).read_bytes())
        prepared = read_value(child / ".charmm-gui/full-run.json")
        self.assertEqual(prepared["config"]["preparation"]["terminals"], self.raw["defaults"]["preparation"]["terminals"])

    def test_nested_defaults_merge_per_chain_and_do_not_accept_flat_v1_fields(self):
        self.raw["jobs"][0]["preparation"] = {"terminals": {"A": {"c_terminal": "CT2"}}}
        self.write()
        options = batch.load_batch(self.path)["jobs"][0]["options"]
        self.assertEqual(options["terminals"], {"A": {"n_terminal": "NTER", "c_terminal": "CT2"}})
        self.raw["jobs"][0]["salt_concentration"] = .2
        self.write()
        with self.assertRaises(ToolError):
            batch.load_batch(self.path)

    def test_v2_implicit_hydrogen_policy_matches_build_yaml_not_v1_default(self):
        from charmm_gui_cli.pipeline.build_config import load_build_config
        self.raw["defaults"].pop("ligand_hydrogens")
        self.write()
        single = self.root / "single.yaml"
        single.write_text(yaml.safe_dump({"version": 2, **self.raw["defaults"]}))
        self.assertEqual(batch.load_batch(self.path)["jobs"][0]["options"]["hydrogens"], load_build_config(single)["ligand_hydrogens"])

    def test_dry_batch_and_sweep_leave_no_global_index_or_latest_run(self):
        for command in ("batch", "sweep"):
            if command == "sweep":
                self.path.write_text(yaml.safe_dump({"version": 1, "inputs": {
                    "protein": str(self.fixture.protein), "ligand": str(self.fixture.ligand)},
                    "axes": {"salt_concentration": [.1, .2]}}))
            out = self.root / command
            code, _, error = self.fixture.call([command, str(self.path), "--out", str(out), "--dry-run"])
            self.assertEqual(code, 0, error)
            self.assertFalse((self.root / "index.json").exists())
            code, output, error = self.fixture.call(["jobs", "list"])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["jobs"], [])

    def test_launch_registers_planned_children_only_once_without_repreparing(self):
        self.assertEqual(self.plan()[0], 0)
        original_inputs = (self.directory / "jobs/first/inputs/complex.pdb").read_bytes()
        with patch.object(build_command, "execute", return_value=({"state": "running"}, 0)) as execute:
            batch.advance(self.directory, wait=False, submit_interval=0)
            self.assertTrue(all(call.args[0].command == "build-resume" for call in execute.call_args_list))
        names = list((self.root / "index.json/names").glob("*.json"))
        self.assertEqual(len(names), 2)
        histories = list((self.root / "index.json/run-history").glob("*.json"))
        with patch.object(build_command, "execute", return_value=({"state": "running"}, 0)):
            batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(len(list((self.root / "index.json/run-history").glob("*.json"))), len(histories))
        self.assertEqual((self.directory / "jobs/first/inputs/complex.pdb").read_bytes(), original_inputs)

    def test_actual_local_failure_can_be_fixed_and_explicitly_retried(self):
        original = self.fixture.protein.read_bytes()
        self.fixture.protein.write_text("ATOM      1  CA  UNK A   1       0.000   0.000   0.000  1.00  0.00           C\nEND\n")
        code, output, error = self.plan()
        self.assertEqual(code, 2, output + error)
        self.assertEqual(batch.status(self.directory)["failed"], 2)
        self.fixture.protein.write_bytes(original)
        real = build_command.execute
        def execute(args):
            return real(args) if args.command == "build" else ({"state": "running"}, 0)
        with patch.object(build_command, "execute", side_effect=execute):
            result, code = batch.advance(self.directory, wait=False, submit_interval=0, retry_failed_local=True)
        self.assertEqual(code, 0)
        self.assertEqual(result["failed"], 0)
        manifest = json.loads((self.directory / "batch.json").read_text())
        self.assertEqual(len(manifest["jobs"][0]["local_attempts"]), 1)
        self.assertTrue((self.directory / "jobs/first/inputs/protein.pdb").is_file())

    def test_local_retry_cannot_replay_unknown_post_or_remove_partial_files(self):
        self.assertEqual(self.plan()[0], 0)
        manifest_path = self.directory / "batch.json"
        original = json.loads(manifest_path.read_text())
        for state in ("unknown", "partial"):
            manifest = json.loads(json.dumps(original))
            row = manifest["jobs"][0]
            row["state"] = "local_failed"
            if state == "unknown":
                row["error_category"] = "submission_unknown"
            atomic_json(manifest_path, manifest)
            with patch.object(build_command, "execute") as execute, self.assertRaises(ToolError) as error:
                batch.advance(self.directory, wait=False, retry_failed_local=True)
            self.assertEqual(error.exception.key, "batch.retry_remote" if state == "unknown" else "batch.retry_partial")
            execute.assert_not_called()
            self.assertTrue((self.directory / "jobs/first/inputs/protein.pdb").is_file())

    def test_readonly_tty_summaries_and_forced_json_are_bilingual_and_byte_stable(self):
        self.assertEqual(self.plan()[0], 0)
        report = batch.status(self.directory)
        for lang, title in (("zh", "批次："), ("en", "Queue:")):
            with patch.object(batch, "status", return_value=report):
                code, output, error = self.fixture.call(["batch-status", str(self.directory)], tty=True, lang=lang)
                self.assertEqual(code, 0, error)
                self.assertIn(title, output)
                self.assertNotIn('"jobs":', output)
                code, output, _ = self.fixture.call(["batch-status", str(self.directory), "--json"], tty=True, lang=lang)
                self.assertEqual(output, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
                code, output, _ = self.fixture.call(["batch-status", str(self.directory)], lang=lang)
                self.assertEqual(output, json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    def test_sweep_and_preview_use_human_summaries_but_keep_json(self):
        config = self.root / "scan.yaml"
        config.write_text(yaml.safe_dump({"version": 1, "inputs": {"protein": str(self.fixture.protein), "ligand": str(self.fixture.ligand)}, "axes": {"salt_concentration": [.1, .2]}}))
        for lang in ("en", "zh"):
            directory = self.root / lang
            code, output, error = self.fixture.call(["sweep", str(config), "--out", str(directory), "--dry-run"], tty=True, lang=lang)
            self.assertEqual(code, 0, error)
            self.assertIn("scan-0001", output)
            code, output, error = self.fixture.call(["sweep-status", str(directory)], tty=True, lang=lang)
            self.assertEqual(code, 0, error)
            self.assertNotIn('"jobs":', output)
        code, output, error = self.fixture.call(["preview", str(self.fixture.protein), "--out", str(self.root / "pictures"), "--view", "side"], tty=True, lang="en")
        self.assertEqual(code, 0, error)
        self.assertIn("View side:", output)

    def test_progress_deduplicates_child_ids_and_uses_plain_log_records(self):
        set_language("en")
        self.assertEqual(self.plan()[0], 0)
        summary = batch.status(self.directory)
        stream = fixtures.Terminal()
        reporter = BatchReporter(self.directory, stream=stream)
        self.addCleanup(reporter.close)
        for _ in range(20):
            reporter.update(summary)
            reporter.child(summary["jobs"][0])({"state": "running ffconverter", "source_job_id": "123", "build_job_id": "456"})
        reporter.close()
        raw = (self.directory / "run.log").read_bytes()
        log = raw.decode()
        self.assertNotIn(b"\r", raw)
        self.assertNotIn(b"\x1b", raw)
        self.assertEqual(log.count("jobid=123"), 1)
        self.assertEqual(log.count("jobid=456"), 1)
        from charmm_gui_cli.presentation.progress import format_progress
        expected = format_progress({"state": "running ffconverter", "source_job_id": "123", "build_job_id": "456"})
        self.assertEqual(log.count(expected), 1)
        self.assertNotIn("run=", stream.getvalue())

    def test_named_directories_and_interruption_recommend_the_right_resume(self):
        for lang in ("en", "zh"):
            name = "named-" + lang
            code, output, error = self.fixture.call(["batch", str(self.path), "--name", name, "--dry-run"], tty=True, lang=lang)
            self.assertEqual(code, 0, error)
            self.assertTrue((self.root / name / "batch.json").is_file())
            with patch.object(batch, "advance", side_effect=KeyboardInterrupt):
                code, output, error = self.fixture.call(["batch-resume", str(self.root / name)], lang=lang)
            self.assertEqual(code, 130)
            self.assertIn("batch-resume", error)
            self.assertNotIn("build-resume", error)

    def test_completed_elapsed_time_is_frozen_even_when_resumed_again(self):
        self.assertEqual(self.plan()[0], 0)
        with patch.object(build_command, "execute", return_value=({"state": "validated"}, 0)), \
                patch("charmm_gui_cli.store.managed._final_summary", return_value={"state": "validated"}):
            result, code = batch.advance(self.directory, wait=False, submit_interval=0)
        self.assertEqual(result["completed"], 2)
        old = json.loads((self.directory / "batch.json").read_text())["finished_at"]
        with patch.object(build_command, "execute") as execute, \
                patch("charmm_gui_cli.store.managed._final_summary", return_value={"state": "validated"}):
            repeated, code = batch.advance(self.directory, wait=False, submit_interval=0)
            execute.assert_not_called()
        self.assertEqual(json.loads((self.directory / "batch.json").read_text())["finished_at"], old)
        self.assertEqual(result["elapsed_seconds"], repeated["elapsed_seconds"])

    def test_actual_build_poll_rounds_share_progress_without_repeated_run_banners(self):
        self.assertEqual(self.plan()[0], 0)
        cookies = self.root / "cookies.json"
        cookies.write_text("[]")
        clock = [10000.]
        stream = io.StringIO()
        reporter = BatchReporter(self.directory, stream=stream)
        self.addCleanup(reporter.close)
        def advance(directory, web, api, **kwargs):
            manifest = read_value(directory / "full-run.json")
            manifest.update(state="bilayer_submitted", source_job_id="123", build_job_id="456")
            atomic_json(directory / "full-run.json", manifest)
            (directory / "bilayer").mkdir(exist_ok=True)
            atomic_json(directory / "bilayer/run.json", {"state": "running ffconverter", "job_id": "456"})
            kwargs["progress"]({"state": "running ffconverter", "source_job_id": "123", "build_job_id": "456"})
            return manifest
        with patch.object(build_command, "load_token", return_value=("fixture", "CHARMMGUI_TOKEN")), \
                patch.object(build_command, "Client"), patch("charmm_gui_cli.remote.web.WebClient"), \
                patch("charmm_gui_cli.pipeline.full_build.advance", side_effect=advance) as remote, \
                patch("time.monotonic", side_effect=lambda: clock[0]), patch("time.time", side_effect=lambda: clock[0]), \
                patch("time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            batch.advance(self.directory, cookies=cookies, wait=True, submit_interval=0, interval=10, max_wait=35, progress=reporter)
        self.assertGreaterEqual(remote.call_count, 3)
        self.assertNotIn("run=", stream.getvalue())
        self.assertEqual(stream.getvalue().count("jobid=123"), 1)
        self.assertEqual(stream.getvalue().count("jobid=456"), 1)
        raw = (self.directory / "jobs/first/run.log").read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertNotIn(b"\x1b", raw)

    def test_batch_v1_defaults_remain_identical_and_yaml_name_is_respected(self):
        self.raw = {"version": 1, "defaults": {"protein": str(self.fixture.protein), "ligand": str(self.fixture.ligand)}, "jobs": [{"name": "first"}]}
        self.write()
        loaded = batch.load_batch(self.path)
        self.assertEqual(loaded["jobs"][0]["options"], {**batch.DEFAULTS, **self.raw["defaults"]})
        self.raw["name"] = "from-yaml"
        self.write()
        code, output, error = self.fixture.call(["batch", str(self.path), "--dry-run"])
        self.assertEqual(code, 0, error)
        self.assertTrue((self.root / "from-yaml/batch.json").is_file())

    def test_real_local_name_lock_is_deferred_and_complete_inputs_are_adopted(self):
        from charmm_gui_cli.store import managed
        from charmm_gui_cli.pipeline.workflow import run_lock
        assign = managed.assign_name
        calls = []
        def contended(directory, name):
            if not calls:
                calls.append(name)
                with run_lock(Path(directory)):
                    return assign(directory, name)
            return assign(directory, name)
        with patch.object(managed, "assign_name", side_effect=contended):
            code, output, error = self.plan()
        self.assertEqual(code, 0, error)
        report = batch.status(self.directory)
        self.assertEqual(report["jobs"][0]["state"], "preparing_local")
        self.assertEqual(report["failed"], 0)
        inputs = (self.directory / "jobs/first/inputs/complex.pdb").read_bytes()
        due = json.loads((self.directory / "batch.json").read_text())["jobs"][0]["next_retry_unix"]
        with patch.object(build_command, "execute", return_value=({"state": "running"}, 0)) as execute, patch("time.time", return_value=due + 1):
            report, code = batch.advance(self.directory, wait=False, submit_interval=0, max_active=2)
        self.assertEqual(code, 0)
        self.assertTrue(all(call.args[0].command == "build-resume" for call in execute.call_args_list))
        self.assertEqual((self.directory / "jobs/first/inputs/complex.pdb").read_bytes(), inputs)
        self.assertNotIn("error_category", report["jobs"][0])
        self.assertEqual(len(list((self.root / "index.json/names").glob("*.json"))), 2)

    def test_legacy_sweep_child_names_are_preserved(self):
        directory = self.root / "legacy/batch"
        child = directory / "jobs/scan-0001"
        child.mkdir(parents=True)
        atomic_json(directory.parent / "sweep.json", {"batch_directory": str(directory)})
        atomic_json(child / "job.json", {"name": "batch-existing-identity-scan-0001"})
        self.assertEqual(batch._named_job(directory, "scan-0001"), "batch-existing-identity-scan-0001")

    def test_log_symlinks_cannot_redirect_queue_records_into_other_files(self):
        self.assertEqual(self.plan()[0], 0)
        foreign = self.root / "foreign-log.txt"
        foreign.write_text("keep this file unchanged")
        directory = self.root / "other-queue"
        directory.mkdir()
        (directory / "run.log").symlink_to(foreign)
        reporter = BatchReporter(directory, stream=io.StringIO())
        self.addCleanup(reporter.close)
        with self.assertRaises(ToolError) as error:
            reporter.update(batch.status(self.directory))
        self.assertEqual(error.exception.key, "batch.log_unwritable")
        self.assertEqual(foreign.read_text(), "keep this file unchanged")


if __name__ == "__main__":
    unittest.main()
