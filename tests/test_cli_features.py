"""User-facing local CLI checks with real coordinates/PNG and no network."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli.pipeline import batch
from charmm_gui_cli.cli import main
from charmm_gui_cli.chem.inputs import Atom, _sdf
from charmm_gui_cli.chem.preview import read_scene


def pdb_atom(serial, name, resname, xyz, element, record="ATOM", chain="A", resid=1):
    return (f"{record:<6}{serial:5d} {name:>4} {resname:<4}{chain}{resid:4d}    "
            f"{xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00  0.00          {element:>2}\n")


class CLIFeatureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-cli-features-")).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.network = self.stack.enter_context(patch("requests.Session.request", side_effect=AssertionError("No network in CLI feature tests")))
        self.auth = self.stack.enter_context(patch("charmm_gui_cli.pipeline.build_command.load_token", side_effect=AssertionError("No authentication")))
        self.stack.enter_context(patch("charmm_gui_cli.store.auth.load_token", side_effect=AssertionError("No authentication")))
        self.stack.enter_context(patch("charmm_gui_cli.store.managed._registry", return_value=self.root / "registry"))
        self.protein = self.root / "protein.pdb"
        self.protein_text = "".join(pdb_atom(i, name, "ALA", xyz, element) for i, name, xyz, element in (
            (1, "N", (-1.458, 0., 0.), "N"), (2, "CA", (0., 0., 0.), "C"),
            (3, "C", (0.55, 1.42, 0.), "C"), (4, "O", (0.1, 2.5, 0.), "O"),
            (5, "CB", (0.52, -0.77, 1.23), "C")))
        self.protein.write_text(self.protein_text + "TER\nEND\n")
        self.ligand = self.root / "bound.sdf"
        self.ligand.write_text(_sdf([Atom(1, "C1", "C", (3., 2., 1.)), Atom(2, "O1", "O", (4.3, 2., 1.))], {(0, 1): 1}, "LIG"))
        self.complex = self.root / "complex.pdb"
        self.complex.write_text(self.protein_text + "TER\n"
            + pdb_atom(6, "C1", "LIG", (3., 2., 1.), "C", "HETATM", "L")
            + pdb_atom(7, "O1", "LIG", (4.3, 2., 1.), "O", "HETATM", "L")
            + "CONECT    6    7\nCONECT    7    6\nEND\n")

    def call(self, args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(args)
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def require_chem(self):
        if importlib.util.find_spec("rdkit") is None:
            self.skipTest("RDKit required for real CLI chemistry preparation")

    def require_png(self):
        if importlib.util.find_spec("PIL") is None or importlib.util.find_spec("numpy") is None:
            self.skipTest("Pillow/numpy required for real PNG rendering")

    def assert_png(self, path, width=None):
        from PIL import Image
        path = Path(path)
        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        with Image.open(path) as image:
            self.assertEqual(image.format, "PNG")
            if width is not None:
                self.assertEqual(image.width, width)
            image.verify()

    def test_check_real_protein_sdf_generates_png_without_auth(self):
        self.require_chem()
        self.require_png()
        protein_before, ligand_before = self.protein.read_bytes(), self.ligand.read_bytes()
        out = self.root / "check"
        code, report, error = self.call(["check", "--protein", str(self.protein), "--ligand", str(self.ligand), "--out", str(out)])
        self.assertEqual(code, 0, error)
        self.assertTrue(report["passed"])
        self.assertFalse(report["server_submitted"])
        self.assertFalse(report["task_registered"])
        self.assertFalse(report["scientific_correctness_verified"])
        self.assertEqual(report["preview"]["status"], "rendered")
        self.assertEqual(report["preview"]["scope"], "prepared_input")
        self.assertEqual(len(report["preview"]["images"]), 3)
        for image in report["preview"]["images"]:
            self.assert_png(image["file"])
        self.assertEqual(self.protein.read_bytes(), protein_before)
        self.assertEqual(self.ligand.read_bytes(), ligand_before)
        self.assertEqual(json.loads((out / "check.json").read_text())["preview"]["status"], "rendered")
        self.auth.assert_not_called()
        self.network.assert_not_called()

    def test_check_no_preview_does_not_render(self):
        self.require_chem()
        with patch("charmm_gui_cli.chem.preview.render_model", side_effect=AssertionError("Preview explicitly disabled")):
            code, report, error = self.call(["check", "--protein", str(self.protein), "--ligand", str(self.ligand),
                                             "--out", str(self.root / "no-preview-check"), "--no-preview"])
        self.assertEqual(code, 0, error)
        self.assertNotIn("preview", report)

    def test_preview_pdb_parses_real_coordinates_and_renders_requested_view(self):
        self.require_png()
        scene = read_scene(self.complex)
        self.assertEqual(scene["total_atoms"], 7)
        self.assertEqual(len(scene["protein"]), 5)
        self.assertEqual(len(scene["components"]), 2)
        self.assertEqual(scene["components"][0]["xyz"], (3., 2., 1.))
        self.assertEqual(scene["component_bonds"], [(6, 7)])
        code, report, error = self.call(["preview", str(self.complex), "--out", str(self.root / "preview"), "--width", "400", "--view", "side"])
        self.assertEqual(code, 0, error)
        self.assertEqual(report["visible_components"], {"LIG": 2})
        self.assertEqual(len(report["images"]), 1)
        self.assert_png(report["images"][0]["file"], 400)
        self.assertEqual(Path(report["model"]).read_bytes(), self.complex.read_bytes())
        self.assertFalse(report["scientific_correctness_verified"])
        self.auth.assert_not_called()
        self.network.assert_not_called()

    def test_preview_rejects_bad_geometry_and_existing_destination(self):
        self.require_png()
        broken = self.root / "nonfinite.pdb"
        broken.write_text(self.complex.read_text().replace("  -1.458", "     nan"))
        code, report, error = self.call(["preview", str(broken), "--out", str(self.root / "bad")])
        self.assertEqual(code, 2)
        self.assertIn("finite", error)
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("retained")
        code, report, error = self.call(["preview", str(self.complex), "--out", str(existing)])
        self.assertEqual(code, 2)
        self.assertEqual(marker.read_text(), "retained")

    def test_sweep_dry_run_two_conditions_and_read_only_status(self):
        self.require_chem()
        config = self.root / "sweep.yaml"
        config.write_text(yaml.safe_dump({"version": 1, "inputs": {"protein": "protein.pdb", "ligand": "bound.sdf"},
                                          "axes": {"margin": [20, 25]}}))
        directory = self.root / "scan"
        code, result, error = self.call(["sweep", str(config), "--out", str(directory), "--dry-run"])
        self.assertEqual(code, 0, error)
        self.assertEqual(result["conditions"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(result["server_submitted"])
        before = (directory / "batch/batch.json").read_bytes()
        code, status, error = self.call(["sweep-status", str(directory)])
        self.assertEqual(code, 0, error)
        self.assertEqual(status["total"], 2)
        self.assertEqual(status["counts"], {"inputs_ready": 2})
        self.assertEqual(before, (directory / "batch/batch.json").read_bytes())
        with patch("charmm_gui_cli.pipeline.build_command.execute", return_value=({"state": "preparation_running"}, 0)) as execute:
            code, result, error = self.call(["sweep-resume", str(directory), "--no-wait", "--no-preview"])
        self.assertEqual(code, 0, error)
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(execute.call_args.args[0].no_preview)
        self.auth.assert_not_called()
        self.network.assert_not_called()

    def test_batch_no_preview_reaches_execute_namespace(self):
        self.require_chem()
        config = self.root / "batch.yaml"
        config.write_text(yaml.safe_dump({"version": 1, "jobs": [{"name": "one", "protein": "protein.pdb", "ligand": "bound.sdf"}]}))
        directory = self.root / "batch"
        batch.initialize(config, directory)
        with patch("charmm_gui_cli.pipeline.build_command.execute", return_value=({"state": "running"}, 0)) as execute:
            code, result, error = self.call(["batch-resume", str(directory), "--no-wait", "--no-preview"])
        self.assertEqual(code, 0, error)
        self.assertTrue(execute.call_args.args[0].no_preview)
        self.assertFalse(execute.call_args.args[0].wait)

    def test_arguments_preview_default_and_override(self):
        self.assertFalse(batch._arguments(self.root).no_preview)
        self.assertTrue(batch._arguments(self.root, no_preview=True).no_preview)


if __name__ == "__main__":
    unittest.main()
