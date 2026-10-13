"""Read-only remote mocks and synthetic inputs for audit regression coverage."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from charmm_gui_cli import cli, managed, full_build, i18n, updater, workflow
from charmm_gui_cli.api import Client, SubmissionUnknown
from charmm_gui_cli.auth import ToolError, atomic_json
from charmm_gui_cli.build_config import validate_preparation, validate_terminal_residues, load_build_config
from charmm_gui_cli.config import composition
from charmm_gui_cli.inputs import _pdb, read_ligand, inspect_ligand, _sdf, Atom
from charmm_gui_cli.terminal_output import format_check, format_job_status
from tests.test_pdb_boundaries import atom


class AuditFixTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-audit-regression-")).resolve()
        i18n.set_language("en")

    def tearDown(self):
        i18n.set_language()

    def test_api_validation_diagnostic_is_safe_and_not_mislabeled_auth(self):
        response = Mock()
        response.json.return_value = {"error": "Invalid lipid name POPX"}
        with self.assertRaises(ToolError) as caught:
            Client._json(response)
        self.assertEqual(caught.exception.category, "api_error")
        self.assertEqual(caught.exception.as_dict()["server_message"], "Invalid lipid name POPX")
        self.assertIn("POPX", caught.exception.localized())
        self.assertNotIn("login", caught.exception.next_step)
        response.close.assert_called_once()

    def test_arbitrary_api_error_body_is_not_reflected(self):
        for payload in ("PASSWORD_SECRET token=COOKIE_SECRET", {"body": "TOKEN_SECRET"}, "Invalid lipid name TOKEN_SECRET token=secret"):
            response = Mock()
            response.json.return_value = {"error": payload}
            with self.assertRaises(ToolError) as caught:
                Client._json(response)
            self.assertNotIn("SECRET", json.dumps(caught.exception.as_dict()))

    def test_invalid_lipid_fails_with_candidates_and_explicit_opt_in(self):
        with self.assertRaises(ToolError) as caught:
            composition("POPX=1")
        self.assertEqual(caught.exception.key, "config.unknown_lipid")
        self.assertIn("POPC", str(caught.exception))
        self.assertEqual(composition("CUSTOM=1", allow_unknown=True), "CUSTOM=1")
        self.assertEqual(composition("POPC:POPE=7:3"), "POPC:POPE=7:3")

    def test_invalid_model_options_leave_no_named_directory(self):
        run = self.root / "same-name/.charmm-gui"
        with patch("charmm_gui_cli.input_sources.prepare_sources") as prepare:
            with self.assertRaises(ToolError):
                managed.create_managed_build(protein="unused", ligand="unused", output=run,
                    membrane={"upper": "POPC:CHL1=3", "lower": "POPC=1", "margin_A": 20})
        prepare.assert_not_called()
        self.assertFalse(run.parent.exists())

    def test_input_preparation_failure_rolls_back_only_new_task(self):
        run = self.root / "same-name/.charmm-gui"
        def fail(**kwargs):
            kwargs["out"].mkdir()
            (kwargs["out"] / "partial.pdb").write_text("partial")
            raise ToolError("Bad local input")
        with patch("charmm_gui_cli.input_sources.prepare_sources", side_effect=fail):
            for _ in range(2):
                with self.assertRaisesRegex(ToolError, "Bad local input"):
                    managed.create_managed_build(protein="unused", ligand="unused", output=run)
                self.assertFalse(run.parent.exists())
        run.parent.mkdir()
        (run.parent / "keep.txt").write_text("user data")
        with self.assertRaises(ToolError):
            managed.create_managed_build(protein="unused", ligand="unused", output=run)
        self.assertEqual((run.parent / "keep.txt").read_text(), "user data")

    def test_multicomponent_preparation_failure_does_not_occupy_name(self):
        run = self.root / "components/.charmm-gui"
        with patch.object(managed, "prepare_build_components", side_effect=ToolError("Bad components")):
            with self.assertRaisesRegex(ToolError, "Bad components"):
                managed.create_component_build(components_config="unused", complex_pdb="unused", output=run)
        self.assertFalse(run.parent.exists())

    def test_local_rollback_preserves_any_submission_evidence(self):
        from charmm_gui_cli.store.local_build import local_initialization
        for name, record in (("source", {"source_job_id": "123"}),
                             ("unknown", {"state": "submission_unknown"}),
                             ("intent", None)):
            run = self.root / name / ".charmm-gui"
            with self.assertRaisesRegex(ToolError, "Local failure"):
                with local_initialization(run):
                    if record is None:
                        atomic_json(run / "preparation/upload.intent.json", {"state": "submitting"})
                    else:
                        atomic_json(run / "full-run.json", record)
                    raise ToolError("Local failure")
            self.assertTrue(run.is_dir())

    def test_historical_special_lipid_resumes_without_resubmission(self):
        config = {"version": 1, "source_job_id": "100", "expected_ligands": ["LIG"],
                  "preparation": {"ligand_parameters_ready": True, "orientation": "ppm"},
                  "membrane": {"upper": "PSM=1", "lower": "POPI=1", "margin_A": 20}}
        run = self.root / "legacy"
        run.mkdir()
        atomic_json(run / "run.json", {"schema_version": 1, "state": "submitted", "job_id": "200", "config": config})
        self.assertEqual(workflow.read(run)["config"], config)
        client = Mock()
        client.status.return_value = {"status": "pending"}
        self.assertEqual(workflow.resume(client, run)["state"], "pending")
        client.submit.assert_not_called()

    def test_running_status_does_not_display_previous_interruption_as_current(self):
        value = {"state": "parameterizing", "name": "task", "execution_state": "interrupted",
                 "background": {"running": True},
                 "recovery": {"error": {"message": "Interrupted earlier", "category": "interrupted", "retryable": True}}}
        text = format_job_status(value)
        self.assertNotIn("Interrupted earlier", text)
        self.assertNotIn("Execution interrupted", text)
        self.assertIn("jobs watch task", text)

    def test_failed_execution_is_durable_without_changing_pipeline_stage(self):
        run = self.root / "task/.charmm-gui"
        run.mkdir(parents=True)
        atomic_json(run / "full-run.json", {"schema_version": 2, "state": "parameterizing"})
        atomic_json(run / "job.json", {"name": "task"})
        error = ToolError(key="api.invalid_lipid", params={"lipid": "POPX"}, category="api_error", next_step="charmm-gui-cli build -h")
        managed.record_recovery(run, error)
        manifest = managed._read_json(run / "full-run.json")
        self.assertEqual(manifest["state"], "parameterizing")
        self.assertEqual(manifest["execution_state"], "failed_remote")
        value = managed.inspect_run(str(run))
        text = format_job_status(value)
        self.assertIn("Remote failure", text)
        self.assertIn("POPX", text)
        self.assertNotIn("jobs resume task", text)
        managed.record_recovery(run, state="validated")
        self.assertFalse(managed._read_json(run / "recovery.json")["active"])

    def test_unknown_post_preserves_reconcile_priority(self):
        run = self.root / "task/.charmm-gui"
        run.mkdir(parents=True)
        atomic_json(run / "full-run.json", {"schema_version": 2, "state": "submission_unknown"})
        atomic_json(run / "job.json", {"name": "task"})
        managed.record_recovery(run, SubmissionUnknown("Uncertain POST"))
        value = managed.inspect_run(str(run))
        self.assertEqual(value["execution_state"], "submission_unknown")
        self.assertIn("jobs reconcile task", format_job_status(value))

    def test_check_failure_and_review_items_are_visible(self):
        value = {"passed": False, "error": {"message": "Incorrect bound pose", "next_step": "charmm-gui-cli check -h"},
                 "protein": {"residues": None, "residues_missing_heavy_atoms": 2, "numbering_gaps": 1}, "review_required": True}
        text = format_check(value)
        self.assertIn("Incorrect bound pose", text)
        self.assertIn("2 residues", text)
        self.assertIn("1 numbering gaps", text)
        self.assertNotIn("None", text)
        self.assertIn("Review items", text)
        self.assertIn('"error"', format_check(value, full=True))

    def test_global_options_work_before_and_after_commands(self):
        for options in (["check", "--json", "--lang", "zh"], ["build", "--foreground"],
                        ["--json", "check"], ["jobs", "status", "task", "--json"]):
            args = cli.parser().parse_args(options)
            if "--json" in options:
                self.assertTrue(args.json)
            if "--foreground" in options:
                self.assertTrue(args.foreground)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main([]), 0)
        self.assertIn("build", output.getvalue())
        self.assertIn("login", output.getvalue())

    def test_per_chain_terminal_config_and_form_selection(self):
        prep = validate_preparation({"n_terminal": "A=GLYP,B=NTER"})
        self.assertEqual(prep["terminals"], {"A": {"n_terminal": "GLYP"}, "B": {"n_terminal": "NTER"}})
        path = self.root / "form.html"
        path.write_text('<form><input type="file" name="sdf_cgenff[LIG]">'
                        '<select name="terminal[PROA][first]"><option value="GLYP">GLYP</option></select>'
                        '<select name="terminal[PROB][first]"><option value="NTER">NTER</option></select></form>')
        fields = full_build._preparation_fields(path, {"ligand_resname": "LIG", "preparation": prep})
        self.assertEqual(fields["terminal[PROA][first]"], "GLYP")
        self.assertEqual(fields["terminal[PROB][first]"], "NTER")

    def test_invalid_per_chain_terminal_mappings_fail_locally(self):
        for value in ("A=INVALID", "A=GLYP,A=NTER", "A=GLYP/", "=GLYP"):
            with self.subTest(value=value), self.assertRaises(ToolError):
                validate_preparation({"n_terminal": value})

    def test_gly_and_pro_patches_reject_incompatible_residue_locally(self):
        predictions = [{"chain": "PROA", "first_resname": "GLY"}, {"chain": "PROB", "first_resname": "ALA"}]
        with self.assertRaises(ToolError) as caught:
            validate_terminal_residues(validate_preparation({"n_terminal": "GLYP"}), predictions)
        self.assertEqual(caught.exception.key, "terminal.residue_mismatch")
        validate_terminal_residues(validate_preparation({"n_terminal": "A=GLYP,B=NTER"}), predictions)
        validate_terminal_residues(validate_preparation({"n_terminal": "PROP"}), [{"chain": "A", "first_resname": "PRO"}])

    def test_yaml_per_chain_and_unknown_lipid_opt_in(self):
        (self.root / "protein.pdb").write_text("END\n")
        (self.root / "ligand.sdf").write_text("fixture\n")
        path = self.root / "build.yaml"
        path.write_text(json.dumps({"version": 2, "protein": "protein.pdb", "ligand": "ligand.sdf",
            "preparation": {"terminals": {"A": {"n_terminal": "GLYP"}, "B": {"c_terminal": "CT2"}}},
            "membrane": {"upper": "CUSTOM=1", "lower": "POPC=1", "margin_A": 20, "allow_unknown_lipids": True}}))
        config = load_build_config(path)
        self.assertEqual(config["preparation"]["terminals"]["A"]["n_terminal"], "GLYP")
        self.assertTrue(config["membrane"]["allow_unknown_lipids"])

    def test_formats_have_actionable_localized_guidance(self):
        cif = self.root / "protein.cif"
        cif.write_text("data_protein\n")
        with self.assertRaises(ToolError) as caught:
            _pdb(cif)
        self.assertEqual(caught.exception.key, "input.mmcif")
        self.assertIn("check --protein converted.pdb", caught.exception.next_step)
        sdf = self.root / "ligand.sdf"
        sdf.write_text("Ligand\n\n\n  0  0 V3000\nM  END\n$$$$\n")
        with self.assertRaises(ToolError) as caught:
            read_ligand(sdf)
        self.assertEqual(caught.exception.key, "input.sdf_format")
        self.assertIn("ligand-v2000.sdf", caught.exception.next_step)

    def test_implicit_hydrogens_are_explicitly_reported(self):
        import importlib.util
        if importlib.util.find_spec("rdkit") is None:
            self.skipTest("RDKit is needed for implicit hydrogen evidence")
        sdf = self.root / "bound.sdf"
        sdf.write_text(_sdf([Atom(1, "C1", "C", (1, 2, 3))], {}, "LIG"))
        report = inspect_ligand(sdf)
        self.assertEqual(report["explicit_hydrogen_atoms"], 0)
        self.assertEqual(report["implicit_hydrogen_atoms"], 4)
        self.assertEqual(report["chemistry_warnings"][0]["key"], "input.implicit_hydrogens")

    def test_unknown_terminal_chain_is_rejected_not_ignored(self):
        prep = validate_preparation({"n_terminal": "B=GLYP"})
        path = self.root / "form.html"
        path.write_text('<form><input type="file" name="sdf_cgenff[LIG]">'
                        '<select name="terminal[PROA][first]"><option value="NTER">NTER</option></select></form>')
        with self.assertRaises(ToolError) as caught:
            full_build._preparation_fields(path, {"ligand_resname": "LIG", "preparation": prep})
        self.assertEqual(caught.exception.key, "terminal.chain_missing")
        with self.assertRaises(ToolError) as caught:
            validate_terminal_residues(prep, [{"chain": "PROA", "first_resname": "GLY"}])
        self.assertEqual(caught.exception.key, "terminal.chain_missing")

    def test_terminal_chain_alias_conflict_is_rejected_locally(self):
        prep = validate_preparation({"terminals": {"A": {"n_terminal": "GLYP"}, "PROA": {"n_terminal": "NTER"}}})
        with self.assertRaises(ToolError) as caught:
            validate_terminal_residues(prep, [{"chain": "PROA", "first_resname": "GLY"}])
        self.assertEqual(caught.exception.key, "terminal.alias_conflict")

    def test_overflow_serial_repair_preserves_source_and_coordinates(self):
        pdb = self.root / "overflow.pdb"
        line = atom(1, "ALA", 1, (1, 2, 3))
        pdb.write_text(line[:6] + "*****" + line[11:] + "\n")
        before = pdb.read_bytes()
        report = {}
        atoms, _ = _pdb(pdb, report=report)
        self.assertEqual(atoms[0].serial, 1)
        self.assertEqual(atoms[0].xyz, (1, 2, 3))
        self.assertEqual(report["repairs"][0]["key"], "input.overflow_renumbered")
        self.assertEqual(pdb.read_bytes(), before)

    def test_invalid_release_metadata_is_controlled_not_traceback(self):
        for value in ({"tag_name": None}, {"tag_name": []}, [], None, {"tag_name": 1}):
            with self.subTest(value=value), patch.object(updater, "_download", return_value=json.dumps(value).encode()):
                with self.assertRaises(ToolError) as caught:
                    updater.check_release(object(), "0.7.0")
                self.assertEqual(caught.exception.key, "update.metadata")

    def test_keyed_errors_relocalize_in_status(self):
        error = ToolError(key="api.invalid_lipid", params={"lipid": "POPX"}, next_step="charmm-gui-cli build -h")
        value = {"state": "parameterizing", "name": "task", "recovery": {"error": error.as_dict()}}
        i18n.set_language("zh")
        self.assertIn("服务器拒绝脂质", format_job_status(value))


if __name__ == "__main__":
    unittest.main()
