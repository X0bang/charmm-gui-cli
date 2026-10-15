"""Original-chain outer ends, explicit fragment overrides and strict form binding.

These offline tests do not establish actual PSF caps or public split support.
"""

from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.chem.fragment_preparation import prepare_fragmented_lines, remap_state_intent
from charmm_gui_cli.chem.fragment_terminals import resolve_fragment_terminals
from charmm_gui_cli.chem.pdb_normalization import N_TERMINALS, C_TERMINALS
from charmm_gui_cli.chem.protein_states import normalize_states
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.pipeline.build_config import validate_preparation
from charmm_gui_cli.pipeline.full_build import _preparation_fields
from charmm_gui_cli.remote.fragment_terminals import bind_fragment_terminals
from charmm_gui_cli.store.auth import ToolError
from tests.test_fragment_preparation import residue


class FragmentTerminalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-fragment-terminals-"))
        self.source = residue(1, 1, 0, segment="PROA") + residue(6, 2, 9, segment="PROA", name="GLY") + ["END"]
        self.prepare()
        self.network = patch("requests.Session.request", side_effect=AssertionError("No HTTP"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(set_language)

    def prepare(self):
        self.prepared, self.plan = prepare_fragmented_lines(self.source, accept_breaks=True)
        self.source_file, self.prepared_file = self.root / "source.pdb", self.root / "prepared.pdb"
        self.source_file.write_text("\n".join(self.source) + "\n")
        self.prepared_file.write_text("\n".join(self.prepared) + "\n")

    def normalize(self, raw=None):
        return validate_preparation({} if raw is None else raw, allow_fragments=True)

    def resolve(self, raw=None):
        return resolve_fragment_terminals(self.normalize(raw), self.source, self.prepared, self.plan)

    def catalogue(self):
        return {"PRO" + row["prepared_chain"]: {str(r["resid"]) + r["icode"]: r["resname"]
                for r in row["source_residues"]} for row in self.plan["fragments"]}

    def page(self, *, catalogue=None, options=None, omit=(), extra="", script=None):
        catalogue = self.catalogue() if catalogue is None else catalogue
        names = self.catalogue()
        controls = '<input type="file" name="sdf_cgenff[LIG]">'
        for chain in names:
            for end, defaults in (("first", N_TERMINALS), ("last", C_TERMINALS)):
                name = f"terminal[{chain}][{end}]"
                if name in omit:
                    continue
                values = (options or {}).get(name, defaults)
                controls += '<select name="' + name + '">' + ''.join(
                    '<option value="' + value + '">' + value + '</option>' for value in values) + '</select>'
        text = ('<script>' + (script if script is not None else 'var prot_chains=' + json.dumps(catalogue) + ';') + '</script>'
                '<form action="?doc=input/pdbreader&amp;step=3"><input name="jobid" value="1234567890">'
                + controls + extra + '</form>')
        path = self.root / "selection.html"
        path.write_text(text)
        return path

    def fields(self, raw=None, snapshot=None, **kwargs):
        receipt = {}
        result = _preparation_fields(snapshot or self.page(), {"ligand_resname": "LIG", "preparation": raw or {}},
            fragment_plan=self.plan, fragment_source_pdb=self.source_file, fragment_protein_pdb=self.prepared_file,
            fragment_mapping=receipt, **kwargs)
        return result, receipt

    def assert_key(self, key, action):
        with self.assertRaises(ToolError) as caught:
            action()
        self.assertEqual(caught.exception.key, key)
        self.assertTrue(caught.exception.next_step)
        return caught.exception

    def test_original_n_first_and_c_last_not_first_fragment(self):
        result = self.resolve({"terminals": {"A": {"n_terminal": "ACE", "c_terminal": "CT2"}}})
        self.assertEqual(result["preparation"]["terminals"], {
            "A": {"n_terminal": "ACE", "c_terminal": "CTER"},
            "B": {"n_terminal": "NTER", "c_terminal": "CT2"}})
        self.assertEqual(result["fragments"][0]["terminals"]["c_terminal"]["origin"], "global")
        self.assertEqual(result["fragments"][1]["terminals"]["c_terminal"]["origin"], "source_chain")

    def test_segment_alias_and_canonical_alias_match_original_identity(self):
        for selector in ("A", "PROA", "TAIL"):
            self.source = [line[:72] + "TAIL" + line[76:] if line.startswith("ATOM") else line for line in self.source]
            self.prepare()
            result = self.resolve({"terminals": {selector: {"c_terminal": "CT3"}}})
            self.assertEqual(result["preparation"]["terminals"]["B"]["c_terminal"], "CT3")

    def test_explicit_fragment_overrides_source_and_global_with_layers(self):
        raw = {"c_terminal": "CNEU", "terminals": {"A": {"c_terminal": "CT2"},
               "fragment:B": {"n_terminal": "GLYP", "c_terminal": "NONE"}}}
        before = deepcopy(raw)
        result = self.resolve(raw)
        self.assertEqual(raw, before)
        value = result["fragments"][1]["terminals"]["c_terminal"]
        self.assertEqual(value["value"], "NONE")
        self.assertEqual(value["origin"], "fragment")
        self.assertEqual([layer["value"] for layer in value["overridden"]], ["CNEU", "CT2"])
        self.assertEqual(result["requested_preparation"]["terminals"], before["terminals"])
        self.assertFalse(result["remote_verified"])

    def test_inline_fragment_requests_only_in_explicit_internal_mode(self):
        raw = {"n_terminal": "A=ACE,fragment:B=GLYP"}
        self.assert_key("terminal.mapping_invalid", lambda: validate_preparation(raw))
        self.assertEqual(self.resolve(raw)["preparation"]["terminals"]["B"]["n_terminal"], "GLYP")

    def test_inline_normalization_does_not_modify_requested_mapping(self):
        raw = {"n_terminal": "A=ACE", "terminals": {"A": {"c_terminal": "CT2"}}}
        before = deepcopy(raw)
        self.resolve(raw)
        self.assertEqual(raw, before)

    def test_bare_new_chain_is_not_silently_a_fragment_selector(self):
        self.assert_key("fragment.selector_missing", lambda: self.resolve({"terminals": {"B": {"n_terminal": "GLYP"}}}))

    def test_unknown_fragment_and_source_selectors_fail(self):
        for selector in ("fragment:Z", "Z"):
            self.assert_key("fragment.selector_missing", lambda: self.resolve({"terminals": {selector: {"c_terminal": "CT2"}}}))

    def test_original_alias_conflict_is_not_resolved_by_order(self):
        self.assert_key("fragment.selector_conflict", lambda: self.resolve({"terminals": {
            "A": {"n_terminal": "ACE"}, "PROA": {"c_terminal": "CT2"}}}))

    def test_repeated_source_chain_ids_require_unique_segment_selector(self):
        self.source = self.source[:-1] + ["TER"] + residue(10, 1, 25, segment="TAIL") + ["END"]
        self.prepare()
        self.assert_key("fragment.selector_ambiguous", lambda: self.resolve({"terminals": {"A": {"c_terminal": "CT2"}}}))
        result = self.resolve({"terminals": {"TAIL": {"n_terminal": "ACE", "c_terminal": "CT2"}}})
        self.assertEqual(result["preparation"]["terminals"]["C"], {"n_terminal": "ACE", "c_terminal": "CT2"})

    def test_original_second_chain_not_confused_with_newly_allocated_chain(self):
        self.source = self.source[:-1] + ["TER"] + residue(10, 1, 25, chain="B", segment="PROB")
        self.prepare()
        result = self.resolve({"terminals": {"B": {"n_terminal": "ACE", "c_terminal": "CT2"}}})
        self.assertEqual([r["prepared_chain"] for r in result["fragments"]], ["A", "C", "B"])
        self.assertEqual(result["preparation"]["terminals"]["B"]["c_terminal"], "CT2")
        self.assertEqual(result["preparation"]["terminals"]["C"]["c_terminal"], "CTER")

    def test_three_fragments_only_outer_ends_inherit_source(self):
        self.source = self.source[:-1] + residue(10, 3, 18, segment="PROA", name="PRO")
        self.prepare()
        result = self.resolve({"terminals": {"A": {"n_terminal": "ACE", "c_terminal": "CT2"}}})
        self.assertEqual(result["preparation"]["terminals"]["B"], {"n_terminal": "NTER", "c_terminal": "CTER"})
        self.assertEqual(result["preparation"]["terminals"]["C"]["c_terminal"], "CT2")

    def test_explicit_residue_specific_patch_must_match_actual_fragment_start(self):
        for raw in ({"n_terminal": "GLYP"}, {"terminals": {"fragment:B": {"n_terminal": "PROP"}}}):
            self.assert_key("terminal.residue_mismatch", lambda: self.resolve(raw))
        self.assertEqual(self.resolve({"terminals": {"fragment:B": {"n_terminal": "GLYP"}}})
                         ["preparation"]["terminals"]["B"]["n_terminal"], "GLYP")

    def test_all_existing_patch_options_and_orientations_are_preserved(self):
        for n in N_TERMINALS:
            if n in ("GLYP", "PROP"):
                continue
            for c in C_TERMINALS:
                result = self.resolve({"n_terminal": n, "c_terminal": c, "orientation": "prepared"})
                self.assertEqual(result["preparation"]["terminals"]["B"], {"n_terminal": n, "c_terminal": c})
                self.assertEqual(result["preparation"]["orientation"], "prepared")

    def test_raw_or_invalid_settings_fail_closed_in_pure_resolver(self):
        for prep in ({}, {"n_terminal": "A=ACE", "c_terminal": "CTER"},
                     {"n_terminal": "NTER", "c_terminal": "CTER", "unexpected": True},
                     {"n_terminal": "NTER", "c_terminal": "CTER", "terminals": {"A": {"n_terminal": "ace"}}}):
            self.assert_key("fragment.terminals_invalid", lambda: resolve_fragment_terminals(prep, self.source, self.prepared, self.plan))

    def test_tampered_plan_source_or_prepared_bytes_refused(self):
        for which in ("plan", "source", "prepared"):
            source, prepared, plan = deepcopy((self.source, self.prepared, self.plan))
            if which == "plan":
                plan["fragments"][0]["c_terminus"]["source"]["resid"] = 100
            elif which == "source":
                source[0] += " "
            else:
                prepared[0] += " "
            self.assert_key("fragment.evidence_changed", lambda: resolve_fragment_terminals(self.normalize(), source, prepared, plan))

    def test_form_binding_uses_exact_residues_and_both_end_controls(self):
        result, receipt = self.fields({"terminals": {"A": {"n_terminal": "ACE", "c_terminal": "CT2"}}})
        self.assertEqual({k: v for k, v in result.items() if k.startswith("terminal[")}, {
            "terminal[PROA][first]": "ACE", "terminal[PROA][last]": "CTER",
            "terminal[PROB][first]": "NTER", "terminal[PROB][last]": "CT2"})
        self.assertEqual(receipt["source_job_id"], "1234567890")
        self.assertEqual(receipt["plan_fingerprint"], self.plan["fingerprint"])
        self.assertFalse(receipt["submission_confirmed"])
        self.assertFalse(receipt["remote_verified"])
        self.assertEqual(receipt["fragments"][1]["terminals"]["c_terminal"]["form_value"], "CT2")

    def test_form_gly_fallback_records_requested_resolved_and_form_value(self):
        snapshot = self.page(options={"terminal[PROB][first]": ("GLYP", "ACE", "NONE")})
        adjustments = []
        with redirect_stderr(io.StringIO()) as output:
            result, receipt = self.fields(snapshot=snapshot, adjustments=adjustments)
        self.assertEqual(result["terminal[PROB][first]"], "GLYP")
        entry = receipt["fragments"][1]["terminals"]["n_terminal"]
        self.assertEqual((entry["value"], entry["form_value"]), ("NTER", "GLYP"))
        self.assertEqual(adjustments[0]["params"]["chain"], "PROB")
        self.assertIn("GLYP", output.getvalue())

    def test_form_pro_fallback_requires_actual_pro_even_if_gly_offered(self):
        self.source = residue(1, 1, 0, segment="PROA") + residue(6, 2, 9, segment="PROA", name="PRO")
        self.prepare()
        snapshot = self.page(options={"terminal[PROB][first]": ("GLYP", "PROP", "ACE", "NONE")})
        with redirect_stderr(io.StringIO()):
            result, _ = self.fields(snapshot=snapshot)
        self.assertEqual(result["terminal[PROB][first]"], "PROP")

    def test_form_fallback_does_not_apply_gly_patch_to_ala(self):
        snapshot = self.page(options={"terminal[PROA][first]": ("GLYP", "ACE", "NONE")})
        self.assert_key("terminal.unavailable", lambda: self.fields(snapshot=snapshot))

    def test_disabled_optgroup_does_not_supply_fallback(self):
        snapshot = self.page(options={"terminal[PROB][first]": ("ACE", "NONE")})
        snapshot.write_text(snapshot.read_text().replace('<select name="terminal[PROB][first]">',
            '<select name="terminal[PROB][first]"><optgroup disabled><option value="GLYP">GLYP</option></optgroup>'))
        self.assert_key("terminal.unavailable", lambda: self.fields(snapshot=snapshot))

    def test_no_fallback_when_nter_is_available(self):
        result, receipt = self.fields()
        self.assertEqual(result["terminal[PROB][first]"], "NTER")
        self.assertEqual(receipt["fragments"][1]["terminals"]["n_terminal"]["form_value"], "NTER")

    def test_insertion_codes_are_matched_not_renumbered_or_stripped(self):
        self.source = residue(1, 59, 0, segment="PROA", insertion="A") + residue(6, 59, 9, segment="PROA", name="GLY", insertion="B")
        self.prepare()
        self.fields()
        catalogue = self.catalogue()
        catalogue["PROB"] = {"59": "GLY"}
        self.assert_key("web.form_missing", lambda: self.fields(snapshot=self.page(catalogue=catalogue)))

    def test_supported_exact_server_spellings_are_recorded_not_guessed(self):
        snapshot = self.page()
        snapshot.write_text(snapshot.read_text().replace("PROA", "A").replace("PROB", "PRO-B"))
        preparation, receipt = bind_fragment_terminals(snapshot, self.normalize(), self.source, self.prepared, self.plan)
        self.assertEqual(set(preparation["terminals"]), {"A", "PRO-B"})
        self.assertEqual([r["server_chain"] for r in receipt["fragments"]], ["A", "PRO-B"])
        result, _ = self.fields(snapshot=snapshot)
        self.assertEqual(result["terminal[PRO-B][last]"], "CTER")

    def test_missing_extra_changed_or_renumbered_catalogue_residue_is_refused(self):
        for change in ({}, {"2": "ALA"}, {"1": "GLY"}, {"2": "GLY", "3": "ALA"}, {"2": {"resname": "GLY"}}):
            catalogue = self.catalogue()
            catalogue["PROB"] = change
            self.assert_key("web.form_missing", lambda: self.fields(snapshot=self.page(catalogue=catalogue)))

    def test_extra_missing_or_ambiguous_catalogue_chain_is_refused(self):
        original = self.catalogue()
        for catalogue in ({"PROA": original["PROA"]}, {**original, "PROC": {"7": "ALA"}},
                          {**original, "B": original["PROB"]}, {"PROA": original["PROA"], "OTHER": original["PROB"]}):
            self.assert_key("web.form_missing", lambda: self.fields(snapshot=self.page(catalogue=catalogue)))

    def test_missing_either_end_control_is_refused_even_global_defaults(self):
        for chain in ("PROA", "PROB"):
            for end in ("first", "last"):
                self.assert_key("web.form_missing", lambda: self.fields(snapshot=self.page(omit=(f"terminal[{chain}][{end}]",))))

    def test_duplicate_disabled_multiple_wrong_type_or_extra_controls_refused(self):
        for replacement in ('<select disabled name=', '<select multiple name=', '<input name='):
            snapshot = self.page()
            snapshot.write_text(snapshot.read_text().replace('<select name=', replacement, 1))
            self.assert_key("web.form_missing", lambda: self.fields(snapshot=snapshot))
        for extra in ('<select name="terminal[PROA][first]"></select>', '<select name="terminal[FOREIGN][last]"></select>',
                      '<select name="terminal[PROA][other]"></select>'):
            self.assert_key("web.form_missing", lambda: self.fields(snapshot=self.page(extra=extra)))

    def test_disabled_fieldset_and_multiple_job_forms_fail(self):
        for update in (lambda text: text.replace('<select name=', '<fieldset disabled><select name=', 1).replace('</form>', '</fieldset></form>'),
                       lambda text: text + '<form><input name="jobid" value="1234567891"></form>',
                       lambda text: text.replace('</form>', '<input name="jobid" value="1234567890"></form>')):
            snapshot = self.page()
            snapshot.write_text(update(snapshot.read_text()))
            self.assert_key("web.form_missing", lambda: self.fields(snapshot=snapshot))

    def test_literal_json_only_no_javascript_or_duplicate_catalogues(self):
        catalogue = json.dumps(self.catalogue())
        for script in ('var prot_chains=JSON.parse(' + json.dumps(catalogue) + ');',
                       'var prot_chains=' + catalogue + ';var prot_chains=' + catalogue + ';',
                       'var prot_chains=' + catalogue + '\n+ mutate();'):
            self.assert_key("web.form_missing", lambda: self.fields(snapshot=self.page(script=script)))

    def test_error_exposes_safe_server_diagnostic_and_snapshot_not_secrets(self):
        for language in ("zh", "en"):
            set_language(language)
            snapshot = self.page(omit=("terminal[PROB][last]",), extra=
                '<input type="password" value="private-secret"><p class="error">Error parsing ATOM private-secret</p>')
            error = self.assert_key("web.form_missing", lambda: self.fields(snapshot=snapshot))
            self.assertIn("Error parsing ATOM", error.localized())
            self.assertIn(str(snapshot), error.localized())
            self.assertNotIn("private-secret", error.localized())

    def test_changed_input_never_returns_a_form_receipt(self):
        snapshot = self.page()
        self.prepared_file.write_text(self.prepared_file.read_text() + "REMARK changed\n")
        receipt = {}
        self.assert_key("fragment.evidence_changed", lambda: _preparation_fields(snapshot,
            {"ligand_resname": "LIG", "preparation": {}}, fragment_plan=self.plan,
            fragment_source_pdb=self.source_file, fragment_protein_pdb=self.prepared_file, fragment_mapping=receipt))
        self.assertEqual(receipt, {})

    def test_form_receipt_cannot_bind_different_bytes_than_generated_fields(self):
        snapshot = self.page()
        initial = snapshot.read_bytes()
        with patch.object(Path, "read_bytes", side_effect=[initial, initial + b"<!-- changed -->"]):
            self.assert_key("fragment.evidence_changed", lambda: self.fields(snapshot=snapshot))

    def test_missing_plan_or_original_reference_is_not_silently_ignored(self):
        snapshot = self.page()
        config = {"ligand_resname": "LIG", "preparation": self.normalize()}
        self.assert_key("fragment.evidence_changed", lambda: _preparation_fields(snapshot, config, fragment_plan=self.plan))
        self.assert_key("fragment.evidence_changed", lambda: _preparation_fields(snapshot, config, fragment_source_pdb=self.source_file))

    def test_failed_option_validation_does_not_publish_partial_mapping(self):
        snapshot = self.page(options={"terminal[PROB][last]": ("CT2",)})
        receipt = {}
        self.assert_key("terminal.unavailable", lambda: _preparation_fields(snapshot,
            {"ligand_resname": "LIG", "preparation": {}}, fragment_plan=self.plan,
            fragment_source_pdb=self.source_file, fragment_protein_pdb=self.prepared_file, fragment_mapping=receipt))
        self.assertEqual(receipt, {})

    def test_disabled_requested_option_does_not_generate_a_field(self):
        snapshot = self.page()
        snapshot.write_text(snapshot.read_text().replace('<option value="CTER">', '<option disabled value="CTER">'))
        self.assert_key("terminal.unavailable", lambda: self.fields(snapshot=snapshot))

    def test_histidine_catalogue_equivalence_does_not_prove_requested_state(self):
        self.source = residue(1, 1, 0, segment="PROA") + residue(6, 2, 9, segment="PROA", name="HIE")
        self.source, intent = normalize_states(self.source, [])
        self.prepare()
        moved = remap_state_intent(intent, self.source, self.prepared, self.plan)
        catalogue = self.catalogue()
        catalogue["PROB"]["2"] = "HIS"
        script = 'var prot_chains=' + json.dumps(catalogue) + ';var charmm_patch={"prot":{"HIS":["HSE"]}};'
        extra = '<input type="checkbox" name="prot_checked">' + ''.join(
            '<select name="protonation[' + key + '][]"></select>' for key in ("chain", "residue", "rid", "patch"))
        snapshot = self.page(script=script, extra=extra)
        states = {}
        result, receipt = self.fields(snapshot=snapshot, state_intent=moved, reference_pdb=self.prepared_file, state_mapping=states)
        self.assertEqual(result["protonation[chain][]"], ["PROB"])
        self.assertEqual(result["protonation[patch][]"], ["HSE"])
        self.assertEqual(states["residues"][0]["server"]["resid"], "2")
        self.assertFalse(receipt["remote_verified"])

    def test_old_normal_form_fields_are_unchanged_without_fragment_arguments(self):
        snapshot = self.page()
        config = {"ligand_resname": "LIG", "preparation": validate_preparation({})}
        normal = _preparation_fields(snapshot, config)
        fragment, _ = self.fields()
        self.assertEqual(normal, fragment)
        self.assertEqual(set(normal), {"rename_checked", "rename[LIG]", "rename_option[cgenff][LIG]", "newname[LIG]", "comment[LIG]",
            "terminal[PROA][first]", "terminal[PROA][last]", "terminal[PROB][first]", "terminal[PROB][last]"})


if __name__ == "__main__":
    unittest.main()
