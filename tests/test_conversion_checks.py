"""Offline full-grid gates; never launch conversion, simulation or HTTP."""

import json
import unittest
from unittest.mock import patch

from charmm_gui_cli.chem.conversion_checks import compare_cmap_parameters


def topology(values=(1., 2., 3., 4.), *, types="C NH1 CT1 C NH1", dimensions="2 2"):
    return ("; preprocessed test parameters\n[ cmaptypes ]\n" + types + " 1 " + dimensions
            + " \\\n" + " ".join(str(v) for v in values[:2]) + " \\\n"
            + " ".join(str(v) for v in values[2:]) + "\n[ moleculetype ]\nProtein 3\n")


class ConversionChecksTests(unittest.TestCase):
    def test_exact_maps_ignore_comments_whitespace_and_record_order(self):
        one = topology()
        two = topology((5, 6, 7, 8), types="C N CP1 C NH1")
        result = compare_cmap_parameters(one + two, two + one)
        self.assertTrue(result["passed"])
        self.assertEqual(result["checked_cells"], 8)

    def test_grid_cell_order_is_not_a_multiset(self):
        for values in [(2, 1, 3, 4), (1, 3, 2, 4), (4, 3, 2, 1)]:
            with self.subTest(values=values):
                result = compare_cmap_parameters(topology(), topology(values))
                self.assertFalse(result["passed"])
                self.assertGreater(result["mismatches"], 0)

    def test_reversed_atom_types_not_silently_equivalent(self):
        result = compare_cmap_parameters(topology(), topology(types="NH1 C CT1 NH1 C"))
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing_map_count"], 1)

    def test_dimensions_must_match_even_with_same_flat_values(self):
        result = compare_cmap_parameters(topology(), topology(dimensions="1 4"))
        self.assertFalse(result["passed"])
        self.assertEqual(result["examples"][0]["code"], "grid_dimensions_differ")

    def test_full_precision_not_rounded_dump_values(self):
        result = compare_cmap_parameters(topology((1., 2., 3., 4.)), topology((1.0002, 2., 3., 4.)))
        self.assertFalse(result["passed"])
        self.assertEqual(result["examples"][0]["row"], 0)
        self.assertEqual(result["examples"][0]["column"], 0)

    def test_explicit_small_tolerances(self):
        result = compare_cmap_parameters(topology(), topology((1+1e-8, 2, 3, 4)))
        self.assertTrue(result["passed"])
        self.assertFalse(compare_cmap_parameters(topology(), topology((1+1e-8, 2, 3, 4)),
                                                relative_tolerance=0, absolute_tolerance=0)["passed"])

    def test_invalid_and_nonfinite_values_refused(self):
        for value in ("nan", "inf", "-inf", "broken"):
            with self.subTest(value=value):
                result = compare_cmap_parameters(topology(), topology((value, 2, 3, 4)))
                self.assertFalse(result["passed"])
                self.assertEqual(result["error"]["source"], "candidate")
                self.assertNotIn(value, str(result))

    def test_duplicate_maps_refused(self):
        result = compare_cmap_parameters(topology() + topology(), topology())
        self.assertFalse(result["passed"])
        self.assertEqual(result["error"]["code"], "duplicate_map")

    def test_extreme_finite_difference_keeps_evidence_json_finite(self):
        result = compare_cmap_parameters(topology((1e308, 2, 3, 4)), topology((-1e308, 2, 3, 4)))
        self.assertFalse(result["passed"])
        self.assertTrue(result["difference_overflow"])
        self.assertIsNone(result["max_absolute_difference"])
        json.dumps(result, allow_nan=False)

    def test_no_maps_and_unresolved_preprocessor_refused(self):
        for value in ("", "[ atoms ]\n", '#include "secret-token.itp"\n'+topology()):
            with self.subTest(value=value):
                result = compare_cmap_parameters(topology(), value)
                self.assertFalse(result["passed"])
                self.assertNotIn("secret-token", str(result))

    def test_grid_bounds_and_unfinished_records(self):
        for value in (topology(dimensions="0 4"), topology(dimensions="-2 2"),
                      topology(dimensions="1024 1024"), topology(values=(1, 2, 3)),
                      topology(dimensions="9" * 5000 + " 1"),
                      '[ cmaptypes ]\nC NH1 CT1 C NH1 1 2 2 \\\n'):
            with self.subTest(value=value):
                self.assertFalse(compare_cmap_parameters(topology(), value)["passed"])

    def test_errors_and_examples_are_bounded(self):
        many = "".join(topology(types=f'C NH1 T{i} C NH1') for i in range(12))
        other = "".join(topology((9, 9, 9, 9), types=f'C NH1 T{i} C NH1') for i in range(12))
        result = compare_cmap_parameters(many, other)
        self.assertEqual(result["mismatches"], 48)
        self.assertEqual(len(result["examples"]), 8)
        with patch("charmm_gui_cli.chem.conversion_checks._MAX_MAPS", 1):
            self.assertEqual(compare_cmap_parameters(many, other)["error"]["code"], "too_many_maps")

    def test_invalid_tolerances_and_text(self):
        for value in (float('nan'), float('inf'), -1, True, "bad", 10**10000):
            with self.subTest(value=value):
                self.assertFalse(compare_cmap_parameters(topology(), topology(), relative_tolerance=value)["passed"])
        self.assertEqual(compare_cmap_parameters(None, topology())["error"]["code"], "invalid_text")
        with patch("charmm_gui_cli.chem.conversion_checks._MAX_TEXT", 4):
            self.assertFalse(compare_cmap_parameters(topology(), topology())["passed"])


if __name__ == "__main__":
    unittest.main()
