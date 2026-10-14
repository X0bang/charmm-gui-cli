"""Pure/local progress presentation tests: no network, MD, or artifact mutation."""

from argparse import Namespace
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from charmm_gui_cli.pipeline import build_command
from charmm_gui_cli.store import managed
from charmm_gui_cli.locales.i18n import t, translate_literal
from charmm_gui_cli.presentation.progress import (
    ProgressReporter, describe_progress, emit_progress, format_progress,
    official_links, result_presentation,
)


class ProgressTests(unittest.TestCase):
    def test_preparation_and_submit_event_id_is_source_not_build(self):
        for state in ("parameterizing", "preparation_running", "preparation_ready", "submitting_bilayer"):
            with self.subTest(state=state):
                value = describe_progress({"state": state, "job_id": "123"})
                self.assertEqual(value["source_job_id"], "123")
                self.assertIsNone(value["build_job_id"])
                self.assertNotIn("build_results", value["official_links"])

    def test_api_events_identify_cloned_build(self):
        for state in ("pending", "running quick_bilayer", "running ffconverter", "done", "downloading"):
            value = describe_progress({"state": state, "job_id": "234"}, source_job_id="123")
            self.assertEqual(value["build_job_id"], "234")
            self.assertEqual(value["source_job_id"], "123")

    def test_explicit_role_ids_override_legacy_ambiguous_id(self):
        value = describe_progress({"state": "submitting_bilayer", "job_id": "234",
                                   "source_job_id": "123", "build_job_id": "234"})
        self.assertEqual(value["source_job_id"], "123")
        self.assertEqual(value["build_job_id"], "234")

    def test_verified_official_destinations_and_separate_ids(self):
        links = official_links(source_job_id="123", build_job_id="234")
        self.assertEqual(urlparse(links["source_preparation"]).netloc, "www.charmm-gui.org")
        self.assertEqual(parse_qs(urlparse(links["source_preparation"]).query),
                         {"doc": ["input/pdbreader"], "jobid": ["123"], "project": ["membrane_bilayer"]})
        self.assertEqual(parse_qs(urlparse(links["build_results"]).query),
                         {"doc": ["input/membrane.quick"], "jobid": ["234"], "step": ["2"]})

    def test_ids_never_reflect_credentials_or_arbitrary_urls(self):
        for bad in ("123&token=SECRET", "https://evil.invalid", "PASSWORD", 123, "123\n", "1" * 33):
            self.assertEqual(official_links(source_job_id=bad, build_job_id=bad), {})

    def test_unknown_event_does_not_echo_raw_server_text(self):
        event = {"state": "running TOKEN_SECRET", "job_id": "PASSWORD_SECRET", "email": "EMAIL_SECRET",
                 "last_output_file": "COOKIE_SECRET", "token": "TOKEN_SECRET"}
        value = json.dumps(describe_progress(event)) + format_progress(event)
        for secret in ("TOKEN_SECRET", "PASSWORD_SECRET", "EMAIL_SECRET", "COOKIE_SECRET"):
            self.assertNotIn(secret, value)

    def test_internal_conversion_is_not_ui_step_number(self):
        value = describe_progress({"state": "running ffconverter", "job_id": "234", "last_output_file": "step5_input.out"})
        self.assertEqual(value["internal_stage"], "ffconverter")
        self.assertIn("力场格式转换", value["label"])
        self.assertIn("Step 2", value["ui_note"])
        self.assertIn("不是网页进度", value["ui_note"])
        self.assertIn("不表示已运行", value["ui_note"])
        self.assertIsNone(value["validation_passed"])
        self.assertEqual(value["terminal_semantics"], "in_progress")

    def test_last_output_never_claims_completion(self):
        for name in ("step4_lipid_checkchol.out", "step4.2_waterbox.out", "step4.3_ion.out", "step5_assembly.out", "step5_input.out"):
            value = describe_progress({"state": "running quick_bilayer", "lastOutFile": name})
            self.assertEqual(value["last_output_file"], name)
            self.assertIsNone(value["remote_done"])
            self.assertFalse(value["archive_verified"])
            self.assertIn("不单独证明", value["output_note"])

    def test_remote_done_is_not_local_validation(self):
        value = result_presentation({"state": "done", "build_job_id": "234"})
        self.assertEqual(value["terminal_semantics"], "remote_done_not_validated")
        self.assertTrue(value["remote_done"])
        self.assertFalse(value["archive_verified"])
        self.assertIsNone(value["validation_passed"])

    def test_download_and_archive_precheck_are_distinct(self):
        downloaded = result_presentation({"state": "downloaded_unverified"})
        checked = result_presentation({"state": "ligands_present"})
        self.assertFalse(downloaded["archive_verified"])
        self.assertTrue(checked["archive_verified"])
        self.assertEqual(checked["terminal_semantics"], "archive_precheck_passed")
        self.assertIsNone(checked["validation_passed"])

    def test_validated_label_without_report_does_not_invent_pass(self):
        result = result_presentation({"state": "validated"})
        self.assertEqual(result["terminal_semantics"], "validation_recorded")
        self.assertIsNone(result["validation_passed"])

    def test_validation_with_warnings_separates_compilation_and_md(self):
        result = result_presentation({"state": "validated"}, {
            "passed": True, "checks": {}, "review_required": True, "review_warnings": ["review chemistry"]})
        self.assertEqual(result["terminal_semantics"], "validation_passed_with_warnings")
        self.assertTrue(result["archive_verified"])
        self.assertTrue(result["validation_passed"])
        self.assertTrue(result["review_required"])
        self.assertEqual(result["warning_count"], 1)
        self.assertFalse(result["gromacs_compiled"])
        self.assertEqual(result["compilation_status"], "not_run")
        self.assertIn("未运行", result["compilation_label"])
        self.assertFalse(result["scientific_correctness_verified"])
        self.assertIn("不等于已执行", result["simulation_note"])

    def test_grompp_pass_does_not_claim_simulation(self):
        result = result_presentation({"state": "validated"}, {"passed": True, "checks": {"grompp": {"passed": True}}})
        self.assertEqual(result["terminal_semantics"], "validation_passed")
        self.assertTrue(result["gromacs_compiled"])
        self.assertEqual(result["compilation_status"], "passed")
        self.assertFalse(result["scientific_correctness_verified"])

    def test_failed_report_overrides_validated_state(self):
        result = result_presentation({"state": "validated"}, {"passed": False, "checks": {"grompp": {"passed": False}}})
        self.assertEqual(result["terminal_semantics"], "validation_failed")
        self.assertFalse(result["validation_passed"])
        self.assertEqual(result["compilation_status"], "failed")

    def test_summary_evidence_without_full_report_is_supported(self):
        result = result_presentation({"state": "validated", "passed": True, "review_required": True, "gromacs_compiled": True})
        self.assertEqual(result["terminal_semantics"], "validation_passed_with_warnings")
        self.assertTrue(result["gromacs_compiled"])

    def test_progress_only_uses_stderr(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            emit_progress({"state": "pending", "job_id": "234"})
            build_command.progress({"state": "parameterizing", "job_id": "123"})
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(t("progress.build", jobid="234"), stderr.getvalue())
        self.assertIn(t("progress.source", jobid="123"), stderr.getvalue())

    def test_reporter_retains_roles_and_prints_links_once(self):
        output = io.StringIO()
        callback = ProgressReporter(stream=output)
        callback({"state": "parameterizing", "job_id": "123"})
        callback({"state": "submitting_bilayer", "job_id": "123"})
        callback({"state": "running ffconverter", "job_id": "234"})
        callback({"state": "done", "job_id": "234"})
        callback({"state": "validating_final_archive (local)"})
        self.assertEqual(callback.source_job_id, "123")
        self.assertEqual(callback.build_job_id, "234")
        links = official_links(source_job_id="123", build_job_id="234")
        for url in links.values():
            self.assertEqual(output.getvalue().count(url), 1)
        from charmm_gui_cli.presentation.progress import UI_NOTE
        self.assertNotIn(translate_literal(UI_NOTE, prefix="progress."), output.getvalue())

    def test_offline_build_summary_keeps_json_and_adds_presentation(self):
        args = Namespace(command="build-resume", directory=Path("/virtual/progress-check"), interval=30,
                         max_wait=100, grompp=False, no_grompp=True, gmx="gmx", token_file=None,
                         cookies=None, wait=False)
        saved = {"state": "ligands_present", "source_job_id": "123", "build_job_id": "234"}
        report = {"passed": True, "checks": {}, "review_required": True}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(managed, "resolve_run", return_value=args.directory), \
                patch.object(Path, "is_file", return_value=True), \
                patch.object(managed, "_read_json", return_value=saved), \
                patch.object(managed, "_job_metadata", return_value={}), \
                patch.object(managed, "remember_run"), patch.object(managed, "record_recovery"), \
                patch.object(managed, "finalize_managed_build", return_value=report), \
                patch.object(build_command, "load_token") as credentials, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result, code = build_command.execute(args)
            print(json.dumps(result))
        credentials.assert_not_called()
        self.assertEqual(code, 0)
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["state"], "validated")
        self.assertEqual(parsed["presentation"]["terminal_semantics"], "validation_passed_with_warnings")
        self.assertIn("build_results", parsed["official_links"])
        self.assertFalse(parsed["gromacs_compiled"])
        self.assertIn(describe_progress({"state": "validating_final_archive (local)"}, localized=True)["label"], stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
