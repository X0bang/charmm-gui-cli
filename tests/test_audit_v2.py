"""Public command and real file-lock regressions from audit v2."""

from contextlib import redirect_stdout, redirect_stderr
import importlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import yaml

from charmm_gui_cli.cli import main, parser
from charmm_gui_cli.pipeline import batch, full_build, sweep
from charmm_gui_cli.store.auth import ToolError, atomic_json, atomic_write
from charmm_gui_cli.pipeline.workflow import run_lock
import test_sweep as fixtures


class AuditV2Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SweepTests()
        self.fixture.setUp()
        self.root = self.fixture.root

    def test_every_registered_command_has_execute(self):
        choices = next(action.choices for action in parser()._actions
                       if getattr(action, "choices", None) and isinstance(action.choices, dict))
        for name in choices:
            with self.subTest(command=name):
                module = importlib.import_module("charmm_gui_cli.commands." + name.replace("-", "_"))
                self.assertTrue(callable(getattr(module, "execute", None)))

    def test_doctor_cli_dispatch_in_both_languages_without_network(self):
        for lang in ("en", "zh"):
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error), \
                    patch("requests.Session.request", side_effect=AssertionError("No remote calls")):
                code = main(["doctor", "--lang", lang, "--json"])
            self.assertIn(code, (0, 2))
            report = json.loads(output.getvalue())
            self.assertIn("python", report["checks"])
            self.assertNotIn("Traceback", error.getvalue())

    def test_terminals_seal_and_real_upload_flow(self):
        directory = self.fixture.make_run("chain-terminals", seal=False)
        manifest = json.loads((directory / "full-run.json").read_text())
        manifest["config"]["preparation"]["terminals"] = {"A": {"n_terminal": "GLYP"}, "B": {"n_terminal": "NTER"}}
        atomic_json(directory / "full-run.json", manifest)
        identity = sweep.preparation_identity(directory)
        self.assertEqual(identity["settings"]["terminals"], {"A": {"n_terminal": "GLYP"}, "B": {"n_terminal": "NTER"}})
        web, api = Mock(), Mock()
        api.submit.return_value = "456"

        def upload(pdb, destination):
            self.assertTrue((directory / "preparation-seal.json").is_file())
            Path(destination).mkdir()
            atomic_write(Path(destination) / "upload-response.html", fixtures.UPLOAD.replace("</form>",
                         '<input type="checkbox" name="chains[PROB][checked]" value="1"></form>'))
            atomic_json(Path(destination) / "web-run.json", {"state": "upload_response_received",
                        "job_id": "123", "input_pdb": str(directory / "inputs/complex.pdb")})

        def submit(source, destination, fields, uploads=None):
            form = fixtures.soup_for(Path(source).read_text()).find("form")
            posted = [[key, value] for key, value in fixtures.form_data(form)
                      if key not in fields and key not in (uploads or {})]
            posted.extend([key, value] for key, value in fields.items())
            atomic_json(Path(destination).with_suffix(".intent.json"), {"state": "response_received",
                        "target": fixtures.modeling_target(form.get("action")), "fields": posted,
                        "upload_fields": list(uploads or {})})
            if Path(destination).name == "selection.html":
                atomic_write(destination, fixtures.SELECTION.replace("NTER", "GLYP").replace("</form>",
                             '<select name="terminal[PROB][first]"><option value="NTER">NTER</option></select>'
                             '<select name="terminal[PROB][last]"><option value="CTER">CTER</option></select></form>'))
            else:
                self.assertEqual(fields["terminal[PROA][first]"], "GLYP")
                self.assertEqual(fields["terminal[PROB][first]"], "NTER")
                atomic_write(destination, fixtures.READY)

        web.upload_structure.side_effect = upload
        web.submit_snapshot.side_effect = submit
        result = full_build.advance(directory, web, api)
        self.assertEqual(result["state"], "bilayer_submitted")
        web.upload_structure.assert_called_once()
        self.assertEqual(web.submit_snapshot.call_count, 2)
        api.submit.assert_called_once()
        sweep.certify_source(directory)

    def test_sealed_chain_patch_changes_are_refused(self):
        directory = self.fixture.make_run("identity", seal=False)
        path = directory / "full-run.json"
        manifest = json.loads(path.read_text())
        manifest["config"]["preparation"]["terminals"] = {"A": {"n_terminal": "GLYP"}}
        atomic_json(path, manifest)
        sweep.seal_preparation(directory)
        manifest["config"]["preparation"]["terminals"]["A"]["n_terminal"] = "ACE"
        atomic_json(path, manifest)
        with self.assertRaises(ToolError):
            sweep.seal_preparation(directory)

    def prepare_scan(self):
        template = self.fixture.make_run("template")
        self.fixture.raw["inputs"] = {"protein": str(template / "inputs/protein.pdb"),
                                      "ligand": str(template / "inputs/ligand.sdf")}
        self.fixture.raw["axes"] = {"margin": [20, 25]}
        self.fixture.write()
        with patch("charmm_gui_cli.store.managed._registry", return_value=self.root / "registry"):
            result = sweep.prepare(self.fixture.path, self.root / "scan")
        return result, Path(result["batch_directory"])

    def test_actual_lock_contention_retries_same_leader_then_reuses(self):
        prepared, queue = self.prepare_scan()
        leader = Path(prepared["leader"])

        def advance_under_lock(args):
            with run_lock(args.directory):
                return {"state": "preparation_running"}, 0

        with run_lock(leader), patch("charmm_gui_cli.pipeline.build_command.execute", side_effect=advance_under_lock):
            summary, code = batch.advance(queue, wait=False, submit_interval=0)
        self.assertEqual(summary["jobs"][0]["state"], "waiting_lock")
        self.assertEqual(summary["failed"], 0)
        self.assertFalse((leader / "preparation").exists())
        manifest = json.loads((queue / "batch.json").read_text())
        manifest["jobs"][0]["next_retry_unix"] = 0
        atomic_json(queue / "batch.json", manifest)
        with patch("charmm_gui_cli.pipeline.build_command.execute", side_effect=advance_under_lock) as advance:
            summary, code = batch.advance(queue, wait=False, submit_interval=0)
        self.assertEqual(advance.call_count, 1)
        self.assertEqual(Path(advance.call_args.args[0].directory), leader)
        self.fixture.complete_source(leader)
        with patch("charmm_gui_cli.pipeline.build_command.execute", return_value=({"state": "running"}, 0)):
            summary, code = batch.advance(queue, wait=False, submit_interval=0, max_active=2)
        child = Path(summary["jobs"][1]["directory"])
        self.assertIn("reused_preparation", json.loads((child / "full-run.json").read_text()))

    def test_source_lock_delays_binding_without_upload_fallback(self):
        source, child = self.fixture.make_run("source"), self.fixture.make_run("child")
        self.fixture.complete_source(source)
        rows = [{"name": path.name, "directory": str(path), "state": "inputs_ready", "started": False}
                for path in (source, child)]
        manifest = {"config": {"reuse_preparation": True}, "jobs": rows}
        with run_lock(source):
            result = sweep.bind_ready_sources(self.root, manifest)
        self.assertEqual(result["waiting"], 1)
        self.assertTrue(rows[1]["reuse_waiting"])
        self.assertFalse((child / "preparation").exists())
        self.assertEqual(sweep.bind_ready_sources(self.root, manifest)["bound"], 1)

    def test_legacy_lock_failed_row_is_not_permanently_skipped(self):
        directory = self.fixture.make_run("old-lock")
        row = {"directory": str(directory), "state": "local_failed", "started": False,
               "error": "Another process is operating on this run."}
        batch._local(row)
        self.assertEqual(row["state"], "inputs_ready")

    def test_failed_leader_elects_unattempted_successor(self):
        source, child = self.fixture.make_run("failed-source"), self.fixture.make_run("successor")
        rows = [{"directory": str(source), "state": "preparation_failed", "started": True},
                {"directory": str(child), "state": "waiting_preparation", "started": False}]
        result = sweep.bind_ready_sources(self.root, {"config": {"reuse_preparation": True}, "jobs": rows})
        self.assertFalse(rows[1]["reuse_waiting"])
        self.assertEqual(result["waiting"], 0)

    def test_sweep_guidance_uses_original_scan_not_nested_batch(self):
        from charmm_gui_cli.locales.i18n import set_language
        prepared, queue = self.prepare_scan()
        for language in ("en", "zh"):
            set_language(language)
            manifest = json.loads((queue / "batch.json").read_text())
            for state in ("running", "paused_auth", "waiting_network", "interrupted"):
                manifest["state"] = state
                next_step = batch._summary(queue, manifest)["next_step"]
                self.assertIn("sweep-resume", next_step)
                self.assertNotIn("batch-resume", next_step)
                if language == "en":
                    self.assertFalse(any("\u4e00" <= character <= "\u9fff" for character in next_step))

    def test_job_list_terminal_outcomes_and_duration(self):
        from charmm_gui_cli.presentation.terminal_output import format_jobs
        from charmm_gui_cli.locales.i18n import set_language
        set_language("en")
        jobs = [{"name": name, "directory": str(self.root / name), "state": state,
                 "execution_state": execution, "passed": passed,
                 "background": {"elapsed_seconds": 1367}}
                for name, state, execution, passed in (
                    ("success", "validated", None, True),
                    ("remote", "bilayer_submitted", "failed_remote", None),
                    ("local", "inputs_ready", "failed_local", None),
                    ("interrupt", "running", "interrupted", None))]
        output = format_jobs({"jobs": jobs})
        for expected in ("success | passed", "remote | failed-remote", "local | failed-local", "interrupt | interrupted", "0:22:47"):
            self.assertIn(expected, output)

    def test_final_status_omits_obsolete_remote_stage(self):
        from charmm_gui_cli.presentation.terminal_output import format_build
        from charmm_gui_cli.locales.i18n import set_language
        set_language("en")
        output = format_build({"state": "validated", "passed": True, "remote_status": "archive_preparing"})
        self.assertNotIn("Remote state:", output)
        self.assertIn("passed", output)

    def test_login_eof_has_login_guidance_not_build_recovery(self):
        output = io.StringIO()
        with redirect_stderr(output), patch("charmm_gui_cli.cli.dispatch", side_effect=EOFError):
            self.assertEqual(main(["login", "--lang", "en"]), 130)
        self.assertIn("charmm-gui-cli login", output.getvalue())
        self.assertNotIn("build-resume", output.getvalue())

    def test_c_terminal_help_and_invalid_patch_name_the_correct_end(self):
        from charmm_gui_cli.pipeline.build_config import validate_preparation
        from charmm_gui_cli.locales.i18n import set_language
        set_language("en")
        with self.assertRaises(ToolError) as caught:
            validate_preparation({"c_terminal": "GLYP"})
        self.assertIn("C-terminal", caught.exception.localized())
        self.assertIn("CTER", caught.exception.localized())
        self.assertNotIn("NTER", caught.exception.localized())
