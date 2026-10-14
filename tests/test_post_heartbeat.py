import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from charmm_gui_cli.remote.web import WebClient


class PostHeartbeatTests(unittest.TestCase):
    def test_post_and_get_timeouts_are_distinct(self):
        client = WebClient(post_timeout=123)
        result = Mock(is_redirect=False, status_code=200)
        with patch("charmm_gui_cli.remote.web.request_with_retry", return_value=result) as request:
            client.request("POST", "?doc=input/pdbreader")
            self.assertEqual(request.call_args.kwargs["timeout"], (15, 123))
            client.request("GET", "?doc=input/pdbreader")
            self.assertEqual(request.call_args.kwargs["timeout"], (15, 90))

    def test_wait_heartbeat_and_single_submission(self):
        client = WebClient(heartbeat_interval=0.01)
        output = io.StringIO()
        def slow(*args, **kwargs):
            time.sleep(0.04)
            return "done"
        with patch.object(client, "request", side_effect=slow) as request, contextlib.redirect_stderr(output):
            result = client._modeling_post("?doc=input/pdbreader", Path(tempfile.mkdtemp()).resolve() / "intent.json", {})
        self.assertEqual(result, "done")
        self.assertEqual(request.call_count, 1)
        self.assertIn("0:00", output.getvalue())
        self.assertNotIn("\r", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())

