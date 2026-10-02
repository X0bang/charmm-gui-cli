import unittest

from charmm_gui_cli.web_state import interpret_api_job_status, interpret_monitor, parse_page_state


def page(stop=0, extra="", action="./?doc=input/pdbreader&step=3"):
    return f'''<script>var jobid='123'; var project='membrane_bilayer';
    var nstep=1; var step='reader'; var formstop={stop};
    function alertstop() {{ alert('CHARMM was terminated abnormally.'); }}</script>
    <div class="strip">Bookmark <a href="/?doc=input/pdbreader&jobid=123&project=membrane_bilayer">link</a></div>
    <form method="post" action="{action}"><input name="jobid" value="123"></form>
    <table id="nextBtn"><div class="nav_title">Generate PDB</div></table>{extra}'''


class WebStateTests(unittest.TestCase):
    def test_ready_step_is_not_entire_workflow_success(self):
        state = parse_page_state(page())
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["next_method"], "POST")
        self.assertFalse(state["workflow_complete"])
        self.assertEqual(state["step_name"], "reader")
        self.assertTrue(state["monitor_url"].endswith("jobid=123&update=1"))

    def test_generic_javascript_error_strings_do_not_fail_ready_page(self):
        self.assertEqual(parse_page_state(page())["errors"], [])

    def test_formstop_error_disables_submission(self):
        state = parse_page_state(page(1, '<div id="error_msg">CGenFF failed</div><div id="error_msg">monovalent carbon</div>'))
        self.assertEqual(state["state"], "error")
        self.assertEqual(len(state["errors"]), 2)
        self.assertIsNone(state["next_url"])

    def test_running_information_can_appear_in_error_container(self):
        state = parse_page_state(page(2, '<div id="error_msg">Process is still running.</div>'))
        self.assertEqual(state["state"], "running")
        self.assertIsNone(state["next_url"])
        self.assertIn("jobid=123", state["recovery_url"])

    def test_error_block_wins_over_formstop_zero(self):
        self.assertEqual(parse_page_state(page(0, '<div id="error_msg">Could not read ligand</div>'))["state"], "error")

    def test_login_wins_over_stale_job_markup(self):
        state = parse_page_state(page() + '<form><input type="password" name="password"></form>')
        self.assertEqual(state["state"], "auth_required")

    def test_next_button_label_is_not_proof_of_success(self):
        state = parse_page_state('<div id="nextBtn"><div class="nav_title">Build Membrane</div></div>')
        self.assertEqual(state["state"], "unknown")
        self.assertIsNone(state["next_url"])

    def test_external_action_is_not_exposed_as_next_step(self):
        for action in ("https://example.org/?doc=input/pdbreader", "http://charmm-gui.org/?doc=input/pdbreader",
                       "https://user:pass@charmm-gui.org/?doc=input/pdbreader", "https://charmm-gui.org:8443/?doc=input/pdbreader",
                       "https://charmm-gui.org:bad/?doc=input/pdbreader"):
            with self.subTest(action=action):
                self.assertIsNone(parse_page_state(page(action=action))["next_url"])

    def test_only_current_job_artifacts_are_reported(self):
        links = '<a href="uploaded_pdb/123/step1.pdb">PDB</a><a href="uploaded_pdb/124/other.pdb">Other</a>'
        self.assertEqual(len(parse_page_state(page(extra=links))["artifacts"]), 1)

    def test_initial_chain_form_without_formstop_can_be_ready(self):
        html = '<form method="post" action="?doc=input/pdbreader&step=2"><input name="jobid" value="123"></form>'
        self.assertEqual(parse_page_state(html)["state"], "ready")

    def test_missing_or_invalid_job_does_not_create_monitor_url(self):
        self.assertIsNone(parse_page_state('<script>var jobid="../../bad";</script>')["monitor_url"])

    def test_monitor_never_confirms_success(self):
        for content in ("Waiting output...\nLast Updated: 12:38", "NORMAL TERMINATION BY NORMAL STOP", ""):
            self.assertFalse(interpret_monitor(content)["completion_confirmed"])

    def test_null_api_status_and_empty_global_queue_not_success(self):
        state = interpret_api_job_status({"status": None, "rqinfo": "default | 0 jobs", "pidfile": ""})
        self.assertEqual(state["status"], "unknown")
        self.assertTrue(state["requires_page_check"])

    def test_api_done_does_not_confirm_whole_web_workflow(self):
        state = interpret_api_job_status({"status": "done"})
        self.assertTrue(state["requires_page_check"])
        self.assertFalse(state["workflow_complete"])


if __name__ == "__main__":
    unittest.main()
