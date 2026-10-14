import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.cli import main
from charmm_gui_cli.presentation import reporting
from charmm_gui_cli.locales import i18n


class ReportingTests(unittest.TestCase):
    def setUp(self):
        # These legacy snapshots intentionally exercise Chinese rendering.
        self.addCleanup(i18n.set_language)
        language = patch.dict(os.environ, {"CHARMM_GUI_CLI_LANG": "zh"})
        language.start()
        self.addCleanup(language.stop)
        i18n.set_language("zh")
        self.root = Path(tempfile.mkdtemp(prefix="cgui-report-test-")).resolve()
        self.report = {"fingerprint": "current", "passed": True, "status": "validated",
                       "review_warnings": ["<script>alert(1)</script>"], "review_required": True}
        self.context = {"directory": str(self.root), "name": "model-a", "passed": True,
                        "source_job_id": "123", "build_job_id": "456", "compilation_status": "passed"}
        self.summary = {"name": "model-a", "passed": True, "review_required": True,
                        "report": str(self.root / "results/validation.json")}
        self.manifest = {"config": {"membrane": {"upper": "POPC=1", "lower": "POPC=1"},
                                    "ions": {"type": "KCl", "concentration_M": 0.15},
                                    "preparation": {"orientation": "ppm"}},
                         "token": "private-value-must-not-be-present"}

    def fixture(self):
        checks = self.root / "results/checks/one/system/gromacs"
        previews = self.root / "results/previews/one"
        checks.mkdir(parents=True)
        previews.mkdir(parents=True)
        (self.root / "bilayer").mkdir()
        source, model, image = checks / "step5_input.pdb", previews / "model.pdb", previews / "side.png"
        data = b"ATOM coordinates fixture\n"
        source.write_bytes(data)
        model.write_bytes(data)
        image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        with tarfile.open(self.root / "bilayer/charmm-gui.tgz", "w:gz") as archive:
            info = tarfile.TarInfo("charmm-gui-456/gromacs/step5_input.pdb")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(data).hexdigest()
        saved = {"source": str(source), "model": str(model), "source_sha256": digest,
                 "scope": "final_simulation_input",
                 "identity": {"source_sha256": digest, "validation_fingerprint": "current"},
                 "images": [{"view": "side", "file": str(image),
                             "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}]}
        (self.root / "results/preview.json").write_text(json.dumps(saved))
        return saved, image, model

    def test_display_whitelists_parameters_and_distinguishes_compile(self):
        result = reporting.display_result(self.summary, self.context, self.manifest, self.report)
        text = reporting.render_text(result)
        self.assertIn("通过，需人工复核", text)
        self.assertIn("GROMACS 输入编译：通过", text)
        self.assertIn("取向：ppm", text)
        self.assertIn("不代表已执行", text)
        self.assertNotIn("private-value", json.dumps(result))
        result["compilation_status"] = "not_run"
        self.assertIn("输入编译：未执行", reporting.render_text(result))

    def test_html_escaped_offline_and_contains_no_scripts(self):
        result = reporting.display_result(self.summary, self.context, self.manifest, self.report)
        content = reporting.render_html(result)
        self.assertIn("&lt;script&gt;", content)
        self.assertNotIn("<script>", content)
        self.assertIn("Content-Security-Policy", content)
        self.assertNotIn("private-value", content)
        self.assertIn("charmm-gui.org", content)

    def test_components_have_individual_readable_counts_poses_and_penalties(self):
        result = reporting.display_result(self.summary, self.context, self.manifest, self.report)
        result["components"] = {"LIG": {"passed": True, "copies": {"coordinates": 1, "topology": 1},
            "ligand_rmsd_A": 0.2, "ligand_max_displacement_A": 0.5,
            "cgenff": {"parameter_max": 0.6, "charge_max": 2.2, "status": "passed"}},
            "COF": {"passed": False, "pose_error": "No unique mapping"}}
        content = reporting.render_text(result)
        self.assertIn("LIG：通过；副本 坐标 1 / 拓扑 1", content)
        self.assertIn("重原子 RMSD：0.2 Å；最大位移：0.5 Å", content)
        self.assertIn("参数 0.6 / 电荷 2.2", content)
        self.assertIn("COF：未通过", content)
        self.assertIn("位姿检查：No unique mapping", content)
        self.assertNotIn("各组分：{", content)

    def test_valid_images_embedded_without_mutation(self):
        saved, image, model = self.fixture()
        before = model.read_bytes()
        images, note = reporting.verified_images(self.root, self.report)
        self.assertEqual(len(images), 1)
        self.assertIn("最终", note)
        result = reporting.display_result(self.summary, self.context, self.manifest, self.report)
        self.assertIn("data:image/png;base64,", reporting.render_html(result, images, note))
        self.assertEqual(model.read_bytes(), before)

    def test_stale_preview_not_embedded(self):
        self.fixture()
        self.report["fingerprint"] = "new"
        images, note = reporting.verified_images(self.root, self.report)
        self.assertFalse(images)
        self.assertIn("过期", note)

    def test_tampered_image_rejected(self):
        saved, image, model = self.fixture()
        image.write_bytes(b"changed")
        self.assertFalse(reporting.verified_images(self.root, self.report)[0])

    def test_tampered_model_rejected_even_when_cache_hash_changed(self):
        saved, image, model = self.fixture()
        source = Path(saved["source"])
        source.write_bytes(b"fake model")
        model.write_bytes(b"fake model")
        digest = hashlib.sha256(b"fake model").hexdigest()
        saved["source_sha256"] = saved["identity"]["source_sha256"] = digest
        (self.root / "results/preview.json").write_text(json.dumps(saved))
        self.assertFalse(reporting.verified_images(self.root, self.report)[0])

    def test_missing_cache_is_actionable(self):
        images, note = reporting.verified_images(self.root, self.report)
        self.assertFalse(images)
        self.assertIn("preview", note)

    def test_export_never_overwrites(self):
        path = self.root / "report.html"
        reporting.write_new(path, "old")
        with self.assertRaises(ToolError):
            reporting.write_new(path, "new")
        self.assertEqual(path.read_text(), "old")

    def test_cli_html_export_no_network_or_model_mutation(self):
        (self.root / "results").mkdir()
        (self.root / "results/validation.json").write_text(json.dumps(self.report))
        with patch("charmm_gui_cli.store.tasks.resolve_run", return_value=self.root), \
                patch("charmm_gui_cli.pipeline.managed.inspect_run", return_value=self.context), \
                patch("charmm_gui_cli.store.tasks._read_json", side_effect=[self.report, self.manifest]), \
                patch("requests.Session.request", side_effect=AssertionError("No network")), \
                redirect_stdout(io.StringIO()):
            code = main(["jobs", "report", "model-a", "--format", "html", "--out", str(self.root / "report.html")])
        self.assertEqual(code, 0)
        self.assertIn("坐标预览", (self.root / "report.html").read_text())

    def test_cli_rejects_html_without_filename(self):
        with patch("charmm_gui_cli.store.tasks.resolve_run", side_effect=AssertionError("No inspection needed")):
            self.assertEqual(main(["jobs", "report", "model-a", "--format", "html"]), 2)
