"""Archive compression backoff, private staging and real streamed-byte records."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli.remote.transfers import ByteProgress, archive_delay, observe
from charmm_gui_cli.store.transfer_state import saved_transfer
from charmm_gui_cli.locales.transfers import transfer_text
from charmm_gui_cli.pipeline import workflow
from charmm_gui_cli.remote.api import Client, ARCHIVE_PREPARING_MESSAGE
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.presentation.progress import ProgressReporter
from test_client import archive_bytes, config, pdb, token


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-transfer-progress-")).resolve()

    def response(self, content, headers=None):
        response = requests.Response()
        response.status_code, response._content, response._content_consumed = 200, content, True
        response.headers.update(headers or {})
        return response

    def test_backoff_is_fast_initially_bounded_and_rejects_invalid_saved_counters(self):
        self.assertEqual([archive_delay(n) for n in range(1, 9)], [20, 30, 45, 60, 90, 120, 120, 120])
        for value in (None, True, 0, -1, "3"):
            with self.assertRaises(ToolError):
                archive_delay(value)

    def test_real_client_streams_counted_bytes_then_removes_private_owned_staging(self):
        atomic_json(self.root / ".charmm-gui/full-run.json", {"schema_version": 2})
        data = archive_bytes({"step5_assembly.pdb": pdb("LIG")})
        session = Mock(spec=requests.Session)
        session.request.return_value = self.response(data, {"Content-Length": str(len(data))})
        events = []
        destination = self.root / "charmm-gui.tgz"
        with observe(events.append):
            Client(token(), session).download("234", destination)
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(events[0]["downloaded_bytes"], 0)
        self.assertEqual(events[-1]["downloaded_bytes"], len(data))
        self.assertEqual(events[-1]["total_bytes"], len(data))
        self.assertTrue(events[-1]["transfer_complete"])
        self.assertFalse(list(self.root.glob(".download-*")))
        self.assertFalse(list((self.root / ".charmm-gui/downloads").iterdir()))

    def test_failed_compression_body_remains_private_and_honors_retry_after(self):
        atomic_json(self.root / ".charmm-gui/full-run.json", {"schema_version": 2})
        session = Mock(spec=requests.Session)
        session.request.return_value = self.response(ARCHIVE_PREPARING_MESSAGE, {"Retry-After": "180"})
        with self.assertRaises(ToolError) as error:
            Client(token(), session).download("234", self.root / "charmm-gui.tgz")
        self.assertEqual(error.exception.retry_after_seconds, 180)
        self.assertNotIn(ARCHIVE_PREPARING_MESSAGE.decode(), error.exception.localized())
        private = list((self.root / ".charmm-gui/downloads").glob(".download-*"))
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0].stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.root.glob(".download-*")))

    def test_bad_total_and_encoded_wire_length_are_unknown_without_echoing_headers(self):
        for value in ("", "-1", "secret-token", "0", "9" * 40):
            response = self.response(b"", {"Content-Length": value})
            events = []
            with observe(events.append):
                meter = ByteProgress(response)
                meter.add(10)
                meter.emit(complete=True)
            self.assertIsNone(events[-1]["total_bytes"])
            self.assertNotIn("secret-token", json.dumps(events))
        with observe(lambda event: self.assertIsNone(event["total_bytes"])):
            ByteProgress(self.response(b"", {"Content-Encoding": "gzip", "Content-Length": "10"}))

    def test_byte_updates_are_throttled_and_console_refreshes_are_not_log_lines(self):
        clock, events = [1.], []
        with observe(events.append), patch("time.monotonic", side_effect=lambda: clock[0]):
            meter = ByteProgress(self.response(b"", {"Content-Length": "100"}))
            for _ in range(8):
                meter.add(10)
            self.assertEqual(len(events), 1)
            clock[0] += 1
            meter.add(20)
            meter.emit(complete=True)
        self.assertEqual([e["downloaded_bytes"] for e in events], [0, 100, 100])
        stream = io.StringIO()
        reporter = ProgressReporter(stream=stream)
        for event in events:
            reporter({"state": "downloading", **event})
        reporter.close()
        self.assertNotIn("\r", stream.getvalue())
        self.assertEqual(len(stream.getvalue().splitlines()), 2)
        self.assertIn("100 B", stream.getvalue())

    def test_durable_attempts_resume_without_early_get_or_another_post(self):
        client = Mock()
        client.submit.return_value, client.status.return_value = "234", {"status": "done"}
        run = self.root / "run"
        workflow.start(client, config(), run)
        client.download.side_effect = ToolError("pending", category="archive_preparing", retryable=True, retry_after_seconds=20)
        for now, due in ((1000, 1020), (1020, 1050), (1050, 1095)):
            with patch("time.time", return_value=now), self.assertRaises(ToolError):
                workflow.resume(client, run)
            self.assertEqual(workflow.read(run)["archive_retry_at_unix"], due)
        before = client.download.call_count
        with patch("time.time", return_value=1094), self.assertRaises(ToolError):
            workflow.resume(client, run)
        self.assertEqual(client.download.call_count, before)
        self.assertEqual(client.submit.call_count, 1)
        self.assertEqual(workflow.read(run)["archive_poll_attempts"], 3)

    def test_saved_progress_is_readonly_allowlisted_and_hides_stale_or_malformed_records(self):
        directory = self.root / ".charmm-gui"
        path = directory / "bilayer/download-progress.json"
        atomic_json(path, {"active": True, "downloaded_bytes": 500, "total_bytes": 1000, "job_id": "234", "cookie": "secret"})
        before = path.read_bytes()
        result = saved_transfer(directory)
        self.assertNotIn("cookie", result)
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("500 B", transfer_text(result))
        for record in ({"active": False}, {"active": True, "downloaded_bytes": "token", "job_id": "234"}):
            atomic_json(path, record)
            self.assertIsNone(saved_transfer(directory))

    def test_workflow_real_client_persists_exact_bytes_and_completes_same_job(self):
        from test_client import response
        data = archive_bytes({"charmm-gui-234/step5_assembly.pdb": pdb("LIG")})
        session = Mock(spec=requests.Session)
        session.request.side_effect = [response({"submitted": "true", "jobid": "234"}), response({"status": "done"}), self.response(data, {"Content-Length": str(len(data))})]
        run = self.root / ".charmm-gui/bilayer"
        atomic_json(run.parent / "full-run.json", {"schema_version": 2})
        client = Client(token(), session)
        workflow.start(client, config(), run)
        events = []
        report = workflow.resume(client, run, progress=events.append)
        self.assertEqual(report["state"], "ligands_present")
        saved = json.loads((run / "download-progress.json").read_text())
        self.assertFalse(saved["active"])
        self.assertTrue(saved["transfer_complete"])
        self.assertEqual(saved["downloaded_bytes"], len(data))
        self.assertEqual([call.args[0] for call in session.request.call_args_list], ["POST", "GET", "GET"])
        self.assertTrue(any(event.get("downloaded_bytes") == len(data) for event in events))

    def test_staging_symlink_is_rejected_and_stream_response_is_closed(self):
        atomic_json(self.root / ".charmm-gui/full-run.json", {"schema_version": 2})
        elsewhere = self.root / "outside"
        elsewhere.mkdir()
        (self.root / ".charmm-gui/downloads").symlink_to(elsewhere, target_is_directory=True)
        response = self.response(b"do not write")
        response.close = Mock()
        session = Mock(spec=requests.Session)
        session.request.return_value = response
        with self.assertRaises(ToolError) as error:
            Client(token(), session).download("234", self.root / "charmm-gui.tgz")
        self.assertEqual(error.exception.key, "download.staging_unsafe")
        response.close.assert_called_once()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_interrupted_stream_marks_viewer_metadata_inactive_without_publishing_archive(self):
        from charmm_gui_cli.remote.transfers import _OBSERVER
        client = Mock()
        client.submit.return_value, client.status.return_value = "234", {"status": "done"}
        run = self.root / "run"
        workflow.start(client, config(), run)
        def interrupt(identifier, path):
            _OBSERVER.get()({"downloaded_bytes": 100, "total_bytes": 200, "transfer_complete": False, "download_elapsed_seconds": 1})
            raise KeyboardInterrupt
        client.download.side_effect = interrupt
        with self.assertRaises(KeyboardInterrupt):
            workflow.resume(client, run)
        self.assertFalse(json.loads((run / "download-progress.json").read_text())["active"])
        self.assertFalse((run / "charmm-gui.tgz").exists())

    def test_watch_renders_live_bytes_without_signaling_or_changing_the_task(self):
        from charmm_gui_cli.store.background import watch
        from test_input_policy import Terminal
        from contextlib import redirect_stdout
        atomic_json(self.root / "download-progress.json", {"active": True, "downloaded_bytes": 1048576, "total_bytes": 2097152, "job_id": "234"})
        before = (self.root / "download-progress.json").read_bytes()
        stream = Terminal()
        with redirect_stdout(stream), patch("charmm_gui_cli.store.background.metadata", side_effect=[{"running": True, "elapsed_seconds": 4}, {"running": False, "exit_code": 0}]), \
                patch("charmm_gui_cli.store.background.time.sleep"), patch("os.kill") as kill:
            self.assertEqual(watch(self.root), 0)
        self.assertIn("1.0 MiB / 2.0 MiB", stream.getvalue())
        self.assertEqual((self.root / "download-progress.json").read_bytes(), before)
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
