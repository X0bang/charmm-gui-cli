import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from charmm_gui_cli.progress import ProgressReporter
from charmm_gui_cli.run_logging import LogStream


class RunLoggingTests(unittest.TestCase):
    def test_unchanged_non_tty_polls_are_deduplicated(self):
        stream = io.StringIO()
        reporter = ProgressReporter(stream=stream)
        for _ in range(25):
            reporter({"state": "pending", "job_id": "123"})
        records = [line for line in stream.getvalue().splitlines() if len(line) > 9 and line[3:4] == ":"]
        self.assertEqual(len(records), 1)
        self.assertNotIn("\r", stream.getvalue())
        self.assertNotIn("\x1b", stream.getvalue())

    def test_inline_refreshes_do_not_enter_log(self):
        path = Path(tempfile.mkdtemp()).resolve() / "run.log"
        stream = LogStream(io.StringIO(), path)
        stream.write("record\n")
        stream.write("\r⠋ refresh")
        stream.write("\x1b[31merror\x1b[0m\n")
        stream.close()
        self.assertEqual(path.read_text(), "record\nerror\n")
        terminal = io.StringIO()
        progress_path = path.with_name("progress.log")
        stream = LogStream(terminal, progress_path)
        with patch.object(terminal, "isatty", return_value=True):
            reporter = ProgressReporter(stream=stream)
            reporter({"state": "running ffconverter", "job_id": "123"})
            self.assertIn("\r", terminal.getvalue())
            self.assertIsNotNone(reporter._thread)
            reporter.close()
        stream.close()
        self.assertNotIn("\r", progress_path.read_text())
        self.assertNotIn("\x1b", progress_path.read_text())
