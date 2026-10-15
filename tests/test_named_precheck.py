"""Owned check-to-build handoff through public CLI; all HTTP is forbidden."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import unittest
from unittest.mock import Mock, patch

from charmm_gui_cli.cli import main
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.pipeline import checked_inputs, full_build
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.paths import read_value
from charmm_gui_cli.remote.api import SubmissionUnknown
from tests import test_precheck as fixtures
import yaml


class Terminal(io.StringIO):
    def isatty(self):
        return True


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NamedPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PrecheckTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.root
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(set_language)
        self.stack.enter_context(patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'data')}))
        self.stack.enter_context(patch('pathlib.Path.home', return_value=self.root / 'home'))
        self.network = self.stack.enter_context(patch('requests.Session.request', side_effect=AssertionError('No HTTP expected')))
        self.counter = 0

    def call(self, args, *, language='en', tty=False):
        output, error = Terminal() if tty else io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(['--lang', language, *args])
        return code, output.getvalue(), error.getvalue()

    def check(self, *flags, **kwargs):
        self.counter += 1
        name = 'checked-' + str(self.counter)
        root = self.root / name
        code, output, error = self.call(['check', '--name', name, '--out', str(root), '--protein', str(self.f.protein),
            '--ligand', str(self.f.ligand), '--hydrogens', 'preserve', '--no-preview', *flags], **kwargs)
        return code, root, output, error

    def build(self, root, *flags, **kwargs):
        return self.call(['build', '--from-check', str(root), '--dry-run', '--no-preview', '--no-grompp', *flags], **kwargs)

    def test_named_check_is_one_owned_task_and_does_not_load_credentials(self):
        original = digest(self.f.protein), digest(self.f.ligand)
        with patch('charmm_gui_cli.store.auth.load_token') as auth:
            code, root, output, error = self.check()
        self.assertEqual(code, 0, error)
        value = json.loads(output)
        self.assertTrue(value['passed'])
        self.assertTrue(value['task_registered'])
        self.assertFalse(value['server_submitted'])
        self.assertEqual(Path(value['directory']), root)
        self.assertEqual(Path(value['prepared_complex']), root / 'inputs/complex.pdb')
        self.assertFalse((root / '.charmm-gui/local-check/prepared').exists())
        self.assertFalse((root / 'model').exists())
        self.assertTrue((root / 'run.log').is_file())
        report, _, request = checked_inputs.verify(root / '.charmm-gui')
        self.assertTrue(report['passed'])
        self.assertIsNone(request)
        self.assertEqual(shlex.split(value['next_step']), ['charmm-gui-cli', 'build', '--from-check', root.name])
        self.assertEqual(original, (digest(self.f.protein), digest(self.f.ligand)))
        auth.assert_not_called()
        self.network.assert_not_called()

    def test_build_from_check_selects_model_options_without_preparing_any_files_again(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        paths = list((root / 'inputs').iterdir())
        before = {p.name: (digest(p), p.stat().st_ino) for p in paths}
        with patch('charmm_gui_cli.chem.precheck.check_inputs', side_effect=AssertionError('No new check')), \
             patch('charmm_gui_cli.chem.inputs.prepare_inputs', side_effect=AssertionError('No restaging')), \
             patch('charmm_gui_cli.chem.input_sources.prepare_sources', side_effect=AssertionError('No source preparation')):
            code, output, error = self.build(root, '--upper', 'POPC:POPE=7:3', '--salt-concentration', '.2')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['state'], 'inputs_ready')
        manifest = read_value(root / '.charmm-gui/full-run.json')
        self.assertEqual(manifest['config']['membrane']['upper'], 'POPC:POPE=7:3')
        self.assertEqual(manifest['config']['ions']['concentration_M'], .2)
        self.assertEqual(manifest['config']['ligand_hydrogens'], 'preserve')
        checked_inputs.require_requested(root / '.charmm-gui', manifest)
        full_build.require_protein_state_support(root / '.charmm-gui', manifest)
        self.assertEqual(before, {p.name: (digest(p), p.stat().st_ino) for p in paths})
        self.network.assert_not_called()

    def test_from_check_cannot_mix_sources_change_location_or_change_fixed_input_policy(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        for flags in (['--protein', str(self.f.protein)], ['--hydrogens', 'add_missing'],
                      ['--model', '1'], ['--ligand-smiles', 'RAW_SECRET'], ['--out', str(self.root / 'other')], ['--name', 'other']):
            code, _, error = self.build(root, *flags)
            self.assertEqual(code, 2, error)
            self.assertNotIn('RAW_SECRET', error)
            self.assertFalse((root / '.charmm-gui' / checked_inputs.REQUEST).exists())
        self.assertFalse((self.root / 'other').exists())

    def test_named_check_status_list_and_all_report_formats_do_not_claim_model_validation(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        for language, phrase in (('en', 'Local precheck passed'), ('zh', '本地预检通过')):
            code, output, error = self.call(['jobs', 'status', root.name], language=language, tty=True)
            self.assertEqual(code, 0, error)
            self.assertIn(phrase, output)
            self.assertNotIn('GROMACS', output)
            code, output, error = self.call(['jobs', 'list'], language=language, tty=True)
            self.assertEqual(code, 0, error)
            self.assertIn(phrase, output)
            code, output, error = self.call(['jobs', 'report', root.name, '--format', 'text'], language=language)
            self.assertEqual(code, 0, error)
            self.assertIn('local input checks only' if language == 'en' else '只记录本地输入检查', output)
        code, output, error = self.call(['jobs', 'report', root.name, '--format', 'json'])
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)['scientific_correctness_verified'])
        html = self.root / 'checked.html'
        code, _, error = self.call(['jobs', 'report', root.name, '--format', 'html', '--out', str(html)])
        self.assertEqual(code, 0, error)
        self.assertIn('<!doctype html>', html.read_text())
        self.assertFalse((root / '.charmm-gui/results/validation.json').exists())
        self.network.assert_not_called()

    def test_modified_prepared_file_is_rejected_without_reseal_or_login(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        seal = root / '.charmm-gui' / checked_inputs.SEAL
        before = seal.read_bytes()
        sdf = root / 'inputs/ligand.sdf'
        sdf.write_text(sdf.read_text() + '\nchanged\n')
        with patch('charmm_gui_cli.pipeline.build_command.load_token') as auth:
            code, _, error = self.build(root)
        self.assertEqual(code, 2, error)
        self.assertIn('sealed evidence', error)
        self.assertEqual(seal.read_bytes(), before)
        self.assertFalse((root / '.charmm-gui' / checked_inputs.REQUEST).exists())
        auth.assert_not_called()

    def test_original_files_can_move_away_before_choosing_the_model(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        self.f.protein.rename(self.f.protein.with_suffix('.retained'))
        self.f.ligand.rename(self.f.ligand.with_suffix('.retained'))
        code, output, error = self.build(root)
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['state'], 'inputs_ready')

    def test_exact_model_request_is_idempotent_but_a_different_one_is_refused(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        code, _, error = self.build(root, '--upper', 'POPC:POPE=7:3')
        self.assertEqual(code, 0, error)
        path = root / '.charmm-gui' / checked_inputs.REQUEST
        before = path.read_bytes()
        for flags in ((), ('--upper', 'POPC:POPE=7:3')):
            code, _, error = self.build(root, *flags)
            self.assertEqual(code, 0, error)
        code, _, error = self.build(root, '--upper', 'POPC=1')
        self.assertEqual(code, 2, error)
        self.assertIn('already records', error)
        self.assertEqual(path.read_bytes(), before)

    def test_default_named_directory_is_the_current_directory_and_legacy_checks_stay_compatible(self):
        with patch('pathlib.Path.cwd', return_value=self.root):
            code, output, error = self.call(['check', '--name', 'my-receptor', '--protein', str(self.f.protein),
                '--ligand', str(self.f.ligand), '--hydrogens', 'preserve', '--no-preview'])
        self.assertEqual(code, 0, error)
        self.assertEqual(Path(json.loads(output)['directory']), self.root / 'my-receptor')
        legacy = self.root / 'old-check-layout'
        code, output, error = self.call(['check', '--out', str(legacy), '--protein', str(self.f.protein),
            '--ligand', str(self.f.ligand), '--hydrogens', 'preserve', '--no-preview'])
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)['task_registered'])
        self.assertTrue((legacy / 'prepared/complex.pdb').is_file())
        self.assertFalse((legacy / '.charmm-gui').exists())

    def test_foreign_or_registered_named_paths_are_not_replaced(self):
        foreign = self.root / 'occupied'
        foreign.mkdir()
        marker = foreign / 'original.txt'
        marker.write_text('user file')
        code, _, error = self.call(['check', '--name', 'new-check', '--out', str(foreign),
            '--protein', str(self.f.protein), '--ligand', str(self.f.ligand), '--no-preview'])
        self.assertEqual(code, 2, error)
        self.assertEqual(marker.read_text(), 'user file')
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        before = (root / '.charmm-gui' / checked_inputs.SEAL).read_bytes()
        other = self.root / 'must-not-exist'
        code, _, error = self.call(['check', '--name', root.name, '--out', str(other), '--protein', str(self.f.protein),
            '--ligand', str(self.f.ligand), '--no-preview'])
        self.assertEqual(code, 2, error)
        self.assertFalse(other.exists())
        self.assertEqual((root / '.charmm-gui' / checked_inputs.SEAL).read_bytes(), before)

    def test_failed_named_checks_remain_inspectable_and_cannot_start_a_remote_build(self):
        self.f.protein.write_text(self.f.protein.read_text().replace('ALA', 'UNK'))
        code, root, output, error = self.check()
        self.assertEqual(code, 2, error)
        self.assertFalse(json.loads(output)['passed'])
        code, output, error = self.call(['jobs', 'status', root.name])
        self.assertEqual(code, 0, error)
        value = json.loads(output)
        self.assertTrue(value['precheck_only'])
        self.assertEqual(value['input_check_status'], 'failed')
        self.assertEqual(value['execution_summary']['outcome'], 'failed_local')
        code, output, error = self.call(['jobs', 'report', root.name])
        self.assertEqual(code, 2, error)
        self.assertFalse(json.loads(output)['passed'])
        saved = self.root / 'failed-report.json'
        code, output, error = self.call(['jobs', 'report', root.name, '--out', str(saved)])
        self.assertEqual(code, 2, error)
        self.assertFalse(json.loads(output)['passed'])
        with patch('charmm_gui_cli.pipeline.build_command.load_token') as auth:
            code, _, error = self.call(['jobs', 'resume', root.name, '--foreground', '--no-grompp', '--no-preview'])
        self.assertEqual(code, 2, error)
        self.assertIn('precheck failed', error)
        self.assertFalse((root / '.charmm-gui/preparation').exists())
        auth.assert_not_called()

    def test_public_check_report_edits_do_not_change_private_proof_or_modeling_inputs(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        seal = root / '.charmm-gui' / checked_inputs.SEAL
        original = seal.read_bytes()
        (root / 'check.json').write_text('user notes, not immutable evidence')
        code, _, error = self.build(root)
        self.assertEqual(code, 0, error)
        self.assertEqual((root / 'check.json').read_text(), 'user notes, not immutable evidence')
        self.assertEqual(seal.read_bytes(), original)

    def test_moved_checked_task_can_relink_and_choose_model_without_rewriting_its_check(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        seal = (root / '.charmm-gui' / checked_inputs.SEAL).read_bytes()
        destination = self.root / '新位置 with spaces' / 'receptor'
        destination.parent.mkdir()
        root.rename(destination)
        code, _, error = self.call(['jobs', 'relink', root.name, str(destination)])
        self.assertEqual(code, 0, error)
        code, output, error = self.call(['jobs', 'status', root.name])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['input_check_status'], 'passed')
        code, _, error = self.build(destination)
        self.assertEqual(code, 0, error)
        self.assertEqual((destination / '.charmm-gui' / checked_inputs.SEAL).read_bytes(), seal)
        self.assertFalse(root.exists())

    def test_interrupted_local_model_commit_finishes_the_same_record_without_reseal(self):
        code, root, _, error = self.check('--split-chain-breaks')
        self.assertEqual(code, 0, error)
        directory = root / '.charmm-gui'
        with patch('charmm_gui_cli.pipeline.fragments.seal_local_inputs', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            checked_inputs.choose_model(directory)
        request = (directory / checked_inputs.REQUEST).read_bytes()
        self.assertFalse((directory / 'local-input-seal.json').exists())
        checked_inputs.choose_model(directory)
        checked_inputs.require_requested(directory, read_value(directory / 'full-run.json'))
        self.assertEqual((directory / checked_inputs.REQUEST).read_bytes(), request)
        local = (directory / 'local-input-seal.json').read_bytes()
        checked_inputs.choose_model(directory)
        self.assertEqual((directory / 'local-input-seal.json').read_bytes(), local)

    def test_unknown_submission_has_priority_over_damaged_checked_inputs_and_no_replay(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        directory = root / '.charmm-gui'
        (root / 'inputs/ligand.sdf').write_text('damaged')
        atomic_json(directory / 'preparation/selection.intent.json', {'state': 'submission_unknown'})
        with self.assertRaises(SubmissionUnknown) as caught:
            checked_inputs.choose_model(directory)
        self.assertEqual(caught.exception.category, 'submission_unknown')
        self.assertFalse((directory / checked_inputs.REQUEST).exists())
        api, web = Mock(), Mock()
        with self.assertRaises(SubmissionUnknown):
            full_build.advance(directory, web, api)
        self.assertEqual(api.method_calls, [])
        self.assertEqual(web.method_calls, [])

    def test_missing_or_mutated_seals_metadata_and_symlink_inputs_cannot_be_adopted(self):
        for mutation in ('seal', 'config', 'manifest', 'symlink'):
            code, root, _, error = self.check()
            self.assertEqual(code, 0, error)
            directory = root / '.charmm-gui'
            original = (directory / checked_inputs.SEAL).read_bytes()
            if mutation == 'seal':
                (directory / checked_inputs.SEAL).rename(directory / 'retained-seal.json')
            elif mutation in ('config', 'manifest'):
                manifest = read_value(directory / 'full-run.json')
                if mutation == 'config':
                    manifest['config']['ligand_hydrogens'] = 'add_missing'
                else:
                    manifest['inputs']['manifest']['input_options']['model'] = 99
                atomic_json(directory / 'full-run.json', manifest)
            else:
                sdf = root / 'inputs/ligand.sdf'
                moved = root / 'retained-ligand.sdf'
                sdf.rename(moved)
                sdf.symlink_to(moved)
            code, _, error = self.build(root)
            self.assertEqual(code, 2, error)
            self.assertFalse((directory / checked_inputs.REQUEST).exists())
            if mutation != 'seal':
                self.assertEqual((directory / checked_inputs.SEAL).read_bytes(), original)
            code, output, error = self.call(['jobs', 'status', root.name])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)['input_check_status'], 'stale')

    def test_prepared_preview_is_visible_cached_and_never_copies_the_coordinates(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        seal = (root / '.charmm-gui' / checked_inputs.SEAL).read_bytes()
        for _ in range(2):
            code, output, error = self.call(['preview', root.name, '--width', '400'])
            self.assertEqual(code, 0, error)
            value = json.loads(output)
            self.assertEqual(value['scope'], 'prepared_input')
            self.assertFalse(value['model_copied'])
            self.assertEqual(Path(value['model']), root / 'inputs/complex.pdb')
            for item in value['images']:
                self.assertTrue(Path(item['file']).is_relative_to(root / 'previews'))
                self.assertTrue(Path(item['file']).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(len(list((root / 'previews').iterdir())), 1)
        self.assertFalse(list((root / 'previews').rglob('model.pdb')))
        self.assertEqual((root / '.charmm-gui' / checked_inputs.SEAL).read_bytes(), seal)

    def test_background_build_from_check_preserves_one_task_and_only_gets_auth_in_front(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        before = {p.name: digest(p) for p in (root / 'inputs').iterdir()}
        web, api = Mock(), Mock()
        web.request.return_value = Mock(text='<form name="pdb"></form>')
        cookie = self.root / 'synthetic.cookies.json'
        atomic_json(cookie, {'version': 1, 'cookies': []})
        with patch('charmm_gui_cli.pipeline.background.load_token', return_value=('synthetic-token', str(self.root / 'session.token'))), \
             patch('charmm_gui_cli.remote.api.Client', return_value=api), patch('charmm_gui_cli.remote.web.WebClient', return_value=web), \
             patch('charmm_gui_cli.remote.sessions.bind'), \
             patch('charmm_gui_cli.pipeline.background.subprocess.Popen', return_value=Mock(pid=999123456)):
            code, output, error = self.call(['build', '--from-check', root.name, '--no-grompp', '--no-preview', '--cookies', str(cookie)])
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)['background']['running'])
        options = read_value(root / '.charmm-gui/worker-options.json')
        self.assertEqual(options['command'], 'build-resume')
        self.assertEqual(Path(options['directory']), root / '.charmm-gui')
        self.assertNotIn('synthetic-token', json.dumps(options))
        self.assertEqual(before, {p.name: digest(p) for p in (root / 'inputs').iterdir()})
        web.request.assert_called_once()
        self.assertEqual(web.request.call_args.args[0], 'GET')
        api.submit.assert_not_called()

    def test_from_checked_fragments_reaches_real_intent_writers_and_only_one_api_clone(self):
        from tests import test_fragment_protocol as protocol_fixtures
        protocol = protocol_fixtures.FragmentProtocolTests()
        protocol.setUp()
        self.addCleanup(protocol.doCleanups)
        protocol.prepare()
        protocol.stage()
        root = self.root / 'fragment-check'
        code, _, error = self.call(['check', '--name', 'fragment-check', '--out', str(root),
            '--protein', str(protocol.root / 'protein.pdb'), '--ligand', str(protocol.root / 'ligand.sdf'),
            '--hydrogens', 'preserve', '--split-chain-breaks', '--no-preview'])
        self.assertEqual(code, 0, error)
        protocol.manifest['inputs']['files'] = {name: str(root / 'inputs' / name) for name in protocol.manifest['inputs']['files']}
        web = protocol.transport()
        api = Mock()
        api.submit.return_value = '1234567891'
        cookie = self.root / 'synthetic.cookies.json'
        atomic_json(cookie, {'version': 1, 'cookies': []})
        with patch('charmm_gui_cli.pipeline.build_command.load_token', return_value=('synthetic-token', str(self.root / 'session.token'))), \
             patch('charmm_gui_cli.pipeline.build_command.Client', return_value=api), \
             patch('charmm_gui_cli.remote.web.WebClient', return_value=web), patch('charmm_gui_cli.remote.sessions.bind'):
            code, output, error = self.call(['build', '--from-check', root.name, '--foreground', '--no-wait', '--no-grompp',
                '--no-preview', '--cookies', str(cookie)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['state'], 'bilayer_submitted')
        self.assertTrue((root / '.charmm-gui/preparation/fragment-proof.json').is_file())
        api.submit.assert_called_once()
        self.assertEqual(sum(call.args[0] == 'POST' for call in web.request.call_args_list), 3)

    def test_checked_task_cannot_export_an_unbuilt_model(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        code, _, error = self.call(['jobs', 'export', root.name])
        self.assertEqual(code, 2, error)
        self.assertIn('no final model', error)
        self.assertFalse((root / 'charmm-gui.tgz').exists())

    def test_multicomponent_handoff_keeps_every_sdf_and_bound_component_coordinate(self):
        from tests import test_multicomponent as component_fixtures
        fixture = component_fixtures.MulticomponentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # Use distinct noncovalent bound positions within the supported 6 A range.
        for index, line in enumerate(fixture.lines):
            if line.startswith('HETATM'):
                oxygen = line[12:16].strip() == 'O1'
                xyz = (1.2 if oxygen else 0., 4.5, 0.) if line[17:20] == 'LIG' else (5.7 if oxygen else 4.5, 0., 0.)
                fixture.lines[index] = line[:30] + ''.join(f'{x:8.3f}' for x in xyz) + line[54:]
        fixture.write_source()
        config = self.root / 'components.yaml'
        config.write_text(yaml.safe_dump({'version': 1, 'components': fixture.instances, 'water_policy': 'reject', 'ion_policy': 'reject'}))
        root = self.root / 'multi-check'
        code, output, error = self.call(['check', '--name', root.name, '--out', str(root), '--complex', str(fixture.source),
            '--components', str(config), '--hydrogens', 'preserve', '--no-preview'])
        self.assertEqual(code, 0, error or json.loads(output).get('error'))
        self.assertEqual({row['resname'] for row in json.loads(output)['chemistry']}, {'LIG', 'COF'})
        before = {p.name: digest(p) for p in (root / 'inputs').iterdir()}
        code, _, error = self.build(root)
        self.assertEqual(code, 0, error)
        manifest = read_value(root / '.charmm-gui/full-run.json')
        self.assertTrue(manifest['config']['components_mode'])
        self.assertEqual(set(manifest['config']['component_resnames']), {'LIG', 'COF'})
        for group in manifest['inputs']['manifest']['chemistry_groups']:
            self.assertTrue(Path(group['files']['sdf']).is_relative_to(root / 'inputs'))
            self.assertTrue(Path(group['files']['sdf']).is_file())
        self.assertEqual(before, {p.name: digest(p) for p in (root / 'inputs').iterdir()})

    def test_model_and_pose_selections_and_repairs_are_not_reapplied_to_prepared_inputs(self):
        text = self.f.protein.read_text().removesuffix('END\n')
        self.f.protein.write_text('MODEL        1\n' + text + 'ENDMDL\nMODEL        2\n' + text + 'ENDMDL\nEND\n')
        self.f.ligand.write_text(self.f.ligand.read_text() * 2)
        code, root, _, error = self.check('--model', '2', '--ligand-pose', '2')
        self.assertEqual(code, 0, error)
        original = {p.name: digest(p) for p in (root / 'inputs').iterdir()}
        code, _, error = self.build(root)
        self.assertEqual(code, 0, error)
        manifest = read_value(root / '.charmm-gui/full-run.json')
        self.assertEqual(manifest['config']['input_options']['model'], 2)
        self.assertEqual(manifest['config']['input_options']['ligand_pose'], 2)
        self.assertEqual(original, {p.name: digest(p) for p in (root / 'inputs').iterdir()})

    def test_held_worker_lease_refuses_model_selection_and_checked_inputs_remain_unchanged(self):
        from charmm_gui_cli.store.workers import claim
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        directory = root / '.charmm-gui'
        fd = claim(directory)
        try:
            code, _, error = self.build(root)
            self.assertEqual(code, 2, error)
            self.assertFalse((directory / checked_inputs.REQUEST).exists())
        finally:
            os.close(fd)

    def test_stale_check_status_remains_readonly_and_shows_the_changed_input(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        sdf = root / 'inputs/ligand.sdf'
        sdf.write_text(sdf.read_text() + '\nchanged\n')
        before = {str(p.relative_to(root)): digest(p) for p in root.rglob('*') if p.is_file()}
        code, output, error = self.call(['jobs', 'status', root.name], tty=True)
        self.assertEqual(code, 0, error)
        self.assertIn('Precheck evidence is stale', output)
        self.assertIn('ligand.sdf', output)
        self.assertEqual(before, {str(p.relative_to(root)): digest(p) for p in root.rglob('*') if p.is_file()})

    def test_committed_split_seal_cannot_be_recreated_if_removed(self):
        code, root, _, error = self.check('--split-chain-breaks')
        self.assertEqual(code, 0, error)
        code, _, error = self.build(root)
        self.assertEqual(code, 0, error)
        directory = root / '.charmm-gui'
        path = directory / 'local-input-seal.json'
        path.rename(directory / 'retained-local-input-seal.json')
        with self.assertRaises(ToolError):
            checked_inputs.choose_model(directory)
        self.assertFalse(path.exists())

    def test_model_request_config_and_identity_mutations_cannot_be_accepted(self):
        for mutation in ('request', 'identity'):
            code, root, _, error = self.check()
            self.assertEqual(code, 0, error)
            code, _, error = self.build(root)
            self.assertEqual(code, 0, error)
            directory = root / '.charmm-gui'
            if mutation == 'request':
                path = directory / checked_inputs.REQUEST
                value = read_value(path)
                value['applied_config']['ions']['concentration_M'] = .9
                atomic_json(path, value)
            else:
                path = directory / 'job.json'
                value = read_value(path)
                value['task_id'] = 'f' * 32
                atomic_json(path, value)
            with self.assertRaises(ToolError):
                checked_inputs.choose_model(directory)

    def test_checked_task_participates_in_recoverable_clean_and_restore(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        seal = (root / '.charmm-gui' / checked_inputs.SEAL).read_bytes()
        trash = self.root / 'retained-tasks'
        code, output, error = self.call(['jobs', 'clean', root.name, '--trash-dir', str(trash), '--dry-run'])
        self.assertEqual(code, 0, error)
        self.assertTrue(root.is_dir())
        code, output, error = self.call(['jobs', 'clean', root.name, '--trash-dir', str(trash), '--yes'])
        self.assertEqual(code, 0, error)
        operation = json.loads(output)['operation']
        self.assertFalse(root.exists())
        code, _, error = self.call(['jobs', 'restore', operation, '--yes'])
        self.assertEqual(code, 0, error)
        self.assertEqual((root / '.charmm-gui' / checked_inputs.SEAL).read_bytes(), seal)
        code, _, error = self.build(root)
        self.assertEqual(code, 0, error)

    def test_authentication_failure_is_not_hidden_by_a_successful_local_check(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        with patch('charmm_gui_cli.pipeline.build_command.load_token', return_value=('synthetic-token', str(self.root / 'session.token'))):
            code, _, error = self.call(['build', '--from-check', root.name, '--foreground', '--no-grompp', '--no-preview'])
        self.assertEqual(code, 2, error)
        self.assertIn('saved website session is missing', error)
        code, output, error = self.call(['jobs', 'status', root.name])
        self.assertEqual(code, 0, error)
        value = json.loads(output)
        self.assertEqual(value['execution_summary']['outcome'], 'paused')
        self.assertIn('charmm-gui-cli login', value['next_step'])
        code, output, error = self.call(['jobs', 'status', root.name], tty=True)
        self.assertEqual(code, 0, error)
        self.assertIn('saved website session is missing', output)
        self.assertIn('charmm-gui-cli login', output)
        self.assertNotIn('Local precheck passed (ready to model)', output)

    def test_interrupted_check_retains_files_releases_lease_and_has_local_guidance(self):
        from charmm_gui_cli.store.workers import metadata
        with patch('charmm_gui_cli.chem.precheck.check_inputs', side_effect=KeyboardInterrupt):
            code, root, _, error = self.check()
        self.assertEqual(code, 130, error)
        self.assertIn('Local precheck was interrupted', error)
        self.assertNotIn('jobs resume NAME', error)
        self.assertTrue((root / '.charmm-gui/full-run.json').is_file())
        self.assertEqual(metadata(root / '.charmm-gui')['exit_code'], 130)
        self.assertFalse(metadata(root / '.charmm-gui')['running'])

    def test_input_check_receipt_binds_final_validation_cache_without_affecting_old_jobs(self):
        from charmm_gui_cli.pipeline import managed
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        code, _, error = self.build(root)
        self.assertEqual(code, 0, error)
        directory = root / '.charmm-gui'
        manifest = read_value(directory / 'full-run.json')
        archive = root / 'charmm-gui.tgz'
        archive.write_bytes(b'unit-test-only archive binding; no physical acceptance claimed')
        with patch.object(managed, '_archive_binding', return_value={'archive_provenance': {'archive_sha256': digest(archive)}}):
            evidence = managed._evidence(manifest, archive, False, 'gmx')
        _, seal, request = checked_inputs.verify(directory)
        self.assertEqual(evidence['input_check_binding'], {'check_fingerprint': seal['fingerprint'], 'request_fingerprint': request['fingerprint']})
        report_path = directory / checked_inputs.REPORT
        report = read_value(report_path)
        report['passed'] = False
        atomic_json(report_path, report)
        with patch.object(managed, '_archive_binding', return_value={'archive_provenance': {'archive_sha256': digest(archive)}}), self.assertRaises(ToolError):
            managed._evidence(manifest, archive, False, 'gmx')

    def test_orphan_check_marker_and_nonmapping_requests_fail_closed_without_traceback(self):
        code, root, _, error = self.check()
        self.assertEqual(code, 0, error)
        code, _, error = self.build(root)
        self.assertEqual(code, 0, error)
        directory = root / '.charmm-gui'
        request = read_value(directory / checked_inputs.REQUEST)
        request['applied_config'] = 'RAW_SECRET'
        body = {key: value for key, value in request.items() if key != 'fingerprint'}
        request['fingerprint'] = checked_inputs._hash(body, directory)
        atomic_json(directory / checked_inputs.REQUEST, request)
        code, _, error = self.build(root)
        self.assertEqual(code, 2, error)
        self.assertNotIn('RAW_SECRET', error)
        manifest = read_value(directory / 'full-run.json')
        manifest.pop('input_check')
        atomic_json(directory / 'full-run.json', manifest)
        with self.assertRaises(ToolError):
            checked_inputs.choose_model(directory)


if __name__ == '__main__':
    unittest.main()
