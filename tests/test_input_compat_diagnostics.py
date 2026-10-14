"""Actual CLI compatibility diagnostics; no input overwrite or remote requests."""

import hashlib
import itertools
import json
from pathlib import Path
import random
import shlex
import unittest
from unittest.mock import patch

from charmm_gui_cli.chem.duplicate_ligand import coordinate_match, _minimum_assignment, _DiagnosticLimit
from charmm_gui_cli.chem.inputs import Atom, _sdf
from charmm_gui_cli.chem.pdb_normalization import normalize_lines
from charmm_gui_cli.store.paths import read_value
import test_input_policy as fixtures


class InputCompatibilityDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InputPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.protein, self.ligand = self.fixture.root, self.fixture.protein, self.fixture.ligand

    def embedded(self, *, offset=0., resid=501, serial=900, hydrogens=False):
        # Names and record order intentionally differ from the independent SDF.
        lines = [fixtures.atom_line(serial, "OX", "LIG", element="O", chain="L", resid=resid,
                                    xyz=(1.2 + offset, 3.2, 0.), record="HETATM"),
                 fixtures.atom_line(serial + 1, "CX", "LIG", element="C", chain="L", resid=resid,
                                    xyz=(offset, 3.2, 0.), record="HETATM")]
        if hydrogens:
            lines.append(fixtures.atom_line(serial + 2, "HX", "LIG", element="H", chain="L", resid=resid,
                                           xyz=(offset, 4.2, 0.), record="HETATM"))
        self.fixture.lines.extend(lines)
        self.fixture.write()

    def test_duplicate_pose_is_reported_bilingually_without_deletion_or_chemical_claim(self):
        self.embedded(hydrogens=True)
        original = self.protein.read_bytes()
        for language, expected in (("zh", "可能重复"), ("en", "may be duplicated")):
            code, report, output, _ = self.fixture.check(lang=language, tty=True)
            self.assertEqual(code, 2)
            self.assertIn(expected, output)
            self.assertEqual(report["error"]["message_key"], "input.protein_hetero")
            candidate = report["duplicate_ligand_candidates"][0]
            self.assertEqual(candidate["resname"], "LIG")
            self.assertEqual(candidate["rmsd_A"], 0.)
            self.assertEqual(candidate["heavy_atoms"], 2)
            self.assertFalse(candidate["aligned"])
            self.assertFalse(candidate["chemical_identity_verified"])
            self.assertEqual({p["protein_serial"] for p in candidate["atom_mapping"]}, {900, 901})
            self.assertEqual({p["ligand_index"] for p in candidate["atom_mapping"]}, {1, 2})
            self.assertIn("--drop-hetero LIG", report["error"]["next_step"])
            self.assertIn(str(self.protein), report["error"]["next_step"])
            self.assertEqual(self.protein.read_bytes(), original)
            self.assertEqual(report["input_evidence"]["protein"]["sha256"], hashlib.sha256(original).hexdigest())

    def test_explicit_duplicate_removal_uploads_only_protein_and_one_independent_ligand(self):
        self.embedded()
        original = self.protein.read_bytes()
        code, report, _, _ = self.fixture.check("--drop-hetero", "LIG")
        self.assertEqual(code, 0, report.get("error"))
        manifest = read_value(report["prepared_files"]["input-manifest.json"])
        self.assertTrue(any(item["key"] == "input.hetero_removed" for item in manifest["automatic_repairs"]))
        self.assertNotIn("HETATM", Path(report["prepared_files"]["protein.pdb"]).read_text())
        ligand_lines = [line for line in Path(report["prepared_complex"]).read_text().splitlines()
                        if line.startswith("HETATM") and line[76:78].strip() != "H"]
        self.assertEqual(len(ligand_lines), 2)
        code, output, errors = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
            "--drop-hetero", "LIG", "--dry-run", "--out", str(self.root / "direct-build"), "--no-preview", "--json"])
        self.assertEqual(code, 0, errors)
        built = json.loads(output)
        self.assertFalse(built["server_submitted"])
        prepared = read_value(Path(built["directory"]) / "full-run.json")["inputs"]["manifest"]
        self.assertTrue(any(item["key"] == "input.hetero_removed" for item in prepared["automatic_repairs"]))
        self.assertEqual(self.protein.read_bytes(), original)

    def test_duplicate_with_metal_does_not_suggest_dropping_the_metal(self):
        self.embedded()
        self.fixture.lines.append(fixtures.atom_line(990, "ZN", "ZN", element="Zn", resid=901,
                                                  xyz=(8., 8., 8.), record="HETATM"))
        self.fixture.write()
        code, report, _, _ = self.fixture.check()
        self.assertEqual(code, 2)
        command = shlex.split(report["error"]["next_step"])
        self.assertEqual(command[command.index("--drop-hetero") + 1], "LIG")
        code, second, _, _ = self.fixture.check("--drop-hetero", "LIG")
        self.assertEqual(code, 2)
        self.assertEqual({item["resname"] for item in second["heterogen_inventory"]}, {"ZN"})

    def test_same_named_unmatched_copy_has_no_name_wide_removal_next_step(self):
        self.embedded()
        self.embedded(offset=10., resid=502, serial=950)
        code, report, output, _ = self.fixture.check(tty=True)
        self.assertEqual(code, 2)
        self.assertEqual(len(report["duplicate_ligand_candidates"]), 1)
        self.assertNotIn("--drop-hetero", report["error"]["next_step"])
        detail = next(d for d in report["error"]["details"] if d.get("key") == "input.duplicate_drop_scope")
        self.assertEqual(detail["params"], {"name": "LIG", "total": 2, "matched": 1})
        self.assertIn("other binding-site", output)

    def test_a_distinct_coordinate_frame_is_not_aligned_to_fabricate_a_duplicate(self):
        self.embedded(offset=10.)
        _, report, _, _ = self.fixture.check()
        self.assertNotIn("duplicate_ligand_candidates", report)

    def test_coordinate_nearness_cannot_override_element_counts(self):
        self.embedded()
        self.fixture.lines[-2] = self.fixture.lines[-2][:76] + " N" + self.fixture.lines[-2][78:]
        self.fixture.write()
        _, report, _, _ = self.fixture.check()
        self.assertNotIn("duplicate_ligand_candidates", report)

    def test_diagnostic_work_limit_keeps_the_original_blocking_error(self):
        self.embedded()
        with patch("charmm_gui_cli.chem.duplicate_ligand.coordinate_match", side_effect=_DiagnosticLimit):
            code, report, output, _ = self.fixture.check(tty=True)
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["message_key"], "input.protein_hetero")
        self.assertNotIn("duplicate_ligand_candidates", report)
        self.assertTrue(any(d.get("key") == "input.duplicate_scan_limited" for d in report["error"]["details"]))
        self.assertIn("work limit", output)
        self.assertFalse(report["server_submitted"])

    def test_duplicate_selection_respects_ligand_pose_without_silently_picking_a_record(self):
        self.embedded()
        bound = self.ligand.read_text()
        shifted = _sdf([Atom(1, "C1", "C", (10., 3.2, 0.)), Atom(2, "O1", "O", (11.2, 3.2, 0.))], {(0, 1): 2}, "LIG")
        self.ligand.write_text(shifted + bound)
        _, unselected, _, _ = self.fixture.check()
        self.assertNotIn("duplicate_ligand_candidates", unselected)
        _, selected, _, _ = self.fixture.check("--ligand-pose", "2")
        self.assertEqual(selected["duplicate_ligand_candidates"][0]["selected_pose"], 2)

    def test_heavy_atom_map_retains_original_sdf_indices_including_preceding_hydrogen(self):
        self.embedded(hydrogens=True)
        self.ligand.write_text(_sdf([Atom(1, "H1", "H", (0., 4.2, 0.)), Atom(2, "C1", "C", (0., 3.2, 0.)),
                                    Atom(3, "O1", "O", (1.2, 3.2, 0.))], {(0, 1): 1, (1, 2): 2}, "LIG"))
        _, report, _, _ = self.fixture.check()
        self.assertEqual({p["ligand_index"] for p in report["duplicate_ligand_candidates"][0]["atom_mapping"]}, {2, 3})

    def test_coordinate_assignment_is_bijective_and_rmsd_threshold_is_strict(self):
        reference = [Atom(1, "A", "C", (-.01, 0., 0.)), Atom(2, "B", "C", (.01, 2., 0.))]
        observed = [Atom(10, "X", "C", (-.01, 2., 0.)), Atom(20, "Y", "C", (.01, 0., 0.))]
        match = coordinate_match(reference, observed, [1000])
        self.assertAlmostEqual(match["rmsd_A"], .02)
        self.assertEqual({p["protein_serial"] for p in match["atom_mapping"]}, {10, 20})
        one = [Atom(1, "C", "C", (0., 0., 0.))]
        self.assertIsNotNone(coordinate_match(one, [Atom(2, "C", "C", (.499, 0., 0.))], [10]))
        self.assertIsNone(coordinate_match(one, [Atom(2, "C", "C", (.5, 0., 0.))], [10]))
        self.assertIsNone(coordinate_match(one, [Atom(2, "C", "C", (float("nan"), 0., 0.))], [10]))
        self.assertIsNone(coordinate_match(one, [Atom(2, "C", "C", (0., 0.))], [10]))
        with self.assertRaises(_DiagnosticLimit):
            coordinate_match(reference, observed, [0])

    def test_minimum_assignment_matches_independent_exhaustive_small_cases(self):
        rng = random.Random(718)
        for count in range(1, 6):
            for _ in range(10):
                costs = [[rng.randrange(20) for _ in range(count)] for _ in range(count)]
                assignment = _minimum_assignment(costs, [100_000])
                self.assertEqual(len({i for i, _ in assignment}), count)
                self.assertEqual(len({j for _, j in assignment}), count)
                expected = min(sum(costs[row][column] for row, column in enumerate(columns))
                               for columns in itertools.permutations(range(count)))
                self.assertEqual(sum(costs[i][j] for i, j in assignment), expected)

    def test_misaligned_pdb_cli_diagnostic_precedes_duplicate_serial_message(self):
        self.protein.write_text("ATOM 1 N ALA A 1 -1.0 0.0 0.0\nATOM 2 CA ALA A 1 0.0 0.0 0.0\n")
        original = self.protein.read_bytes()
        for language, expected in (("zh", "列未对齐"), ("en", "misaligned")):
            code, report, output, _ = self.fixture.check(lang=language, tty=True)
            self.assertEqual(code, 2)
            self.assertEqual(report["error"]["message_key"], "input.pdb_columns")
            self.assertEqual(report["error"]["message_params"]["line"], 1)
            self.assertIn(expected, output)
            self.assertNotIn("unique serial", output)
            self.assertIn(str(self.protein), report["error"]["next_step"])
            self.assertEqual(self.protein.read_bytes(), original)

    def test_hybrid36_and_number_widths_are_rejected_without_truncation(self):
        line = self.fixture.lines[0]
        examples = [(line[:6] + "A0000" + line[11:], "input.pdb_hybrid36"),
                    (line[:22] + "A000" + line[26:], "input.pdb_hybrid36"),
                    (line[:6] + "100000" + line[11:], "input.serial_limit"),
                    (line[:22] + "10000" + line[26:], "input.pdb_residue_width")]
        for text, expected in examples:
            with self.subTest(key=expected):
                self.protein.write_text(text + "\n")
                original = self.protein.read_bytes()
                code, report, _, _ = self.fixture.check()
                self.assertEqual(code, 2)
                self.assertEqual(report["error"]["message_key"], expected)
                self.assertFalse(report["server_submitted"])
                self.assertEqual(self.protein.read_bytes(), original)

    def test_header_only_file_is_not_misreported_as_duplicate_serials(self):
        self.protein.write_text("HEADER    No coordinates\nEND\n")
        code, report, _, _ = self.fixture.check()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["message_key"], "input.pdb_no_atoms")
        self.assertNotIn("unique serial", report["error"]["message"])

    def test_selected_valid_model_is_not_blocked_by_malformed_unselected_model(self):
        self.protein.write_text("MODEL        1\nATOM 1 N ALA A 1 -1 0 0\nENDMDL\nMODEL        2\n" +
                                "\n".join(self.fixture.lines) + "\nENDMDL\n")
        code, report, _, _ = self.fixture.check("--model", "2")
        self.assertEqual(code, 0, report.get("error"))
        self.assertEqual(report["original_inventory_error"]["message_key"], "input.pdb_columns")
        self.assertEqual(report["protein_inventory"]["protein_residues_all_models"], 1)
        self.assertEqual(report["automatic_repairs"][0]["params"]["number"], 2)
        code, output, errors = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
            "--model", "2", "--dry-run", "--out", str(self.root / "model-2-build"), "--no-preview", "--json"])
        self.assertEqual(code, 0, errors)
        self.assertFalse(json.loads(output)["server_submitted"])

    def test_model_selection_reports_the_original_source_line_for_an_element_error(self):
        missing = self.fixture.lines[0][:12] + " XX " + self.fixture.lines[0][16:76] + "  "
        self.protein.write_text("MODEL        1\n" + self.fixture.lines[0] + "\nENDMDL\nMODEL        2\n" + missing + "\nENDMDL\n")
        _, report, _, _ = self.fixture.check("--model", "2")
        self.assertEqual(report["error"]["message_key"], "input.element_unknown")
        self.assertEqual(report["error"]["message_params"]["line"], 5)

    def test_insertion_codes_do_not_invent_gaps_and_are_preserved_in_prepared_pdb(self):
        lines = []
        for group, (resid, icode) in enumerate(((59, " "), (59, "A"), (59, "B"), (59, "C"), (60, " "))):
            for name, xyz in (("N", (-1., 0., 0.)), ("CA", (0., 0., 0.)), ("C", (1., 0., 0.)),
                              ("O", (1., .8, 0.)), ("CB", (0., -1., 0.))):
                lines.append(fixtures.atom_line(len(lines) + 1, name, resid=resid, insertion=icode,
                                                xyz=(xyz[0] + 3.33 * group, xyz[1], xyz[2])))
        self.fixture.lines = lines
        self.fixture.write()
        code, report, _, _ = self.fixture.check()
        self.assertEqual(code, 0, report.get("error"))
        self.assertEqual(report["protein_inventory"]["numbering_gaps"], [])
        self.assertEqual(report["protein_inventory"]["protein_residues_all_models"], 5)
        output = Path(report["prepared_files"]["protein.pdb"]).read_text().splitlines()
        self.assertEqual({line[22:27] for line in output if line.startswith("ATOM")}, {line[22:27] for line in lines})
        self.fixture.lines[-5:] = [line[:22] + "  62" + line[26:] for line in lines[-5:]]
        self.fixture.write()
        code, report, _, _ = self.fixture.check()
        self.assertEqual(code, 0, report.get("error"))
        gap = report["protein_inventory"]["numbering_gaps"][0]
        self.assertEqual(gap["unused_integer_numbers"], 2)
        self.assertFalse(gap["missing_residues_inferred"])


if __name__ == "__main__":
    unittest.main()
