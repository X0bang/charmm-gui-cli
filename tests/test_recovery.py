"""Local-only retry and durable recovery regression tests (retain artifacts)."""

import base64
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli import full_build, workflow
from charmm_gui_cli.api import AuthError, Client, SubmissionUnknown
from charmm_gui_cli.auth import ToolError, atomic_json, atomic_write
from charmm_gui_cli.http_retry import request, retry_delay
from charmm_gui_cli.web import WebClient


def token(expiry=None):
    encoded = base64.urlsafe_b64encode(json.dumps({"exp": expiry or time.time() + 3600}).encode()).decode().rstrip("=")
    return "header." + encoded + ".signature"


def response(status=200, value=None, headers=None):
    item = requests.Response()
    item.status_code = status
    item._content = json.dumps(value or {"status": "pending"}).encode()
    item._content_consumed = True
    item.headers.update(headers or {})
    return item


def config():
    return {"version": 1, "source_job_id": "123", "expected_ligands": ["LIG"],
            "preparation": {"ligand_parameters_ready": True, "orientation": "ppm"},
            "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20}}


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(spec=requests.Session)
        self.sleep = patch("charmm_gui_cli.http_retry.time.sleep").start()
        self.addCleanup(patch.stopall)

    def test_get_connection_and_http_failures_retry_then_succeed(self):
        unavailable = response(503, headers={"Retry-After": "30"})
        unavailable.close = Mock()
        self.session.request.side_effect = [requests.ConnectionError("SECRET"), unavailable, response()]
        self.assertEqual(Client(token(), self.session).status("234")["status"], "pending")
        self.assertEqual(self.session.request.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1.0, 30.0])
        unavailable.close.assert_called_once()

    def test_all_transient_statuses_are_bounded_and_actionable(self):
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                self.session.reset_mock()
                self.session.request.side_effect = [response(status) for _ in range(3)]
                with self.assertRaises(ToolError) as caught:
                    Client(token(), self.session).status("234")
                self.assertEqual(caught.exception.category, "rate_limited" if status == 429 else "transient_http")
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(self.session.request.call_count, 3)

    def test_timeout_exhaustion_does_not_leak_exception_or_headers(self):
        self.session.request.side_effect = requests.Timeout("PASSWORD_SECRET")
        with self.assertRaises(ToolError) as caught:
            Client(token(), self.session).status("234")
        self.assertEqual(caught.exception.category, "transient_network")
        self.assertNotIn("PASSWORD_SECRET", json.dumps(caught.exception.as_dict()))
        self.assertEqual(self.session.request.call_count, 3)

    def test_auth_and_permissions_are_not_retried(self):
        for status, category in ((401, "authentication"), (403, "access_denied")):
            self.session.reset_mock()
            self.session.request.return_value = response(status)
            with self.assertRaises(AuthError) as caught:
                Client(token(), self.session).status("234")
            self.assertEqual(caught.exception.category, category)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(self.session.request.call_count, 1)

    def test_ssl_failure_not_retried(self):
        self.session.request.side_effect = requests.exceptions.SSLError("bad certificate")
        with self.assertRaises(requests.exceptions.SSLError):
            request(self.session, "GET", "https://example.invalid")
        self.assertEqual(self.session.request.call_count, 1)

    def test_interrupted_get_body_is_retryable_but_tls_error_is_not(self):
        self.session.request.side_effect = [requests.exceptions.ChunkedEncodingError("truncated"), response()]
        self.assertEqual(Client(token(), self.session).status("234")["status"], "pending")
        self.assertEqual(self.session.request.call_count, 2)
        self.session.request.side_effect = requests.exceptions.SSLError("bad certificate")
        with self.assertRaises(ToolError) as caught:
            Client(token(), self.session).status("234")
        self.assertEqual(caught.exception.category, "transport_error")
        self.assertFalse(caught.exception.retryable)

    def test_post_errors_never_retry_and_remain_ambiguous(self):
        for failure in (response(503), response(401), requests.Timeout("secret")):
            self.session.reset_mock()
            self.session.request.side_effect = [failure]
            with self.assertRaises(SubmissionUnknown) as caught:
                Client(token(), self.session).submit({})
            self.assertEqual(caught.exception.category, "submission_unknown")
            self.assertEqual(self.session.request.call_count, 1)
        self.sleep.assert_not_called()

    def test_api_post_auth_cause_and_action_survive_ambiguity(self):
        self.session.request.return_value = response(401)
        with self.assertRaises(SubmissionUnknown) as caught:
            Client(token(), self.session).submit({})
        error = caught.exception
        self.assertEqual(error.category, "submission_unknown")
        self.assertEqual(error.as_dict()["cause_category"], "authentication")
        self.assertIn("login", error.next_step)
        self.assertIn("attach", error.next_step)
        self.assertFalse(error.retryable)
        self.assertEqual(self.session.request.call_count, 1)

    def test_api_post_retry_after_retained_but_never_replayed(self):
        self.session.request.return_value = response(429, headers={"Retry-After": "300"})
        with self.assertRaises(SubmissionUnknown) as caught:
            Client(token(), self.session).submit({})
        error = caught.exception
        self.assertEqual(error.cause_category, "rate_limited")
        self.assertEqual(error.as_dict()["retry_after_seconds"], 300)
        self.assertFalse(error.retryable)
        self.assertEqual(self.session.request.call_count, 1)
        self.sleep.assert_not_called()

    def test_absent_cause_is_omitted_from_serialized_error(self):
        self.assertNotIn("cause_category", ToolError("example").as_dict())

    def test_retry_after_dates_and_invalid_values(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(hours=1))
        past = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
        self.assertGreater(retry_delay(future, 0), 3500)
        self.assertEqual(retry_delay(past, 0), 0)
        for invalid in (None, "nonsense", "nan", "inf"):
            self.assertEqual(retry_delay(invalid, 1), 2)

    def test_long_retry_after_returns_without_early_retry(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(hours=1))
        for value in ("999", future):
            self.session.reset_mock()
            self.session.request.return_value = response(429, headers={"Retry-After": value})
            with self.assertRaises(ToolError) as caught:
                Client(token(), self.session).status("234")
            self.assertEqual(caught.exception.category, "rate_limited")
            self.assertGreater(caught.exception.as_dict()["retry_after_seconds"], 30)
            self.assertEqual(self.session.request.call_count, 1)
            self.sleep.assert_not_called()

    def test_web_get_retries_but_upload_does_not(self):
        self.session.headers = {}
        self.session.cookies = requests.cookies.RequestsCookieJar()
        client = WebClient(session=self.session)
        self.session.request.side_effect = [response(504), response()]
        self.assertEqual(client.request("GET", "?doc=input/pdbreader").status_code, 200)
        self.assertEqual(self.session.request.call_count, 2)
        self.session.reset_mock()
        self.session.request.side_effect = requests.ConnectionError("secret")
        with self.assertRaises(SubmissionUnknown):
            client.request("POST", "?doc=input/pdbreader", files={})
        self.assertEqual(self.session.request.call_count, 1)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-recovery-test-"))
        self.directory = self.root / "build"
        self.client = Mock()
        self.client.submit.return_value = "234"

    def test_expired_auth_creates_no_api_submission_intent(self):
        session = Mock(spec=requests.Session)
        client = Client(token(1), session)
        with self.assertRaises(AuthError):
            workflow.start(client, config(), self.directory)
        self.assertFalse(self.directory.exists())
        with self.assertRaises(AuthError):
            client.submit({})
        session.request.assert_not_called()

    def test_network_error_retains_id_and_resume_only_polls(self):
        workflow.start(self.client, config(), self.directory)
        self.client.status.side_effect = ToolError("Network unavailable", category="transient_network", retryable=True)
        with self.assertRaises(ToolError):
            workflow.resume(self.client, self.directory)
        saved = workflow.read(self.directory)
        self.assertEqual(saved["job_id"], "234")
        self.assertEqual(saved["last_error"]["category"], "transient_network")
        self.client.status.side_effect = None
        self.client.status.return_value = {"status": "running"}
        saved = workflow.resume(self.client, self.directory)
        self.assertNotIn("last_error", saved)
        self.assertEqual(self.client.submit.call_count, 1)

    def test_submission_interrupt_is_durable_unknown_not_replayed(self):
        self.client.submit.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            workflow.start(self.client, config(), self.directory)
        saved = workflow.read(self.directory)
        self.assertEqual(saved["state"], "submission_unknown")
        self.assertEqual(saved["last_error"]["category"], "submission_unknown")
        with self.assertRaises(SubmissionUnknown):
            workflow.resume(self.client, self.directory)
        self.assertEqual(self.client.submit.call_count, 1)

    def test_poll_interrupt_retains_running_state(self):
        workflow.start(self.client, config(), self.directory)
        self.client.status.return_value = {"status": "running"}
        with patch("charmm_gui_cli.workflow.time.sleep", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                workflow.resume(self.client, self.directory, wait=True)
        saved = workflow.read(self.directory)
        self.assertEqual(saved["state"], "running")
        self.assertEqual(saved["last_error"]["category"], "interrupted")
        self.assertEqual(saved["job_id"], "234")

    def test_remote_error_is_terminal_not_retryable(self):
        workflow.start(self.client, config(), self.directory)
        self.client.status.return_value = {"status": "error"}
        with self.assertRaises(ToolError) as caught:
            workflow.resume(self.client, self.directory)
        self.assertEqual(caught.exception.category, "remote_failure")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(workflow.read(self.directory)["state"], "remote_error")

    def test_expired_cookie_preflight_leaves_no_post_intent(self):
        snapshot = self.root / "page.html"
        atomic_write(snapshot, '<form action="?doc=input/pdbreader"><input name="jobid" value="123"></form>')
        client = WebClient(self.root / "missing.cookies.json")
        with self.assertRaises(AuthError):
            client.submit_snapshot(snapshot, self.root / "result.html")
        self.assertFalse((self.root / "result.intent.json").exists())

    def test_modeling_post_http_auth_failure_is_durable_unknown(self):
        snapshot = self.root / "page.html"
        output = self.root / "result.html"
        atomic_write(snapshot, '<form action="?doc=input/pdbreader"><input name="jobid" value="123"></form>')
        session = Mock(spec=requests.Session)
        session.headers = {}
        session.cookies = requests.cookies.RequestsCookieJar()
        session.request.return_value = response(401)
        client = WebClient(session=session)
        with self.assertRaises(SubmissionUnknown) as caught:
            client.submit_snapshot(snapshot, output)
        self.assertEqual(caught.exception.cause_category, "authentication")
        self.assertEqual(caught.exception.as_dict()["cause_category"], "authentication")
        self.assertFalse(caught.exception.retryable)
        intent = json.loads(output.with_suffix(".intent.json").read_text())
        self.assertEqual(intent["state"], "submission_unknown")
        self.assertEqual(intent["last_error"]["cause_category"], "authentication")
        self.assertIn("login", intent["last_error"]["next_step"])
        with self.assertRaises(SubmissionUnknown):
            client.submit_snapshot(snapshot, output)
        self.assertEqual(session.request.call_count, 1)

    def test_website_post_retains_rate_limit_without_retry(self):
        client = WebClient()
        intent_path = self.root / "modeling.intent.json"
        error = ToolError("HTTP 429", category="rate_limited", retry_after_seconds=120)
        with patch.object(client, "request", side_effect=error) as request_mock:
            with self.assertRaises(SubmissionUnknown) as caught:
                client._modeling_post("?doc=input/pdbreader", intent_path, {"state": "submitting"})
        self.assertEqual(caught.exception.cause_category, "rate_limited")
        self.assertEqual(caught.exception.retry_after_seconds, 120)
        self.assertFalse(caught.exception.retryable)
        request_mock.assert_called_once()
        saved = json.loads(intent_path.read_text())["last_error"]
        self.assertEqual(saved["retry_after_seconds"], 120)
        self.assertEqual(saved["cause_category"], "rate_limited")

    def test_modeling_post_interrupt_keeps_unknown_intent(self):
        snapshot = self.root / "page.html"
        output = self.root / "result.html"
        atomic_write(snapshot, '<form action="?doc=input/pdbreader"><input name="jobid" value="123"></form>')
        client = WebClient()
        with patch.object(client, "request", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                client.submit_snapshot(snapshot, output)
        intent = json.loads(output.with_suffix(".intent.json").read_text())
        self.assertEqual(intent["state"], "submission_unknown")

    def test_full_build_local_auth_failure_keeps_inputs_resumable(self):
        self.directory.mkdir()
        manifest = {"schema_version": 2, "state": "inputs_ready", "inputs": {"files": {}}, "config": {}}
        atomic_json(self.directory / "full-run.json", manifest)
        api = Client(token(1))
        web = Mock()
        with self.assertRaises(AuthError):
            full_build.advance(self.directory, web, api)
        saved = json.loads((self.directory / "full-run.json").read_text())
        self.assertEqual(saved["state"], "inputs_ready")
        self.assertEqual(saved["last_error"]["category"], "authentication")
        self.assertFalse((self.directory / "preparation").exists())
        web.upload_structure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
