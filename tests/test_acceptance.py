import json
from pathlib import Path
import tempfile
import unittest

from charmm_gui_cli.acceptance import (assess_acceptance, check_leaflets,
                                       extract_cgenff_penalties, summarize_charmm_minimization)


WORKSPACE = Path(__file__).resolve().parents[1]
REAL_SYSTEMS = list((WORKSPACE / "runs/crtw-full-build-corrected-ions/results/checks").glob("*/system"))


def pdb_atom(serial, resid, resname, atom, segment, z, element):
    line = list(" " * 80)
    line[:6] = "ATOM  "
    line[6:11] = f"{serial:5d}"
    line[12:16] = f"{atom:>4}"
    line[17:21] = f"{resname:<4}"
    line[22:27] = f"{resid:5d}"
    line[30:54] = f"{0.0:8.3f}{0.0:8.3f}{z:8.3f}"
    line[72:76] = f"{segment:<4}"
    line[76:78] = f"{element:>2}"
    return "".join(line)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="charmm-acceptance-test-"))
        (self.root / "lig").mkdir()
        (self.root / "step3_size.str").write_text("SET NLIPTOP = 4\nSET NLIPBOT = 4\nSET ZCEN = -8.425\n")
        self.write_coordinates()
        (self.root / "lig/lig.rtf").write_text("RESI LIG 0.000 ! param penalty= 1.4 ; charge penalty= 1.442\n")
        (self.root / "lig/lig.prm").write_text("BONDS\nCT CT 1 1 ! penalty= 1.4\nEND\n")
        (self.root / "step5_input.out").write_text(
            "MINI>        0  100.0000   0.0000  10.0000  0.01\n"
            " EPHI: WARNING. bent improper torsion angle is\n"
            "MINI>      100-944146.96916 12977.24220 2.69359 5.27394\n"
            "ABNER> Minimization exiting with number of steps limit (  100) exceeded.\n"
            "NORMAL TERMINATION BY NORMAL STOP\nMOST SEVERE WARNING WAS AT LEVEL 1\n")
        self.membrane = {"upper": "POPC:CHL1=3:1", "lower": "POPC:CHL1=3:1"}

    def write_coordinates(self, upper=None, lower=None, *, packing_upper=None, flip=None, explicit_elements=True):
        upper = upper or ["POPC", "POPC", "POPC", "CHL1"]
        lower = lower or ["POPC", "POPC", "POPC", "CHL1"]
        packing = (packing_upper or upper) + lower
        final_lines, packing_lines = [], []
        for i, name in enumerate(upper + lower, 1):
            z = 18 if i <= len(upper) else -18
            if flip == i:
                z *= -1
            atom, element = ("O3", "O") if name == "CHL1" else ("P", "P")
            final_lines.append(pdb_atom(i, i, name, atom, "MEMB", z, element if explicit_elements else ""))
            packing_lines.append(pdb_atom(i, i, packing[i - 1], "POLO", "HEAD", 12 if i <= len(upper) else -12, "P"))
        (self.root / "step5_assembly.pdb").write_text("\n".join(final_lines) + "\nEND\n")
        (self.root / "step3_packing_head.pdb").write_text("\n".join(packing_lines) + "\nEND\n")

    def test_matching_final_leaflet_counts_and_ratios(self):
        result = check_leaflets(self.root, self.membrane)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["leaflets"]["upper"]["lipid_counts"], {"POPC": 3, "CHL1": 1})
        self.assertFalse(result["checks"]["final_leaflet_side"]["box_ZCEN_used_as_midplane"])
        self.assertTrue(result["evidence"]["coordinates"]["sha256"])

    def test_mixed_ratio_allows_only_one_molecule_rounding(self):
        self.write_coordinates(upper=["POPC", "POPC", "CHL1", "CHL1"])
        result = check_leaflets(self.root, self.membrane)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["checks"]["upper_requested_ratio"]["count_deviations"], {"POPC": -1.0, "CHL1": 1.0})

    def test_wrong_ratio_not_masked_by_correct_total(self):
        self.write_coordinates(upper=["POPC", "CHL1", "CHL1", "CHL1"])
        result = check_leaflets(self.root, self.membrane)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["upper_requested_ratio"]["passed"])
        self.assertTrue(result["checks"]["final_residue_identity"]["passed"])

    def test_final_composition_must_match_packing_evidence(self):
        self.write_coordinates(packing_upper=["POPC", "POPC", "CHL1", "CHL1"])
        result = check_leaflets(self.root, self.membrane)
        self.assertFalse(result["checks"]["upper_packing_final_composition"]["passed"])

    def test_final_leaflet_flip_rejected(self):
        self.write_coordinates(flip=2)
        self.assertFalse(check_leaflets(self.root, self.membrane)["checks"]["final_leaflet_side"]["passed"])

    def test_missing_element_evidence_does_not_pass(self):
        self.write_coordinates(explicit_elements=False)
        self.assertFalse(check_leaflets(self.root, self.membrane)["checks"]["final_headgroup_identity"]["passed"])

    def test_conflicting_psf_element_is_not_hidden_by_pdb_label(self):
        atoms = []
        for i, resname in enumerate(["POPC", "POPC", "POPC", "CHL1"] * 2, 1):
            name, mass = ("O3", 15.999) if resname == "CHL1" else ("P", 30.974)
            atoms.append(f"{i} MEMB {i} {resname} {name} TYPE 0.0 {1.008 if i == 1 else mass} 0")
        (self.root / "step5_assembly.psf").write_text("PSF EXT\n8 !NATOM\n" + "\n".join(atoms) + "\n")
        result = check_leaflets(self.root, self.membrane)
        self.assertFalse(result["checks"]["final_headgroup_identity"]["passed"])

    def test_missing_declared_count_is_incomplete(self):
        (self.root / "step3_size.str").write_text("SET NLIPTOP = 4\n")
        result = assess_acceptance(self.root, membrane=self.membrane)
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["leaflets"]["status"], "incomplete")

    def test_penalties_are_review_metadata_not_physical_certificate(self):
        (self.root / "lig/lig.rtf").write_text("RESI LIG 0 ! param penalty= 75 ; charge penalty= 80\n")
        result = assess_acceptance(self.root, membrane=self.membrane)
        self.assertTrue(result["passed"])
        self.assertTrue(result["checks"]["cgenff"]["review_required"])
        self.assertFalse(result["checks"]["cgenff"]["blocking"])
        self.assertFalse(result["scientific_correctness_verified"])
        self.assertEqual(result["checks"]["cgenff"]["charge_max"], 80)
        self.assertFalse(extract_cgenff_penalties(self.root, review_threshold=100)["review_required"])

    def test_minimization_limit_warning_does_not_claim_convergence(self):
        result = summarize_charmm_minimization(self.root)
        self.assertTrue(result["passed"])
        self.assertEqual(result["minimization_stages"][-1]["last"]["cycle"], 100)
        self.assertAlmostEqual(result["minimization_stages"][-1]["last"]["grms"], 2.69359)
        self.assertEqual(result["warning_counts"]["bent_improper_torsion"], 1)
        self.assertFalse(result["minimization_convergence_verified"])
        self.assertFalse(result["equilibration_verified"])

    def test_abnormal_termination_blocks_even_if_normal_marker_also_exists(self):
        path = self.root / "step5_input.out"
        path.write_text(path.read_text() + "***** ABNORMAL TERMINATION *****\n")
        result = assess_acceptance(self.root, membrane=self.membrane)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["charmm_minimization"]["passed"])

    def test_adjacent_negative_fixed_width_minimization_fields(self):
        path = self.root / "step5_input.out"
        path.write_text(path.read_text().replace("MINI>      100", "MINI>       70 82230835.055-82557778.812  1377592.420       32.289\nMINI>      100"))
        result = summarize_charmm_minimization(self.root)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["warning_counts"].get("unparsed_minimization_rows", 0), 0)

    def test_unparseable_minimization_values_cannot_pass(self):
        path = self.root / "step5_input.out"
        path.write_text(path.read_text() + "MINI> 101 NaN NaN NaN\n")
        self.assertFalse(summarize_charmm_minimization(self.root)["passed"])

    @unittest.skipUnless(REAL_SYSTEMS, "Private generated archive is not distributed")
    def test_real_generated_archive(self):
        result = assess_acceptance(REAL_SYSTEMS[0], membrane={"upper": "POPC=1", "lower": "POPC=1"})
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["checks"]["leaflets"]["leaflets"]["upper"]["observed_total"], 479)
        self.assertEqual(result["checks"]["leaflets"]["leaflets"]["lower"]["observed_total"], 463)
        self.assertAlmostEqual(result["checks"]["cgenff"]["charge_max"], 1.442)


if __name__ == "__main__":
    unittest.main()
