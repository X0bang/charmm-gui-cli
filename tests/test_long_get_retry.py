"""Virtual-time command-scoped GET backoff with observable, safe task state."""

from argparse import Namespace
from contextlib import ExitStack, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli.remote import http_retry
from charmm_gui_cli.store import managed
from charmm_gui_cli.store.auth import atomic_json
from charmm_gui_cli.commands import build_command
from charmm_gui_cli.commands.terminal_output import format_job_status
from tests.test_recovery import response, token


class LongGetRetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.sleeps, self.events = [], []
        self.session = Mock(spec=requests.Session)
        self.session.cookies = {}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(http_retry.time, "monotonic", side_effect=lambda: self.now))
        self.stack.enter_context(patch.object(http_retry.time, "sleep", side_effect=self.sleep))

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def test_get_recovers_after_more_than_three_attempts(self):
        self.session.request.side_effect = [requests.ConnectionError("secret") for _ in range(4)] + [response()]
        with http_retry.long_get_retries(on_wait=self.events.append):
            result = http_retry.request(self.session, "GET", "https://example.invalid/secret")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.session.request.call_count, 5)
        self.assertEqual(self.sleeps, [1, 2, 4, 8])
        self.assertEqual(self.events[-1]["event"], "finished")
        self.assertNotIn("secret", json.dumps(self.events))

    def test_twenty_minute_window_is_bounded_and_waits_over_ten_minutes(self):
        self.session.request.side_effect = requests.ConnectionError("private detail")
        with http_retry.long_get_retries(1200, on_wait=self.events.append), self.assertRaises(requests.ConnectionError):
            http_retry.request(self.session, "GET", "https://example.invalid")
        self.assertGreater(self.now, 600)
        self.assertLessEqual(self.now, 1200)
        self.assertGreater(self.session.request.call_count, 3)
        self.assertLessEqual(max(self.sleeps), 30)

    def test_long_retry_after_is_honored_in_interruptible_chunks(self):
        unavailable = response(503, headers={"Retry-After": "300"})
        unavailable.close = Mock()
        self.session.request.side_effect = [unavailable, response()]
        with http_retry.long_get_retries(1200, on_wait=self.events.append):
            self.assertEqual(http_retry.request(self.session, "GET", "https://example.invalid").status_code, 200)
        self.assertEqual(sum(self.sleeps), 300)
        self.assertEqual(max(self.sleeps), 30)
        unavailable.close.assert_called_once()

    def test_retry_after_outside_window_returns_without_shortening_it(self):
        self.session.request.return_value = response(429, headers={"Retry-After": "1801"})
        with http_retry.long_get_retries(1200):
            result = http_retry.request(self.session, "GET", "https://example.invalid")
        self.assertEqual(result.status_code, 429)
        self.assertEqual(self.session.request.call_count, 1)
        self.assertFalse(self.sleeps)

    def test_post_is_never_retried_even_inside_long_get_context(self):
        self.session.request.return_value = response(503)
        with http_retry.long_get_retries(on_wait=self.events.append):
            self.assertEqual(http_retry.request(self.session, "POST", "https://example.invalid").status_code, 503)
        self.assertEqual(self.session.request.call_count, 1)
        self.assertFalse(self.sleeps)
        self.assertFalse(self.events)

    def test_ssl_auth_and_access_failures_stop_immediately(self):
        for failure in (requests.exceptions.SSLError("private certificate"), response(401), response(403)):
            self.session.reset_mock()
            self.session.request.side_effect = [failure]
            with http_retry.long_get_retries():
                if isinstance(failure, Exception):
                    with self.assertRaises(requests.exceptions.SSLError):
                        http_retry.request(self.session, "GET", "https://example.invalid")
                else:
                    self.assertEqual(http_retry.request(self.session, "GET", "https://example.invalid").status_code, failure.status_code)
            self.assertEqual(self.session.request.call_count, 1)
        self.assertFalse(self.sleeps)

    def test_interrupt_closes_pending_http_response_and_finishes_wait(self):
        item = response(503)
        item.close = Mock()
        self.session.request.return_value = item
        with patch.object(http_retry.time, "sleep", side_effect=KeyboardInterrupt), \
                http_retry.long_get_retries(on_wait=self.events.append), self.assertRaises(KeyboardInterrupt):
            http_retry.request(self.session, "GET", "https://example.invalid")
        item.close.assert_called_once()
        self.assertEqual(self.events[-1]["event"], "finished")
        self.assertEqual(self.session.request.call_count, 1)

    def test_context_does_not_change_standalone_query_or_other_context(self):
        for seconds in (0, 10):
            self.session.request.side_effect = requests.ConnectionError("private")
            with http_retry.long_get_retries(seconds), self.assertRaises(requests.ConnectionError):
                http_retry.request(self.session, "GET", "https://example.invalid")
        self.session.reset_mock()
        with self.assertRaises(requests.ConnectionError):
            http_retry.request(self.session, "GET", "https://example.invalid")
        self.assertEqual(self.session.request.call_count, 3)
        for bad in (True, -1, float("inf"), float("nan"), 1801, "1200"):
            with self.subTest(bad=bad), self.assertRaises(ValueError), http_retry.long_get_retries(bad):
                pass

    def test_build_wait_is_visible_in_status_and_cleared_after_network_recovery(self):
        root = Path(tempfile.mkdtemp(prefix="cgui-network-wait-"))
        run = root / "network/.charmm-gui"
        (run / "bilayer").mkdir(parents=True)
        atomic_json(run / "full-run.json", {"schema_version": 2, "state": "bilayer_submitted", "source_job_id": "123", "build_job_id": "456"})
        atomic_json(run / "bilayer/run.json", {"state": "submitted", "job_id": "456", "config": {"source_job_id": "123"}})
        atomic_json(run / "job.json", {"name": "network"})
        snapshots = []
        attempts = 0

        def network(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                value = managed.inspect_run(str(run))
                snapshots.append(value)
                self.assertTrue(value["network_wait"]["active"])
                self.assertIn("retry", format_job_status(value).lower())
            if attempts <= 4:
                raise requests.ConnectionError("private proxy information")
            return response(value={"status": "running"})

        self.session.request.side_effect = network
        args = Namespace(command="build-resume", directory=run, interval=30, max_wait=21600,
                         post_timeout=900, dry_run=False, wait=False, token_file=None, cookies=None,
                         grompp=False, no_grompp=True, gmx="gmx", no_preview=True)
        def advance(directory, web, api, **kwargs):
            self.assertEqual(api.status("456")["status"], "running")
            return {"state": "running", "source_job_id": "123", "build_job_id": "456"}

        output = io.StringIO()
        with patch("requests.Session", return_value=self.session), \
                patch("charmm_gui_cli.build_command.load_token", return_value=(token(), "fixture")), \
                patch("charmm_gui_cli.managed._registry", return_value=root / "index"), \
                patch("charmm_gui_cli.full_build.advance", side_effect=advance), \
                patch.dict("os.environ", {"CHARMM_GUI_GET_RETRY_SECONDS": "1200", "CHARMM_GUI_CLI_LANG": "en"}), \
                redirect_stderr(output):
            from charmm_gui_cli.i18n import set_language
            set_language("en")
            result, code = build_command.execute(args)
        self.assertEqual(code, 0)
        self.assertEqual(len(snapshots), 4)
        self.assertFalse(json.loads((run / "network-wait.json").read_text())["active"])
        log = (root / "network/run.log").read_text()
        self.assertNotIn("private proxy information", log)
        self.assertNotIn("\r", log)
        self.assertNotIn("\x1b", log)
