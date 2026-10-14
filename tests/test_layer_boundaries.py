"""Task-layer extraction with real processes and immutable synthetic evidence."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli.cli import parser
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store import configuration, provenance, run_manifest, tasks, workers
from charmm_gui_cli.pipeline import workflow
from test_client import archive_bytes, config
import test_single_directory as single_directory


class LayerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-layer-boundaries-")).resolve()

    def test_downstream_provenance_has_identical_function_and_strict_config_checks(self):
        self.assertIs(workflow.read, run_manifest.read)
        self.assertIs(workflow.verify_archive_provenance, provenance.verify_archive_provenance)
        archive = self.root / "charmm-gui.tgz"
        archive.write_bytes(archive_bytes({"charmm-gui-234/step5_assembly.pdb": "synthetic coordinate bytes"}))
        manifest = {"schema_version": 1, "job_id": "234", "state": "ligands_present", "config": config(),
                    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
        atomic_json(self.root / "run.json", manifest)
        before = (self.root / "run.json").read_bytes()
        proof = provenance.verify_archive_provenance(self.root)
        self.assertEqual(proof["job_id"], "234")
        self.assertEqual((self.root / "run.json").read_bytes(), before)
        self.assertFalse((self.root / "download-receipt.json").exists())
        manifest["config"]["unexpected"] = "must not bypass validation after extraction"
        atomic_json(self.root / "run.json", manifest)
        with self.assertRaises(ToolError):
            provenance.verify_archive_provenance(self.root)
        self.assertFalse((self.root / "download-receipt.json").exists())

    def test_leaf_config_normalization_matches_historical_api_and_rejects_duplicate_yaml(self):
        from charmm_gui_cli.pipeline import config as old_config
        for name in ("mapping", "number", "composition", "validate_config", "UniqueLoader"):
            self.assertIs(getattr(old_config, name), getattr(configuration, name))
        value = config()
        self.assertEqual(configuration.validate_config(value), value)
        with self.assertRaises(ToolError):
            yaml.load("version: 1\nversion: 2\n", Loader=configuration.UniqueLoader)

    def test_record_listing_preserves_newest_readable_order_and_readonly_enrichment(self):
        from charmm_gui_cli.pipeline import managed
        registry = self.root / "index"
        directories = {}
        for name in ("a", "b"):
            directory = self.root / name
            tasks._new_json(directory / "full-run.json", {"schema_version": 2, "state": "inputs_ready"})
            tasks._new_json(directory / "job.json", {"name": name, "directory": str(directory)})
            directories[name] = directory
        records = [{"directory": str(directories[name]), "remembered_at": stamp} for name, stamp in
                   (("a", "2026-01-01T00:00:00+00:00"), ("b", "2026-01-02T00:00:00+00:00"), ("a", "2026-01-03T00:00:00+00:00"))]
        for index, record in enumerate(records):
            tasks._new_json(registry / "run-history" / f"{index}.json", record)
        tasks._new_json(registry / "latest-run.json", records[1])
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        with patch.object(tasks, "_registries", return_value=(registry,)), \
                patch("requests.Session.request", side_effect=AssertionError("Listing must stay offline")):
            stored = tasks.list_records()
            displayed = managed.list_runs()
        self.assertEqual([entry["directory"] for entry in stored], [str(directories[name]) for name in ("a", "b")])
        self.assertEqual([entry["remembered_at"] for entry in stored], [records[2]["remembered_at"], records[1]["remembered_at"]])
        self.assertEqual([entry["name"] for entry in displayed], ["a", "b"])
        self.assertTrue(all(not entry["background"]["running"] for entry in displayed))
        self.assertTrue(all(entry["state"] == "inputs_ready" for entry in displayed))
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*.json")}, before)

    def test_all_worker_module_entry_points_resume_offline_with_the_inherited_lock(self):
        # These records deliberately use tiny synthetic archive/cached evidence;
        # success here proves process/layout compatibility, not new chemistry.
        for module in ("charmm_gui_cli.pipeline.background", "charmm_gui_cli.background", "charmm_gui_cli.store.background"):
            with self.subTest(module=module):
                fixture = single_directory.SingleDirectoryTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                directory = fixture.run
                startup = fixture.root / "offline-startup"
                startup.mkdir()
                (startup / "sitecustomize.py").write_text(
                    "import requests\n"
                    "def forbidden(*args, **kwargs):\n"
                    "    raise AssertionError('offline worker must not issue HTTP requests')\n"
                    "requests.Session.request = forbidden\n", encoding="utf-8")
                args = parser().parse_args(["--foreground", "build-resume", str(directory), "--no-preview", "--no-grompp"])
                options = dict(vars(args), _worker=True)
                options["token_file"] = str(fixture.root / "nonexistent-session.token")
                options = {key: str(value) if isinstance(value, Path) else value for key, value in options.items()}
                atomic_json(directory / "worker-options.json", options)
                fd = workers.claim(directory)
                workers.write_lock(fd, {"pid": os.getpid(), "started_at": "2026-01-01T00:00:00+00:00"})
                environment = dict(os.environ)
                for key in ("CHARMMGUI_TOKEN", "CHARMM_GUI_PASSWORD_COMMAND"):
                    environment.pop(key, None)
                package_parent = str(Path(tasks.__file__).resolve().parents[2])
                environment.update(PYTHONPATH=str(startup) + os.pathsep + package_parent + os.pathsep + environment.get("PYTHONPATH", ""),
                                   XDG_DATA_HOME=str(fixture.root / "worker-index"),
                                   XDG_CONFIG_HOME=str(fixture.root / "worker-config"))
                log = fixture.root / "run.log"
                try:
                    with log.open("ab") as output:
                        process = subprocess.Popen([sys.executable, "-m", module, str(directory), str(fd)],
                            stdin=subprocess.DEVNULL, stdout=output, stderr=output, cwd=fixture.root,
                            env=environment, start_new_session=True, pass_fds=(fd,))
                finally:
                    os.close(fd)
                try:
                    code = process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    self.fail("Synthetic offline worker did not finish: " + log.read_text())
                text = log.read_text()
                self.assertEqual(code, 0, text)
                state = workers.metadata(directory)
                self.assertEqual(state["pid"], process.pid)
                self.assertEqual(state["exit_code"], 0)
                self.assertFalse(state["running"])
                self.assertNotIn("Traceback", text)
                self.assertNotIn("\r", text)
                self.assertNotIn("\x1b", text)
                result = tasks._read_json(directory / "worker-result.json")
                self.assertEqual(result["state"], "validated")
                self.assertFalse(result["validation"]["scientific_correctness_verified"])
                self.assertEqual(result["delivery"]["directory"], str(fixture.root))
                self.assertTrue((fixture.root / "report.txt").is_file())
                self.assertEqual(len(list(fixture.root.rglob("*.tgz"))), 1)


if __name__ == "__main__":
    unittest.main()
