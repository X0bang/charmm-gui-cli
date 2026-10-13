"""Specific stale reasons and safe recovery from missing, not edited, models."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import unittest
from unittest.mock import patch

from charmm_gui_cli.store import managed, delivery
from charmm_gui_cli.store.auth import atomic_json
from charmm_gui_cli.cli import main
from charmm_gui_cli.commands.terminal_output import format_job_status
from charmm_gui_cli.locales.i18n import set_language
from tests import test_single_directory as fixtures


class ValidationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SingleDirectoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.run, self.root = self.fixture.run, self.fixture.root

    def tearDown(self):
        set_language()

    def issue(self):
        value = managed.inspect_run("receptor-a")
        self.assertNotIn("passed", value)
        self.assertEqual(value["validation_status"], "stale_or_unreadable")
        return value["validation_issues"]

    def test_missing_archive_identifies_file_and_hides_obsolete_remote_status(self):
        (self.root / "charmm-gui.tgz").rename(self.root / "kept-for-test.tgz")
        issue = self.issue()[0]
        self.assertEqual(issue["key"], "validation.archive_missing")
        self.assertEqual(issue["params"]["path"], str(self.root / "charmm-gui.tgz"))
        for lang in ("en", "zh"):
            set_language(lang)
            value = managed.inspect_run("receptor-a")
            value["remote_status"] = "archive_preparing"
            text = format_job_status(value)
            self.assertIn("charmm-gui.tgz", text)
            self.assertNotIn("Remote state:", text)
            self.assertNotIn("远端状态：", text)

    def test_changed_input_identifies_exact_file(self):
        path = self.root / "inputs/complex.pdb"
        path.write_text("changed input")
        issues = self.issue()
        self.assertEqual(issues[0]["key"], "validation.input_changed")
        self.assertEqual(issues[0]["params"]["path"], str(path))

    def test_missing_input_and_model_are_distinguished_from_archive(self):
        path = self.root / "inputs/complex.pdb"
        path.rename(self.root / "kept-complex.pdb")
        self.assertEqual(self.issue()[0], {"key": "validation.file_missing", "params": {"path": str(path)}})
        (self.root / "kept-complex.pdb").rename(path)
        (self.root / "model").rename(self.root / "kept-model")
        self.assertEqual(self.issue()[0], {"key": "validation.file_missing", "params": {"path": str(self.root / "model")}})

    def test_config_and_lineage_changes_are_named_checks(self):
        manifest = managed._read_json(self.run / "full-run.json")
        manifest["config"]["membrane"]["margin_A"] = 30
        atomic_json(self.run / "full-run.json", manifest)
        self.assertEqual(self.issue()[0]["key"], "validation.config_changed")
        manifest["build_job_id"] = "300"
        atomic_json(self.run / "full-run.json", manifest)
        self.assertEqual(self.issue()[0]["key"], "validation.lineage_changed")

    def test_changed_model_has_precise_reason_not_a_pass(self):
        (self.root / "model/charmm-gui-200/step5_assembly.pdb").write_text("user edit")
        self.assertEqual(self.issue()[0]["key"], "validation.model_changed")

    def test_report_cli_exposes_missing_file_and_concrete_resume_command(self):
        (self.root / "charmm-gui.tgz").rename(self.root / "kept-archive.tgz")
        error = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(error):
            code = main(["jobs", "report", "receptor-a", "--lang", "en"])
        self.assertEqual(code, 2)
        self.assertIn("archive is missing", error.getvalue())
        self.assertIn("charmm-gui.tgz", error.getvalue())
        self.assertIn("jobs resume receptor-a", error.getvalue())
        self.assertNotIn(" NAME", error.getvalue())

    def test_malformed_evidence_is_controlled_not_a_traceback(self):
        report = self.fixture.report
        report["evidence"]["inputs_sha256"] = ["bad data"]
        atomic_json(self.run / "results/validation.json", report)
        self.assertIn("validation.metadata_unreadable", [issue["key"] for issue in self.issue()])

    def test_missing_model_is_reextracted_and_user_report_notes_are_retained(self):
        # Scientific routines are mocked because this archive is a tiny path/
        # provenance fixture, not a physical model. Extraction/provenance and
        # publication/backup checks remain real.
        manifest = managed._read_json(self.run / "full-run.json")
        manifest["config"]["membrane"] = {"upper": "POPC=1", "lower": "POPC=1"}
        manifest["config"]["ions"] = {"type": "KCl", "concentration_M": 0.15}
        atomic_json(self.run / "full-run.json", manifest)
        previous = self.fixture.report
        previous["evidence"] = managed._evidence(manifest, self.root / "charmm-gui.tgz", False, "gmx")
        previous["fingerprint"] = managed._canonical_hash(previous["evidence"])
        atomic_json(self.run / "results/validation.json", previous)
        self.assertTrue(managed._cache_reusable(previous, previous["evidence"]))
        delivery.auto_export(self.run)
        notes = (self.root / "report.txt").read_text() + "\nUser annotation\n"
        (self.root / "report.txt").write_text(notes)
        (self.root / "model").rename(self.root / "kept-model")
        scientific = {"passed": True, "checks": {}, "files": {"coordinates": "mocked coordinates"}}
        with patch.object(managed, "validate_system", return_value=scientific), \
                patch.object(managed, "validate_bound_pose", return_value={"passed": True}), \
                patch.object(managed, "assess_acceptance", return_value={"passed": True}), \
                patch.object(managed, "inspect_environment", return_value={"passed": True}):
            report = managed.finalize_managed_build(self.run)
        self.assertTrue(report["passed"])
        self.assertEqual((self.root / "model/charmm-gui-200/step5_assembly.pdb").read_bytes(),
                         (self.root / "kept-model/charmm-gui-200/step5_assembly.pdb").read_bytes())
        delivery.auto_export(self.run)
        self.assertEqual((self.root / "report.txt.user-edited").read_text(), notes)
        self.assertTrue(managed.inspect_run("receptor-a")["passed"])
