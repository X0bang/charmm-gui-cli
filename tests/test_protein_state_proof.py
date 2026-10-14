"""Actual synthetic PDB/PSF proof, mocked HTTP transport and durable resume."""

import copy
import json
from pathlib import Path
import shutil
import tarfile
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli.chem.protein_state_proof import verify_states, verify_archive_states
from charmm_gui_cli.chem.protein_states import intent_for
from charmm_gui_cli.pipeline import full_build, sweep, managed
from charmm_gui_cli.pipeline.protein_states import check_prepared, ensure_prepared, RECEIPT
from charmm_gui_cli.remote.web import WebClient
from charmm_gui_cli.remote.protein_states import state_fields
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.paths import read_value
import test_input_policy as fixtures
from test_full_build import UPLOAD, SELECTION, rendered
import test_protein_state_intent as state_fixtures

# Independent synthetic state sites, not derived from the implementation table.
SITES = {
    "ASPP": [("OD1", "OB", -.55, 15.9994), ("OD2", "OH1", -.61, 15.9994), ("HD2", "H", .44, 1.008)],
    "HSD": [("ND1", "NR1", -.36, 14.007), ("NE2", "NR2", -.70, 14.007), ("HD1", "H", .32, 1.008)],
    "HSE": [("ND1", "NR2", -.70, 14.007), ("NE2", "NR1", -.36, 14.007), ("HE2", "H", .32, 1.008)],
    "HSP": [("ND1", "NR3", -.51, 14.007), ("NE2", "NR3", -.51, 14.007), ("HD1", "H", .44, 1.008), ("HE2", "H", .44, 1.008)],
    "GLUP": [("OE1", "OB", -.55, 15.9994), ("OE2", "OH1", -.61, 15.9994), ("HE2", "H", .44, 1.008)],
    "LSN": [("NZ", "NH2", -.96, 14.007), ("HZ1", "HC", .34, 1.008), ("HZ2", "HC", .34, 1.008)],
    "CYM": [("SG", "SS", -.80, 32.06)], "DISU": [("SG", "SM", -.08, 32.06)],
}


def server_files(directory, *, state="ASPP", base="ASP", name="step1_pdbreader", mutate=None):
    rows = [("N", "NH3", -.30, 14.007), ("CA", "CT1", .21, 12.011), ("C", "C", .51, 12.011),
            ("O", "O", -.51, 15.9994), ("CB", "CT2", -.21, 12.011), *SITES[state]]
    if mutate:
        rows = mutate(rows)
    groups = [("PROA", base, rows)]
    if state == "DISU":
        groups.append(("PROB", "CYS", [("CA", "CT1", .07, 12.011), ("SG", "SM", -.08, 32.06)]))
    atoms, pdb, edges, lookup = [], [], [], {}
    for segment, resname, rows in groups:
        for atom_name, atom_type, charge, mass in rows:
            serial = len(atoms) + 1
            lookup[segment, atom_name] = serial
            atoms.append(f"{serial} {segment} 1 {resname} {atom_name} {atom_type} {charge:.6f} {mass:.6f} 0")
            element = "H" if mass < 2.5 else "C" if mass < 13 else "N" if mass < 15 else "O" if mass < 17 else "S"
            line = fixtures.atom_line(serial, atom_name, resname, chain=segment[-1], element=element, xyz=(float(serial), 0., 0.))
            pdb.append(line.ljust(80)[:72] + segment + line.ljust(80)[76:])
    pair_names = {"ASPP": [("OD2", "HD2")], "GLUP": [("OE2", "HE2")], "LSN": [("NZ", "HZ1"), ("NZ", "HZ2")],
                  "HSD": [("ND1", "HD1")], "HSE": [("NE2", "HE2")], "HSP": [("ND1", "HD1"), ("NE2", "HE2")]}
    for left, right in pair_names.get(state, []):
        if ("PROA", left) in lookup and ("PROA", right) in lookup:
            edges.append((lookup["PROA", left], lookup["PROA", right]))
    if state == "DISU":
        edges.append((lookup["PROA", "SG"], lookup["PROB", "SG"]))
    directory.mkdir(parents=True, exist_ok=True)
    pdb_path, psf_path = directory / (name + ".pdb"), directory / (name + ".psf")
    pdb_path.write_text("\n".join(pdb) + "\nEND\n")
    psf_path.write_text("PSF EXT\n\n" + str(len(atoms)) + " !NATOM\n" + "\n".join(atoms) + "\n\n" +
                        str(len(edges)) + " !NBOND\n" + " ".join(str(value) for edge in edges for value in edge) + "\n\n0 !NTHETA\n")
    return pdb_path, psf_path


def checked_rows(snapshot, prefix, rows):
    switch, columns = (("prot_checked", ("chain", "residue", "rid", "patch")) if prefix == "protonation" else
                       ("ssbonds_checked", ("segid1", "resid1", "segid2", "resid2")))
    text = snapshot.read_text().replace('name="' + switch + '"', 'checked name="' + switch + '"')
    for index, column in enumerate(columns):
        name = prefix + '[' + column + '][]'
        text = text.replace('<select name="' + name + '"></select>', ''.join(
            '<select name="' + name + '"><option selected value="' + row[index] + '">' + row[index] + '</option></select>'
            for row in rows))
    snapshot.write_text(text)


class ProteinStateProofTests(unittest.TestCase):
    def setUp(self):
        self.f = state_fixtures.ProteinStateIntentTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.root
        self.f.rename("ASH")
        out = self.root / "build"
        code, output, errors = self.f.fixture.call(["build", "--protein", str(self.f.protein), "--ligand", str(self.f.ligand),
            "--out", str(out), "--dry-run", "--no-preview", "--json"])
        self.assertEqual(code, 0, errors)
        self.directory = Path(json.loads(output)["directory"])
        self.manifest = read_value(self.directory / "full-run.json")
        self.plan = intent_for(self.manifest["inputs"]["manifest"])
        self.reference = self.manifest["inputs"]["files"]["complex.pdb"]
        self.mapping = {"residues": [{"source": {key: self.plan["residues"][0][key] for key in
            ("polymer_block", "chain", "segment", "resid", "icode")}, "server": {"segment": "PROA", "resid": "1", "resname": "ASP"},
            "state": "ASPP", "method": "patch"}], "disulfides": []}

    def test_actual_atoms_types_charges_and_hydrogen_bond_pass_not_total_residue_charge(self):
        pdb, psf = server_files(self.root / "source")
        proof = verify_states(self.plan, self.mapping, pdb, psf, reference=self.reference)
        self.assertTrue(proof["passed"])
        self.assertEqual(proof["residues"][0]["state"], "ASPP")
        self.assertFalse(proof["scientific_correctness_verified"])

    def test_right_residue_name_or_total_charge_cannot_hide_wrong_state(self):
        mutations = [lambda rows: [row for row in rows if row[0] != "HD2"],
            lambda rows: [(n, "OC" if n == "OD2" else t, q, m) for n, t, q, m in rows],
            lambda rows: [(n, t, q + (.1 if n == "OD1" else -.1 if n == "OD2" else 0), m) for n, t, q, m in rows]]
        for index, mutate in enumerate(mutations):
            pdb, psf = server_files(self.root / str(index), mutate=mutate)
            with self.assertRaises(ToolError) as error:
                verify_states(self.plan, self.mapping, pdb, psf, reference=self.reference)
            self.assertEqual(error.exception.key, "state.proof_invalid")

    def test_orphan_forbidden_hydrogen_wrong_mass_and_double_bonded_proton_are_rejected(self):
        for index, mutate in enumerate((lambda rows: [*rows, ("HD1", "H", .44, 1.008)],
                lambda rows: [(n, t, q, 12.011 if n == "OD2" else m) for n, t, q, m in rows])):
            pdb, psf = server_files(self.root / ('invalid-site-' + str(index)), mutate=mutate)
            with self.assertRaises(ToolError):
                verify_states(self.plan, self.mapping, pdb, psf)
        pdb, psf = server_files(self.root / 'extra-hydrogen-bond')
        psf.write_text(psf.read_text().replace('1 !NBOND', '2 !NBOND').replace('7 8\n', '7 8 6 8\n'))
        with self.assertRaises(ToolError):
            verify_states(self.plan, self.mapping, pdb, psf)

    def test_checked_unrelated_protonation_and_native_disulfide_defaults_are_preserved(self):
        snapshot = self.f.form({"PROA": {"1": "ASP"}, "PROB": {"2": "GLU", "3": "CYS", "4": "CYS"}})
        checked_rows(snapshot, 'protonation', [('PROB', 'GLU', '2', 'GLUP')])
        checked_rows(snapshot, 'ssbonds', [('PROB', '3', 'PROB', '4')])
        fields, mapping = state_fields(snapshot, self.reference, self.plan)
        self.assertEqual(fields['protonation[patch][]'], ['GLUP', 'ASPP'])
        self.assertEqual(fields['protonation[rid][]'], ['2', '1'])
        self.assertEqual(fields['ssbonds[resid1][]'], ['3'])
        self.assertEqual(fields['ssbonds[resid2][]'], ['4'])
        self.assertEqual(mapping, self.mapping)

    def test_invalid_default_patch_chain_or_disulfide_conflict_refuses_before_post(self):
        for rows in ([('PROB', 'GLU', '2', 'ASPP')], [('PROX', 'GLU', '2', 'GLUP')]):
            snapshot = self.f.form({"PROA": {"1": "ASP"}, "PROB": {"2": "GLU"}})
            checked_rows(snapshot, 'protonation', rows)
            with self.assertRaises(ToolError):
                state_fields(snapshot, self.reference, self.plan)
        snapshot = self.f.form({"PROA": {"1": "ASP"}, "PROB": {"2": "CYS", "3": "CYS", "4": "CYS"}})
        checked_rows(snapshot, 'ssbonds', [('PROB', '2', 'PROB', '3'), ('PROB', '2', 'PROB', '4')])
        with self.assertRaises(ToolError):
            state_fields(snapshot, self.reference, self.plan)

    def test_cym_observed_as_cys_requires_the_actual_supported_patch(self):
        self.f.rename('CYM')
        lines, plan = self.f.normalize()
        reference = self.root / 'cym.pdb'
        reference.write_text('\n'.join(lines) + '\n')
        snapshot = self.f.form({'PROA': {'1': 'CYS'}})
        fields, _ = state_fields(snapshot, reference, plan)
        self.assertEqual(fields['protonation[patch][]'], ['CYM'])
        snapshot = self.f.form({'PROA': {'1': 'CYS'}}, patches={'CYS': []})
        with self.assertRaises(ToolError):
            state_fields(snapshot, reference, plan)

    def test_changed_catalogue_shapes_and_conflicting_form_job_ids_fail_without_traceback(self):
        for chain_map in ({'PROA': {'1': ['ASP']}}, {'PROA': {'1': 'ASP'}}):
            snapshot = self.f.form(chain_map)
            if isinstance(chain_map['PROA']['1'], str):
                text = snapshot.read_text()
                start = text.index('var charmm_patch=')
                end = text.index(';', start)
                snapshot.write_text(text[:start] + 'var charmm_patch={"prot":[]};' + text[end + 1:])
            with self.assertRaises(ToolError):
                state_fields(snapshot, self.reference, self.plan)
        snapshot = self.f.form({'PROA': {'1': 'ASP'}})
        snapshot.write_text(snapshot.read_text().replace('</form>', '<input name="jobid" value="999"></form>'))
        with self.assertRaises(ToolError):
            state_fields(snapshot, self.reference, self.plan)

    def test_wrong_proton_attachment_and_changed_pdb_psf_identity_fail(self):
        pdb, psf = server_files(self.root / "wrong-bond")
        psf.write_text(psf.read_text().replace("7 8\n", "6 8\n"))
        with self.assertRaises(ToolError):
            verify_states(self.plan, self.mapping, pdb, psf)
        pdb, psf = server_files(self.root / "wrong-name")
        pdb.write_text(pdb.read_text().replace("HD2", "HD1"))
        with self.assertRaises(ToolError):
            verify_states(self.plan, self.mapping, pdb, psf)

    def test_all_named_state_signatures_and_disulfide_edge_are_required(self):
        for original, state, base in (("HID", "HSD", "HSD"), ("HIE", "HSE", "HSE"), ("HIP", "HSP", "HSP"),
                                      ("GLH", "GLUP", "GLU"), ("LYN", "LSN", "LYS"), ("CYM", "CYM", "CYM")):
            self.f.fixture.lines = self.f.fixture.lines[:5]
            self.f.rename(original)
            _, plan = self.f.normalize()
            mapping = copy.deepcopy(self.mapping)
            mapping["residues"][0].update(state=state, method=plan["residues"][0]["method"])
            mapping["residues"][0]["server"]["resname"] = base
            pdb, psf = server_files(self.root / state, state=state, base=base)
            self.assertTrue(verify_states(plan, mapping, pdb, psf)["passed"])
        self.f.fixture.lines = self.f.fixture.lines[:5]
        self.f.disulfide(second="CYS")
        _, plan = self.f.normalize()
        left, right = plan["disulfides"][0]["left"], plan["disulfides"][0]["right"]
        a, b = {"segment": "PROA", "resid": "1", "resname": "CYS"}, {"segment": "PROB", "resid": "1", "resname": "CYS"}
        mapping = {"residues": [{"source": left, "server": a, "state": "DISU", "method": "disulfide"}],
                   "disulfides": [{"source_left": left, "source_right": right, "left": a, "right": b}]}
        pdb, psf = server_files(self.root / "disulfide", state="DISU", base="CYS")
        self.assertTrue(verify_states(plan, mapping, pdb, psf)["passed"])
        psf.write_text(psf.read_text().replace("1 !NBOND", "0 !NBOND").replace("6 8\n", "\n"))
        with self.assertRaises(ToolError):
            verify_states(plan, mapping, pdb, psf)

    def test_archive_must_prove_both_source_and_assembly_states(self):
        root = self.root / "model/charmm-gui-456"
        server_files(root)
        _, final_psf = server_files(root, name="step5_assembly")
        manifest = self.manifest["inputs"]["manifest"]
        self.assertTrue(verify_archive_states(root.parent, manifest, reference=self.reference, mapping=self.mapping)["passed"])
        final_psf.write_text(final_psf.read_text().replace("OD2 OH1", "OD2 OC"))
        with self.assertRaises(ToolError):
            verify_archive_states(root.parent, manifest, reference=self.reference, mapping=self.mapping)

    def setup_web(self, *, bad_psf=False, interrupt_get=False):
        source_pdb, source_psf = server_files(self.root / "remote")
        if bad_psf:
            source_psf.write_text(source_psf.read_text().replace("OD2 OH1", "OD2 OC"))
        state_form = self.f.form({"PROA": {"1": "ASP"}}).read_text()
        selection = SELECTION.replace('</form>', state_form.split('<form', 1)[1].split('>', 1)[1])
        selection = selection.replace('</form>', '') + '</form>'
        selection = state_form.split('<form', 1)[0] + selection
        web = WebClient(session=Mock(headers={}))
        web.validate_auth = Mock()
        def upload(pdb, directory):
            directory.mkdir()
            (directory / "upload-response.html").write_text(UPLOAD)
            atomic_json(directory / "web-run.json", {"state": "upload_response_received", "job_id": "123", "input_pdb": str(pdb)})
        web.upload_structure = Mock(side_effect=upload)
        def post(target, intent_path, intent, **kwargs):
            result = requests.Response()
            result.status_code = 200
            result.url = "https://www.charmm-gui.org/"
            result._content = (selection if intent_path.name == "selection.intent.json" else rendered()).encode()
            return result
        web._modeling_post = Mock(side_effect=post)
        def get(method, target):
            self.assertEqual(method, "GET")
            if interrupt_get and target.endswith('.psf'):
                raise KeyboardInterrupt
            result = requests.Response()
            result.status_code = 200
            result.url = target
            result._content = (source_psf if target.endswith('.psf') else source_pdb).read_bytes()
            return result
        web.request = Mock(side_effect=get)
        return web

    def run_advance(self, web):
        def start(api, config, directory, **kwargs):
            self.assertTrue((self.directory / RECEIPT).is_file())
            directory.mkdir()
            atomic_json(directory / 'run.json', {"job_id": "456"})
            return {"job_id": "456", "state": "submitted"}
        with patch.object(full_build, "start_api", side_effect=start) as submit:
            result = full_build.advance(self.directory, web, Mock())
        return result, submit.call_count

    def test_full_mocked_http_advance_records_flat_fields_then_get_proof_before_api_clone(self):
        web = self.setup_web()
        result, submitted = self.run_advance(web)
        self.assertEqual(result["state"], "bilayer_submitted")
        self.assertEqual(submitted, 1)
        self.assertEqual(web._modeling_post.call_count, 2)
        self.assertEqual(web.request.call_count, 2)
        intent = read_value(self.directory / 'preparation/parameterization.intent.json')
        self.assertIn(["protonation[patch][]", "ASPP"], intent["fields"])
        self.assertTrue(all(not isinstance(value, list) for _, value in intent["fields"]))
        self.assertTrue(check_prepared(self.directory, result)["checks"]["passed"])
        certificate = sweep.certify_source(self.directory)
        self.assertIn(RECEIPT, certificate["evidence_sha256"])

    def test_reused_child_copies_verified_mapping_and_rejects_edited_source_psf(self):
        web = self.setup_web()
        self.run_advance(web)
        code, output, errors = self.f.fixture.call(['build', '--protein', str(self.f.protein), '--ligand', str(self.f.ligand),
            '--out', str(self.root / 'follower'), '--dry-run', '--no-preview', '--json'])
        self.assertEqual(code, 0, errors)
        child = Path(json.loads(output)['directory'])
        with patch('requests.Session.request', side_effect=AssertionError('No network for binding')):
            sweep.bind_source(child, self.directory)
            manifest = read_value(child / 'full-run.json')
            self.assertEqual(manifest['protein_state_mapping'], self.mapping)
            self.assertEqual(sweep.validate_reuse(child), '123')
            self.assertEqual(sweep.reuse_api_config(child)['source_job_id'], '123')
            self.assertFalse((child / 'preparation').exists())
            proof = read_value(self.directory / RECEIPT)
            (self.directory / proof['psf']).write_text('changed source PSF')
            with self.assertRaises(ToolError):
                sweep.validate_reuse(child)

    def downloaded(self, directory=None, *, bad_assembly=False):
        directory = directory or self.directory
        manifest = read_value(directory / 'full-run.json')
        model = self.root / 'archive-fixture/charmm-gui-456'
        server_files(model)
        _, final_psf = server_files(model, name='step5_assembly')
        if bad_assembly:
            final_psf.write_text(final_psf.read_text().replace('OD2 OH1', 'OD2 OC'))
        archive = directory.parent / 'charmm-gui.tgz'
        with tarfile.open(archive, 'w:gz') as handle:
            for path in model.iterdir():
                handle.add(path, arcname='charmm-gui-456/' + path.name)
        manifest.update(state='ligands_present', source_job_id='123', build_job_id='456')
        atomic_json(directory / 'full-run.json', manifest)
        (directory / 'bilayer').mkdir(exist_ok=True)
        atomic_json(directory / 'bilayer/run.json', {'job_id': '456', 'state': 'ligands_present',
            'config': {'source_job_id': '123'}, 'archive_sha256': managed._digest(archive)})

    def finalize(self, directory):
        # Only unrelated ligand/pose/environment checks are mocked. Extraction,
        # archive ownership, state proof and validation caching run unchanged.
        with patch.object(managed, 'validate_system', return_value={'passed': True, 'files': {'coordinates': 'synthetic.gro'}}), \
                patch.object(managed, 'validate_bound_pose', return_value={'passed': True}), \
                patch.object(managed, 'assess_acceptance', return_value={'passed': True}), \
                patch.object(managed, 'inspect_environment', return_value={'passed': True}):
            return managed.finalize_managed_build(directory)

    def test_actual_assembly_proof_is_required_before_managed_acceptance(self):
        self.run_advance(self.setup_web())
        self.downloaded(bad_assembly=True)
        with self.assertRaises(ToolError):
            self.finalize(self.directory)
        self.assertFalse((self.directory / 'results/validation.json').exists())

    def test_completed_reused_child_remains_verifiable_after_source_unavailable_and_move(self):
        self.run_advance(self.setup_web())
        code, output, errors = self.f.fixture.call(['build', '--protein', str(self.f.protein), '--ligand', str(self.f.ligand),
            '--out', str(self.root / 'completed-follower'), '--dry-run', '--no-preview', '--json'])
        self.assertEqual(code, 0, errors)
        child = Path(json.loads(output)['directory'])
        sweep.bind_source(child, self.directory)
        self.downloaded(child)
        report = self.finalize(child)
        self.assertTrue(report['passed'])
        self.assertTrue(report['protein_state_validation']['assembly']['passed'])
        self.assertEqual(report['evidence']['protein_state_revision'], 1)
        hidden_source = self.root / 'unmounted-source'
        shutil.move(str(self.directory.parent), hidden_source)
        moved = self.root / 'moved-completed-follower'
        shutil.move(str(child.parent), moved)
        child = moved / '.charmm-gui'
        with patch.object(managed, 'validate_system', side_effect=AssertionError('Should reuse verified cache')):
            self.assertTrue(managed.finalize_managed_build(child)['passed'])
        psf = moved / 'model/charmm-gui-456/step5_assembly.psf'
        psf.write_text(psf.read_text().replace('OD2 OH1', 'OD2 OC'))
        with self.assertRaises(ToolError):
            self.finalize(child)

    def test_state_specific_cache_requires_the_state_proof_field(self):
        evidence = {'grompp': False, 'protein_state_revision': 1}
        report = {'evidence': evidence, 'fingerprint': managed._canonical_hash(evidence), 'validation_complete': True}
        self.assertFalse(managed._cache_reusable(report, evidence))
        report['protein_state_validation'] = {'passed': True}
        self.assertTrue(managed._cache_reusable(report, evidence))

    def test_terminal_and_report_show_verified_states_only_after_actual_assembly_proof(self):
        from charmm_gui_cli.locales.i18n import get_language, set_language
        from charmm_gui_cli.presentation.terminal_output import format_build
        from charmm_gui_cli.presentation.reporting import render_text
        self.run_advance(self.setup_web())
        self.downloaded()
        report = self.finalize(self.directory)
        summary = managed._final_summary(self.directory)
        self.assertTrue(summary['protein_state_validation']['passed'])
        language = get_language()
        self.addCleanup(set_language, language)
        for lang, expected in (('zh', '1 项显式状态已通过'), ('en', 'verification count: 1;')):
            set_language(lang)
            self.assertIn(expected, format_build(summary))
            self.assertIn(expected, render_text({'passed': True, 'protein_state_validation': report['protein_state_validation']}))

    def test_managed_acceptance_cannot_invent_a_missing_submission_mapping(self):
        self.run_advance(self.setup_web())
        self.downloaded()
        manifest = read_value(self.directory / 'full-run.json')
        manifest.pop('protein_state_mapping')
        atomic_json(self.directory / 'full-run.json', manifest)
        with self.assertRaises(ToolError):
            self.finalize(self.directory)

    def test_wrong_remote_state_never_submits_the_membrane_job(self):
        web = self.setup_web(bad_psf=True)
        with patch.object(full_build, "start_api") as submit, self.assertRaises(ToolError):
            full_build.advance(self.directory, web, Mock())
        submit.assert_not_called()
        self.assertFalse((self.directory / RECEIPT).exists())

    def test_changed_sealed_input_refuses_resume_without_repeating_any_post(self):
        web = self.setup_web(interrupt_get=True)
        with self.assertRaises(KeyboardInterrupt):
            self.run_advance(web)
        source = Path(self.reference)
        source.write_text(source.read_text() + 'REMARK changed after upload\n')
        with patch.object(full_build, 'start_api') as submit, self.assertRaises(ToolError):
            full_build.advance(self.directory, web, Mock())
        submit.assert_not_called()
        self.assertEqual(web._modeling_post.call_count, 2)

    def test_same_job_artifact_url_and_saved_proof_are_required(self):
        web = self.setup_web(interrupt_get=True)
        with self.assertRaises(KeyboardInterrupt):
            self.run_advance(web)
        manifest = read_value(self.directory / 'full-run.json')
        manifest['preparation_artifacts'] = [url.replace('/123/', '/999/') for url in manifest['preparation_artifacts']]
        previous = web.request.call_count
        with self.assertRaises(ToolError):
            ensure_prepared(self.directory, manifest, web)
        for candidate in (None, 'https://[', 'http://www.charmm-gui.org/uploaded_pdb/123/step1_pdbreader.pdb',
                          'https://example.org/uploaded_pdb/123/step1_pdbreader.pdb'):
            manifest['preparation_artifacts'] = [candidate]
            with self.assertRaises(ToolError):
                ensure_prepared(self.directory, manifest, web)
        self.assertEqual(web.request.call_count, previous)
        self.assertFalse((self.directory / RECEIPT).exists())

    def test_interrupted_get_resume_never_repeats_a_modeling_post(self):
        web = self.setup_web(interrupt_get=True)
        with patch.object(full_build, "start_api") as submit, self.assertRaises(KeyboardInterrupt):
            full_build.advance(self.directory, web, Mock())
        submit.assert_not_called()
        self.assertEqual(web._modeling_post.call_count, 2)
        good = self.setup_web()
        web.request.side_effect = good.request.side_effect
        _, submitted = self.run_advance(web)
        self.assertEqual(submitted, 1)
        self.assertEqual(web._modeling_post.call_count, 2)

    def test_moved_source_and_edited_proof_are_checked_without_network(self):
        web = self.setup_web()
        result, _ = self.run_advance(web)
        root = self.directory.parent
        moved = root.with_name(root.name + '-moved')
        shutil.move(str(root), moved)
        directory = moved / '.charmm-gui'
        manifest = read_value(directory / 'full-run.json')
        self.assertTrue(check_prepared(directory, manifest)["checks"]["passed"])
        proof = read_value(directory / RECEIPT)
        (directory / proof['psf']).write_text('edited')
        with self.assertRaises(ToolError):
            check_prepared(directory, manifest)

    def test_repeated_array_fields_match_recorded_post_without_default_rows(self):
        snapshot = self.root / 'array.html'
        snapshot.write_text('<form action="?doc=input/pdbreader&step=3"><input name="jobid" value="123">'
                            '<input name="protonation[rid][]" value="old"></form>')
        web = WebClient(session=Mock(headers={}))
        web.validate_auth = Mock()
        response = requests.Response()
        response.status_code = 200
        response.url = 'https://www.charmm-gui.org/'
        response._content = b'<form></form>'
        web._modeling_post = Mock(return_value=response)
        out = self.root / 'arrays.html'
        web.submit_snapshot(snapshot, out, {'protonation[rid][]': ['1', '2']})
        actual = web._modeling_post.call_args.kwargs['data']
        self.assertEqual([value for key, value in actual if key == 'protonation[rid][]'], ['1', '2'])
        self.assertEqual(read_value(out.with_suffix('.intent.json'))['fields'], [list(pair) for pair in actual])


if __name__ == '__main__':
    unittest.main()
