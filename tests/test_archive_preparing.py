"""Observed on-demand compression is pending, not a corrupt or final archive."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli import workflow
from charmm_gui_cli.api import ARCHIVE_PREPARING_MESSAGE, ARCHIVE_PREPARING_MESSAGES, Client
from charmm_gui_cli.auth import ToolError
from charmm_gui_cli.progress import describe_progress
from tests.test_client import archive_bytes, config, pdb, token


def raw_response(content):
    response = requests.Response()
    response.status_code = 200
    response._content = content
    response._content_consumed = True
    return response


class ArchivePreparingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-archive-preparing-"))
        self.session = Mock(spec=requests.Session)

    def test_exact_observed_message_retains_response_without_publishing_archive(self):
        observed = [message + suffix for message in ARCHIVE_PREPARING_MESSAGES for suffix in (b"", b"\n", b"\r\n")]
        for index, content in enumerate(observed):
            directory = self.root / str(index)
            self.session.request.return_value = raw_response(content)
            with self.assertRaises(ToolError) as caught:
                Client(token(), self.session).download("234", directory / "charmm-gui.tgz")
            self.assertEqual(caught.exception.category, "archive_preparing")
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(caught.exception.retry_after_seconds, 120)
            self.assertIn("GET", caught.exception.next_step)
            self.assertFalse((directory / "charmm-gui.tgz").exists())
            retained = list(directory.glob(".download-*"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), content)
        self.assertEqual(self.session.request.call_count, 6)

    def test_near_match_html_and_bad_gzip_are_not_pending_success(self):
        invalid = [content for message in ARCHIVE_PREPARING_MESSAGES for content in (
            message + b"garbage", b" " + message, b"<html>" + message + b"</html>",
            message[:-1], message + b"\n\n", message + b"\r")]
        for index, content in enumerate(invalid + [b"\x1f\x8bnot-gzip", b"not ready"]):
            self.session.request.return_value = raw_response(content)
            destination = self.root / str(index) / "charmm-gui.tgz"
            with self.assertRaises(ToolError) as caught:
                Client(token(), self.session).download("234", destination)
            self.assertNotEqual(caught.exception.category, "archive_preparing")
            self.assertFalse(destination.exists())

    def setup_run(self):
        self.client = Mock()
        self.client.submit.return_value = "234"
        self.run = self.root / "run"
        workflow.start(self.client, config(), self.run)
        self.client.status.return_value = {"status": "done", "hasTarFile": False}
        self.pending = ToolError("Archive is preparing", category="archive_preparing", retryable=True, retry_after_seconds=120)
        self.clock = [1000.0]

    def publish(self, identifier, archive):
        self.assertEqual(identifier, "234")
        archive.write_bytes(archive_bytes({"charmm-gui-234/step5_assembly.pdb": pdb("LIG")}))

    def test_single_round_persists_backoff_and_resume_never_reposts(self):
        self.setup_run()
        self.client.download.side_effect = self.pending
        events = []
        with patch("charmm_gui_cli.workflow.time.time", return_value=1000), self.assertRaises(ToolError) as caught:
            workflow.resume(self.client, self.run, progress=events.append)
        self.assertEqual(caught.exception.category, "archive_preparing")
        saved = workflow.read(self.run)
        self.assertEqual(saved["state"], "archive_preparing")
        self.assertEqual(saved["archive_retry_at_unix"], 1120)
        self.assertEqual(saved["last_error"]["category"], "archive_preparing")
        self.assertEqual(events[-1]["state"], "archive_preparing")
        with patch("charmm_gui_cli.workflow.time.time", return_value=1100), self.assertRaises(ToolError) as caught:
            workflow.resume(self.client, self.run)
        self.assertEqual(caught.exception.retry_after_seconds, 20)
        self.assertEqual(self.client.status.call_count, 1)
        self.assertEqual(self.client.download.call_count, 1)
        self.client.download.side_effect = self.publish
        with patch("charmm_gui_cli.workflow.time.time", return_value=1121):
            result = workflow.resume(self.client, self.run)
        self.assertEqual(result["state"], "ligands_present")
        self.assertNotIn("archive_retry_at_unix", result)
        self.assertNotIn("last_error", result)
        self.assertEqual(self.client.status.call_count, 1)
        self.assertEqual(self.client.download.call_count, 2)
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertTrue((self.run / "download-receipt.json").exists())

    def test_wait_mode_retries_get_after_full_delay_without_new_submission(self):
        self.setup_run()
        attempts = []
        def download(identifier, archive):
            attempts.append(self.clock[0])
            if len(attempts) == 1:
                raise self.pending
            self.publish(identifier, archive)
        def sleep(delay):
            self.assertLessEqual(delay, 30)
            self.clock[0] += delay
        self.client.download.side_effect = download
        with patch("charmm_gui_cli.workflow.time.time", side_effect=lambda: self.clock[0]), \
                patch("charmm_gui_cli.workflow.time.monotonic", side_effect=lambda: self.clock[0]), \
                patch("charmm_gui_cli.workflow.time.sleep", side_effect=sleep):
            result = workflow.resume(self.client, self.run, wait=True, max_wait=300)
        self.assertEqual(attempts, [1000, 1120])
        self.assertEqual(result["state"], "ligands_present")
        self.assertEqual(self.client.submit.call_count, 1)

    def test_wait_budget_stops_before_early_download_and_retains_retry_time(self):
        self.setup_run()
        self.client.download.side_effect = self.pending
        with patch("charmm_gui_cli.workflow.time.time", side_effect=lambda: self.clock[0]), \
                patch("charmm_gui_cli.workflow.time.monotonic", side_effect=lambda: self.clock[0]), \
                patch("charmm_gui_cli.workflow.time.sleep", side_effect=lambda delay: self.clock.__setitem__(0, self.clock[0] + delay)), \
                self.assertRaises(ToolError) as caught:
            workflow.resume(self.client, self.run, wait=True, max_wait=45)
        self.assertEqual(caught.exception.category, "archive_preparing")
        self.assertEqual(caught.exception.retry_after_seconds, 75)
        self.assertEqual(self.client.download.call_count, 1)
        self.assertEqual(workflow.read(self.run)["archive_retry_at_unix"], 1120)

    def test_observed_compressing_status_waits_then_downloads_same_job(self):
        self.setup_run()
        self.client.status.return_value = {"status": "compressing", "hasTarFile": False}
        with patch("charmm_gui_cli.workflow.time.time", return_value=1000), self.assertRaises(ToolError) as caught:
            workflow.resume(self.client, self.run)
        self.assertEqual(caught.exception.category, "archive_preparing")
        saved = workflow.read(self.run)
        self.assertEqual(saved["raw_remote_status"], "compressing")
        self.assertEqual(saved["state"], "archive_preparing")
        self.client.download.assert_not_called()
        self.client.download.side_effect = self.publish
        with patch("charmm_gui_cli.workflow.time.time", return_value=1121):
            self.assertEqual(workflow.resume(self.client, self.run)["state"], "ligands_present")
        self.assertEqual(self.client.status.call_count, 1)
        self.assertEqual(self.client.download.call_count, 1)
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertTrue((self.run / "download-receipt.json").is_file())

    def test_only_observed_compression_status_is_recognized(self):
        self.assertEqual(workflow.status_summary({"status": "compressing"}),
                         {"status": "archive_preparing", "raw_status": "compressing"})
        for value in ("compressed", "compressing arbitrary", "archive_preparing"):
            with self.assertRaises(ToolError):
                workflow.status_summary({"status": value})

    def test_progress_never_calls_compressing_a_verified_archive(self):
        for state in ("archive_preparing", "compressing"):
            result = describe_progress({"state": state, "job_id": "234"})
            self.assertEqual(result["terminal_semantics"], "remote_done_archive_preparing")
            self.assertEqual(result["build_job_id"], "234")
            self.assertTrue(result["remote_done"])
            self.assertFalse(result["archive_verified"])
            self.assertIsNone(result["validation_passed"])


if __name__ == "__main__":
    unittest.main()
