"""Explicit naming intent and no unverified residue-state POSTs."""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import yaml

from charmm_gui_cli.chem.inputs import _pdb
from charmm_gui_cli.chem.pdb_normalization import normalize_lines
from charmm_gui_cli.chem.protein_states import intent_for, validate_intent
from charmm_gui_cli.pipeline import full_build, sweep
from charmm_gui_cli.remote.protein_states import state_fields
from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.store.paths import read_value
import test_input_policy as fixtures


class ProteinStateIntentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InputPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.protein, self.ligand = self.fixture.root, self.fixture.protein, self.fixture.ligand

    def rename(self, name):
        self.fixture.lines = [line[:17] + f"{name:<4}" + line[21:] for line in self.fixture.lines]
        self.fixture.write()

    def normalize(self):
        report = {}
        lines = normalize_lines(self.protein, report, options={}, role="protein")
        return lines, report["protein_state_intent"]

    def form(self, chain_map, *, patches=None, omit=()):
        patch_map = {"ASP": ["ASPP"], "GLU": ["GLUP"], "LYS": ["LSN"], "CYS": ["CYM"],
                     "HIS": ["HSD", "HSE", "HSP"], "HSD": ["HSE", "HSP"], "HSE": ["HSD", "HSP"]}
        names = ["prot_checked", "ssbonds_checked", *["protonation[" + key + "][]" for key in ("chain", "residue", "rid", "patch")],
                 *["ssbonds[" + key + "][]" for key in ("segid1", "resid1", "segid2", "resid2")]]
        path = self.root / "selection.html"
        path.write_text('<script>var prot_chains=' + json.dumps(chain_map) + '; var charmm_patch=' +
                        json.dumps({"prot": patch_map if patches is None else patches}) + ';</script>' +
                        '<form action="?doc=input/pdbreader&step=3"><input name="jobid" value="123">' +
                        ''.join(('<input type="checkbox" name="' + name + '">' if name.endswith("_checked")
                                 else '<select name="' + name + '"></select>') for name in names if name not in omit) + '</form>')
        return path

    def test_histidine_amber_mapping_preserves_coordinates_and_is_visible_bilingually(self):
        for source, target in (("HID", "HSD"), ("HIE", "HSE"), ("HIP", "HSP")):
            self.rename(source)
            original = self.protein.read_bytes()
            for language, expected in (("zh", "上传基础残基"), ("en", "upload base residue")):
                code, report, output, _ = self.fixture.check(lang=language, tty=True)
                self.assertEqual(code, 0, report.get("error"))
                self.assertIn(expected, output)
                plan = intent_for({"input_normalization": report["input_normalization"]})
                self.assertEqual(plan["residues"][0]["state"], target)
                self.assertEqual(plan["residues"][0]["pdb_resname"], target)
                self.assertFalse(plan["remote_verified"])
                atoms, _ = _pdb(report["prepared_files"]["protein.pdb"])
                self.assertEqual({a.line[17:21].strip() for a in atoms}, {target})
                self.assertEqual([a.xyz for a in atoms], [a.xyz for a in _pdb(self.protein)[0]])
                self.assertEqual(self.protein.read_bytes(), original)
                self.assertTrue(report["review_required"])
                self.assertFalse(report["server_submitted"])

    def test_patch_names_are_not_written_as_nonexistent_residue_templates(self):
        for source, base, patch_name in (("ASH", "ASP", "ASPP"), ("GLH", "GLU", "GLUP"), ("LYN", "LYS", "LSN"),
                                    ("ASPP", "ASP", "ASPP"), ("GLUP", "GLU", "GLUP"), ("LSN", "LYS", "LSN")):
            self.rename(source)
            code, report, _, _ = self.fixture.check()
            self.assertEqual(code, 0, report.get("error"))
            plan = report["input_normalization"]["protein_state_intent"]
            entry = plan["residues"][0]
            self.assertEqual((entry["source_resname"], entry["pdb_resname"], entry["state"], entry["method"]),
                             (source, base, patch_name, "patch"))
            self.assertEqual({a.line[17:21].strip() for a in _pdb(report["prepared_files"]["protein.pdb"])[0]}, {base})
            self.assertEqual(read_value(report["prepared_files"]["input-manifest.json"])["input_normalization"]["protein_state_intent"], plan)

    def test_existing_generic_reader_never_renames_remote_or_trusted_coordinates(self):
        self.rename("HIE")
        self.assertEqual({a.line[17:21].strip() for a in _pdb(self.protein)[0]}, {"HIE"})
        self.assertEqual({line[17:21].strip() for line in normalize_lines(self.protein) if line.startswith("ATOM")}, {"HIE"})

    def test_contradictory_explicit_histidine_hydrogen_is_not_silently_dropped(self):
        self.rename("HIE")
        self.fixture.lines.append(fixtures.atom_line(900, "HD1", "HIE", element="H", xyz=(3., 0., 0.)))
        self.fixture.write()
        before = self.protein.read_bytes()
        code, report, _, _ = self.fixture.check()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["message_key"], "state.hydrogen_conflict")
        self.assertEqual(self.protein.read_bytes(), before)

    def test_lyn_hydrogen_labels_are_mapped_without_deletion_or_coordinate_change(self):
        self.rename("LYN")
        self.fixture.lines.extend([fixtures.atom_line(900, "HZ2", "LYN", element="H", xyz=(3., -1., 0.)),
                                   fixtures.atom_line(901, "HZ3", "LYN", element="H", xyz=(4., -1., 0.))])
        self.fixture.write()
        lines, plan = self.normalize()
        self.assertEqual(len([line for line in lines if line.startswith("ATOM")]), 7)
        self.assertEqual(plan["residues"][0]["hydrogen_renames"], [{"from": "HZ3", "to": "HZ1"}])
        hydrogen = [line for line in lines if line[12:16].strip() == "HZ1"][0]
        self.assertEqual(hydrogen[30:54], self.fixture.lines[-1][30:54])

    def disulfide(self, *, second="CYX", third=False):
        self.rename("CYX")
        self.fixture.lines.append(fixtures.atom_line(10, "SG", "CYX", element="S", xyz=(0., 2., 0.)))
        self.fixture.lines.extend(["TER", fixtures.atom_line(20, "CA", second, chain="B", xyz=(4., 0., 0.)),
                                   fixtures.atom_line(21, "SG", second, chain="B", element="S", xyz=(2.03, 2., 0.))])
        if third:
            self.fixture.lines.extend(["TER", fixtures.atom_line(30, "SG", "CYS", chain="C", element="S", xyz=(-2.03, 2., 0.))])
        self.fixture.write()

    def test_unique_cyx_pair_is_recorded_with_both_endpoints_and_no_ph_inference(self):
        for partner in ("CYX", "CYS"):
            self.fixture.lines = self.fixture.lines[:5]
            self.disulfide(second=partner)
            lines, plan = self.normalize()
            self.assertEqual(len(plan["disulfides"]), 1)
            pair = plan["disulfides"][0]
            self.assertAlmostEqual(pair["distance_A"], 2.03)
            self.assertEqual({pair["left"]["chain"], pair["right"]["chain"]}, {"A", "B"})
            self.assertFalse(plan["pH_selected"])
            self.assertEqual({line[17:21].strip() for line in lines if line.startswith("ATOM")}, {"CYS"})

    def test_missing_or_multiple_cyx_partners_are_rejected(self):
        self.rename("CYX")
        with self.assertRaises(ToolError) as error:
            self.normalize()
        self.assertEqual(error.exception.key, "state.disulfide_ambiguous")
        self.disulfide(third=True)
        with self.assertRaises(ToolError) as error:
            self.normalize()
        self.assertEqual(error.exception.key, "state.disulfide_ambiguous")

    def test_native_cys_pairs_are_not_invented_as_disulfides_by_distance(self):
        self.disulfide(second="CYS")
        self.fixture.lines = [line.replace("CYX ", "CYS ") for line in self.fixture.lines]
        self.fixture.write()
        report = {}
        normalize_lines(self.protein, report, options={}, role="protein")
        self.assertNotIn("protein_state_intent", report)

    def test_local_build_dryrun_preserves_original_state_and_validates_request_before_upload(self):
        self.rename("ASH")
        out = self.root / "dry-run"
        code, output, error = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
                                               "--out", str(out), "--dry-run", "--no-preview", "--json"])
        self.assertEqual(code, 0, error)
        directory = Path(json.loads(output)["directory"])
        manifest = read_value(directory / "full-run.json")
        plan = intent_for(manifest["inputs"]["manifest"])
        self.assertEqual(plan["residues"][0]["state"], "ASPP")
        full_build.require_protein_state_support(directory, manifest)
        self.assertEqual(manifest["state"], "inputs_ready")
        self.assertFalse((directory / "preparation").exists())
        self.assertFalse(plan["remote_verified"])

    def test_sealed_identity_distinguishes_same_base_coordinates_with_different_state_intent(self):
        paths = []
        for source in ("ASP", "ASH"):
            self.rename(source)
            out = self.root / source
            code, output, errors = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
                "--out", str(out), "--dry-run", "--no-preview", "--hydrogens", "preserve", "--json"])
            self.assertEqual(code, 0, errors)
            paths.append(Path(json.loads(output)["directory"]))
        old, charged = [sweep.preparation_identity(path) for path in paths]
        self.assertEqual(old["input_sha256"], charged["input_sha256"])
        self.assertNotIn("protein_state_intent_sha256", old)
        self.assertIn("protein_state_intent_sha256", charged)
        self.assertNotEqual(old, charged)

    def test_unrecognized_or_tampered_state_recipes_are_rejected(self):
        self.rename("ASH")
        _, plan = self.normalize()
        self.assertEqual(validate_intent(plan), plan)
        bad = copy.deepcopy(plan)
        bad["residues"][0]["state"] = "GUESS"
        for value in ([], "bad", bad, {**plan, "remote_verified": True}):
            with self.assertRaises(ToolError) as error:
                validate_intent(value)
            self.assertEqual(error.exception.key, "state.intent_invalid")

    def test_unverified_state_cannot_be_accepted_by_standalone_validation(self):
        from charmm_gui_cli.chem.validation import validate_system, validate_charmm_source
        self.rename("ASH")
        _, plan = self.normalize()
        manifest = {"input_normalization": {"protein_state_intent": plan}, "ligand": {"resname": "LIG"}}
        report = validate_system(self.root, manifest)
        self.assertFalse(report["passed"])
        self.assertTrue(report["errors"])
        with self.assertRaises(ToolError) as error:
            validate_charmm_source("unused", "unused", "unused", "unused", manifest)
        self.assertEqual(error.exception.key, "state.proof_invalid")

    def test_existing_unknown_submission_keeps_priority_over_the_temporary_state_gate(self):
        from charmm_gui_cli.remote.api import SubmissionUnknown
        from charmm_gui_cli.store.auth import atomic_json
        self.rename("ASH")
        out = self.root / "unknown"
        code, output, errors = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
            "--out", str(out), "--dry-run", "--no-preview", "--json"])
        self.assertEqual(code, 0, errors)
        directory = Path(json.loads(output)["directory"])
        preparation = directory / "preparation"
        preparation.mkdir()
        (preparation / "upload-response.html").write_text("saved upload")
        (preparation / "selection.html").write_text("saved selection")
        atomic_json(preparation / "parameterization.intent.json", {"state": "submission_unknown"})
        web, api = Mock(), Mock()
        with self.assertRaises(SubmissionUnknown):
            full_build.advance(directory, web, api)
        web.upload_structure.assert_not_called()
        web.submit_snapshot.assert_not_called()
        api.submit.assert_not_called()

    def test_state_recipe_cannot_receive_a_remote_preparation_certificate_without_psf_proof(self):
        self.rename("ASH")
        out = self.root / "uncertified"
        code, output, errors = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
            "--out", str(out), "--dry-run", "--no-preview", "--json"])
        self.assertEqual(code, 0, errors)
        directory = Path(json.loads(output)["directory"])
        with self.assertRaises(ToolError) as error:
            sweep.certify_source(directory)
        self.assertIn("seal", str(error.exception).lower())

    def test_background_preflight_still_requires_authentication_for_explicit_states(self):
        self.rename("ASH")
        out = self.root / "background"
        with patch("charmm_gui_cli.pipeline.background.subprocess.Popen") as spawn:
            code, output, errors = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
                "--out", str(out), "--no-preview", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("login", errors)
        spawn.assert_not_called()
        self.assertFalse((out / ".charmm-gui/worker-options.json").exists())

    def test_native_state_dryrun_summary_does_not_claim_remote_application(self):
        self.rename("HSD")
        code, output, errors = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
            "--out", str(self.root / "native-hsd"), "--dry-run", "--no-preview"], tty=True)
        self.assertEqual(code, 0, errors)
        self.assertIn("Protein states: explicit intent count 1", output)
        self.assertIn("will verify the actual source PDB/PSF", output)
        self.assertNotIn("State: Passed", output)

    def test_explicit_model_and_altloc_selection_precede_state_interpretation(self):
        bad = [line[:17] + "CYX " + line[21:] for line in self.fixture.lines]
        good = [line[:17] + "HIE " + line[21:] for line in self.fixture.lines]
        self.protein.write_text("MODEL        1\n" + "\n".join(bad) + "\nENDMDL\nMODEL        2\n" + "\n".join(good) + "\nENDMDL\n")
        code, report, _, _ = self.fixture.check("--model", "2")
        self.assertEqual(code, 0, report.get("error"))
        plan = report["input_normalization"]["protein_state_intent"]
        self.assertEqual([item["state"] for item in plan["residues"]], ["HSE"])
        self.assertEqual(plan["disulfides"], [])

    def test_ssbond_conflicts_with_unique_cyx_geometry_are_refused(self):
        self.disulfide(second="CYS")
        self.fixture.lines.extend(["TER", fixtures.atom_line(30, "SG", "CYS", chain="C", element="S", xyz=(8., 2., 0.))])
        record = list(" " * 80)
        for start, value in ((0, "SSBOND"), (7, "  1"), (11, "CYS"), (15, "A"), (17, "   1"),
                             (25, "CYS"), (29, "C"), (31, "   1")):
            record[start:start + len(value)] = value
        self.fixture.lines.append("".join(record))
        self.fixture.write()
        with self.assertRaises(ToolError) as error:
            self.normalize()
        self.assertEqual(error.exception.key, "state.ssbond_conflict")

    def test_batch_and_sweep_preserve_patch_intent_after_normalized_staging(self):
        self.rename("ASH")
        inputs = {"protein": str(self.protein), "ligand": str(self.ligand), "hydrogens": "preserve"}
        for command, data in (("batch", {"version": 1, "defaults": inputs, "jobs": [{"name": "a"}, {"name": "b"}]}),
                              ("sweep", {"version": 1, "inputs": inputs, "axes": {"salt_concentration": [.1, .2]}})):
            config, out = self.root / (command + ".yaml"), self.root / command
            config.write_text(yaml.safe_dump(data))
            code, output, errors = self.fixture.call([command, str(config), "--out", str(out), "--dry-run", "--json"])
            self.assertEqual(code, 0, errors + output)
            children = out / "batch" if command == "sweep" else out
            paths = list(children.glob("jobs/*/inputs/input-manifest.json"))
            self.assertEqual(len(paths), 2)
            for path in paths:
                plan = intent_for(read_value(path))
                self.assertEqual((plan["residues"][0]["source_resname"], plan["residues"][0]["state"]), ("ASH", "ASPP"))

    def test_component_check_and_build_keep_original_patch_intent(self):
        self.rename("ASH")
        self.fixture.lines.extend(["TER", fixtures.atom_line(800, "C1", "LIG", chain="L", record="HETATM", xyz=(0., 3.2, 0.)),
                                   fixtures.atom_line(801, "O1", "LIG", chain="L", record="HETATM", element="O", xyz=(1.2, 3.2, 0.))])
        self.fixture.write()
        config = self.root / "components.yaml"
        config.write_text(yaml.safe_dump({"version": 1, "water_policy": "reject", "ion_policy": "reject",
            "components": [{"resname": "LIG", "chain": "L", "resid": 1, "sdf": str(self.ligand)}]}))
        for command in ("check", "build"):
            out = self.root / (command + "-components")
            options = ["--no-preview"] if command == "check" else ["--dry-run"]
            code, output, errors = self.fixture.call([command, "--complex", str(self.protein), "--components", str(config),
                                                    "--out", str(out), *options, "--json"])
            self.assertEqual(code, 0, errors + output)
            manifest = read_value(out / "check.json") if command == "check" else read_value(out / "inputs/input-manifest.json")
            entry = intent_for(manifest)["residues"][0]
            self.assertEqual((entry["source_resname"], entry["pdb_resname"], entry["state"]), ("ASH", "ASP", "ASPP"))

    def test_large_mapping_lists_are_compact_in_tty_but_complete_in_json(self):
        self.fixture.lines = [fixtures.atom_line(index, "CA", "HID", resid=index, xyz=(index * .2, 0., 0.)) for index in range(1, 101)]
        self.fixture.write()
        code, report, output, _ = self.fixture.check(tty=True)
        self.assertEqual(code, 0, report.get("error"))
        self.assertEqual(len(report["input_normalization"]["protein_state_intent"]["residues"]), 100)
        self.assertIn("HID→HSD/HSD=100", output)
        self.assertLess(len(output.splitlines()), 25)

    def test_sulfur_and_hydrogen_element_mismatches_are_not_used_for_state_mapping(self):
        self.disulfide(second="CYS")
        self.fixture.lines[-1] = self.fixture.lines[-1][:76] + "Se"
        self.fixture.write()
        with self.assertRaises(ToolError) as error:
            self.normalize()
        self.assertEqual(error.exception.key, "state.disulfide_ambiguous")
        self.fixture.lines = self.fixture.lines[:5]
        self.rename("LYN")
        self.fixture.lines.append(fixtures.atom_line(900, "HZ3", "LYN", element="C", xyz=(3., 0., 0.)))
        self.fixture.write()
        with self.assertRaises(ToolError) as error:
            self.normalize()
        self.assertEqual(error.exception.key, "state.hydrogen_conflict")

    def test_patch_form_mapping_uses_json_catalogue_without_javascript_execution(self):
        self.rename("ASH")
        lines, plan = self.normalize()
        prepared = self.root / "prepared.pdb"
        prepared.write_text("\n".join(lines) + "\n")
        snapshot = self.form({"PROA": {"1": "ASP"}})
        fields, requests = state_fields(snapshot, prepared, plan)
        self.assertEqual(fields["prot_checked"], "1")
        self.assertEqual(fields["protonation[patch][]"], ["ASPP"])
        self.assertEqual(fields["protonation[chain][]"], ["PROA"])
        self.assertEqual(requests["residues"][0]["server"]["resid"], "1")
        self.assertFalse(requests.get("remote_verified", False))

    def test_missing_controls_unsupported_patch_or_ambiguous_chain_are_not_guessed(self):
        self.rename("ASH")
        lines, plan = self.normalize()
        prepared = self.root / "prepared.pdb"
        prepared.write_text("\n".join(lines) + "\n")
        cases = [({"PROA": {"1": "ASP"}}, {"omit": ["prot_checked"]}),
                 ({"PROA": {"1": "ASP"}}, {"patches": {"ASP": []}}),
                 ({"PROA": {"1": "ASP"}, "A": {"1": "ASP"}}, {}),
                 ({"PROA": {"2": "ASP"}}, {})]
        for chain_map, options in cases:
            snapshot = self.form(chain_map, **options)
            with self.assertRaises(ToolError) as error:
                state_fields(snapshot, prepared, plan)
            self.assertEqual(error.exception.key, "web.form_missing")

    def test_checked_dynamic_defaults_cannot_be_silently_discarded(self):
        self.rename("ASH")
        lines, plan = self.normalize()
        prepared = self.root / "prepared.pdb"
        prepared.write_text("\n".join(lines) + "\n")
        snapshot = self.form({"PROA": {"1": "ASP"}})
        snapshot.write_text(snapshot.read_text().replace('name="prot_checked"', 'checked name="prot_checked"'))
        with self.assertRaises(ToolError) as error:
            state_fields(snapshot, prepared, plan)
        self.assertEqual(error.exception.key, "web.form_missing")
        self.assertIn("checked protonation/disulfide", str(error.exception))

    def test_disulfide_form_rows_keep_both_exact_chain_and_residue_ids(self):
        self.disulfide(second="CYS")
        lines, plan = self.normalize()
        prepared = self.root / "prepared.pdb"
        prepared.write_text("\n".join(lines) + "\n")
        snapshot = self.form({"PROA": {"1": "CYS"}, "PROB": {"1": "CYS"}})
        fields, receipt = state_fields(snapshot, prepared, plan)
        self.assertEqual(fields["ssbonds_checked"], "1")
        self.assertEqual(fields["ssbonds[segid1][]"], ["PROA"])
        self.assertEqual(fields["ssbonds[segid2][]"], ["PROB"])
        self.assertEqual(fields["ssbonds[resid1][]"], ["1"])
        self.assertEqual(len(receipt["disulfides"]), 1)


if __name__ == "__main__":
    unittest.main()
