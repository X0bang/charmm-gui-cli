"""Readable settings are derived from records, not promoted from requests."""

import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr

from charmm_gui_cli.cli import main
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.store.auth import atomic_json
from charmm_gui_cli.store.settings import parameter_summary
from charmm_gui_cli.store.result_files import _system_configuration
from charmm_gui_cli.presentation.parameters import parameter_lines
from charmm_gui_cli.presentation.reporting import display_result, render_text, render_html
from charmm_gui_cli.presentation.terminal_output import format_build
import test_single_directory as single_directory
import test_input_policy as input_policy


class ParameterSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='cgui-parameter-view-')).resolve()
        self.manifest = {'source_job_id': '123', 'build_job_id': '456', 'config': {
            'membrane': {'upper': 'POPC:POPE=7:3', 'lower': 'POPC=1', 'margin_A': 20, 'water_padding_A': 22.5},
            'ions': {'type': 'KCl', 'concentration_M': .15},
            'preparation': {'orientation': 'ppm', 'n_terminal': 'NTER', 'c_terminal': 'CTER',
                            'terminals': {'A': {'n_terminal': 'NTER'}, 'B': {'n_terminal': 'PROP'}}}}}
        self.addCleanup(set_language)

    def intent(self, *, state='response_received', fields=None, identifier='123', directory=None):
        directory = directory or self.root
        path = directory / 'preparation/parameterization.intent.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        values = [['jobid', identifier], ['terminal[PROA][first]', 'GLYP'], ['terminal[PROA][last]', 'CTER'],
                  ['terminal[PROB][first]', 'PROP'], ['terminal[PROB][last]', 'CTER']]
        atomic_json(path, {'state': state, 'fields': values if fields is None else fields})
        return path

    def api(self, *, state='submitted', concentration=.15):
        path = self.root / 'bilayer/run.json'
        path.parent.mkdir(exist_ok=True)
        config = copy.deepcopy(self.manifest['config'])
        config['source_job_id'] = '123'
        config['ions']['concentration_M'] = concentration
        form = {'jobid': '123', 'upper': 'POPC:POPE=7:3', 'lower': 'POPC=1', 'margin': '20', 'wdist': '22.5',
                'ion_type': 'KCl', 'ion_conc': str(concentration), 'ppm': 'true'}
        value = {'state': state, 'job_id': None if state == 'submission_unknown' else '456', 'config': config,
                 'plan': {'request': {'method': 'POST', 'endpoint': '/quick_bilayer', 'form': form}}}
        atomic_json(path, value)
        return path, value

    def test_view_has_readable_keys_ratios_and_preserves_original_requested_applied_fields(self):
        self.intent()
        before = copy.deepcopy(self.manifest)
        config = _system_configuration(self.root, self.manifest)
        self.assertEqual(config['requested'], before['config'])
        self.assertEqual(config['applied']['preparation']['terminals']['terminal[PROA][first]'], 'GLYP')
        summary = config['parameter_summary']
        self.assertEqual(summary['requested']['membrane']['upper'][0], {'lipid': 'POPC', 'parts': 7., 'fraction': .7})
        row = summary['submission']['preparation']['chains'][0]
        self.assertEqual(row['chain'], 'PROA')
        self.assertEqual((row['requested_n_terminal'], row['n_terminal']), ('NTER', 'GLYP'))
        self.assertEqual(self.manifest, before)
        self.assertNotIn('terminal[', json.dumps(summary))

    def test_zh_en_text_html_and_terminal_show_explicit_changes_without_raw_form_fields(self):
        self.intent()
        self.api()
        for lang, label in (('zh', '已提交的末端设置'), ('en', 'Submitted terminal settings')):
            set_language(lang)
            view = parameter_summary(self.manifest, self.root)
            value = {'state': 'validated', 'passed': True, 'parameter_summary': view}
            displayed = display_result({'name': 'model-a'}, {'directory': str(self.root)}, self.manifest, {})
            texts = (format_build(value), render_text(displayed), render_html(displayed))
            for text in texts:
                self.assertIn(label, text)
                self.assertIn('NTER → GLYP', text)
                self.assertIn('POPC:POPE=7:3', text)
                self.assertIn('70%:30%', text)
                self.assertNotIn('terminal[', text)

    def test_unknown_post_and_adjustment_only_are_never_described_as_submitted(self):
        self.intent(state='submission_unknown')
        self.api(state='submission_unknown')
        for lang, phrase in (('zh', '提交待核对'), ('en', 'needs reconciliation')):
            set_language(lang)
            view = parameter_summary(self.manifest, self.root)
            text = '\n'.join(parameter_lines(view))
            self.assertIn(phrase, text)
            self.assertNotIn('已提交的末端设置' if lang == 'zh' else 'Submitted terminal settings', text)
        pure = copy.deepcopy(self.manifest)
        pure['terminal_adjustments'] = [{'key': 'terminal.adjusted', 'params': {'chain': 'PROA', 'patch': 'GLYP', 'residue': 'Gly'}}]
        summary = parameter_summary(pure)
        self.assertEqual(summary['submission']['preparation']['status'], 'adjustment_record')
        self.assertEqual(summary['submission']['preparation']['chains'][0]['n_terminal'], 'GLYP')

    def test_missing_end_is_not_filled_from_the_requested_default(self):
        self.intent(fields=[['jobid', '123'], ['terminal[PROA][first]', 'GLYP']])
        row = parameter_summary(self.manifest, self.root)['submission']['preparation']['chains'][0]
        self.assertIsNone(row['c_terminal'])
        self.assertEqual(row['requested_c_terminal'], 'CTER')

    def test_duplicate_patches_and_foreign_job_ids_are_visible_as_invalid_not_last_value_wins(self):
        self.intent(fields=[['jobid', '123'], ['terminal[PROA][first]', 'GLYP'], ['terminal[PROA][first]', 'NTER']])
        summary = parameter_summary(self.manifest, self.root)['submission']['preparation']
        self.assertEqual(summary['status'], 'conflicting_record')
        self.assertIsNone(summary['chains'][0]['n_terminal'])
        self.intent(identifier='999')
        self.assertEqual(parameter_summary(self.manifest, self.root)['submission']['preparation']['status'], 'invalid_record')

    def test_membrane_submission_and_requested_values_are_distinct_and_tampering_is_not_confirmed(self):
        path, value = self.api(concentration=.2)
        summary = parameter_summary(self.manifest, self.root)
        self.assertEqual(summary['requested']['ions']['concentration_M'], .15)
        self.assertEqual(summary['submission']['membrane']['settings']['ions']['concentration_M'], .2)
        self.assertEqual(summary['submission']['membrane']['status'], 'response_received')
        value['plan']['request']['form']['ion_conc'] = '.3'
        atomic_json(path, value)
        self.assertEqual(parameter_summary(self.manifest, self.root)['submission']['membrane']['status'], 'conflicting_record')

    def test_sealed_source_and_completed_child_record_remain_readable_without_network_or_mutation(self):
        source = self.root / 'source'
        path = self.intent(directory=source)
        child = self.root / 'child'
        child.mkdir()
        manifest = copy.deepcopy(self.manifest)
        manifest['reused_preparation'] = {'certificate': {'source_directory': str(source), 'source_job_id': '123',
            'evidence_sha256': {'preparation/parameterization.intent.json': hashlib.sha256(path.read_bytes()).hexdigest()}}}
        before = path.read_bytes()
        with patch('requests.Session.request', side_effect=AssertionError('No network')):
            self.assertEqual(parameter_summary(manifest, child)['submission']['preparation']['status'], 'response_received')
        self.assertEqual(path.read_bytes(), before)
        shutil.move(str(source), str(self.root / 'unmounted-source'))
        self.assertEqual(parameter_summary(manifest, child)['submission']['preparation']['status'], 'unavailable')
        manifest['applied_terminals'] = {'terminal[PROA][first]': 'GLYP'}
        value = parameter_summary(manifest, child)['submission']['preparation']
        self.assertEqual(value['status'], 'recorded')
        self.assertEqual(value['chains'][0]['n_terminal'], 'GLYP')

    def test_bad_records_and_secrets_never_leak_into_the_view_or_break_readonly_status(self):
        self.manifest['config'].update(token='PRIVATE_TOKEN', password='PRIVATE_PASSWORD')
        self.manifest['config']['preparation']['terminals'] = {1: {'n_terminal': 'GLYP'}, 'A': {'n_terminal': 'PASSWORD'}}
        path = self.intent(fields=[['jobid', '123'], ['password', 'PRIVATE_PASSWORD'], ['terminal[PROA][first]', 'PRIVATE_TOKEN']])
        value = parameter_summary(self.manifest, self.root)
        self.assertEqual(value['submission']['preparation']['status'], 'invalid_record')
        self.assertIsNone(value['requested']['terminals']['chains'][0]['n_terminal'])
        self.assertNotIn('PRIVATE_', json.dumps(value))
        for lang in ('zh', 'en'):
            set_language(lang)
            self.assertNotIn('PRIVATE_', '\n'.join(parameter_lines(value)))
        path.write_text('not json')
        self.assertEqual(parameter_summary(self.manifest, self.root)['submission']['preparation']['status'], 'invalid_record')

    def test_summary_is_locale_independent_and_chain_rendering_is_bounded(self):
        fields = [['jobid', '123']]
        for i in range(50):
            fields.append(['terminal[P' + str(i) + '][first]', 'NTER'])
        self.intent(fields=fields)
        set_language('zh')
        zh = parameter_summary(self.manifest, self.root)
        set_language('en')
        self.assertEqual(zh, parameter_summary(self.manifest, self.root))
        lines = parameter_lines(zh)
        self.assertLess(len(lines), 35)
        self.assertEqual(len(zh['submission']['preparation']['chains']), 50)
        self.assertIn('30 additional chains', '\n'.join(lines))

    def test_actual_single_directory_delivery_refresh_and_jobs_report_agree(self):
        # The synthetic archive/validation fixture is real ownership evidence;
        # this test exercises display, exports and edits, not new MD acceptance.
        fixture = single_directory.SingleDirectoryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        base = fixture.fixture
        base.manifest['config'] = copy.deepcopy(self.manifest['config'])
        base.save()
        base.publish_report()
        self.intent(directory=fixture.run, identifier='100')
        from charmm_gui_cli.pipeline import delivery
        from charmm_gui_cli.store.paths import read_value
        for lang in ('zh', 'en'):
            set_language(lang)
            with patch('requests.Session.request', side_effect=AssertionError('No network')):
                delivery.publish_in_place(fixture.run)
            text = (fixture.root / 'report.txt').read_text()
            self.assertIn('NTER → GLYP', text)
            settings = json.loads((fixture.root / 'inputs/system-config.json').read_text())
            self.assertEqual(settings['parameter_summary'], parameter_summary(base.manifest, fixture.run))
            saved = read_value(fixture.run / 'full-run.json')
            with patch('charmm_gui_cli.store.tasks.resolve_run', return_value=fixture.run), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                code = main(['--lang', lang, 'jobs', 'report', 'receptor-a', '--format', 'text'])
            self.assertEqual(code, 0)
            self.assertIn('NTER → GLYP', output.getvalue())
            self.assertEqual(read_value(fixture.run / 'full-run.json'), saved)

    def test_cli_dryrun_shows_request_only_and_keeps_existing_json_fields(self):
        fixture = input_policy.InputPolicyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for lang in ('zh', 'en'):
            code, output, error = fixture.call(['build', '--protein', str(fixture.protein), '--ligand', str(fixture.ligand),
                '--out', str(fixture.root / ('run-' + lang)), '--dry-run', '--no-preview'], tty=True, lang=lang)
            self.assertEqual(code, 0, error)
            self.assertIn('POPC=1', output)
            self.assertIn('尚无末端提交记录' if lang == 'zh' else 'No terminal submission record', output)
        code, output, error = fixture.call(['build', '--protein', str(fixture.protein), '--ligand', str(fixture.ligand),
            '--out', str(fixture.root / 'json'), '--dry-run', '--no-preview', '--json'])
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual((data['state'], data['server_submitted']), ('inputs_ready', False))
        self.assertEqual(data['parameter_summary']['submission']['preparation']['status'], 'missing')


if __name__ == '__main__':
    unittest.main()
