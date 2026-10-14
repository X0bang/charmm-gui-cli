import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from charmm_gui_cli.store.auth import atomic_json, atomic_write
from charmm_gui_cli.remote.reconcile import reconcile


OLD = '''<script>var nstep=1; var formstop=0;</script><form action="?doc=input/pdbreader&amp;step=2">
<input name="jobid" value="123"><input name="chains[PROA][checked]"></form>'''


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp()).resolve()
        self.preparation = self.directory / "preparation"
        atomic_json(self.directory / "full-run.json", {"schema_version": 2, "source_job_id": "123", "state": "parameterizing"})
        atomic_write(self.preparation / "upload-response.html", OLD)
        atomic_json(self.preparation / "selection.intent.json", {"state": "submission_unknown", "fields": [["jobid", "123"]]})
        self.web = Mock()

    def test_accepted_page_saves_response_without_post(self):
        self.web.request.return_value.text = '''<form action="?doc=input/pdbreader&amp;step=3"><input name="jobid" value="123">
<input type="file" name="sdf_cgenff[LIG]"></form>'''
        result = reconcile(self.directory, self.web)
        self.assertEqual(result["state"], "response_received")
        self.assertTrue((self.preparation / "selection.html").is_file())
        self.assertEqual(self.web.request.call_args.args[0], "GET")
        self.assertEqual(self.web.request.call_count, 1)

    def test_same_step_requires_explicit_confirmation(self):
        self.web.request.return_value.text = OLD
        result = reconcile(self.directory, self.web)
        self.assertEqual(result["state"], "confirmation_required")
        self.assertTrue((self.preparation / "selection.intent.json").exists())
        result = reconcile(self.directory, self.web, confirm_not_submitted=True)
        self.assertEqual(result["state"], "not_submitted_confirmed")
        self.assertFalse((self.preparation / "selection.intent.json").exists())
        self.assertTrue(Path(result["retired_intent"]).exists())
        self.assertTrue(all(call.args[0] == "GET" for call in self.web.request.call_args_list))

    def test_unknown_page_keeps_intent_even_with_confirmation(self):
        self.web.request.return_value.text = "<p>Unrecognized page</p>"
        result = reconcile(self.directory, self.web, confirm_not_submitted=True)
        self.assertEqual(result["state"], "unknown")
        self.assertTrue((self.preparation / "selection.intent.json").exists())
        self.assertEqual(self.web.request.call_args.args[0], "GET")
