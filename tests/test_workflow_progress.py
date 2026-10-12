"""API callback metadata is useful for stages without exposing response text."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from charmm_gui_cli import workflow


class WorkflowProgressTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-workflow-progress-")).resolve()
        self.run = self.root / "run"
        self.client = Mock()
        self.client.submit.return_value = "234"
        workflow.start(self.client, {
            "version": 1, "source_job_id": "123", "expected_ligands": ["LIG"],
            "preparation": {"ligand_parameters_ready": True, "orientation": "ppm"},
            "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20}}, self.run)

    def test_known_last_output_is_exposed_with_build_role(self):
        self.client.status.return_value = {"status": "running ffconverter", "lastOutFile": "step5_input.out", "lastOutLine": "PRIVATE LOG"}
        events = []
        workflow.resume(self.client, self.run, progress=events.append)
        self.assertEqual(events, [{"state": "running ffconverter", "job_id": "234", "build_job_id": "234", "last_output_file": "step5_input.out"}])

    def test_untrusted_output_text_is_not_forwarded(self):
        for output in ("https://example.invalid/?token=SECRET", "step5_input.out\nSECRET", "SECRET", {"secret": "value"}):
            with self.subTest(output=output):
                self.client.status.return_value = {"status": "pending", "lastOutFile": output, "token": "SECRET"}
                events = []
                workflow.resume(self.client, self.run, progress=events.append)
                self.assertNotIn("last_output_file", events[0])
                self.assertNotIn("token", events[0])
                self.assertEqual(events[0]["build_job_id"], "234")

    def test_exact_ppm_output_is_exposed(self):
        self.client.status.return_value = {"status": "running", "lastOutFile": "ppm.out"}
        events = []
        workflow.resume(self.client, self.run, progress=events.append)
        self.assertEqual(events[0]["last_output_file"], "ppm.out")


if __name__ == "__main__":
    unittest.main()
