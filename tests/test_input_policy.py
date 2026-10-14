"""Shared input policies verified through public commands and uploaded files."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from charmm_gui_cli.cli import main
from charmm_gui_cli.chem.inputs import Atom, _pdb, _sdf
from charmm_gui_cli.chem.pdb_normalization import normalize_lines
from charmm_gui_cli.chem.residues import WATERS
from charmm_gui_cli.pipeline import full_build, sweep
from charmm_gui_cli.store.auth import ToolError, atomic_write
from charmm_gui_cli.store.paths import read_value
import test_sweep as web_fixtures
from test_precheck import atom_line as base_atom_line


def atom_line(serial, name, resname="ALA", **kwargs):
    line = base_atom_line(serial, name, resname, **kwargs)
    return line[:21] + line[22:] if len(resname) == 4 else line


class Terminal(io.StringIO):
    def isatty(self):
        return True


class InputPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-input-policy-")).resolve()
        self.protein = self.root / "protein with spaces.pdb"
        self.ligand = self.root / "bound ligand.sdf"
        self.lines = [atom_line(index, name, xyz=xyz) for index, (name, xyz) in enumerate(
            (("N", (-1., 0., 0.)), ("CA", (0., 0., 0.)), ("C", (1., 0., 0.)), ("O", (1., .8, 0.)), ("CB", (0., -1., 0.))), 1)]
        self.write()
        self.ligand.write_text(_sdf([Atom(1, "C1", "C", (0., 3.2, 0.)), Atom(2, "O1", "O", (1.2, 3.2, 0.))], {(0, 1): 2}, "LIG"))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("requests.Session.request", side_effect=AssertionError("No live network")))
        self.stack.enter_context(patch("charmm_gui_cli.store.managed._registry", return_value=self.root / "index.json"))
        self.stack.enter_context(patch("charmm_gui_cli.store.managed._registries", return_value=(self.root / "index.json",)))
        self.stack.enter_context(patch("pathlib.Path.cwd", return_value=self.root))
        self.counter = 0

    def write(self):
        self.protein.write_text("\n".join(self.lines) + "\nEND\n")

    def call(self, args, *, tty=False, lang="en"):
        output, error = Terminal() if tty else io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(["--lang", lang, *args])
        return code, output.getvalue(), error.getvalue()

    def check(self, *options, lang="en", tty=False):
        self.counter += 1
        out = self.root / f"check-{self.counter}"
        code, output, error = self.call(["check", "--protein", str(self.protein), "--ligand", str(self.ligand),
                                         "--out", str(out), "--no-preview", *options], tty=tty, lang=lang)
        report = json.loads((out / "check.json").read_text())
        return code, report, output, error

    def repairs(self, report, key):
        return [entry for entry in report["automatic_repairs"] if entry["key"] == key]

    def test_all_water_names_removed_and_cli_report_matches_uploaded_protein(self):
        self.lines.extend(atom_line(index, "O", name, resid=index, xyz=(20., 0., 0.), record="HETATM")
                          for index, name in enumerate(sorted(WATERS), 900))
        self.write()
        original = self.protein.read_bytes()
        for lang, expected in (("zh", "已删除结晶水"), ("en", "crystallographic waters")):
            with self.subTest(lang=lang):
                code, report, output, _ = self.check(lang=lang, tty=True)
                self.assertEqual(code, 0, report.get("error"))
                self.assertIn(expected, output)
                entry = self.repairs(report, "input.waters_removed")[0]
                self.assertEqual(entry["params"]["count"], len(WATERS))
                self.assertEqual({r["resname"] for r in entry["residues"]}, set(WATERS))
                prepared = report["prepared_files"]
                manifest = read_value(prepared["input-manifest.json"])
                self.assertEqual(manifest["automatic_repairs"], report["automatic_repairs"])
                self.assertEqual(manifest["source_evidence"]["protein"]["sha256"], hashlib.sha256(original).hexdigest())
                self.assertNotIn("HETATM", Path(prepared["protein.pdb"]).read_text())
                self.assertEqual(self.protein.read_bytes(), original)

    def test_water_removal_does_not_hide_metal_and_generates_usable_retention_draft(self):
        self.lines += [atom_line(60, "SG", "CYS", resid=97, xyz=(0., .5, 0.), element="S"),
                       atom_line(900, "O", "HOH", resid=950, record="HETATM", xyz=(20., 0., 0.)),
                       atom_line(901, "ZN", "ZN", resid=901, record="HETATM", element="Zn", xyz=(0., 1.5, 0.))]
        self.write()
        code, report, output, _ = self.check(lang="zh", tty=True)
        self.assertEqual(code, 2)
        self.assertIn("ZN A 901", output)
        self.assertIn("CYS A 97 SG", output)
        self.assertNotIn("辅因子/配体 HOH", output)
        self.assertEqual(report["heterogen_inventory"][0]["resname"], "ZN")
        self.assertEqual(self.repairs(report, "input.waters_removed")[0]["params"]["count"], 1)
        self.assertIn("--drop-hetero ZN", report["error"]["next_step"])
        self.assertIn(str(self.protein), report["error"]["next_step"])
        draft = report["retention_draft"]
        self.assertFalse(draft["ready_to_submit"])
        atoms, _ = _pdb(draft["complex"])
        self.assertEqual({a.line[17:21].strip() for a in atoms if a.line.startswith("HETATM")}, {"ZN", "LIG"})
        config = yaml.safe_load(Path(draft["config"]).read_text())
        metal = next(spec for spec in config["components"] if spec["resname"] == "ZN")
        self.assertFalse(metal["topology"]["trusted"])
        self.assertTrue(metal["sdf"].startswith("REQUIRED/"))
        self.assertIn("核对形式电荷", Path(draft["config"]).read_text())
        code, dropped, output, _ = self.check("--drop-hetero", "ZN", tty=True)
        self.assertEqual(code, 0, dropped.get("error"))
        self.assertEqual(self.repairs(dropped, "input.hetero_removed")[0]["residues"][0]["resname"], "ZN")

    def test_drop_requires_explicit_names_and_cannot_remove_polymer(self):
        for names in ("*", "ALL", "MSE", "ALA"):
            if names == "ALL":
                # ALL is just a literal name, never a wildcard.
                self.lines.append(atom_line(901, "ZN", "ZN", resid=901, record="HETATM", element="Zn", xyz=(0., 1.5, 0.)))
                self.write()
            code, report, _, _ = self.check("--drop-hetero", names)
            self.assertEqual(code, 2)
            self.assertFalse(report["passed"])

    def alternate_lys(self):
        self.lines = [atom_line(index, name, "LYS", xyz=xyz) for index, (name, xyz) in enumerate(
            (("N", (-1., 0., 0.)), ("CA", (0., 0., 0.)), ("C", (1., 0., 0.)), ("O", (1., .8, 0.))), 1)]
        for label, names, occupancy in (("A", ("CB", "CG"), .9), ("B", ("CB", "CG", "CD", "CE", "NZ"), .1)):
            for name in names:
                line = atom_line(len(self.lines) + 1, name, "LYS", altloc=label, xyz=(0., -len(self.lines), 0.))
                self.lines.append(line[:54] + f"{occupancy:6.2f}" + line[60:])
        self.write()

    def test_complete_altloc_fallback_b_and_forced_incomplete_a_cli(self):
        self.alternate_lys()
        original = self.protein.read_bytes()
        for lang in ("zh", "en"):
            code, report, output, _ = self.check(tty=True, lang=lang)
            self.assertEqual(code, 0, report.get("error"))
            self.assertIn("B=1", output)
            entry = self.repairs(report, "input.altloc_selected")[0]
            self.assertEqual(entry["params"]["altloc"], "B")
            self.assertTrue(entry["independent_residue_choice"])
            atoms, _ = _pdb(report["prepared_files"]["protein.pdb"])
            self.assertEqual(len(atoms), 9)
            self.assertTrue(all(a.line[16] == " " and float(a.line[54:60]) == 1 for a in atoms))
            self.assertEqual(self.protein.read_bytes(), original)
            code, failed, output, _ = self.check("--altloc", "A", lang=lang, tty=True)
            self.assertEqual(code, 2)
            self.assertIn("LYS A 1", output)
            for name in ("CD", "CE", "NZ"):
                self.assertIn(name, output)
            self.assertEqual(failed["error"]["message_key"], "input.altloc_incomplete")

    def test_altloc_occupancy_and_lexical_tie_preserve_shared_atoms(self):
        self.lines = self.lines[:4]
        for index, (label, occupancy) in enumerate((("A", .5), ("B", .5)), 5):
            line = atom_line(index, "CB", altloc=label, xyz=(0., -index, 0.))
            self.lines.append(line[:54] + f"{occupancy:6.2f}" + line[60:])
        self.write()
        code, report, _, _ = self.check()
        self.assertEqual(code, 0)
        self.assertEqual(self.repairs(report, "input.altloc_selected")[0]["params"]["altloc"], "A")
        self.lines[-1] = self.lines[-1][:54] + "  0.70" + self.lines[-1][60:]
        self.write()
        code, report, _, _ = self.check()
        self.assertEqual(code, 0)
        self.assertEqual(self.repairs(report, "input.altloc_selected")[0]["params"]["altloc"], "B")
        self.assertEqual(self.repairs(report, "input.altloc_selected")[0]["shared_atoms"], 4)

    def mse(self):
        names = ("N", "CA", "C", "O", "CB", "CG", "SE", "CE")
        self.lines = [atom_line(index, name, "MSE", xyz=(index * .2, -1., 0.), record="HETATM", element="Se" if name == "SE" else name[0])
                      for index, name in enumerate(names, 1)]
        self.write()

    def test_mse_conversion_has_no_fake_gaps_and_keeps_coordinates(self):
        self.mse()
        original, _ = _pdb(self.protein)
        for lang, text in (("zh", "坐标不变"), ("en", "coordinates unchanged")):
            code, report, output, _ = self.check(lang=lang, tty=True)
            self.assertEqual(code, 0, report.get("error"))
            self.assertIn("MSE→MET", output)
            self.assertIn(text, output)
            self.assertEqual(report["protein_inventory"]["protein_residues_all_models"], 1)
            self.assertEqual(report["protein_inventory"]["numbering_gaps"], [])
            prepared, _ = _pdb(report["prepared_files"]["protein.pdb"])
            self.assertEqual([atom.xyz for atom in prepared], [atom.xyz for atom in original])
            self.assertEqual(next(atom.element for atom in prepared if atom.name == "SD"), "S")
        code, report, output, _ = self.check("--keep-mse", lang="zh", tty=True)
        self.assertEqual(code, 2)
        self.assertIn("非标准氨基酸 MSE A 1", output)

    def test_modified_residue_is_recognized_in_inventory_but_never_converted(self):
        self.lines = [atom_line(1, "CA", resid=1), atom_line(2, "CA", "SEP", resid=2, record="HETATM"), atom_line(3, "CA", resid=3)]
        self.write()
        original = self.protein.read_bytes()
        code, report, output, _ = self.check(lang="zh", tty=True)
        self.assertEqual(code, 2)
        self.assertIn("非标准氨基酸 SEP A 2", output)
        self.assertEqual(report["protein_inventory"]["protein_residues_all_models"], 3)
        self.assertEqual(report["protein_inventory"]["numbering_gaps"], [])
        self.assertEqual(self.protein.read_bytes(), original)

    def test_unknown_residue_and_geometric_break_block_before_upload(self):
        self.lines.append(atom_line(50, "CA", "UNK", resid=2))
        self.write()
        code, report, output, _ = self.check(tty=True)
        self.assertEqual(code, 2)
        self.assertIn("UNK A 2", output)
        self.assertEqual(report["error"]["message_key"], "input.unknown_residues")
        self.lines = self.lines[:-1] + [atom_line(50, "N", resid=2, xyz=(15., 0., 0.)), atom_line(51, "CA", resid=2, xyz=(16., 0., 0.))]
        self.write()
        code, report, output, _ = self.check(tty=True, lang="zh")
        self.assertEqual(code, 2)
        self.assertIn("主链断裂", output)
        self.assertIn("14.00 Å", output)
        self.assertEqual(report["error"]["details"][0]["distance_A"], 14.)
        self.lines.insert(5, "TER")
        self.write()
        code, report, _, _ = self.check()
        self.assertEqual(code, 0, report.get("error"))

    def test_cgenff_elements_and_reserved_names_block_without_fake_inventory_gaps(self):
        for element in ("Zn", "B", "Si", "Se"):
            self.ligand.write_text(_sdf([Atom(1, "X1", element, (0., 3.2, 0.))], {}, "LIG"))
            code, report, _, _ = self.check()
            self.assertEqual(code, 2)
            self.assertEqual(report["error"]["message_key"], "input.cgenff_elements")
        self.ligand.write_text(_sdf([Atom(1, "C1", "C", (0., 3.2, 0.))], {}, "LIG"))
        for name in ("CLA", "POT", "SOD", "TIP", "HSD", "POP", "ALA"):
            code, report, _, _ = self.check("--ligand-resname", name)
            self.assertEqual(code, 2)
            self.assertEqual(report["error"]["message_key"], "input.reserved_name")
            self.assertEqual(report["protein_inventory"]["protein_residues_all_models"], 1)
            self.assertEqual(report["protein_inventory"]["numbering_gaps"], [])

    def test_additives_drop_does_not_remove_detergent(self):
        self.lines += [atom_line(900, "C1", "GOL", resid=900, record="HETATM", xyz=(20., 0., 0.)),
                       atom_line(901, "C1", "LMT", resid=901, record="HETATM", xyz=(20., 0., 0.))]
        self.write()
        code, report, output, _ = self.check("--drop-additives", tty=True, lang="zh")
        self.assertEqual(code, 2)
        self.assertIn("去垢剂/脂质碎片 LMT", output)
        self.assertEqual({entry["resname"] for entry in report["heterogen_inventory"]}, {"LMT"})
        self.assertEqual(self.repairs(report, "input.hetero_removed")[0]["residues"][0]["resname"], "GOL")

    def test_model_selection_precedes_duplicate_serials_and_retains_global_connectivity(self):
        self.protein.write_text("MODEL        1\n" + "\n".join(self.lines) + "\nENDMDL\nMODEL        2\n"
                                + "\n".join(self.lines) + "\nENDMDL\nCONECT    1    2\nEND\n")
        code, report, output, _ = self.check(tty=True)
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["message_key"], "input.models")
        self.assertNotIn("Duplicate serials", output)
        code, report, _, _ = self.check("--model", "2")
        self.assertEqual(code, 0, report.get("error"))
        self.assertEqual(self.repairs(report, "input.model_selected")[0]["params"]["number"], 2)
        _, bonds = _pdb(report["prepared_files"]["protein.pdb"])
        self.assertEqual(bonds, {(1, 2): 1})

    def test_generic_final_model_reader_never_drops_water_or_changes_mse(self):
        self.mse()
        self.lines.append(atom_line(900, "O", "HOH", resid=900, record="HETATM", xyz=(20., 0., 0.)))
        self.write()
        atoms, _ = _pdb(self.protein)
        self.assertEqual({atom.line[17:21].strip() for atom in atoms}, {"MSE", "HOH"})
        self.assertEqual(next(atom.element for atom in atoms if atom.name == "SE"), "Se")

    def test_linked_glycan_cannot_be_silently_dropped_or_uploaded_as_noncovalent(self):
        self.lines += [atom_line(901, "C1", "NAG", resid=901, record="HETATM", xyz=(20., 0., 0.)),
                       "LINK        ND2  ASN A   1                 C1   NAG A 901"]
        self.write()
        code, report, _, _ = self.check("--drop-hetero", "NAG")
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["message_key"], "input.linkage")
        self.assertFalse(report["server_submitted"])

    def test_dryrun_terminal_shows_repairs_in_both_languages(self):
        self.mse()
        self.lines.append(atom_line(900, "O", "HOH", resid=900, record="HETATM", xyz=(20., 0., 0.)))
        self.write()
        for lang, text in (("zh", "已删除结晶水"), ("en", "crystallographic waters")):
            code, output, error = self.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
                "--out", str(self.root / ("terminal-" + lang)), "--dry-run", "--hydrogens", "preserve"], tty=True, lang=lang)
            self.assertEqual(code, 0, error)
            self.assertIn("MSE→MET", output)
            self.assertIn(text, output)

    def test_invalid_option_mapping_does_not_mask_localized_error_or_echo_unknown_fields(self):
        from charmm_gui_cli.chem.input_sources import prepare_sources
        for value in ([], {"not_an_option": "not-for-output"}):
            with self.assertRaises(ToolError) as error:
                prepare_sources(out=self.root / "unused", protein=self.protein, ligand=self.ligand, input_options=value)
            self.assertEqual(error.exception.key, "input.options_invalid")
            self.assertNotIn("not-for-output", error.exception.localized())
            self.assertNotIn("not-for-output", error.exception.next_step)

    def test_duplicate_mse_and_removed_water_serials_are_safe_without_conect(self):
        self.mse()
        self.lines = [line[:6] + "    1" + line[11:] for line in self.lines]
        self.lines += [atom_line(900, "O", "HOH", resid=900, record="HETATM", xyz=(20., 0., 0.)),
                       atom_line(900, "O", "HOH", resid=901, record="HETATM", xyz=(20., 1., 0.))]
        self.write()
        original = self.protein.read_bytes()
        code, report, _, _ = self.check()
        self.assertEqual(code, 0, report.get("error"))
        self.assertEqual(self.repairs(report, "input.renumbered")[0]["params"]["count"], 8)
        atoms, _ = _pdb(report["prepared_files"]["protein.pdb"])
        self.assertEqual([atom.serial for atom in atoms], list(range(1, 9)))
        self.assertEqual(self.protein.read_bytes(), original)
        self.lines.append("CONECT    1  900")
        self.write()
        code, report, _, _ = self.check()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["message_key"], "input.duplicate_conect")

    def test_deleted_blank_chain_water_does_not_steal_protein_segment_chain(self):
        self.lines = [line[:21] + " " + line[22:72].ljust(50) + "PROA" + line[76:] for line in self.lines]
        self.lines.append(atom_line(900, "O", "HOH", chain=" ", resid=900, record="HETATM", xyz=(20., 0., 0.)))
        self.write()
        code, report, _, _ = self.check()
        self.assertEqual(code, 0, report.get("error"))
        atoms, _ = _pdb(report["prepared_files"]["protein.pdb"])
        self.assertEqual({atom.line[21] for atom in atoms}, {"A"})
        self.assertEqual(self.repairs(report, "input.chains_added")[0]["params"]["segment"], "PROA")

    def test_build_batch_sweep_use_identical_repairs_and_files(self):
        self.mse()
        se = self.lines[6]
        self.lines[6] = se[:16] + "A" + se[17:54] + "  0.60" + se[60:]
        self.lines.append(se[:6] + "  100" + se[11:16] + "B" + se[17:54] + "  0.40" + se[60:])
        self.lines += [atom_line(900, "O", "HOH", resid=900, record="HETATM", xyz=(20., 0., 0.)),
                       atom_line(901, "ZN", "ZN", resid=901, record="HETATM", element="Zn", xyz=(20., 1., 0.)),
                       atom_line(902, "C1", "GOL", resid=902, record="HETATM", xyz=(20., 2., 0.))]
        self.write()
        out = self.root / "single"
        code, output, error = self.call(["build", "--dry-run", "--protein", str(self.protein), "--ligand", str(self.ligand),
                                         "--out", str(out), "--name", "single", "--hydrogens", "preserve", "--drop-hetero", "ZN", "--drop-additives", "--json"])
        self.assertEqual(code, 0, error)
        single = read_value(out / "inputs/input-manifest.json")
        self.assertEqual({entry["key"] for entry in single["automatic_repairs"]}, {"input.mse_converted", "input.waters_removed", "input.altloc_selected", "input.hetero_removed"})
        inputs = {"protein": str(self.protein), "ligand": str(self.ligand), "hydrogens": "preserve", "altloc": "highest", "drop_hetero": ["ZN"], "drop_additives": True}
        for command, raw in (("batch", {"version": 1, "defaults": inputs, "jobs": [{"name": "a"}, {"name": "b"}]}),
                             ("sweep", {"version": 1, "inputs": inputs, "axes": {"salt_concentration": [.1, .2]}})):
            config = self.root / (command + ".yaml")
            config.write_text(yaml.safe_dump(raw))
            directory = self.root / command
            code, output, error = self.call([command, str(config), "--out", str(directory), "--dry-run", "--json"])
            self.assertEqual(code, 0, error + output)
            children = directory / "batch" if command == "sweep" else directory
            for manifest_path in children.glob("jobs/*/inputs/input-manifest.json"):
                manifest = read_value(manifest_path)
                self.assertEqual(manifest["automatic_repairs"], single["automatic_repairs"])
                self.assertEqual((manifest_path.parent / "protein.pdb").read_bytes(), (out / "inputs/protein.pdb").read_bytes())
            self.assertEqual(len(list(children.glob("jobs/*/inputs/input-manifest.json"))), 2)

    def test_308_modified_residues_count_and_coordinates_survive_conversion(self):
        self.lines = []
        names = (("N", 0., 0.), ("CA", .8, 0.), ("C", 1.7, 0.), ("O", 1.7, .8),
                 ("CB", .8, -1.), ("CG", .8, -2.), ("SE", .8, -3.), ("CE", .8, -4.))
        for resid in range(1, 309):
            for name, x, y in names:
                self.lines.append(atom_line(len(self.lines) + 1, name, "MSE", resid=resid, record="HETATM",
                                            element="Se" if name == "SE" else name[0], xyz=(3 * (resid - 1) + x, y, 0.)))
        self.write()
        original = self.protein.read_bytes()
        code, report, output, _ = self.check(tty=True, lang="zh")
        self.assertEqual(code, 0, report.get("error"))
        self.assertIn("308", output)
        self.assertEqual(report["protein_inventory"]["protein_residues_all_models"], 308)
        self.assertEqual(report["protein_inventory"]["numbering_gaps"], [])
        self.assertEqual(report["original_protein_inventory"]["missing_heavy_atoms"], [])
        self.assertEqual(self.repairs(report, "input.mse_converted")[0]["params"]["count"], 308)
        prepared, _ = _pdb(report["prepared_files"]["protein.pdb"])
        self.assertEqual([atom.xyz for atom in prepared], [atom.xyz for atom in _pdb(self.protein)[0]])
        self.assertEqual(self.protein.read_bytes(), original)

    def test_components_use_shared_normalization_and_correct_discard_and_hydrogen_counts(self):
        self.mse()
        self.lines += ["TER", atom_line(800, "C1", "LIG", chain="L", record="HETATM", xyz=(0., 3.2, 0.)),
                       atom_line(801, "O1", "LIG", chain="L", record="HETATM", element="O", xyz=(1.2, 3.2, 0.)),
                       atom_line(900, "O", "HOH", resid=900, record="HETATM", xyz=(20., 0., 0.))]
        self.write()
        config = self.root / "components.yaml"
        config.write_text(yaml.safe_dump({"version": 1, "water_policy": "reject", "ion_policy": "reject",
                         "components": [{"resname": "LIG", "chain": "L", "resid": 1, "sdf": str(self.ligand)}]}))
        out = self.root / "component-check"
        code, output, error = self.call(["check", "--complex", str(self.protein), "--components", str(config),
                                         "--out", str(out), "--hydrogens", "preserve", "--no-preview", "--json"])
        self.assertEqual(code, 0, error + output)
        report = read_value(out / "check.json")
        manifest = read_value(report["prepared_files"]["input-manifest.json"])
        self.assertEqual(manifest["automatic_repairs"], report["automatic_repairs"])
        self.assertEqual(manifest["discarded_atoms"], 1)
        self.assertEqual(manifest["hydrogens_added"], 0)
        self.assertNotIn("HOH", Path(report["prepared_complex"]).read_text())
        code, output, error = self.call(["build", "--complex", str(self.protein), "--components", str(config),
                                         "--out", str(self.root / "component-build"), "--hydrogens", "preserve", "--dry-run", "--json"])
        self.assertEqual(code, 0, error + output)
        build = read_value(self.root / "component-build/inputs/input-manifest.json")
        self.assertEqual(build["automatic_repairs"], manifest["automatic_repairs"])

    def test_version_two_configuration_uses_model_selection_and_repair_policy(self):
        self.protein.write_text("MODEL        1\n" + "\n".join(self.lines) + "\nENDMDL\nMODEL        2\n"
                                + "\n".join(self.lines) + "\nENDMDL\nEND\n")
        config = self.root / "build.yaml"
        config.write_text(yaml.safe_dump({"version": 2, "protein": str(self.protein), "ligand": str(self.ligand),
                          "input_options": {"model": 2, "altloc": "highest"}, "ligand_hydrogens": "preserve",
                          "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
                          "ions": {"type": "KCl", "concentration_M": .15}}))
        out = self.root / "configured"
        code, output, error = self.call(["build", str(config), "--out", str(out), "--dry-run", "--json"])
        self.assertEqual(code, 0, error + output)
        manifest = read_value(out / "inputs/input-manifest.json")
        self.assertEqual(self.repairs(manifest, "input.model_selected")[0]["params"]["number"], 2)

    def test_repaired_upload_uses_sealed_inputs_not_original_structures(self):
        self.lines.append(atom_line(900, "O", "HOH", resid=900, record="HETATM", xyz=(20., 0., 0.)))
        self.write()
        out = self.root / "upload"
        code, _, error = self.call(["build", "--dry-run", "--protein", str(self.protein), "--ligand", str(self.ligand),
                                    "--out", str(out), "--hydrogens", "preserve", "--json"])
        self.assertEqual(code, 0, error)
        web, api = Mock(), Mock()
        api.submit.return_value = "456"
        uploaded = []

        def upload(pdb, destination):
            uploaded.append(Path(pdb).read_text())
            self.assertNotIn("HOH", uploaded[0])
            Path(destination).mkdir()
            atomic_write(Path(destination) / "upload-response.html", web_fixtures.UPLOAD)

        def submit(source, destination, fields, uploads=None):
            atomic_write(destination, web_fixtures.SELECTION if Path(destination).name == "selection.html" else web_fixtures.READY)

        web.upload_structure.side_effect = upload
        web.submit_snapshot.side_effect = submit
        result = full_build.advance(out / ".charmm-gui", web, api)
        self.assertEqual(result["state"], "bilayer_submitted")
        web.upload_structure.assert_called_once()
        self.assertTrue((out / ".charmm-gui/preparation-seal.json").is_file())
        self.assertIn("HOH", self.protein.read_text())


if __name__ == "__main__":
    unittest.main()
