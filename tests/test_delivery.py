import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.store import delivery, managed
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.cli import main
from charmm_gui_cli.presentation.terminal_output import format_build
from charmm_gui_cli.chem.validation import extract_for_validation
from tests import test_managed_provenance as provenance


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        fixture = provenance.ManagedProvenanceTests()
        fixture.setUp()
        self.run, self.root = fixture.run, fixture.root
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch("pathlib.Path.home", return_value=self.home))
        self.stack.enter_context(patch("pathlib.Path.cwd", return_value=self.project))
        fixture.manifest["delivery_context"] = delivery.creation_context()
        fixture.save()
        managed._evidence(fixture.manifest, fixture.archive, False, "gmx", create_receipt=True)
        self.report = fixture.publish_report()
        self.extracted = extract_for_validation(fixture.archive, self.run / "results/system")
        self.report["extracted_directory"] = str(self.extracted)
        atomic_json(self.run / "results/validation.json", self.report)
        atomic_json(self.run / "job.json", {"name": "crtw-bcar"})

    def tearDown(self):
        self.stack.close()

    def test_export_has_all_deliverables_without_internal_state(self):
        result = delivery.export_run(self.run)
        out = self.project / "crtw-bcar"
        self.assertEqual(result["directory"], str(out))
        for name in ("charmm-gui.tgz", "model/charmm-gui-200/step5_assembly.pdb",
                     "reports/validation.json", "reports/validation.txt", "inputs/protein.pdb",
                     "inputs/ligand.sdf", "inputs/complex.pdb", "inputs/input-manifest.json",
                     "inputs/system-config.json", "README.txt"):
            self.assertTrue((out / name).is_file(), name)
        self.assertFalse((out / "full-run.json").exists())
        source = self.run / "bilayer/charmm-gui.tgz"
        self.assertNotEqual(source.stat().st_ino, (out / "charmm-gui.tgz").stat().st_ino)
        self.assertEqual(source.read_bytes(), (out / "charmm-gui.tgz").read_bytes())

    def test_creation_directory_is_retained_when_resuming_elsewhere(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        with patch("pathlib.Path.cwd", return_value=elsewhere):
            result = delivery.auto_export(self.run)
        self.assertEqual(Path(result["directory"]).parent, self.project)

    def test_old_run_defaults_to_current_directory(self):
        manifest = managed._read_json(self.run / "full-run.json")
        manifest.pop("delivery_context")
        atomic_json(self.run / "full-run.json", manifest)
        self.assertEqual(Path(delivery.export_run(self.run)["directory"]).parent, self.project)

    def test_explicit_to_is_exact_destination(self):
        out = self.root / "custom model"
        self.assertEqual(delivery.export_run(self.run, to=out)["directory"], str(out))

    def test_foreign_directory_is_not_overwritten(self):
        out = self.project / "crtw-bcar"
        out.mkdir()
        (out / "keep.txt").write_text("keep")
        with self.assertRaises(ToolError) as error:
            delivery.export_run(self.run)
        self.assertEqual(error.exception.key, "delivery.foreign")
        self.assertEqual(list(out.iterdir()), [out / "keep.txt"])
        self.assertEqual((out / "keep.txt").read_text(), "keep")
        with self.assertRaises(ToolError):
            delivery.check_destination(self.run)

    def test_foreign_marker_and_symlink_are_rejected(self):
        out = self.project / "crtw-bcar"
        out.mkdir()
        atomic_json(out / delivery.MARKER, {"task_id": "another-task"})
        with self.assertRaises(ToolError):
            delivery.export_run(self.run)
        link = self.root / "linked"
        link.symlink_to(out, target_is_directory=True)
        with self.assertRaises(ToolError):
            delivery.export_run(self.run, to=link)

    def test_owned_destination_can_be_exported_again(self):
        first = delivery.export_run(self.run)
        self.assertEqual(first, delivery.export_run(self.run))

    def test_modified_owned_files_are_not_overwritten(self):
        result = delivery.export_run(self.run)
        path = Path(result["inputs"]) / "protein.pdb"
        path.write_text("user edits")
        with self.assertRaises(ToolError) as error:
            delivery.export_run(self.run)
        self.assertEqual(error.exception.key, "delivery.modified")
        self.assertEqual(path.read_text(), "user edits")
        self.assertNotEqual((self.run / "inputs/protein.pdb").read_text(), "user edits")

    def test_moving_delivery_does_not_invalidate_report_or_resume(self):
        result = delivery.export_run(self.run)
        Path(result["directory"]).rename(self.root / "moved-model")
        self.assertTrue(managed._final_summary(self.run)["passed"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["jobs", "report", str(self.run)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["passed"])
        self.assertTrue(delivery.export_run(self.run)["passed"])

    def test_cli_export_and_completed_resume_export_automatically(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["jobs", "export", str(self.run), "--to", str(self.root / "exported")])
        self.assertEqual(code, 0)
        self.assertIn("delivery", json.loads(output.getvalue()))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--foreground", "jobs", "resume", str(self.run), "--no-grompp", "--no-preview"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["delivery"]["directory"], str(self.project / "crtw-bcar"))
        self.assertTrue(managed._final_summary(self.run)["passed"])

    def test_completion_summary_shows_only_delivery_path(self):
        value = {"name": "crtw-bcar", "state": "validated", "passed": True,
                 "directory": str(self.run), "archive": str(self.run / "bilayer/charmm-gui.tgz"),
                 "report": str(self.run / "results/validation.json"),
                 "delivery": {"directory": str(self.project / "crtw-bcar")}}
        text = format_build(value)
        self.assertIn("crtw-bcar", text)
        self.assertNotIn(str(self.run), text)
        self.assertNotIn("validation.json", text)
        with patch("pathlib.Path.cwd", return_value=self.root / "elsewhere"):
            text = format_build(value)
        self.assertIn("../project/crtw-bcar", text)
        self.assertNotIn(str(self.project), text)

    def test_incomplete_stale_and_active_tasks_are_rejected(self):
        with patch("charmm_gui_cli.store.background.metadata", return_value={"running": True}):
            with self.assertRaises(ToolError) as error:
                delivery.export_run(self.run)
            self.assertEqual(error.exception.key, "background.running")
        self.report["validation_complete"] = False
        atomic_json(self.run / "results/validation.json", self.report)
        with self.assertRaises(ToolError) as error:
            delivery.export_run(self.run)
        self.assertEqual(error.exception.key, "delivery.incomplete")
        self.report["validation_complete"] = True
        self.report["fingerprint"] = "stale"
        atomic_json(self.run / "results/validation.json", self.report)
        with self.assertRaises(ToolError) as error:
            delivery.export_run(self.run)
        self.assertEqual(error.exception.key, "cli.stale_report")

    def test_source_symlink_and_destination_inside_run_are_rejected(self):
        with self.assertRaises(ToolError):
            delivery.export_run(self.run, to=self.run / "export")
        (self.run / "inputs/link.pdb").symlink_to(self.run / "inputs/protein.pdb")
        with self.assertRaises(ToolError) as error:
            delivery.export_run(self.run)
        self.assertEqual(error.exception.key, "delivery.unsafe")

    def test_owned_refresh_does_not_rewrite_unchanged_archive(self):
        result = delivery.export_run(self.run)
        path = Path(result["archive"])
        before = path.stat().st_ino
        self.report["review_required"] = True
        atomic_json(self.run / "results/validation.json", self.report)
        delivery.export_run(self.run)
        self.assertEqual(before, path.stat().st_ino)
        self.assertTrue(json.loads((Path(result["report"])).read_text())["review_required"])

    def test_partial_export_is_preserved_and_not_overwritten(self):
        out = self.project / "crtw-bcar"
        out.mkdir()
        context = managed._read_json(self.run / "full-run.json")["delivery_context"]
        atomic_json(out / delivery.MARKER, {"task_id": context["task_id"], "run_directory": str(self.run), "complete": False})
        with self.assertRaises(ToolError) as error:
            delivery.export_run(self.run)
        self.assertEqual(error.exception.key, "delivery.partial")

    def test_modified_internal_model_cannot_be_exported_as_validated(self):
        (self.extracted / "charmm-gui-200/step5_assembly.pdb").write_text("modified model")
        with self.assertRaises(ToolError) as error:
            delivery.export_run(self.run)
        self.assertEqual(error.exception.key, "delivery.model_changed")
        self.assertFalse((self.project / "crtw-bcar").exists())

    def test_failed_validation_exports_diagnostics_without_claiming_success(self):
        self.report.update(status="validation_failed", passed=False)
        atomic_json(self.run / "results/validation.json", self.report)
        result = delivery.export_run(self.run)
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "validation_failed")
        self.assertFalse(json.loads(Path(result["report"]).read_text())["passed"])

    def test_old_client_validation_can_be_exported_without_fresh_validation_claim(self):
        self.report["evidence"]["tool_version"] = "0.5.2"
        self.report["fingerprint"] = managed._canonical_hash(self.report["evidence"])
        atomic_json(self.run / "results/validation.json", self.report)
        result = delivery.export_run(self.run)
        self.assertEqual(result["validation_tool_version"], "0.5.2")
        self.assertEqual(json.loads(Path(result["report"]).read_text())["evidence"]["tool_version"], "0.5.2")

    def test_new_initialization_records_creation_directory_before_remote_work(self):
        from charmm_gui_cli.pipeline.full_build import initialize
        config = {"protein": "protein.pdb", "ligand": "ligand.sdf", "ligand_resname": "LIG", "ligand_hydrogens": "preserve",
                  "preparation": {"n_terminal": "NTER", "c_terminal": "CTER", "orientation": "ppm"}}
        with patch("charmm_gui_cli.pipeline.full_build.load_build_config", return_value=config), \
                patch("charmm_gui_cli.pipeline.full_build.prepare_inputs", return_value={"files": {}}):
            result = initialize(self.root / "config.yaml", self.root / "new-run")
        context = result["delivery_context"]
        self.assertEqual(context["creation_directory"], str(self.project))
        self.assertEqual(len(context["task_id"]), 32)

    def test_legacy_filename_like_job_remains_exportable(self):
        atomic_json(self.run / "job.json", {"name": "legacy.sdf"})
        result = delivery.export_run(self.run, to=self.root / "legacy-export")
        self.assertTrue(result["passed"])
        self.assertTrue((self.root / "legacy-export/report.txt").is_file())
        config = json.loads((self.root / "legacy-export/inputs/system-config.json").read_text())
        self.assertIn("requested", config)
        self.assertIn("applied", config)

