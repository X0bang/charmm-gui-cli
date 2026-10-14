import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.store.auth import ToolError, atomic_write
from charmm_gui_cli.chem.preview import read_scene, render_model, preview_run, _primitives


def atom(index, name, residue, xyz, *, segment="", chain="A", resid=1, element="C", altloc=" "):
    line = list(" " * 80)
    line[:6] = "ATOM  "
    line[6:11] = f"{index:5d}"
    line[12:16] = f"{name:>4}"
    line[16] = altloc
    line[17:21] = f"{residue:<4}"
    line[21] = chain
    line[22:27] = f"{resid:5d}"
    line[30:54] = "".join(f"{v:8.3f}" for v in xyz)
    line[72:76] = f"{segment:<4}"
    line[76:78] = f"{element:>2}"
    return "".join(line)


@unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"), "Preview dependencies unavailable")
class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-preview-test-")).resolve()
        self.pdb = self.root / "model.pdb"
        rows = [atom(1, "CA", "ALA", (-2, 0, 0), resid=1),
                atom(2, "CA", "ALA", (1.8, 0, 0), resid=2),
                atom(3, "C1", "LIG", (0, 2, 0), chain="L"),
                atom(4, "O1", "LIG", (1.4, 2, 0), chain="L", element="O"),
                atom(5, "P", "POPC", (5, 0, 18), segment="MEMB", element="P"),
                atom(6, "P", "POPC", (-5, 0, -18), segment="MEMB", resid=2, element="P"),
                atom(7, "OH2", "TIP3", (0, 0, 30), element="O"),
                atom(8, "POT", "POT", (0, 0, 35), element="K"),
                atom(9, "H1", "LIG", (-1, 2, 0), chain="L", element="H")]
        atomic_write(self.pdb, "\n".join(rows) + "\nCONECT    3    4\nEND\n")

    def test_scene_preserves_coordinates_and_explicit_connectivity(self):
        scene = read_scene(self.pdb)
        self.assertEqual(scene["total_atoms"], 9)
        self.assertEqual(scene["components"][0]["xyz"], (0., 2., 0.))
        self.assertEqual(scene["component_bonds"], [(3, 4)])
        self.assertEqual(len(scene["membranes"]), 2)
        self.assertEqual(scene["hidden"], {"water_atoms": 1, "ion_atoms": 1, "hydrogen_atoms": 1})

    def test_png_dimensions_nonblank_and_source_model_unmodified(self):
        from PIL import Image
        before = self.pdb.read_bytes()
        result = render_model(self.pdb, self.root / "preview", width=640)
        self.assertEqual(self.pdb.read_bytes(), before)
        self.assertEqual(Path(result["model"]).read_bytes(), before)
        self.assertEqual(len(result["images"]), 3)
        self.assertFalse(result["scientific_correctness_verified"])
        for entry in result["images"]:
            with Image.open(entry["file"]) as image:
                self.assertEqual(image.size, (640, 460))
                self.assertGreater(len(image.getcolors(image.width * image.height)), 50)
                self.assertEqual(image.info["SourceSHA256"], hashlib.sha256(before).hexdigest())

    def test_existing_outputs_are_never_replaced(self):
        output = self.root / "retained"
        output.mkdir()
        atomic_write(output / "keep.txt", "preserve")
        with self.assertRaisesRegex(ToolError, "already exists"):
            render_model(self.pdb, output)
        self.assertEqual((output / "keep.txt").read_text(), "preserve")

    def test_psf_bonds_use_atom_order_not_overflowing_serials(self):
        text = self.pdb.read_text().replace("CONECT    3    4\n", "")
        atomic_write(self.pdb, text)
        atoms = ["1 PROA 1 ALA CA CT 0 12", "2 PROA 2 ALA CA CT 0 12",
                 "3 HETA 1 LIG C1 CT 0 12", "4 HETA 1 LIG O1 OT 0 16",
                 "5 MEMB 1 POPC P PT 0 31", "6 MEMB 2 POPC P PT 0 31",
                 "7 WAT 1 TIP3 OH2 OT 0 16", "8 IONS 1 POT POT KT 1 39", "9 HETA 1 LIG H1 HT 0 1"]
        atomic_write(self.pdb.with_suffix(".psf"), "PSF\n9 !NATOM\n" + "\n".join(atoms) + "\n1 !NBOND\n3 4\n")
        self.assertEqual(read_scene(self.pdb)["component_bonds"], [(3, 4)])

    def test_psf_identity_mismatch_refused(self):
        atomic_write(self.pdb, self.pdb.read_text().replace("CONECT    3    4\n", ""))
        atomic_write(self.pdb.with_suffix(".psf"), "PSF\n9 !NATOM\n1 P 1 ALA CA C 0 12\n2 P 2 ALA CA C 0 12\n3 H 1 LIG WRONG C 0 12\n")
        with self.assertRaises(ToolError):
            read_scene(self.pdb)

    def test_multiple_models_and_altloc_refused(self):
        original = self.pdb.read_text()
        atomic_write(self.pdb, "MODEL        1\n" + original + "ENDMDL\nMODEL        2\n" + original)
        with self.assertRaisesRegex(ToolError, "one PDB model"):
            read_scene(self.pdb)
        atomic_write(self.pdb, atom(1, "CA", "ALA", (0, 0, 0), altloc="A") + "\n")
        with self.assertRaisesRegex(ToolError, "alternate"):
            read_scene(self.pdb)

    def test_invalid_resolution_and_views_refused_before_output(self):
        for kwargs in ({"width": 399}, {"width": True}, {"views": ("side", "side")}, {"views": ("unknown",)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ToolError):
                render_model(self.pdb, self.root / "invalid", **kwargs)
        self.assertFalse((self.root / "invalid").exists())

    def test_no_components_omits_component_view(self):
        atomic_write(self.pdb, atom(1, "CA", "ALA", (0, 0, 0)) + "\n")
        report = render_model(self.pdb, self.root / "protein-only", width=400)
        self.assertEqual([entry["view"] for entry in report["images"]], ["side", "top"])
        self.assertEqual(json.loads((self.root / "protein-only/preview.json").read_text())["status"], "rendered")

    def final_fixture(self):
        run = self.root / "run"
        system = run / "results/checks/synthetic/system"
        (system / "gromacs").mkdir(parents=True)
        pdb = system / "gromacs/step5_input.pdb"
        atomic_write(pdb, self.pdb.read_text())
        (run / "bilayer").mkdir()
        with tarfile.open(run / "bilayer/charmm-gui.tgz", "w:gz") as archive:
            archive.add(pdb, arcname="system/gromacs/step5_input.pdb")
        atomic_write(run / "full-run.json", json.dumps({"config": {"ligand_resname": "LIG"}}))
        atomic_write(run / "results/validation.json", json.dumps({"fingerprint": "fixture", "extracted_directory": str(system)}))
        return run, pdb

    def test_final_preview_cache_preserves_old_images_and_repairs_changed_artifact(self):
        run, _ = self.final_fixture()
        with patch("charmm_gui_cli.store.managed._final_summary", return_value={"report_file": "validation.json"}):
            first = preview_run(run, width=640)
            self.assertEqual(first["scope"], "final_simulation_input")
            self.assertEqual(preview_run(run, width=640), first)
            atomic_write(first["images"][0]["file"], "changed")
            second = preview_run(run, width=640)
            self.assertNotEqual(first["model"], second["model"])
            self.assertEqual(Path(first["images"][0]["file"]).read_text(), "changed")

    def test_changed_extracted_coordinates_rejected_without_rendering(self):
        run, pdb = self.final_fixture()
        atomic_write(pdb, pdb.read_text() + "REMARK changed\n")
        with patch("charmm_gui_cli.store.managed._final_summary", return_value={"report_file": "validation.json"}), \
                self.assertRaisesRegex(ToolError, "differ from"):
            preview_run(run)
        self.assertFalse((run / "results/previews").exists())

    def test_stale_validation_rejected_before_render(self):
        run, _ = self.final_fixture()
        with patch("charmm_gui_cli.store.managed._final_summary", return_value={"validation_status": "stale_or_unreadable"}), \
                self.assertRaisesRegex(ToolError, "current final"):
            preview_run(run)

    def test_duplicate_archive_models_are_not_arbitrarily_selected(self):
        run, pdb = self.final_fixture()
        with tarfile.open(run / "bilayer/charmm-gui.tgz", "w:gz") as archive:
            archive.add(pdb, arcname="one/gromacs/step5_input.pdb")
            archive.add(pdb, arcname="two/gromacs/step5_input.pdb")
        with patch("charmm_gui_cli.store.managed._final_summary", return_value={"report_file": "validation.json"}), \
                self.assertRaisesRegex(ToolError, "unique matching"):
            preview_run(run)

    def test_leaflet_coloring_is_independent_of_coordinate_origin(self):
        scene = read_scene(self.pdb)
        for atoms in scene["membranes"].values():
            for entry in atoms:
                entry["xyz"] = tuple(v + 100 if i == 2 else v for i, v in enumerate(entry["xyz"]))
        heads = [p for p in _primitives(scene) if p[0] == "sphere" and p[3] == 1.1]
        self.assertEqual(len({p[4] for p in heads}), 2)

    def test_unreadable_text_has_actionable_error(self):
        self.pdb.write_bytes(b"\xffinvalid")
        with self.assertRaisesRegex(ToolError, "readable text"):
            read_scene(self.pdb)


if __name__ == "__main__":
    unittest.main()
