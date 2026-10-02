import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from charmm_gui_cli import full_build
from charmm_gui_cli.auth import ToolError, atomic_json, atomic_write


def rendered(stop=0, complete=True, errors="", next_step="2", identifier="123"):
    links = ""
    if complete:
        links = "".join(f'<a href="uploaded_pdb/{identifier}/{name}">{name}</a>'
                        for name in ("step1_pdbreader.pdb", "step1_pdbreader.psf", "lig/lig.rtf", "lig/lig.prm"))
    return f'''<script>var formstop={stop};var jobid='{identifier}';var project='membrane_bilayer';</script>
    <div class="strip"><a href="/?doc=input/pdbreader&jobid={identifier}&project=membrane_bilayer">Bookmark</a></div>
    <form action="?doc=input/membrane.bilayer&step={next_step}" method="post">
    <input name="jobid" value="{identifier}"></form>{links}{errors}'''


UPLOAD = '''<form method="post" action="?doc=input/pdbreader&step=2"><input name="jobid" value="123">
<input type="checkbox" name="chains[PROA][checked]" checked value="1">
<input type="checkbox" name="chains[HETA][checked]" value="1"></form>'''
SELECTION = '''<form method="post" action="?doc=input/pdbreader&step=3"><input name="jobid" value="123">
<input type="file" name="sdf_cgenff[LIG]">
<select name="terminal[PROA][first]"><option value="NTER">NTER</option></select>
<select name="terminal[PROA][last]"><option value="CTER">CTER</option></select></form>'''


class FullBuildTests(unittest.TestCase):
    def setUp(self):
        # User explicitly prohibited deletion: preserve artifacts after tests.
        self.root = Path(tempfile.mkdtemp(prefix="cgui-full-build-test-"))
        (self.root / "preparation").mkdir()
        (self.root / "inputs").mkdir()
        self.initial = self.root / "preparation" / "parameterization.html"
        self.config = {"ligand_resname": "LIG", "preparation": {"n_terminal": "NTER", "c_terminal": "CTER"}}
        self.manifest = {"schema_version": 2, "state": "parameterizing", "source_job_id": "123",
                         "config": self.config, "inputs": {"files": {
                             "complex.pdb": str(self.root / "inputs" / "complex.pdb"),
                             "ligand.sdf": str(self.root / "inputs" / "ligand.sdf")}}}
        self.web = Mock()

    def await_page(self, html, wait=False):
        atomic_write(self.initial, html)
        full_build._await_preparation(self.web, self.root, self.manifest, self.initial, wait, 10, 60)

    def test_complete_stage_requires_structure_and_ligand_parameter_links(self):
        self.await_page(rendered())
        self.assertEqual(self.manifest["state"], "prepared")
        self.assertEqual(len(self.manifest["preparation_artifacts"]), 4)
        self.web.request.assert_not_called()

    def test_formstop_zero_without_stage_artifacts_is_not_prepared(self):
        with self.assertRaisesRegex(ToolError, "completion was not established"):
            self.await_page(rendered(complete=False))
        self.assertEqual(self.manifest["state"], "preparation_unrecognized")

    def test_wrong_next_stage_is_not_prepared(self):
        with self.assertRaises(ToolError):
            self.await_page(rendered(next_step="4"))

    def test_actual_error_fails_even_with_ready_form_and_artifact_links(self):
        with self.assertRaisesRegex(ToolError, "preparation failed"):
            self.await_page(rendered(errors='<div id="error_msg">CGenFF failed</div>'))
        self.assertEqual(self.manifest["preparation_errors"], ["CGenFF failed"])
        self.web.submit_snapshot.assert_not_called()

    def test_job_mismatch_refuses_to_continue(self):
        with self.assertRaisesRegex(ToolError, "different job"):
            self.await_page(rendered(identifier="999"))

    def test_pending_without_wait_persists_then_resume_gets_fresh_page(self):
        self.await_page(rendered(stop=2, complete=False))
        self.assertEqual(self.manifest["state"], "preparation_running")
        self.web.request.assert_not_called()
        self.web.request.return_value = Mock(text=rendered())
        full_build._await_preparation(self.web, self.root, self.manifest, self.initial, False, 10, 60)
        self.assertEqual(self.manifest["state"], "prepared")
        self.assertEqual(self.web.request.call_args.args[0], "GET")
        self.web.submit_snapshot.assert_not_called()

    def test_waiting_polls_only_get(self):
        self.web.request.return_value = Mock(text=rendered())
        with patch.object(full_build.time, "sleep"):
            self.await_page(rendered(stop=2, complete=False), wait=True)
        self.assertEqual(self.manifest["state"], "prepared")
        self.assertEqual(self.web.request.call_args.args[0], "GET")

    def test_expired_session_can_resume_after_login(self):
        with self.assertRaisesRegex(ToolError, "session expired"):
            self.await_page('<form><input type="password"></form>')
        self.web.request.return_value = Mock(text=rendered())
        full_build._await_preparation(self.web, self.root, self.manifest, self.initial, False, 10, 60)
        self.assertEqual(self.manifest["state"], "prepared")

    def test_existing_post_response_is_not_submitted_again(self):
        atomic_write(self.initial, rendered())
        full_build._submit(self.web, "unused", self.initial, {})
        self.web.submit_snapshot.assert_not_called()

    def test_full_advance_uses_prepared_file_paths_and_resume_never_reposts(self):
        self.root = self.root / "fresh-run"
        self.root.mkdir()
        atomic_json(self.root / "full-run.json", self.manifest)
        uploaded = self.root / "preparation" / "upload-response.html"
        selected = self.root / "preparation" / "selection.html"
        def upload(pdb, directory):
            self.assertEqual(pdb, self.manifest["inputs"]["files"]["complex.pdb"])
            directory.mkdir()
            atomic_write(uploaded, UPLOAD)
        def submit(source, destination, fields, uploads=None):
            if Path(destination) == selected:
                self.assertEqual(fields["chains[HETA][checked]"], "1")
                atomic_write(destination, SELECTION)
            else:
                self.assertEqual(uploads["sdf_cgenff[LIG]"], self.manifest["inputs"]["files"]["ligand.sdf"])
                atomic_write(destination, rendered())
        def start(api, config, directory):
            directory.mkdir()
            atomic_json(directory / "run.json", {"job_id": "456"})
            return {"job_id": "456", "state": "submitted"}
        self.web.upload_structure.side_effect = upload
        self.web.submit_snapshot.side_effect = submit
        with patch.object(full_build, "api_config", return_value={}), patch.object(full_build, "start_api", side_effect=start) as start_mock:
            result = full_build.advance(self.root, self.web, Mock())
            self.assertEqual(result["state"], "bilayer_submitted")
            self.assertEqual(self.web.submit_snapshot.call_count, 2)
            with patch.object(full_build, "resume_api", return_value={"job_id": "456", "state": "running"}):
                result = full_build.advance(self.root, self.web, Mock())
            self.assertEqual(result["state"], "running")
            self.assertEqual(self.web.upload_structure.call_count, 1)
            self.assertEqual(self.web.submit_snapshot.call_count, 2)
            start_mock.assert_called_once()

    def test_failed_preparation_never_starts_bilayer(self):
        atomic_json(self.root / "full-run.json", self.manifest)
        atomic_write(self.root / "preparation" / "upload-response.html", UPLOAD)
        atomic_write(self.root / "preparation" / "selection.html", SELECTION)
        atomic_write(self.initial, rendered(stop=1, errors='<div id="error_msg">No ligand parameters</div>'))
        with patch.object(full_build, "start_api") as start:
            with self.assertRaises(ToolError):
                full_build.advance(self.root, self.web, Mock())
            start.assert_not_called()
        self.web.submit_snapshot.assert_not_called()

    def test_ambiguous_post_intent_is_not_repeated(self):
        atomic_json(self.initial.with_suffix(".intent.json"), {"state": "submitting"})
        with self.assertRaisesRegex(ToolError, "unresolved submission intent"):
            full_build._submit(self.web, "unused", self.initial, {})
        self.web.submit_snapshot.assert_not_called()

    def test_ambiguous_upload_is_not_repeated(self):
        atomic_json(self.root / "full-run.json", self.manifest)
        with self.assertRaisesRegex(ToolError, "prior upload"):
            full_build.advance(self.root, self.web, Mock())
        self.web.upload_structure.assert_not_called()

    def test_existing_bilayer_resumes_without_cookie_or_preparation_pages(self):
        atomic_json(self.root / "full-run.json", self.manifest)
        (self.root / "bilayer").mkdir()
        atomic_json(self.root / "bilayer" / "run.json", {"job_id": "456"})
        with patch.object(full_build, "resume_api", return_value={"job_id": "456", "state": "complete", "validation": {"passed": True}}):
            result = full_build.advance(self.root, None, Mock())
        self.assertEqual(result["state"], "complete")
        self.assertTrue(result["validation"]["passed"])

    def test_legacy_relative_snapshot_resolves_under_run_from_other_cwd(self):
        self.assertEqual(full_build._snapshot_path(self.root, "runs/old-run/preparation/poll-123.html"),
                         self.root / "preparation" / "poll-123.html")
        self.manifest["preparation_snapshot"] = "preparation/parameterization.html"
        self.await_page(rendered())
        self.assertEqual(self.manifest["state"], "prepared")


if __name__ == "__main__":
    unittest.main()
