"""Sealed actual uploads, successful POST evidence and GET-only cap proofs.

All IDs/coordinates are synthetic. Topology facts come from the independent
artifact fixture, not the verifier; no actual provider model is asserted.
"""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch

import yaml

from charmm_gui_cli.chem.inputs import Atom, _sdf, prepare_inputs
from charmm_gui_cli.chem.protein_state_proof import canonical
from charmm_gui_cli.chem.protein_states import intent_for
from charmm_gui_cli.locales.i18n import set_language, t
from charmm_gui_cli.pipeline import fragments, protein_states, full_build, sweep, managed
from charmm_gui_cli.pipeline.build_config import load_build_config
from charmm_gui_cli.remote.web import WebClient, merged_form_data, modeling_target, soup_for
from charmm_gui_cli.remote.web_state import parse_page_state
from charmm_gui_cli.remote.api import SubmissionUnknown
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.paths import read_value
from tests import test_fragment_proof as artifact_fixture
from tests.test_full_build import rendered


IDENTIFIER = "1234567890"


class FragmentProtocolTests(unittest.TestCase):
    def setUp(self):
        self.f = artifact_fixture.FragmentProofTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(set_language)
        self.root = self.f.root
        self.task = self.root / "task with spaces"
        self.directory = self.task / ".charmm-gui"
        self.directory.mkdir(parents=True)
        atomic_json(self.directory / "path-mode.json", {"version": 1})
        self.web = Mock()
        self.responses = []

    def prepare(self, *, disulfide=False, first="ALA", preparation=None):
        self.f.setup_input(first="CYS" if disulfide else first, linkage="ssbond" if disulfide else None)
        raw = self.f.source
        if disulfide:
            raw = [line[:17] + "CYX " + line[21:] if line.startswith("ATOM") and line[17:20] == "CYS" else line for line in raw]
            raw = [line[:30] + f"{2.05:8.3f}{-2.:8.3f}{0.:8.3f}" + line[54:]
                   if line.startswith("ATOM") and line[12:16].strip() == "SG" and int(line[22:26]) == 3 else line for line in raw]
        protein, ligand = self.root / "protein.pdb", self.root / "ligand.sdf"
        protein.write_text("\n".join(raw) + "\n")
        ligand.write_text(_sdf([Atom(1, "C1", "C", (0., 3.5, 0.)), Atom(2, "O1", "O", (1.2, 3.5, 0.))], {(0, 1): 2}, "LIG"))
        inputs = prepare_inputs(protein, ligand, self.task / "inputs", fragment_preparation=True)
        config_path = self.root / "build.yaml"
        config_path.write_text(yaml.safe_dump({"version": 2, "protein": str(protein), "ligand": str(ligand),
            "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20}, "preparation": preparation or {}}))
        self.manifest = {"schema_version": 2, "state": "inputs_ready", "inputs": inputs, "config": load_build_config(config_path)}
        atomic_json(self.directory / "full-run.json", self.manifest)
        self.record = fragments.record_for(self.manifest)
        return self.manifest

    def stage(self, *, gly_fallback=False):
        atomic_json(self.directory / "full-run.json", self.manifest)
        sweep.seal_preparation(self.directory)
        fragments.seal_request(self.directory, self.manifest, create=True)
        protein_states.seal_request(self.directory, self.manifest, create=True)
        prep = self.directory / "preparation"
        prep.mkdir()
        upload = prep / "upload-response.html"
        selected = prep / "selection.html"
        parameterized = prep / "parameterization.html"
        controls = '<input name="jobid" value="' + IDENTIFIER + '">'
        chains = {}
        for fragment in self.record["plan"]["fragments"]:
            name = "PRO" + fragment["prepared_chain"]
            chains[name] = {str(row["resid"]) + row["icode"]: row["resname"] for row in fragment["source_residues"]}
        upload.write_text('<form action="?doc=input/pdbreader&step=2">' + controls + ''.join(
            '<input type="checkbox" name="chains[' + name + '][checked]" value="1">' for name in [*chains, "HETL"]) + '</form>')
        controls += '<input type="file" name="sdf_cgenff[LIG]">'
        for name in chains:
            for end, values in (("first", ["NTER", "NNEU", "GLYP", "PROP", "ACE", "ACP", "NONE"]),
                                ("last", ["CTER", "CNEU", "CT1", "CT2", "CT3", "NONE"])):
                if gly_fallback and end == "first":
                    values = [value for value in values if value != "NTER"]
                controls += '<select name="terminal[' + name + '][' + end + ']">' + ''.join(
                    '<option value="' + value + '">' + value + '</option>' for value in values) + '</select>'
        if intent_for(self.manifest["inputs"]["manifest"]):
            controls += '<input type="checkbox" name="ssbonds_checked">'
            controls += ''.join('<select name="ssbonds[' + name + '][]"></select>' for name in ("segid1", "resid1", "segid2", "resid2"))
        selected.write_text('<script>var prot_chains=' + json.dumps(chains) + ';var charmm_patch={"prot":{}};</script>' +
            '<form action="?doc=input/pdbreader&step=3">' + controls + '</form>')
        atomic_json(prep / "web-run.json", {"state": "upload_response_received", "job_id": IDENTIFIER,
            "input_pdb": self.manifest["inputs"]["files"]["complex.pdb"]})
        self.intent(upload, selected, full_build._chains(upload))
        mapping, states = {}, {}
        fields = full_build._preparation_fields(selected, self.manifest["config"],
            adjustments=self.manifest.setdefault("terminal_adjustments", []),
            state_intent=intent_for(self.manifest["inputs"]["manifest"]), state_mapping=states,
            reference_pdb=self.manifest["inputs"]["files"]["complex.pdb"], **fragments.request_kwargs(self.manifest, mapping))
        self.manifest.update(source_job_id=IDENTIFIER, fragment_mapping=mapping)
        if states:
            self.manifest["protein_state_mapping"] = states
        self.intent(selected, parameterized, fields, ["sdf_cgenff[LIG]"])
        parameterized.write_text(rendered(identifier=IDENTIFIER))
        self.manifest.update(state="prepared", preparation_snapshot=str(parameterized),
            preparation_artifacts=parse_page_state(parameterized.read_text())["artifacts"])
        atomic_json(self.directory / "full-run.json", self.manifest)
        self.f.plan = self.record["plan"]
        self.f.receipt = mapping
        self.pdb, self.psf = self.f.model(cross=bool(intent_for(self.manifest["inputs"]["manifest"])))
        self.web.request.side_effect = self.response
        return fields

    def finish_proof(self):
        receipt = fragments.ensure_prepared(self.directory, self.manifest, self.web)
        self.manifest['fragment_source_proof'] = fragments.source_binding(receipt)
        if intent_for(self.manifest['inputs']['manifest']):
            proof = protein_states.ensure_prepared(self.directory, self.manifest, self.web)
            self.manifest['protein_state_proof_fingerprint'] = proof['fingerprint']
        atomic_json(self.directory / 'full-run.json', self.manifest)
        return receipt

    def clone_local(self, name):
        task = self.root / name
        directory = task / '.charmm-gui'
        directory.mkdir(parents=True)
        atomic_json(directory / 'path-mode.json', {'version': 1})
        shutil.copytree(self.task / 'inputs', task / 'inputs')
        inputs = deepcopy(self.manifest['inputs'])
        inputs['files'] = {name: str(task / 'inputs' / name) for name in inputs['files']}
        manifest = {'schema_version': 2, 'state': 'inputs_ready', 'inputs': inputs, 'config': deepcopy(self.manifest['config'])}
        atomic_json(directory / 'full-run.json', manifest)
        return directory, manifest

    def archive(self):
        model = self.root / 'archive-files'
        model.mkdir()
        (model / 'toppar').mkdir()
        for suffix, source in (('pdb', self.pdb), ('psf', self.psf)):
            for stem in ('step1_pdbreader', 'step5_assembly'):
                shutil.copyfile(source, model / (stem + '.' + suffix))
        shutil.copyfile(self.f.topology, model / 'toppar/top_all36_prot.rtf')
        return model

    def transport(self):
        """Run the real upload/intent writer over a mocked request boundary."""
        pages = {str(step): (self.directory / 'preparation' / name).read_text() for step, name in
                 ((1, 'upload-response.html'), (2, 'selection.html'), (3, 'parameterization.html'))}
        web = WebClient()
        def request(method, target, **kwargs):
            if method == 'GET' and not target:
                return Mock(url='https://www.charmm-gui.org/', text='<form name="pdb" action="?doc=input/pdbreader&step=1"></form>')
            if method == 'POST':
                from urllib.parse import parse_qs, urlparse
                step = parse_qs(urlparse(target).query)['step'][0]
                files = kwargs.get('files') or {}
                for name, (_, handle, _) in files.items():
                    expected = self.manifest['inputs']['files']['complex.pdb' if name == 'file' else 'ligand.sdf']
                    self.assertEqual(handle.read(), Path(expected).read_bytes())
                    handle.seek(0)
                return Mock(url=target, text=pages[step])
            return self.response(method, target)
        web.request = Mock(side_effect=request)
        return web

    def intent(self, source, target, overrides, uploads=()):
        form = soup_for(source.read_text()).find("form")
        atomic_json(target.with_suffix(".intent.json"), {"state": "response_received", "snapshot": str(target),
            "fields": merged_form_data(form, overrides, uploads), "upload_fields": list(uploads), "target": modeling_target(form.get("action"))})

    def response(self, method, target):
        self.assertEqual(method, "GET")
        data = self.pdb if target.endswith(".pdb") else self.psf if target.endswith(".psf") else self.f.topology
        response = Mock(url=target, text=data.read_text())
        self.responses.append(response)
        return response

    def refused(self, operation, *, key="fragment.proof_invalid"):
        with self.assertRaises(ToolError) as caught:
            operation()
        self.assertEqual(caught.exception.key, key)
        self.assertTrue(caught.exception.next_step)
        self.assertNotIn("NAME", caught.exception.next_step)
        self.web.submit_snapshot.assert_not_called()
        return caught.exception

    def test_seals_actual_full_uploads_and_recipe_before_any_remote_work(self):
        self.prepare()
        seal = fragments.seal_request(self.directory, self.manifest, create=True)
        self.assertEqual(seal["fragment_record_sha256"], canonical(self.record))
        self.assertIn("fragment-source.pdb", seal["input_sha256"])
        self.assertIn("input-manifest.json", seal["input_sha256"])
        before = (self.directory / fragments.REQUEST).read_bytes()
        self.assertEqual(fragments.seal_request(self.directory, self.manifest, create=True), seal)
        self.assertEqual((self.directory / fragments.REQUEST).read_bytes(), before)
        self.assertEqual((self.directory / fragments.REQUEST).stat().st_mode & 0o777, 0o600)
        self.web.request.assert_not_called()

    def test_no_fragment_leaves_old_and_minimal_tasks_unchanged(self):
        for manifest in ({}, {"inputs": {"manifest": {}, "files": {}}}):
            self.assertIsNone(fragments.seal_request(self.directory, manifest, create=True))
            self.assertIsNone(fragments.check_prepared(self.directory, manifest))
            self.assertIsNone(fragments.ensure_prepared(self.directory, manifest, self.web))
            self.assertEqual(fragments.request_kwargs(manifest), {})
        self.assertEqual(list(self.directory.iterdir()), [self.directory / "path-mode.json"])

    def test_missing_seal_cannot_be_created_after_upload_or_for_reused_child(self):
        self.prepare()
        for addition in ({"state": "prepared"}, {"source_job_id": IDENTIFIER}, {"reused_preparation": {"version": 1}}):
            self.refused(lambda: fragments.seal_request(self.directory, {**self.manifest, **addition}, create=True))
        (self.directory / "preparation").mkdir()
        self.refused(lambda: fragments.seal_request(self.directory, self.manifest, create=True))

    def test_successful_actual_post_binding_and_gets_verify_caps_not_science(self):
        self.prepare()
        self.stage()
        receipt = fragments.ensure_prepared(self.directory, self.manifest, self.web)
        self.assertTrue(receipt["source_origin_verified"])
        self.assertFalse(receipt["scientific_correctness_verified"])
        self.assertTrue(receipt["checks"]["passed"])
        self.assertFalse(receipt["checks"]["server_origin_verified"])
        self.assertFalse(receipt["mapping"]["submission_confirmed"])
        self.assertEqual(receipt["checks"]["fragment_count"], 2)
        self.assertEqual(len(self.web.request.call_args_list), 3)
        self.assertTrue(self.web.request.call_args_list[-1].args[1].endswith('/' + IDENTIFIER + '/toppar/top_all36_prot.rtf'))
        for response in self.responses:
            response.close.assert_called_once()
        original = (self.directory / fragments.RECEIPT).read_bytes()
        self.assertEqual(fragments.ensure_prepared(self.directory, self.manifest, self.web), receipt)
        self.assertEqual(fragments.check_prepared(self.directory, self.manifest), receipt)
        self.assertEqual((self.directory / fragments.RECEIPT).read_bytes(), original)
        self.assertEqual(len(self.web.request.call_args_list), 3)

    def test_input_and_terminal_configuration_changes_cannot_reseal(self):
        self.prepare()
        fragments.seal_request(self.directory, self.manifest, create=True)
        old = (self.directory / fragments.REQUEST).read_bytes()
        self.manifest["config"]["preparation"]["c_terminal"] = "CT2"
        self.refused(lambda: fragments.seal_request(self.directory, self.manifest, create=True))
        self.assertEqual((self.directory / fragments.REQUEST).read_bytes(), old)
        self.manifest["config"]["preparation"]["c_terminal"] = "CTER"
        Path(self.manifest["inputs"]["files"]["ligand.sdf"]).write_text("changed input")
        self.refused(lambda: fragments.seal_request(self.directory, self.manifest))

    def test_embedded_recipe_must_equal_the_actual_disk_recipe(self):
        self.prepare()
        path = Path(self.manifest["inputs"]["files"]["input-manifest.json"])
        value = read_value(path)
        value["input_normalization"]["fragment_preparation"]["plan"]["fragments"][0]["index"] = True
        atomic_json(path, value)
        self.refused(lambda: fragments.record_for(self.manifest), key="fragment.evidence_changed")

    def test_input_file_alias_escape_and_symlink_are_refused_before_get(self):
        self.prepare()
        original = dict(self.manifest["inputs"]["files"])
        for name, value in (("../ligand.sdf", original["ligand.sdf"]), ("ligand.sdf", str(self.root / "ligand.sdf"))):
            self.manifest["inputs"]["files"] = {**original, name: value}
            self.refused(lambda: fragments.record_for(self.manifest), key="fragment.evidence_changed")
        link = self.task / "inputs" / "linked.sdf"
        link.symlink_to(self.root / "ligand.sdf")
        self.manifest["inputs"]["files"] = {**original, "linked.sdf": str(link)}
        self.refused(lambda: fragments.record_for(self.manifest), key="fragment.evidence_changed")
        self.web.request.assert_not_called()

    def test_changed_post_fields_upload_target_or_state_refuse_before_download(self):
        self.prepare()
        self.stage()
        for filename in ("selection.intent.json", "parameterization.intent.json"):
            path = self.directory / "preparation" / filename
            original = read_value(path)
            for key, value in (("state", "submission_unknown"), ("fields", []), ("target", "https://example.org/"),
                               ("upload_fields", ["sdf_cgenff[OTHER]"])):
                atomic_json(path, {**original, key: value})
                self.refused(lambda: fragments.ensure_prepared(self.directory, self.manifest, self.web))
                atomic_json(path, original)
        self.web.request.assert_not_called()

    def test_upload_receipt_and_ready_page_must_match_the_sealed_source(self):
        self.prepare()
        self.stage()
        path = self.directory / "preparation/web-run.json"
        original = read_value(path)
        for key, value in (("job_id", "1234567891"), ("state", "upload_submitting"), ("input_pdb", str(self.root / "protein.pdb"))):
            atomic_json(path, {**original, key: value})
            self.refused(lambda: fragments.ensure_prepared(self.directory, self.manifest, self.web))
            atomic_json(path, original)
        page = Path(self.manifest["preparation_snapshot"])
        before = page.read_text()
        for html in (rendered(identifier="1234567891"), rendered(identifier=IDENTIFIER, stop=2),
                     rendered(identifier=IDENTIFIER, complete=False), rendered(identifier=IDENTIFIER, next_step="4"),
                     before.replace('uploaded_pdb/', 'https://example.org/uploaded_pdb/')):
            page.write_text(html)
            self.refused(lambda: fragments.ensure_prepared(self.directory, self.manifest, self.web))
        self.web.request.assert_not_called()

    def test_get_url_html_and_foreign_job_redirects_never_create_a_receipt(self):
        self.prepare()
        self.stage()
        for mode in ("external", "wrong_job", "html", "query", "credentials"):
            def response(method, target):
                result = self.response(method, target)
                if mode == "external": result.url = 'https://example.org/' + Path(target).name
                elif mode == "wrong_job": result.url = target.replace(IDENTIFIER, "1234567891")
                elif mode == "html": result.text = '<html>credential and response body must not be displayed</html>'
                elif mode == "query": result.url = target + '?token=private'
                elif mode == "credentials": result.url = target.replace('https://', 'https://private:secret@')
                return result
            self.web.request.side_effect = response
            error = self.refused(lambda: fragments.ensure_prepared(self.directory, self.manifest, self.web))
            self.assertNotIn("secret", str(error))
            self.assertNotIn("response body must", error.localized())
            self.assertFalse((self.directory / fragments.RECEIPT).exists())
        self.assertTrue(all(call.args[0] == "GET" for call in self.web.request.call_args_list))

    def test_interrupted_artifact_get_repeats_only_get_retaining_the_old_candidate(self):
        self.prepare()
        self.stage()
        def interrupted(method, target):
            if target.endswith('.psf'):
                raise ToolError("synthetic outage", category="transient_network", retryable=True)
            return self.response(method, target)
        self.web.request.side_effect = interrupted
        with self.assertRaises(ToolError):
            fragments.ensure_prepared(self.directory, self.manifest, self.web)
        candidates = list((self.directory / "preparation").glob("fragment-*"))
        self.assertEqual(len(candidates), 1)
        retained = (candidates[0] / "step1_pdbreader.pdb").read_bytes()
        self.web.request.side_effect = self.response
        proof = fragments.ensure_prepared(self.directory, self.manifest, self.web)
        self.assertTrue(proof["checks"]["passed"])
        self.assertEqual((candidates[0] / "step1_pdbreader.pdb").read_bytes(), retained)
        self.assertEqual(len(list((self.directory / "preparation").glob("fragment-*"))), 3)  # two candidates and proof
        self.assertTrue(all(call.args[0] == "GET" for call in self.web.request.call_args_list))
        self.web.submit_snapshot.assert_not_called()

    def test_changed_receipt_artifact_and_rehashed_path_escape_do_not_redownload(self):
        self.prepare()
        self.stage()
        receipt = fragments.ensure_prepared(self.directory, self.manifest, self.web)
        path = self.directory / fragments.RECEIPT
        for key, value in (("job_id", "1234567891"), ("schema_version", True), ("source_origin_verified", False), ("evidence_sha256", {})):
            body = {k: v for k, v in {**receipt, key: value}.items() if k != "fingerprint"}
            atomic_json(path, {**body, "fingerprint": canonical(body)})
            self.refused(lambda: fragments.ensure_prepared(self.directory, self.manifest, self.web))
        escaped = deepcopy(receipt)
        escaped["artifacts"]["pdb"] = '../outside/step1_pdbreader.pdb'
        escaped["fingerprint"] = canonical({key: value for key, value in escaped.items() if key != "fingerprint"})
        atomic_json(path, escaped)
        self.refused(lambda: fragments.check_prepared(self.directory, self.manifest))
        atomic_json(path, receipt)
        actual = self.directory / receipt["artifacts"]["pdb"]
        actual.write_text(actual.read_text() + '\nREMARK edited\n')
        self.refused(lambda: fragments.ensure_prepared(self.directory, self.manifest, self.web))
        self.assertEqual(len(self.web.request.call_args_list), 3)

    def test_changed_topology_fails_actual_types_not_just_a_download_hash(self):
        self.prepare()
        self.stage()
        self.f.topology.write_text(self.f.topology.read_text().replace('ATOM N NH3 -0.3', 'ATOM N NH3 -0.2'))
        self.refused(lambda: fragments.ensure_prepared(self.directory, self.manifest, self.web), key="fragment.topology_invalid")
        self.assertFalse((self.directory / fragments.RECEIPT).exists())

    def test_portable_move_rechecks_the_same_bytes_without_gets_or_rewriting_receipts(self):
        self.prepare()
        self.stage()
        proof = fragments.ensure_prepared(self.directory, self.manifest, self.web)
        before = {path.relative_to(self.task): path.read_bytes() for path in self.task.rglob('*') if path.is_file()}
        moved = self.root / "moved task"
        shutil.move(str(self.task), moved)
        directory = moved / '.charmm-gui'
        manifest = read_value(directory / 'full-run.json')
        self.assertEqual(fragments.check_prepared(directory, manifest), proof)
        self.assertEqual(before, {path.relative_to(moved): path.read_bytes() for path in moved.rglob('*') if path.is_file()})
        self.assertEqual(len(self.web.request.call_args_list), 3)

    def test_original_outer_terminal_rules_and_remapped_disulfide_states_coexist(self):
        self.prepare(disulfide=True, preparation={"terminals": {"A": {"n_terminal": "NNEU", "c_terminal": "CT2"}}})
        fields = self.stage()
        self.assertEqual(fields['terminal[PROA][last]'], 'CTER')
        self.assertEqual(fields['terminal[PROB][last]'], 'CT2')
        self.assertEqual(fields['ssbonds[segid1][]'], ['PROA'])
        self.assertEqual(fields['ssbonds[segid2][]'], ['PROB'])
        fragment_proof = fragments.ensure_prepared(self.directory, self.manifest, self.web)
        state_proof = protein_states.ensure_prepared(self.directory, self.manifest, self.web)
        self.assertTrue(state_proof['checks']['passed'])
        self.assertEqual(fragment_proof['checks']['disulfide_bonds_checked'], 1)
        self.assertEqual(len(self.web.request.call_args_list), 5)
        self.web.submit_snapshot.assert_not_called()

    def test_local_seal_and_clone_have_distinct_stage_appropriate_gates(self):
        self.prepare()
        full_build.require_protein_state_support(self.directory, self.manifest)
        identity = sweep.preparation_identity(self.directory, self.manifest)
        self.assertIn('fragment_preparation_sha256', identity)
        self.assertIn('fragment-source.pdb', identity['input_sha256'])
        api = Mock()
        self.refused(lambda: full_build._start_prepared_bilayer(self.directory, self.manifest, api, {}, False, 30, 60, None))
        self.assertFalse((self.directory / 'api-config.json').exists())
        self.assertEqual(api.method_calls, [])

    def test_real_orchestrator_posts_once_then_checks_source_before_api_clone_and_resume(self):
        self.prepare()
        self.stage()
        web = self.transport()
        directory, _ = self.clone_local('actual-workflow')
        api = Mock()
        api.submit.return_value = '1234567891'
        result = full_build.advance(directory, web, api)
        self.assertEqual(result['state'], 'bilayer_submitted')
        self.assertEqual(result['build_job_id'], '1234567891')
        self.assertTrue(result['fragment_source_proof']['source_origin_verified'])
        self.assertTrue((directory / fragments.RECEIPT).is_file())
        self.assertEqual([call.args[0] for call in web.request.call_args_list], ['GET', 'POST', 'POST', 'POST', 'GET', 'GET', 'GET'])
        api.submit.assert_called_once()
        web.request.reset_mock()
        api.status.return_value = {'status': 'pending'}
        resumed = full_build.advance(directory, web, api)
        self.assertEqual(resumed['state'], 'pending')
        api.submit.assert_called_once()
        web.request.assert_not_called()

    def test_failed_source_proof_and_get_recovery_never_repeat_successful_modeling_posts(self):
        self.prepare()
        self.stage()
        web = self.transport()
        original = web.request.side_effect
        failed = True
        def interrupted(method, target, **kwargs):
            if failed and method == 'GET' and target.endswith('.psf'):
                raise ToolError('synthetic outage', category='transient_network', retryable=True)
            return original(method, target, **kwargs)
        web.request.side_effect = interrupted
        directory, _ = self.clone_local('recover-workflow')
        api = Mock()
        api.submit.return_value = '1234567891'
        with self.assertRaises(ToolError):
            full_build.advance(directory, web, api)
        self.assertEqual(sum(call.args[0] == 'POST' for call in web.request.call_args_list), 3)
        api.submit.assert_not_called()
        failed = False
        result = full_build.advance(directory, web, api)
        self.assertEqual(result['state'], 'bilayer_submitted')
        self.assertEqual(sum(call.args[0] == 'POST' for call in web.request.call_args_list), 3)
        api.submit.assert_called_once()

    def test_gly_fallback_is_recorded_in_actual_intent_and_verified_source_caps(self):
        self.prepare(first='GLY')
        fields = self.stage(gly_fallback=True)
        self.assertEqual(fields['terminal[PROA][first]'], 'GLYP')
        self.assertEqual(fields['terminal[PROB][first]'], 'GLYP')
        proof = self.finish_proof()
        self.assertEqual([row['patch'] for row in proof['checks']['terminals'] if row['end'] == 'n_terminal'], ['GLYP', 'GLYP'])
        self.assertTrue(sweep.certify_source(self.directory)['fragment_source_proof']['source_origin_verified'])

    def test_final_archive_verifies_both_stages_and_exact_authenticated_source_bytes(self):
        self.prepare()
        self.stage()
        self.finish_proof()
        archive = self.archive()
        result = fragments.verify_archive(self.directory, self.manifest, archive)
        self.assertTrue(result['passed'])
        self.assertTrue(result['server_origin_verified'])
        self.assertFalse(result['scientific_correctness_verified'])
        self.assertEqual(result['checks']['source']['fragment_count'], 2)
        self.assertEqual(result['checks']['assembly']['fragment_count'], 2)
        source = archive / 'step1_pdbreader.pdb'
        source.write_text(source.read_text() + '\nREMARK same chemistry but not downloaded source bytes\n')
        self.refused(lambda: fragments.verify_archive(self.directory, self.manifest, archive))

    def test_missing_or_wrong_final_caps_cannot_be_hidden_by_correct_source_files(self):
        self.prepare()
        self.stage()
        self.finish_proof()
        archive = self.archive()
        psf = archive / 'step5_assembly.psf'
        psf.write_text(psf.read_text().replace('HT1 HC 0.330000', 'HT1 HC 0.230000'))
        self.refused(lambda: fragments.verify_archive(self.directory, self.manifest, archive), key='fragment.topology_invalid')

    def test_reuse_binds_full_source_proof_then_child_final_validation_survives_source_move(self):
        self.prepare()
        self.stage()
        self.finish_proof()
        certificate = sweep.certify_source(self.directory)
        directory, _ = self.clone_local('child')
        sweep.bind_source(directory, self.directory)
        manifest = read_value(directory / 'full-run.json')
        self.assertEqual(sweep.validate_reuse(directory, manifest), IDENTIFIER)
        self.assertEqual(manifest['fragment_source_proof'], certificate['fragment_source_proof'])
        self.assertFalse((directory / 'preparation').exists())
        fragments.require_prepared(directory, manifest)
        archive = self.archive()
        shutil.move(str(self.task), self.root / 'source-not-mounted-at-its-recorded-location')
        result = fragments.verify_archive(directory, manifest, archive)
        self.assertTrue(result['passed'])
        manifest['fragment_source_proof']['fingerprint'] = '0' * 64
        self.refused(lambda: fragments.verify_archive(directory, manifest, archive))

    def test_final_cache_requires_fragment_origin_and_current_request_identity(self):
        self.prepare()
        self.stage()
        self.finish_proof()
        archive = self.archive()
        proof = fragments.verify_archive(self.directory, self.manifest, archive)
        fake_binding = {'archive_provenance': {'archive_sha256': 'a' * 64}}
        with patch.object(managed, '_archive_binding', return_value=fake_binding):
            evidence = managed._evidence(self.manifest, self.task / 'charmm-gui.tgz', False, None)
        self.assertEqual(evidence['fragment_request_sha256'], proof['request_sha256'])
        report = {'evidence': evidence, 'fingerprint': managed._canonical_hash(evidence),
                  'validation_complete': True, 'fragment_validation': proof}
        self.assertTrue(managed._cache_reusable(report, evidence))
        for value in ({}, {**proof, 'server_origin_verified': False}, {**proof, 'request_sha256': '0' * 64}):
            self.assertFalse(managed._cache_reusable({**report, 'fragment_validation': value}, evidence))

    def test_library_validation_needs_actual_fragment_mapping_and_checks_both_stages(self):
        from charmm_gui_cli.chem.fragment_inputs import verify_input_archive
        from charmm_gui_cli.chem.validation import validate_system
        self.prepare()
        self.stage()
        archive = self.archive()
        path = self.manifest['inputs']['files']['input-manifest.json']
        proof = verify_input_archive(archive, path, mapping=self.manifest['fragment_mapping'])
        self.assertTrue(proof['passed'])
        self.assertFalse(proof['server_origin_verified'])
        report = validate_system(archive, path)
        self.assertFalse(report['passed'])
        self.assertIn('Fragment topology verification failed', ' '.join(report['errors']))

    def test_saved_response_does_not_override_an_uncertain_intent_even_with_stale_inputs(self):
        self.prepare()
        self.stage()
        path = self.directory / 'preparation/parameterization.intent.json'
        value = read_value(path)
        atomic_json(path, {**value, 'state': 'submission_unknown'})
        Path(self.manifest['inputs']['files']['fragment-source.pdb']).write_text('REMARK changed input\n')
        with self.assertRaises(SubmissionUnknown):
            full_build.advance(self.directory, self.web, Mock())
        self.web.request.assert_not_called()
        self.web.submit_snapshot.assert_not_called()
        self.assertEqual(read_value(path)['state'], 'submission_unknown')

    def test_a_valid_source_proof_cannot_authorize_cloning_a_different_source_job(self):
        self.prepare()
        self.stage()
        self.finish_proof()
        api = Mock()
        self.refused(lambda: full_build._start_prepared_bilayer(self.directory, self.manifest, api,
            {'source_job_id': '1234567891'}, False, 30, 60, None))
        api.submit.assert_not_called()

    def test_canonical_parent_alias_rechecks_portable_paths_without_changing_bytes(self):
        self.prepare()
        self.stage()
        proof = self.finish_proof()
        alias = self.root.parent / (self.root.name + '-alias')
        alias.symlink_to(self.root, target_is_directory=True)
        directory = alias / self.task.name / '.charmm-gui'
        self.assertEqual(fragments.check_prepared(directory, read_value(directory / 'full-run.json')), proof)

    def test_fragment_parameter_views_do_not_apply_original_outer_requests_to_inner_ends(self):
        from charmm_gui_cli.store.settings import parameter_summary
        from charmm_gui_cli.presentation.parameters import parameter_lines
        self.prepare(preparation={'terminals': {'A': {'n_terminal': 'NNEU', 'c_terminal': 'CT2'}}})
        self.stage()
        original = deepcopy(self.manifest)
        summary = parameter_summary(self.manifest, self.directory)
        first, last = summary['submission']['preparation']['chains']
        self.assertEqual((first['requested_n_terminal'], first['requested_c_terminal']), ('NNEU', 'CTER'))
        self.assertEqual((last['requested_n_terminal'], last['requested_c_terminal']), ('NTER', 'CT2'))
        self.assertEqual(first['requested_origins']['c_terminal'], 'global')
        self.assertEqual(last['requested_origins']['c_terminal'], 'source_chain')
        for lang, label in (('zh', '片段 A，原链 A'), ('en', 'fragment A, original chain A')):
            set_language(lang)
            text = '\n'.join(parameter_lines(summary))
            self.assertIn(label, text)
            self.assertNotIn('terminal[', text)
        self.assertEqual(original, self.manifest)
        self.manifest['fragment_mapping']['fragments'][0]['terminals']['c_terminal']['form_value'] = 'CT2'
        changed = parameter_summary(self.manifest, self.directory)
        self.assertEqual(changed['submission']['preparation']['status'], 'invalid_record')

    def test_fragment_terminal_and_report_summaries_only_claim_authenticated_final_checks(self):
        from charmm_gui_cli.presentation.reporting import display_result, render_text, render_html
        from charmm_gui_cli.presentation.terminal_output import format_build
        self.prepare()
        self.stage()
        self.finish_proof()
        proof = fragments.verify_archive(self.directory, self.manifest, self.archive())
        for lang in ('zh', 'en'):
            set_language(lang)
            value = {'state': 'validated', 'passed': True, 'fragment_validation': proof}
            displayed = display_result({}, {'directory': str(self.directory)}, self.manifest, {'fragment_validation': proof})
            label = t('fragment.verified', count=2)
            for text in (format_build(value), render_text(displayed), render_html(displayed)):
                self.assertIn(label, text)
            unverified = {**value, 'fragment_validation': {**proof, 'server_origin_verified': False}}
            self.assertNotIn(label, format_build(unverified))

    def test_malformed_fragment_envelopes_produce_localized_errors_not_tracebacks(self):
        for manifest in ({'inputs': None}, {'inputs': {'manifest': None}}, {'inputs': {'manifest': {'input_normalization': []}}}):
            self.refused(lambda: fragments.record_for(manifest), key='fragment.evidence_changed')

    def test_messages_are_bilingual_and_do_not_include_remote_bodies(self):
        for language in ('zh', 'en'):
            set_language(language)
            error = fragments.failure(self.directory)
            self.assertEqual(error.category, 'fragment_topology_unverified')
            self.assertIn('charmm-gui-cli jobs status', error.next_step)
            self.assertEqual(error.localized(), t('fragment.proof_invalid'))


if __name__ == '__main__':
    unittest.main()
