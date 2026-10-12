import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import os

from charmm_gui_cli.auth import ToolError
from charmm_gui_cli.background import claim, metadata, watch, write_lock


class BackgroundTests(unittest.TestCase):
    def test_active_lock_rejects_duplicate(self):
        directory = Path(tempfile.mkdtemp())
        fd = claim(directory)
        try:
            write_lock(fd, {"pid": os.getpid()})
            self.assertTrue(metadata(directory)["running"])
            with self.assertRaises(ToolError):
                claim(directory)
        finally:
            os.close(fd)
        self.assertFalse(metadata(directory)["running"])

    def test_stale_lock_can_be_reused(self):
        directory = Path(tempfile.mkdtemp())
        fd = claim(directory)
        write_lock(fd, {"pid": 999999999})
        os.close(fd)
        fd = claim(directory)
        write_lock(fd, {"pid": os.getpid()})
        os.close(fd)
        self.assertEqual(metadata(directory)["pid"], os.getpid())

    def test_watch_interrupt_never_signals_worker(self):
        directory = Path(tempfile.mkdtemp())
        with patch("charmm_gui_cli.background.metadata", return_value={"running": True}), \
                patch("charmm_gui_cli.background.time.sleep", side_effect=KeyboardInterrupt), \
                patch("os.kill") as kill, patch("sys.stderr", io.StringIO()):
            self.assertEqual(watch(directory), 130)
            kill.assert_not_called()
