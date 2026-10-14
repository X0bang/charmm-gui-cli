"""Sanitized remote failures are useful, durable and never trigger a replay."""

import charmm_gui_cli.store.tasks as _task_store

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli.remote.diagnostics import diagnostic
from charmm_gui_cli.remote.api import Client, SubmissionUnknown
from charmm_gui_cli.pipeline import managed
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.presentation.terminal_output import format_job_status
from charmm_gui_cli.cli import main
from charmm_gui_cli.locales.i18n import set_language
from tests.test_recovery import token, response


class DiagnosticTests(unittest.TestCase):
    def tearDown(self):
        set_language()

    def test_variants_of_lipid_rejection_are_preserved_without_exact_match(self):
        for error in ("Unknown lipid POPX requested in upper leaflet", {"message": "POPCX is not in the lipid library"},
                      [{"reason": "Cannot generate lipid POPX"}], True):
            body = {"error": error}
            if error is True:
                body["message"] = "Invalid membrane lipid: POPX"
            with self.subTest(error=error), self.assertRaises(ToolError) as caught:
                Client._json(response(value=body))
            self.assertIn("lipid", caught.exception.server_message.lower())
            self.assertNotIn("--remote-check", caught.exception.next_step)
            self.assertNotIn("without a safely displayable diagnostic", caught.exception.localized())

    def test_redaction_precedes_truncation_and_handles_structures_html_and_controls(self):
        password = "a complicated password with spaces"
        cookie = "a_cookie_value_1234"
        payload = {"message": '<p>Error parsing ATOM\x1b[31m</p><input name="password" value="hidden_pw"> '
                              f'{password}; hidden_pw; {cookie}; token="embedded value"; '
                              "Bearer raw_auth_value; https://user:pass@example.invalid/path\n" + "X" * 500,
                   "password": password, "cookie": cookie, "body": "private_body",
                   "debug": {"token": "nested_token", "value": "nested_token"}}
        text = diagnostic(payload)
        self.assertIn("Error parsing ATOM", text)
        self.assertLessEqual(len(text), 300)
        for secret in (password, cookie, "hidden_pw", "embedded value", "raw_auth_value", "user:pass", "private_body", "nested_token", "\x1b", "\n"):
            self.assertNotIn(secret, text)

    def test_known_active_credentials_and_sensitive_sibling_values_are_removed(self):
        active_token = token()
        session = Mock(spec=requests.Session)
        session.cookies = {"session": "raw-cookie"}
        session.request.return_value = response(value={"error": f"Cannot generate lipid POPX: {active_token}; raw-cookie; sibling-secret",
                                                       "token": "sibling-secret"})
        with self.assertRaises(ToolError) as caught:
            Client(active_token, session).status("456")
        serialized = json.dumps(caught.exception.as_dict())
        for secret in (active_token, "raw-cookie", "sibling-secret"):
            self.assertNotIn(secret, serialized)
        self.assertIn("POPX", serialized)

    def test_login_json_failure_has_no_server_diagnostic(self):
        session = Mock(spec=requests.Session)
        session.request.return_value = response(value={"error": "credentials echoed: private-login-password"})
        with self.assertRaises(ToolError) as caught:
            Client(session=session).login("user@example.invalid", "private-login-password")
        self.assertIsNone(caught.exception.server_message)
        self.assertNotIn("private-login-password", caught.exception.localized())

    def test_secret_lipid_identifier_is_not_reflected_from_known_format(self):
        with self.assertRaises(ToolError) as caught:
            Client._json(response(value={"error": "Invalid lipid name private", "password": "private"}))
        self.assertNotIn("private", json.dumps(caught.exception.as_dict()))

    def test_http_error_preserves_safe_message_not_full_response(self):
        session = Mock(spec=requests.Session)
        session.request.return_value = response(400, {"error": "Membrane lipid POPX was rejected", "body": "private-body"})
        with self.assertRaises(ToolError) as caught:
            Client(token(), session).status("456")
        self.assertEqual(caught.exception.category, "http_error")
        self.assertIn("POPX", caught.exception.server_message)
        self.assertNotIn("private-body", json.dumps(caught.exception.as_dict()))

    def test_post_diagnostic_survives_unknown_submission_without_retry(self):
        session = Mock(spec=requests.Session)
        session.request.return_value = response(value={"error": "Cannot build unknown lipid POPX"})
        with self.assertRaises(SubmissionUnknown) as caught:
            Client(token(), session).submit({})
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(caught.exception.cause_category, "api_error")
        self.assertFalse(caught.exception.retryable)
        self.assertIn("POPX", caught.exception.server_message)

    def test_api_error_is_persisted_and_rendered_in_both_languages(self):
        root = Path(tempfile.mkdtemp(prefix="cgui-server-error-"))
        run = root / "sample/.charmm-gui"
        run.mkdir(parents=True)
        atomic_json(run / "full-run.json", {"schema_version": 2, "state": "submission_unknown"})
        atomic_json(run / "job.json", {"name": "sample"})
        error = SubmissionUnknown("Uncertain response", cause_category="api_error", server_message="Unknown lipid POPX")
        _task_store.record_recovery(run, error)
        value = managed.inspect_run(str(run))
        self.assertEqual(value["recovery"]["error"]["server_message"], "Unknown lipid POPX")
        self.assertNotIn(" NAME", value["recovery"]["error"]["next_step"])
        for language in ("en", "zh"):
            set_language(language)
            text = format_job_status(value)
            self.assertIn("Unknown lipid POPX", text)
            self.assertIn("jobs reconcile sample", text)

    def test_remote_check_next_step_is_local_and_query_never_changes_records(self):
        root = Path(tempfile.mkdtemp(prefix="cgui-check-error-"))
        run = root / "sample/.charmm-gui"
        (run / "bilayer").mkdir(parents=True)
        atomic_json(run / "full-run.json", {"schema_version": 2, "state": "remote_error", "source_job_id": "123", "build_job_id": "456"})
        atomic_json(run / "bilayer/run.json", {"state": "remote_error", "job_id": "456", "config": {"source_job_id": "123"}})
        atomic_json(run / "job.json", {"name": "sample"})
        before = {path: path.read_bytes() for path in run.rglob("*.json")}
        session = Mock(spec=requests.Session)
        session.request.return_value = response(value={"error": "Unknown lipid POPX"})
        output = io.StringIO()
        with patch("charmm_gui_cli.store.auth.load_token", return_value=(token(), "fixture")), \
                patch("requests.Session", return_value=session), redirect_stderr(output):
            self.assertEqual(main(["jobs", "status", str(run), "--remote-check", "--lang", "en"]), 2)
        self.assertIn("Unknown lipid POPX", output.getvalue())
        self.assertIn("jobs status sample", output.getvalue())
        self.assertNotIn("--remote-check", output.getvalue())
        self.assertNotIn(" NAME", output.getvalue())
        self.assertEqual(session.request.call_args.args[0], "GET")
        self.assertEqual(before, {path: path.read_bytes() for path in run.rglob("*.json")})

    def test_oversized_or_unrecognized_error_fields_are_not_rendered(self):
        self.assertIsNone(diagnostic("x" * 65537))
        self.assertIsNone(diagnostic({"body": "untrusted-response-body"}))

    def test_bearer_header_and_multiple_cookies_do_not_leave_unlabeled_secrets(self):
        text = diagnostic("Unknown lipid POPX\nAuthorization: Bearer opaque-value\nCookie: a=one; b=two\nTry another lipid")
        self.assertIn("Unknown lipid POPX", text)
        for secret in ("opaque-value", "one", "two"):
            self.assertNotIn(secret, text)

    def test_url_encoded_and_multiline_known_credentials_are_removed(self):
        self.assertNotIn("a/b", diagnostic("Lipid failure a%2Fb", secrets=["a/b"]))
        self.assertNotIn("a b", diagnostic("Lipid failure a\nb", secrets=["a\nb"]))

    def test_build_error_log_contains_safe_remote_reason_and_stage_ids(self):
        from argparse import Namespace
        import sys
        from charmm_gui_cli.pipeline import build_command
        from charmm_gui_cli.store.run_logging import LogStream
        root = Path(tempfile.mkdtemp(prefix="cgui-error-log-"))
        run = root / "sample/.charmm-gui"
        run.mkdir(parents=True)
        atomic_json(run / "full-run.json", {"schema_version": 2, "state": "submitting_bilayer", "source_job_id": "123", "build_job_id": "456"})
        atomic_json(run / "job.json", {"name": "sample"})

        def fail(args, holder):
            holder.append(run)
            sys.stderr = LogStream(sys.stderr, root / "sample/run.log")
            raise SubmissionUnknown("Uncertain response", cause_category="api_error", server_message="Unknown lipid POPX")

        set_language("en")
        with redirect_stderr(io.StringIO()), patch.object(build_command, "_execute", side_effect=fail), self.assertRaises(SubmissionUnknown):
            build_command.execute(Namespace())
        log = (root / "sample/run.log").read_text()
        for expected in ("Source job 123", "Bilayer job 456", "Unknown lipid POPX", "jobs", "sample"):
            self.assertIn(expected, log)
        self.assertNotIn(" NAME", log)

    def test_network_guidance_separates_sentences_and_binds_task_name(self):
        set_language("en")
        error = ToolError("Network interrupted", category="transient_network").for_task("sample")
        self.assertIn("Retry-After. After", error.next_step)
        self.assertNotIn("Retry-After.After", error.next_step)
        self.assertNotIn(" NAME", error.next_step)
