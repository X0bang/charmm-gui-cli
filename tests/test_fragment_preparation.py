"""Explicit fragment primitives preserve coordinates and source evidence.

No CLI opt-in or provider support is implied by these local tests.
"""

from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.chem.fragment_preparation import (
    backbone_breaks, prepare_fragmented_lines, verify_fragment_preparation, remap_state_intent,
)
from charmm_gui_cli.chem.inputs import Atom, _pdb, _sdf
from charmm_gui_cli.chem.input_safety import validate_protein
from charmm_gui_cli.cli import main
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.store.auth import ToolError
from tests.test_pdb_boundaries import atom


class Terminal(io.StringIO):
    def isatty(self):
        return True


def residue(serial, resid, x, *, chain="A", segment="", name="ALA", insertion=" "):
    positions = [("N", (x, 0, 0)), ("CA", (x+.5, 0, 0)), ("C", (x+1, 0, 0)),
                 ("O", (x+1, .8, 0))]
    if name != "GLY":
        positions.append(("CB", (x, -1, 0)))
    if name in ("CYS", "CYX"):
        positions.append(("SG", (x, -2, 0)))
    if name == 'ILE':
        positions.extend([('CG1', (x, -2, 0)), ('CG2', (x, -1, 1)), ('CD1', (x, -3, 0))])
    rows = [atom(serial+i, name, resid, xyz, chain=chain, segment=segment, name=kind, element=kind[0])
            for i, (kind, xyz) in enumerate(positions)]
    if insertion != " ":
        rows = [row[:26]+insertion+row[27:] for row in rows]
    return rows


def ssbond(left_chain="A", left=1, right_chain="A", right=2):
    line = list(" "*80)
    line[:6] = "SSBOND"
    line[7:10] = "  1"
    for chain_index, residue_index, name_index, chain, resid in (
        (15, 17, 11, left_chain, left), (29, 31, 25, right_chain, right),
    ):
        line[chain_index] = chain
        line[residue_index:residue_index+4] = f"{resid:4d}"
        line[name_index:name_index+3] = "CYS"
    return "".join(line)


class FragmentPreparationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-fragment-preparation-")).resolve()
        self.path = self.root / "protein with spaces.pdb"
        self.lines = residue(1, 1, 0) + residue(6, 2, 9, name="GLY") + ["END"]
        self.network = patch("requests.Session.request", side_effect=AssertionError("No HTTP"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(set_language)

    def atoms(self, lines):
        self.path.write_text("\n".join(lines)+"\n")
        return _pdb(self.path)[0]

    def assert_refused(self, lines, key, **kwargs):
        with self.assertRaises(ToolError) as caught:
            prepare_fragmented_lines(lines, **kwargs)
        self.assertEqual(caught.exception.key, key)
        return caught.exception

    def test_default_still_rejects_and_carries_endpoint_atom_evidence(self):
        before = list(self.lines)
        error = self.assert_refused(self.lines, "input.chain_breaks")
        self.assertEqual(error.params["count"], 1)
        self.assertEqual(error.details[0]["left_atom_serial"], 3)
        self.assertEqual(error.details[0]["right_atom_serial"], 6)
        self.assertEqual(error.details[0]["distance_A"], 8.)
        self.assertEqual(error.details[0]["right_source"]["resname"], "GLY")
        self.assertEqual(self.lines, before)

    def test_explicit_acceptance_preserves_all_atom_fields_except_chain_segment(self):
        self.lines = residue(1, 1, 0, segment="PROA") + residue(6, 2, 9, segment="PROA") + ["END"]
        before = list(self.lines)
        result, plan = prepare_fragmented_lines(self.lines, accept_breaks=True)
        source_atoms = [row for row in before if row.startswith("ATOM")]
        output_atoms = [row for row in result if row.startswith("ATOM")]
        self.assertEqual(len(source_atoms), len(output_atoms))
        for old, new in zip(source_atoms, output_atoms):
            self.assertEqual(old[:21], new[:21])
            self.assertEqual(old[22:72], new[22:72])
            self.assertEqual(old[76:], new[76:])
            self.assertEqual(new[72:76], "    ")
        self.assertEqual([f["prepared_chain"] for f in plan["fragments"]], ["A", "B"])
        self.assertEqual(result.count("TER"), 2)
        self.assertEqual(self.lines, before)
        validate_protein(self.atoms(result))
        self.assertFalse(plan["coordinates_modified"])
        self.assertFalse(plan["caps_generated_locally"])
        self.assertFalse(plan["remote_verified"])
        proof = verify_fragment_preparation(before, result, plan)
        self.assertTrue(proof["passed"])
        self.assertEqual(proof["scope"], "local_fragment_preparation_only")

    def test_no_break_no_new_chains_or_ter_or_plan(self):
        lines = residue(1, 1, 0) + residue(6, 9, 2.3) + ["END"]
        for accepted in (False, True):
            self.assertEqual(prepare_fragmented_lines(lines, accept_breaks=accepted), (lines, {}))

    def test_numbering_gaps_and_insertion_codes_do_not_force_splits(self):
        lines = residue(1, 59, 0) + residue(6, 59, 2.3, insertion="A") + residue(11, 90, 4.6)
        self.assertEqual(backbone_breaks(self.atoms(lines)), [])
        self.assertEqual(prepare_fragmented_lines(lines, accept_breaks=True), (lines, {}))

    def test_threshold_is_strictly_above_two_angstroms(self):
        for x, count in ((3., 0), (3.001, 1)):
            with self.subTest(x=x):
                lines = residue(1, 1, 0) + residue(6, 2, x)
                self.assertEqual(len(backbone_breaks(self.atoms(lines))), count)

    def test_stored_plan_distance_is_portable_but_detection_keeps_full_precision(self):
        lines = list(self.lines)
        lines[5] = lines[5][:38]+f'{.001:8.3f}'+lines[5][46:]
        distance = backbone_breaks(self.atoms(lines))[0]["distance_A"]
        self.assertGreater(distance, 8.)
        _, plan = prepare_fragmented_lines(lines, accept_breaks=True)
        self.assertEqual(plan["breaks"][0]["distance_A"], round(distance, 6))

    def test_all_original_chains_and_heterogen_chain_ids_are_reserved(self):
        lines = self.lines[:-1] + residue(10, 1, 30, chain="B")
        lines.append(atom(15, "LIG", 1, (0, 5, 0), chain="C", record="HETATM"))
        result, plan = prepare_fragmented_lines(lines, accept_breaks=True)
        self.assertEqual([f["prepared_chain"] for f in plan["fragments"]], ["A", "D", "B"])
        self.assertEqual(result[-1], lines[-1])

    def test_existing_ter_and_segment_boundaries_remain_separate(self):
        lines = self.lines[:-1] + ["TER", "TER"] + residue(10, 1, 25, segment="TAIL")
        result, plan = prepare_fragmented_lines(lines, accept_breaks=True)
        self.assertEqual(len(plan["fragments"]), 3)
        self.assertEqual([r["source_chain"]["polymer_block"] for r in plan["fragments"]], [0, 0, 2])
        self.assertEqual([r["prepared_polymer_block"] for r in plan["fragments"]], [0, 1, 3])
        self.assertEqual(result.count("TER"), 4)
        self.assertEqual(backbone_breaks(self.atoms(result)), [])
        annotated = list(" "*80)
        for start, value in ((0, "TER   "), (6, "   10"), (17, "GLY"), (21, "A"), (22, "   2")):
            annotated[start:start+len(value)] = value
        annotated = "".join(annotated)
        source = self.lines[:-1] + [annotated, "END"]
        prepared, plan = prepare_fragmented_lines(source, accept_breaks=True)
        updated = next(row for row in prepared if row.startswith('TER   '))
        self.assertEqual(updated[21], 'B')
        self.assertEqual(updated[:21]+updated[22:], annotated[:21]+annotated[22:])
        self.assertTrue(verify_fragment_preparation(source, prepared, plan)['passed'])
        for changed in (annotated[:21]+'Z'+annotated[22:], annotated[:22]+'  99'+annotated[26:]):
            self.assert_refused(self.lines[:-1]+[changed], 'fragment.ter_ambiguous', accept_breaks=True)

    def test_chain_and_segment_changes_alone_do_not_invent_geometric_break(self):
        for tail in (residue(6, 2, 9, chain="B"), residue(6, 2, 9, segment="SEG2"), ["TER"]+residue(6, 2, 9)):
            lines = residue(1, 1, 0) + tail
            self.assertEqual(backbone_breaks(self.atoms(lines)), [])

    def test_explicit_ter_same_chain_separates_without_inventing_geometry_finding(self):
        lines = residue(1, 1, 0) + ["TER"] + residue(6, 2, 2.3)
        self.assertEqual(prepare_fragmented_lines(lines), (lines, {}))
        prepared, plan = prepare_fragmented_lines(lines, accept_breaks=True)
        self.assertEqual(plan["breaks"], [])
        self.assertEqual([f["prepared_chain"] for f in plan["fragments"]], ["A", "B"])
        self.assertTrue(verify_fragment_preparation(lines, prepared, plan)["passed"])

    def test_explicit_states_follow_prepared_identity_without_mutating_original_recipe(self):
        from charmm_gui_cli.chem.protein_states import normalize_states
        lines = residue(1, 1, 0) + residue(6, 2, 9, name="HIE")
        normalized, intent = normalize_states(lines, [])
        original_intent = copy.deepcopy(intent)
        prepared, plan = prepare_fragmented_lines(normalized, accept_breaks=True)
        result = remap_state_intent(intent, normalized, prepared, plan)
        self.assertEqual(intent, original_intent)
        entry = result["residues"][0]
        self.assertEqual((entry["chain"], entry["segment"], entry["polymer_block"], entry["resid"]), ("B", "", 1, 2))
        self.assertEqual((entry["source_resname"], entry["state"]), ("HIE", "HSE"))
        self.assertFalse(entry["remote_verified"])

    def test_disulfide_recipe_endpoints_follow_two_different_fragments(self):
        from charmm_gui_cli.chem.protein_states import normalize_states
        lines = ([ssbond()] + residue(1, 1, 0, name="CYX") + residue(7, 2, 9, name="CYS")
                 + ["TER"] + residue(13, 3, 30, chain="C"))
        # Synthetic protocol fixture: only SG proximity is supplied here; this
        # does not assert realistic side-chain geometry or remote S-S support.
        lines = [row[:30]+f'{2.05:8.3f}{-2.:8.3f}{0.:8.3f}'+row[54:]
                 if row.startswith('ATOM') and int(row[6:11]) == 12 else row for row in lines]
        normalized, intent = normalize_states(lines, [])
        prepared, plan = prepare_fragmented_lines(normalized, accept_breaks=True)
        remapped = remap_state_intent(intent, normalized, prepared, plan)
        pair = remapped["disulfides"][0]
        self.assertEqual((pair["left"]["chain"], pair["right"]["chain"]), ("A", "B"))
        self.assertEqual((pair["left"]["polymer_block"], pair["right"]["polymer_block"]), (0, 1))
        self.assertEqual(pair["distance_A"], intent["disulfides"][0]["distance_A"])
        self.assertFalse(pair["remote_verified"])
        foreign = copy.deepcopy(intent)
        foreign["disulfides"][0]["right"].update(chain="C", resid=3, polymer_block=1)
        with self.assertRaises(ToolError):
            remap_state_intent(foreign, normalized, prepared, plan)

    def test_fingerprint_recomputation_and_unmapped_state_do_not_bypass_rederivation(self):
        from charmm_gui_cli.chem.fragment_preparation import _fingerprint
        from charmm_gui_cli.chem.protein_states import normalize_states
        normalized, intent = normalize_states(residue(1, 1, 0) + residue(6, 2, 9, name="HIE"), [])
        prepared, plan = prepare_fragmented_lines(normalized, accept_breaks=True)
        edited = copy.deepcopy(plan)
        edited["fragments"][1]["prepared_chain"] = "C"
        del edited["fingerprint"]
        edited["fingerprint"] = _fingerprint(edited)
        with self.assertRaises(ToolError):
            verify_fragment_preparation(normalized, prepared, edited)
        for wrong_residue in (99, 1):
            # A missing address and an existing ALA address are both invalid
            # for the HSE recipe, even when the recipe itself is well formed.
            changed = copy.deepcopy(intent)
            changed["residues"][0]["resid"] = wrong_residue
            with self.assertRaises(ToolError):
                remap_state_intent(changed, normalized, prepared, plan)

    def test_endpoint_mapping_keeps_gly_and_pro_residue_intent(self):
        lines = residue(1, 1, 0, name="GLY") + residue(5, 2, 9, name="PRO")
        _, plan = prepare_fragmented_lines(lines, accept_breaks=True)
        self.assertEqual([f["n_terminus"]["source"]["resname"] for f in plan["fragments"]], ["GLY", "PRO"])
        self.assertEqual(plan["breaks"][0]["required_terminal_sides"], {"left": "c_terminal", "right": "n_terminal"})
        self.assertEqual(plan["fragments"][1]["n_terminus"]["atom_serial"], 5)

    def test_explicit_peptide_conect_is_not_silently_deleted(self):
        for conect in ("CONECT    3    6", "CONECT    6    3", "CONECT    1    8"):
            self.assert_refused(self.lines + [conect], "fragment.explicit_peptide_bond", accept_breaks=True)

    def test_ssbond_chain_endpoints_are_uniquely_remapped(self):
        lines = [ssbond()] + residue(1, 1, 0, name="CYS") + residue(7, 2, 9, name="CYS")
        result, plan = prepare_fragmented_lines(lines, accept_breaks=True)
        self.assertEqual((result[0][15], result[0][29]), ("A", "B"))
        self.assertEqual(result[0][:29], lines[0][:29])
        self.assertEqual(result[0][30:], lines[0][30:])
        self.assertFalse(plan["remote_verified"])

    def test_sidechain_cross_fragment_bonds_are_preserved_and_recorded(self):
        lines = [ssbond()] + residue(1, 1, 0, name="CYS") + residue(7, 2, 9, name="CYS") + ["CONECT    6   12", "CONECT   12    6"]
        result, plan = prepare_fragmented_lines(lines, accept_breaks=True)
        self.assertEqual(plan["retained_cross_fragment_bonds"], [[6, 12]])
        self.assertEqual([r for r in result if r.startswith("CONECT")], lines[-2:])

    def test_ambiguous_ssbond_address_is_not_guessed(self):
        lines = [ssbond(left=1, right=2)] + residue(1, 1, 0, name="CYS") + residue(7, 2, 9, name="CYS") + ["TER"] + residue(13, 2, 20, name="CYS")
        self.assert_refused(lines, "fragment.ssbond_ambiguous", accept_breaks=True)

    def test_ssbond_to_absent_cysteine_is_not_retained_as_valid(self):
        self.assert_refused([ssbond()] + self.lines, "fragment.ssbond_ambiguous", accept_breaks=True)

    def test_link_and_unknown_residue_not_implicitly_repaired(self):
        self.assert_refused(["LINK  "] + self.lines, "fragment.linked_input", accept_breaks=True)
        lines = self.lines[:-1] + residue(10, 3, 10, name="UNK")
        self.assert_refused(lines, "fragment.unsupported_polymer", accept_breaks=True)

    def test_chain_exhaustion_preserves_original_and_does_not_use_reserved_ids(self):
        from charmm_gui_cli.chem.fragment_preparation import _CHAINS
        lines = self.lines[:-1] + [atom(100+i, "LIG", 1, (0, 5, 0), chain=c, record="HETATM") for i, c in enumerate(_CHAINS)]
        original = list(lines)
        self.assert_refused(lines, "fragment.chains_exhausted", accept_breaks=True)
        self.assertEqual(lines, original)

    def test_malformed_multimodel_or_altloc_input_not_normalized_implicitly(self):
        cases = [["MODEL        1"] + self.lines, ["ENDMDL"] + self.lines, self.lines + [self.lines[0]],
                 [self.lines[0][:16]+"A"+self.lines[0][17:]] + self.lines[1:],
                 [self.lines[0][:30]+"     nan"+self.lines[0][38:]] + self.lines[1:]]
        for lines in cases:
            with self.subTest(lines=lines[:1]):
                self.assert_refused(lines, "fragment.invalid", accept_breaks=True)
        self.assert_refused(self.lines, "fragment.invalid", accept_breaks=1)

    def test_recurring_residue_or_competing_resname_cannot_be_merged(self):
        for lines in (self.lines[:-1] + residue(10, 1, 30),
                      residue(1, 1, 0) + residue(6, 1, 9, name="GLY")):
            self.assert_refused(lines, "fragment.recurring_residue", accept_breaks=True)

    def test_interleaved_chains_still_detect_gaps_but_split_requires_sorting(self):
        lines = residue(1, 1, 0) + residue(6, 1, 30, chain="B") + residue(11, 2, 9)
        self.assertEqual(len(backbone_breaks(self.atoms(lines))), 1)
        self.assert_refused(lines, "fragment.interleaved_chains", accept_breaks=True)

    def test_missing_terminal_atoms_are_not_rebuilt(self):
        self.assert_refused(self.lines[1:], "fragment.terminal_atoms_missing", accept_breaks=True)

    def test_plan_and_coordinate_or_metadata_edits_are_detected(self):
        prepared, plan = prepare_fragmented_lines(self.lines, accept_breaks=True)
        for key, value in (("remote_verified", True), ("source_sha256", "0"*64), ("fingerprint", "0"*64)):
            edited = copy.deepcopy(plan)
            edited[key] = value
            with self.assertRaises(ToolError):
                verify_fragment_preparation(self.lines, prepared, edited)
        changed = list(prepared)
        changed[0] = changed[0][:30]+" 100.000"+changed[0][38:]
        with self.assertRaises(ToolError):
            verify_fragment_preparation(self.lines, changed, plan)
        with self.assertRaises(ToolError):
            verify_fragment_preparation(self.lines + ["REMARK changed"], prepared, plan)

    def test_bilingual_public_check_still_blocks_without_hidden_opt_in(self):
        self.path.write_text("\n".join(self.lines)+"\n")
        original = self.path.read_bytes()
        ligand = self.root / "ligand.sdf"
        ligand.write_text(_sdf([Atom(1, "C1", "C", (0, 5, 0)), Atom(2, "O1", "O", (1.2, 5, 0))], {(0, 1): 2}, "LIG"))
        for lang, phrase in (("en", "Backbone break"), ("zh", "主链断裂")):
            out = self.root / lang
            output, error = Terminal(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = main(["--lang", lang, "check", "--protein", str(self.path), "--ligand", str(ligand),
                             "--out", str(out), "--no-preview"])
            self.assertEqual(code, 2)
            report = json.loads((out/"check.json").read_text())
            self.assertEqual(report["error"]["message_key"], "input.chain_breaks")
            self.assertIn(phrase, error.getvalue()+output.getvalue())
            self.assertIn(str(self.path), report["error"]["next_step"])
            self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
