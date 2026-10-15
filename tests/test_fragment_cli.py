"""Public explicit splitting through real command/config/queue staging paths.

Network calls are forbidden. No live server or scientific acceptance is implied.
"""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli.cli import main, parser
from charmm_gui_cli.chem.fragment_inputs import resolve_fragment_option, verify_prepared_fragments
from charmm_gui_cli.chem.inputs import prepare_inputs
from charmm_gui_cli.chem.residues import validate_input_options
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.pipeline import batch, sweep, full_build
from charmm_gui_cli.pipeline.fragments import LOCAL_RECEIPT, seal_local_inputs
from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.store.paths import read_value
from tests import test_fragment_inputs as fixtures
from tests import test_fragment_protocol as protocol_fixtures
from tests.test_fragment_preparation import residue


class Terminal(io.StringIO):
    def isatty(self):
        return True


class FragmentCliTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.FragmentInputTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.root.resolve()
        self.environment = patch.dict('os.environ', {'XDG_DATA_HOME': str(self.root / 'data')})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.home = patch('pathlib.Path.home', return_value=self.root / 'home')
        self.home.start()
        self.addCleanup(self.home.stop)
        self.addCleanup(set_language)

    def call(self, argv, *, lang='en', tty=False):
        output, error = (Terminal() if tty else io.StringIO()), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            try:
                code = main(['--lang', lang, *argv])
            except SystemExit as exit:
                code = exit.code
        return code, output.getvalue(), error.getvalue()

    def check(self, *extra, **kwargs):
        out = self.f.out()
        code, output, error = self.call(['check', '--protein', str(self.f.protein), '--ligand', str(self.f.ligand),
            '--out', str(out), '--hydrogens', 'preserve', '--no-preview', *extra], **kwargs)
        return code, read_value(out / 'check.json'), output, error

    def build(self, *extra, **kwargs):
        out = self.f.out()
        code, output, error = self.call(['build', '--protein', str(self.f.protein), '--ligand', str(self.f.ligand),
            '--out', str(out), '--hydrogens', 'preserve', '--dry-run', '--no-preview', '--no-grompp', *extra], **kwargs)
        return code, out, output, error

    def record(self, prepared):
        record = verify_prepared_fragments(prepared)
        self.assertIsNotNone(record)
        self.assertTrue(Path(prepared['files']['fragment-source.pdb']).is_file())
        return record

    def config(self, *, options=None, preparation=None):
        path = self.root / 'model.yaml'
        path.write_text(yaml.safe_dump({'version': 2, 'protein': str(self.f.protein), 'ligand': str(self.f.ligand),
            'input_options': {'split_chain_breaks': True} if options is None else options,
            'preparation': preparation or {}, 'membrane': {'upper': 'POPC=1', 'lower': 'POPC=1', 'margin_A': 20}}))
        return path

    def test_default_and_explicit_false_still_refuse_geometric_breaks(self):
        for flags in ((), ('--no-split-chain-breaks',)):
            code, report, _, _ = self.check(*flags)
            self.assertEqual(code, 2)
            self.assertEqual(report['error']['message_key'], 'input.chain_breaks')
            code, out, _, _ = self.build(*flags)
            self.assertEqual(code, 2)
            self.assertFalse((out / '.charmm-gui/full-run.json').exists())

    def test_check_public_flag_writes_actual_full_upload_plan_and_bilingual_summary(self):
        original = self.f.protein.read_bytes(), self.f.ligand.read_bytes()
        for lang, phrase in (('zh', '已按明确请求分离'), ('en', 'Explicitly separated')):
            code, report, output, error = self.check('--split-chain-breaks', lang=lang, tty=True)
            self.assertEqual(code, 0, error)
            self.assertIn(phrase, output)
            self.assertTrue(report['review_required'])
            self.assertTrue(report['input_options']['split_chain_breaks'])
            prepared = {'files': report['prepared_files'], 'manifest': read_value(report['prepared_files']['input-manifest.json'])}
            record = self.record(prepared)
            self.assertEqual(record['plan']['fragments'][1]['prepared_chain'], 'B')
            self.assertFalse(report['server_submitted'])
        self.assertEqual(original, (self.f.protein.read_bytes(), self.f.ligand.read_bytes()))

    def test_direct_build_preserves_flag_through_source_staging_and_local_terminal_validation(self):
        code, out, output, error = self.build('--split-chain-breaks', '--n-terminal', 'fragment:B=GLYP', '--json')
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertTrue(result['review_required'])
        manifest = read_value(out / '.charmm-gui/full-run.json')
        self.record(manifest['inputs'])
        self.assertTrue(manifest['config']['input_options']['split_chain_breaks'])
        self.assertTrue(manifest['inputs']['manifest']['input_options']['split_chain_breaks'])
        self.assertEqual(manifest['config']['preparation']['terminals']['fragment:B']['n_terminal'], 'GLYP')
        full_build.require_protein_state_support(out / '.charmm-gui', manifest)
        self.assertFalse((out / '.charmm-gui/preparation').exists())

    def test_yaml_and_direct_build_generate_the_same_actual_upload_plan(self):
        path = self.config()
        out = self.f.out()
        code, output, error = self.call(['build', str(path), '--out', str(out), '--dry-run', '--no-grompp', '--no-preview', '--json'])
        self.assertEqual(code, 0, error)
        configured = read_value(out / '.charmm-gui/full-run.json')
        code, direct_out, _, error = self.build('--split-chain-breaks', '--json')
        self.assertEqual(code, 0, error)
        direct = read_value(direct_out / '.charmm-gui/full-run.json')
        self.assertEqual(self.record(configured['inputs'])['plan'], self.record(direct['inputs'])['plan'])

    def test_fragment_selector_requires_explicit_opt_in_and_a_matching_actual_fragment(self):
        for flags in (('--n-terminal', 'fragment:B=GLYP'),
                      ('--split-chain-breaks', '--n-terminal', 'fragment:Z=GLYP'),
                      ('--split-chain-breaks', '--n-terminal', 'fragment:A=GLYP')):
            code, out, _, _ = self.build(*flags, '--json')
            self.assertEqual(code, 2)
            self.assertFalse((out / '.charmm-gui/preparation').exists())

    def test_yaml_false_and_nonboolean_options_do_not_enable_splitting(self):
        for value in (False, 'true', 1, None, []):
            path = self.config(options={'split_chain_breaks': value})
            out = self.f.out()
            code, _, _ = self.call(['build', str(path), '--out', str(out), '--dry-run', '--no-grompp', '--no-preview', '--json'])
            self.assertEqual(code, 2)
            self.assertFalse((out / '.charmm-gui/preparation').exists())

    def test_yaml_cannot_mix_the_flag_with_direct_input_options(self):
        path = self.config()
        code, _, error = self.call(['build', str(path), '--split-chain-breaks', '--out', str(self.f.out()), '--dry-run', '--no-grompp'])
        self.assertEqual(code, 2)
        self.assertIn('configuration', error.lower())

    def test_option_is_a_noop_for_complete_chains_and_old_default_manifest_keys_stay_unchanged(self):
        self.f.lines = residue(10, 1, 0, segment='PROA') + residue(40, 2, 2.3, segment='PROA', name='GLY')
        self.f.write()
        baseline = prepare_inputs(self.f.protein, self.f.ligand, self.f.out())
        code, out, _, error = self.build('--split-chain-breaks', '--json')
        self.assertEqual(code, 0, error)
        manifest = read_value(out / '.charmm-gui/full-run.json')
        self.assertIsNone(verify_prepared_fragments(manifest['inputs']))
        self.assertNotIn('fragment-source.pdb', manifest['inputs']['files'])
        self.assertNotIn('split_chain_breaks', baseline['manifest']['input_options'])

    def test_inputs_command_supports_the_same_explicit_opt_in_without_other_default_changes(self):
        out = self.f.out()
        code, output, error = self.call(['inputs', '--protein', str(self.f.protein), '--ligand', str(self.f.ligand),
            '--out', str(out), '--split-chain-breaks', '--json'])
        self.assertEqual(code, 0, error)
        self.record(json.loads(output))

    def test_public_flag_does_not_relax_unknown_residue_or_covalent_link_checks(self):
        original = self.f.lines
        for extra in ('unknown', 'link'):
            self.f.lines = list(original)
            if extra == 'unknown':
                self.f.lines[0] = self.f.lines[0][:17] + 'UNK ' + self.f.lines[0][21:]
            else:
                self.f.lines.append('LINK        C1   LIG L   1                 CA  ALA A   1')
            self.f.write()
            code, _, _, _ = self.check('--split-chain-breaks', '--json')
            self.assertEqual(code, 2)

    def test_component_check_and_build_preserve_reserved_chains_and_fragment_controls(self):
        complex_path, components = self.f.components()
        out = self.f.out()
        code, output, error = self.call(['build', '--complex', str(complex_path), '--components', str(components),
            '--out', str(out), '--hydrogens', 'preserve', '--split-chain-breaks', '--n-terminal', 'fragment:D=GLYP',
            '--dry-run', '--no-grompp', '--no-preview', '--json'])
        self.assertEqual(code, 0, error)
        manifest = read_value(out / '.charmm-gui/full-run.json')
        record = self.record(manifest['inputs'])
        self.assertEqual([row['prepared_chain'] for row in record['plan']['fragments']], ['A', 'D'])
        self.assertTrue(manifest['config']['input_options']['split_chain_breaks'])
        checked = self.f.out()
        code, output, error = self.call(['check', '--complex', str(complex_path), '--components', str(components),
            '--out', str(checked), '--hydrogens', 'preserve', '--split-chain-breaks', '--no-preview', '--json'])
        self.assertEqual(code, 0, error)
        check = read_value(checked / 'check.json')
        self.assertEqual(record['plan'], self.record({'files': check['prepared_files'],
            'manifest': read_value(check['prepared_files']['input-manifest.json'])})['plan'])

    def test_batch_v1_v2_inheritance_and_per_job_false_are_validated_after_merging(self):
        for version in (1, 2):
            path = self.root / ('batch-' + str(version) + '.yaml')
            defaults = {'protein': str(self.f.protein), 'ligand': str(self.f.ligand)}
            override = {'name': 'one'}
            if version == 1:
                defaults.update(split_chain_breaks=True, hydrogens='preserve')
                override['n_terminal'] = 'fragment:B=GLYP'
            else:
                defaults.update(input_options={'split_chain_breaks': True}, ligand_hydrogens='preserve')
                override['preparation'] = {'n_terminal': 'fragment:B=GLYP'}
            value = {'version': version, 'defaults': defaults, 'jobs': [override]}
            path.write_text(yaml.safe_dump(value))
            normalized = batch.load_batch(path)
            self.assertTrue(normalized['jobs'][0]['options']['split_chain_breaks'])
            out = self.f.out()
            code, output, error = self.call(['batch', str(path), '--out', str(out), '--dry-run', '--no-preview', '--no-grompp', '--json'])
            self.assertEqual(code, 0, error)
            parent = read_value(out / 'batch.json')
            child = read_value(Path(parent['jobs'][0]['directory']) / 'full-run.json')
            self.record(child['inputs'])
            if version == 1:
                value['jobs'][0]['split_chain_breaks'] = False
            else:
                value['jobs'][0]['input_options'] = {'split_chain_breaks': False}
            path.write_text(yaml.safe_dump(value))
            with self.assertRaises(ToolError):
                batch.load_batch(path)

    def test_sweep_prepares_all_explicit_fragments_and_seals_their_shared_input_identity(self):
        path = self.root / 'sweep.yaml'
        path.write_text(yaml.safe_dump({'version': 1, 'inputs': {'protein': str(self.f.protein), 'ligand': str(self.f.ligand),
            'split_chain_breaks': True, 'hydrogens': 'preserve', 'n_terminal': 'fragment:B=GLYP'},
            'axes': {'salt_concentration': [.10, .20]}}))
        out = self.f.out()
        code, output, error = self.call(['sweep', str(path), '--out', str(out), '--dry-run', '--no-preview', '--no-grompp', '--json'])
        self.assertEqual(code, 0, error)
        parent = read_value(out / 'batch/batch.json')
        identities = []
        for row in parent['jobs']:
            directory = Path(row['directory'])
            manifest = read_value(directory / 'full-run.json')
            self.record(manifest['inputs'])
            identities.append(sweep.preparation_identity(directory, manifest))
        self.assertEqual(identities[0], identities[1])
        self.assertIn('fragment-source.pdb', identities[0]['input_sha256'])

    def test_boolean_and_internal_option_conflict_fail_locally(self):
        for value in (0, 1, 'true', None, []):
            with self.assertRaises(ToolError):
                validate_input_options({'split_chain_breaks': value})
        with self.assertRaises(ToolError) as caught:
            resolve_fragment_option(True, {'split_chain_breaks': False})
        self.assertEqual(caught.exception.key, 'fragment.option_conflict')

    def test_public_help_and_parser_defaults_are_bilingual_and_do_not_enable_the_flag(self):
        for lang in ('zh', 'en'):
            set_language(lang)
            for command in ('build', 'check', 'inputs'):
                code, output, _ = self.call([command, '-h'], lang=lang)
                self.assertEqual(code, 0)
                self.assertIn('--split-chain-breaks', output)
                self.assertIn('--no-split-chain-breaks', output)
        args = parser().parse_args(['build', '--protein', 'protein.pdb', '--ligand', 'ligand.sdf'])
        self.assertIsNone(args.split_chain_breaks)

    def prepared_queue(self):
        path = self.root / 'adoption-batch.yaml'
        path.write_text(yaml.safe_dump({'version': 2, 'defaults': {'protein': str(self.f.protein), 'ligand': str(self.f.ligand),
            'input_options': {'split_chain_breaks': True}}, 'jobs': [{'name': 'one'}]}))
        out = self.f.out()
        batch.initialize(path, out, register=False)
        parent = read_value(out / 'batch.json')
        parent['jobs'][0]['state'] = 'preparing_local'
        directory = Path(parent['jobs'][0]['directory'])
        return out, parent, directory

    def adopt(self, out, parent):
        from charmm_gui_cli.store.locking import run_lock
        with run_lock(out):
            batch._prepare(out, parent)
        return parent['jobs'][0]

    def test_complete_local_fragment_task_is_adopted_without_repreparing_or_file_writes(self):
        out, parent, directory = self.prepared_queue()
        self.assertTrue((directory / LOCAL_RECEIPT).is_file())
        original = {path.relative_to(directory.parent): path.read_bytes() for path in directory.parent.rglob('*') if path.is_file()}
        with patch('charmm_gui_cli.pipeline.build_command.execute', side_effect=AssertionError('Do not prepare twice')):
            row = self.adopt(out, parent)
        self.assertEqual(row['state'], 'inputs_ready')
        current = {path.relative_to(directory.parent): path.read_bytes() for path in directory.parent.rglob('*') if path.is_file()}
        self.assertEqual(original, current)
        self.record(read_value(directory / 'full-run.json')['inputs'])

    def test_prepared_policy_noop_is_sealed_and_adopted_without_inventing_a_fragment_plan(self):
        self.f.lines = residue(10, 1, 0) + residue(40, 2, 2.3, name='GLY')
        self.f.write()
        out, parent, directory = self.prepared_queue()
        manifest = read_value(directory / 'full-run.json')
        self.assertIsNone(verify_prepared_fragments(manifest['inputs']))
        self.assertTrue((directory / LOCAL_RECEIPT).is_file())
        self.assertEqual(self.adopt(out, parent)['state'], 'inputs_ready')

    def test_changed_source_snapshot_or_chemical_file_is_not_adopted_or_overwritten(self):
        for name in ('fragment-source.pdb', 'ligand.sdf'):
            out, parent, directory = self.prepared_queue()
            manifest = read_value(directory / 'full-run.json')
            path = Path(manifest['inputs']['files'][name])
            path.write_text(path.read_text() + '\nchanged local input\n')
            changed = path.read_bytes()
            with patch('charmm_gui_cli.pipeline.build_command.execute', side_effect=AssertionError('No silent reprepare')):
                row = self.adopt(out, parent)
            self.assertEqual(row['state'], 'local_failed')
            self.assertEqual(row['error_detail']['message_key'], 'fragment.adoption_invalid')
            self.assertIn('charmm-gui-cli check', row['error_detail']['next_step'])
            self.assertIn('--split-chain-breaks', row['error_detail']['next_step'])
            self.assertEqual(path.read_bytes(), changed)

    def test_missing_local_seal_is_not_rebuilt_from_cached_files(self):
        out, parent, directory = self.prepared_queue()
        receipt = directory / LOCAL_RECEIPT
        receipt.rename(receipt.with_suffix('.retained'))
        row = self.adopt(out, parent)
        self.assertEqual(row['state'], 'local_failed')
        self.assertFalse(receipt.exists())

    def test_dropping_the_embedded_fragment_marker_does_not_drop_its_ownership_proof(self):
        from charmm_gui_cli.store.auth import atomic_json
        out, parent, directory = self.prepared_queue()
        manifest = read_value(directory / 'full-run.json')
        del manifest['inputs']['manifest']['input_normalization']['fragment_preparation']
        atomic_json(directory / 'full-run.json', manifest)
        self.assertEqual(self.adopt(out, parent)['state'], 'local_failed')

    def test_queue_parameter_changes_or_disabling_splitting_cannot_adopt_a_different_ready_child(self):
        for key, value in (('c_terminal', 'CT2'), ('split_chain_breaks', False)):
            out, parent, _ = self.prepared_queue()
            parent['config']['jobs'][0]['options'][key] = value
            row = self.adopt(out, parent)
            self.assertEqual(row['state'], 'local_failed')
            self.assertEqual(row['error_detail']['message_key'], 'fragment.adoption_invalid')

    def test_moved_ready_child_uses_the_same_local_seal_and_actual_pre_split_snapshot(self):
        out, parent, directory = self.prepared_queue()
        original = (directory / LOCAL_RECEIPT).read_bytes()
        destination = self.f.out()
        shutil.move(str(directory.parent), destination)
        moved = destination / '.charmm-gui'
        parent['jobs'][0]['directory'] = str(moved)
        row = self.adopt(out, parent)
        self.assertEqual(row['state'], 'inputs_ready', row)
        self.assertEqual((moved / LOCAL_RECEIPT).read_bytes(), original)
        self.record(read_value(moved / 'full-run.json')['inputs'])

    def test_local_input_seal_matches_its_manifest_and_refuses_resealing_an_edit(self):
        code, out, _, error = self.build('--split-chain-breaks', '--json')
        self.assertEqual(code, 0, error)
        directory = out / '.charmm-gui'
        manifest = read_value(directory / 'full-run.json')
        seal_local_inputs(directory, manifest)
        original = (directory / LOCAL_RECEIPT).read_bytes()
        changed = deepcopy(manifest)
        changed['config']['ions']['concentration_M'] = .2
        with self.assertRaises(ToolError):
            seal_local_inputs(directory, changed, create=True)
        self.assertEqual((directory / LOCAL_RECEIPT).read_bytes(), original)

    def test_model_and_ligand_pose_are_selected_once_before_fragment_staging(self):
        text = '\n'.join(self.f.lines)
        self.f.protein.write_text('MODEL        1\n' + text + '\nENDMDL\nMODEL        2\n' + text + '\nENDMDL\nEND\n')
        self.f.ligand.write_text(self.f.ligand.read_text() * 2)
        code, out, _, error = self.build('--split-chain-breaks', '--model', '2', '--ligand-pose', '2', '--json')
        self.assertEqual(code, 0, error)
        manifest = read_value(out / '.charmm-gui/full-run.json')
        self.record(manifest['inputs'])
        self.assertEqual(manifest['config']['input_options']['model'], 2)
        self.assertEqual(manifest['config']['input_options']['ligand_pose'], 2)

    def test_input_recovery_command_contains_a_boolean_flag_not_a_true_value_argument(self):
        from charmm_gui_cli.chem.input_diagnostics import check_command
        command = check_command(protein=self.f.protein, ligand=self.f.ligand, input_options={'split_chain_breaks': True})
        self.assertIn('--split-chain-breaks', command)
        self.assertNotIn(' True', command)
        args = parser().parse_args(command.split()[1:])
        self.assertTrue(args.split_chain_breaks)

    def test_changed_local_chemical_input_is_rejected_before_auth_or_upload(self):
        from unittest.mock import Mock
        code, out, _, error = self.build('--split-chain-breaks', '--json')
        self.assertEqual(code, 0, error)
        directory = out / '.charmm-gui'
        manifest = read_value(directory / 'full-run.json')
        ligand = Path(manifest['inputs']['files']['ligand.sdf'])
        ligand.write_text(ligand.read_text() + '\nchanged after local check\n')
        web, api = Mock(), Mock()
        with self.assertRaises(ToolError):
            full_build.advance(directory, web, api)
        self.assertEqual(web.method_calls, [])
        self.assertEqual(api.method_calls, [])

    def test_uncertain_post_still_precedes_stale_input_checks_when_split_policy_was_a_noop(self):
        from charmm_gui_cli.remote.api import SubmissionUnknown
        from charmm_gui_cli.store.auth import atomic_json
        self.f.lines = residue(10, 1, 0) + residue(40, 2, 2.3, name='GLY')
        self.f.write()
        code, out, _, error = self.build('--split-chain-breaks', '--json')
        self.assertEqual(code, 0, error)
        directory = out / '.charmm-gui'
        manifest = read_value(directory / 'full-run.json')
        Path(manifest['inputs']['files']['ligand.sdf']).write_text('changed')
        atomic_json(directory / 'preparation/parameterization.intent.json', {'state': 'submission_unknown'})
        with self.assertRaises(SubmissionUnknown):
            full_build.require_protein_state_support(directory, manifest)

    def test_foreground_public_cli_reaches_real_intent_writers_source_proof_and_single_api_clone(self):
        from unittest.mock import Mock
        protocol = protocol_fixtures.FragmentProtocolTests()
        protocol.setUp()
        self.addCleanup(protocol.doCleanups)
        protocol.prepare()
        protocol.stage()
        out = self.f.out()
        protocol.manifest['inputs']['files'] = {name: str(out / 'inputs' / name) for name in protocol.manifest['inputs']['files']}
        web = protocol.transport()
        api = Mock()
        api.submit.return_value = '1234567891'
        cookies = self.root / 'synthetic.cookies.json'
        cookies.write_text('{"version":1,"cookies":[]}')
        with patch('charmm_gui_cli.pipeline.build_command.load_token', return_value=('synthetic-token', str(self.root / 'session.token'))), \
             patch('charmm_gui_cli.pipeline.build_command.Client', return_value=api), \
             patch('charmm_gui_cli.remote.web.WebClient', return_value=web), \
             patch('charmm_gui_cli.remote.sessions.bind'):
            code, output, error = self.call(['build', '--protein', str(protocol.root / 'protein.pdb'),
                '--ligand', str(protocol.root / 'ligand.sdf'), '--out', str(out), '--hydrogens', 'preserve',
                '--split-chain-breaks', '--foreground', '--no-wait', '--no-preview', '--no-grompp', '--cookies', str(cookies), '--json'])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['state'], 'bilayer_submitted')
        self.assertTrue((out / '.charmm-gui/preparation/fragment-proof.json').is_file())
        api.submit.assert_called_once()
        self.assertEqual(sum(call.args[0] == 'POST' for call in web.request.call_args_list), 3)

    def test_default_background_handoff_keeps_the_public_flag_and_only_checks_auth_in_front(self):
        from unittest.mock import Mock
        web, api = Mock(), Mock()
        web.request.return_value = Mock(text='<form name="pdb"></form>')
        out = self.f.out()
        cookies = self.root / 'synthetic.cookies.json'
        cookies.write_text('{"version":1,"cookies":[]}')
        with patch('charmm_gui_cli.pipeline.background.load_token', return_value=('synthetic-token', str(self.root / 'session.token'))), \
             patch('charmm_gui_cli.remote.api.Client', return_value=api), \
             patch('charmm_gui_cli.remote.web.WebClient', return_value=web), \
             patch('charmm_gui_cli.remote.sessions.bind'), \
             patch('charmm_gui_cli.pipeline.background.subprocess.Popen', return_value=Mock(pid=999123456)):
            code, output, error = self.call(['build', '--protein', str(self.f.protein), '--ligand', str(self.f.ligand),
                '--out', str(out), '--hydrogens', 'preserve', '--split-chain-breaks', '--cookies', str(cookies),
                '--no-grompp', '--no-preview', '--json'])
        self.assertEqual(code, 0, error)
        self.assertIn('background', json.loads(output))
        options = read_value(out / '.charmm-gui/worker-options.json')
        self.assertTrue(options['split_chain_breaks'])
        self.assertTrue(options['foreground'])
        self.assertEqual(options['command'], 'build-resume')
        self.assertNotIn('synthetic-token', json.dumps(options))
        web.request.assert_called_once()
        self.assertEqual(web.request.call_args.args[0], 'GET')
        api.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
