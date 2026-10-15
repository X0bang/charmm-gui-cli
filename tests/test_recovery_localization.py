"""Bilingual recovery commands retain legacy JSON and uncertain-POST behavior."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
import ast
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import yaml

from charmm_gui_cli.cli import main, parser
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.pipeline import workflow, managed, full_build
from charmm_gui_cli.remote.api import SubmissionUnknown
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.paths import read_value
from tests import test_client as fixtures


class RecoveryLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='cgui-recovery-i18n-')).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(set_language)
        self.stack.enter_context(patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'data')}))
        self.stack.enter_context(patch('pathlib.Path.home', return_value=self.root / 'home'))
        self.network = self.stack.enter_context(patch('requests.Session.request', side_effect=AssertionError('No HTTP expected')))

    def call(self, args, language='zh'):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(['--lang', language, *args])
        return code, output.getvalue(), errors.getvalue()

    def task(self, name='任务 with spaces', *, state='submitted', job='456', managed_root=False):
        root = self.root / name
        directory = root / '.charmm-gui/bilayer' if managed_root else root
        directory.mkdir(parents=True)
        if managed_root:
            atomic_json(root / '.charmm-gui/full-run.json', {'schema_version': 2})
        manifest = {'schema_version': 1, 'state': state, 'job_id': job, 'config': fixtures.config()}
        atomic_json(directory / 'run.json', manifest)
        return directory, manifest

    def assert_bilingual(self, error, zh, en):
        self.assertIsNotNone(error.key)
        set_language('zh')
        self.assertIn(zh, error.localized())
        set_language('en')
        self.assertIn(en, error.localized())
        self.assertNotIn('RAW_SECRET', error.localized())
        self.assertTrue(error.next_step)
        self.assertNotIn(' NAME', error.next_step)
        self.assertNotEqual(error.next_step, 'charmm-gui-cli -h')

    def test_runtime_and_component_errors_use_real_public_cli_before_authentication(self):
        cases = [(['--interval', '1'], '至少为 10 秒', 'at least 10 seconds'),
                 (['--max-wait', '0'], '正数秒', 'positive number of seconds'),
                 (['--components', str(self.root / '组分 settings.yaml')], '--components 必须', '--components requires')]
        for flags, zh, en in cases:
            for language, text in (('zh', zh), ('en', en)):
                with patch('charmm_gui_cli.pipeline.build_command.load_token') as auth:
                    code, _, error = self.call(['build', '--foreground', '--protein', 'protein.pdb', '--ligand', 'ligand.sdf', *flags], language)
                self.assertEqual(code, 2, error)
                self.assertIn(text, error)
                self.assertNotIn('charmm-gui-cli -h', error)
                auth.assert_not_called()
        self.network.assert_not_called()

    def test_preparation_blockers_are_bilingual_without_changing_legacy_plan_or_error(self):
        path = self.root / 'API settings.yaml'
        raw = fixtures.config()
        raw['source_job_id'] = None
        raw['preparation']['ligand_parameters_ready'] = False
        path.write_text(yaml.safe_dump(raw))
        proposal = workflow.plan(raw)
        legacy = 'Preparation incomplete: ' + ' '.join(proposal['blockers'])
        for language, text in (('zh', '前处理尚未完成'), ('en', 'Preparation is incomplete')):
            with patch('charmm_gui_cli.store.auth.load_token', return_value=(fixtures.token(), str(self.root / 'session.token'))):
                code, _, error = self.call(['run', str(path), '--out', str(self.root / 'not-created')], language)
            self.assertEqual(code, 2, error)
            self.assertIn(text, error)
            self.assertIn('source_job_id', error)
            self.assertIn('ligand_parameters_ready: true', error)
            command = shlex.join(['charmm-gui-cli', 'plan', str(path)])
            self.assertIn(command, error)
            code, output, _ = self.call(shlex.split(command)[1:], language)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)['blockers'], proposal['blockers'])
        client = Mock()
        with self.assertRaises(ToolError) as caught:
            workflow.start(client, raw, self.root / 'new')
        self.assertEqual(caught.exception.as_dict()['message'], legacy)
        self.assertEqual(caught.exception.category, 'operation_error')
        self.assertFalse(caught.exception.retryable)
        client.validate_auth.assert_not_called()
        client.submit.assert_not_called()
        self.assertFalse((self.root / 'not-created').exists())

    def test_unknown_server_status_is_never_echoed_or_classified_as_complete(self):
        with self.assertRaises(ToolError) as caught:
            workflow.status_summary({'status': 'RAW_SECRET'})
        self.assert_bilingual(caught.exception, '无法识别', 'unrecognized')
        self.assertEqual(caught.exception.category, 'unexpected_response')
        self.assertEqual(caught.exception.as_dict()['message'], 'Unrecognized job status; cannot infer completion or successful authentication.')
        self.assertNotIn('RAW_SECRET', json.dumps(caught.exception.as_dict()))

    def test_existing_advanced_directory_is_retained_and_gets_a_quoted_inspection_command(self):
        directory = self.root / 'existing folder'
        directory.mkdir()
        marker = directory / 'user-file.txt'
        marker.write_text('untouched')
        client = Mock()
        with self.assertRaises(ToolError) as caught:
            workflow.start(client, fixtures.config(), directory)
        self.assert_bilingual(caught.exception, '目录已存在', 'directory already exists')
        self.assertEqual(shlex.split(caught.exception.next_step), ['ls', '-l', str(directory / 'run.json')])
        self.assertEqual(marker.read_text(), 'untouched')
        client.submit.assert_not_called()

    def test_missing_session_guidance_names_the_existing_task_without_exposing_token(self):
        directory = self.root / 'resume with spaces'
        directory.mkdir()
        atomic_json(directory / 'full-run.json', {'schema_version': 2, 'state': 'inputs_ready', 'inputs': {'files': {}}})
        for language, text in (('zh', '缺少已保存的网页会话'), ('en', 'saved website session is missing')):
            with patch('charmm_gui_cli.pipeline.build_command.load_token', return_value=('RAW_SECRET', str(self.root / 'session.token'))):
                code, _, error = self.call(['build-resume', str(directory), '--foreground'], language)
            self.assertEqual(code, 2, error)
            self.assertIn(text, error)
            self.assertIn('charmm-gui-cli login', error)
            self.assertIn(shlex.join(['charmm-gui-cli', 'jobs', 'resume', str(directory)]), error)
            self.assertNotIn('RAW_SECRET', error)
        self.network.assert_not_called()

    def test_clone_identity_unknown_is_retained_and_never_automatically_submitted_again(self):
        directory = self.root / 'clone unknown'
        client = Mock()
        client.submit.return_value = '123'
        with self.assertRaises(SubmissionUnknown) as caught:
            workflow.start(client, fixtures.config(), directory)
        self.assert_bilingual(caught.exception, '返回了源任务编号', 'returned the source job ID')
        record = read_value(directory / 'run.json')
        self.assertEqual(record['state'], 'submission_unknown')
        self.assertEqual(record['last_error']['category'], 'submission_unknown')
        self.assertEqual(record['last_error']['message_key'], 'workflow.clone_identity_unknown')
        with self.assertRaises(SubmissionUnknown) as resumed:
            workflow.resume(client, directory)
        self.assertEqual(resumed.exception.key, 'workflow.submission_unknown')
        client.submit.assert_called_once()
        client.status.assert_not_called()
        self.network.assert_not_called()

    def test_interrupted_post_keeps_unknown_semantics_and_localized_saved_error(self):
        client = Mock()
        client.submit.side_effect = KeyboardInterrupt
        directory = self.root / 'interrupted POST'
        with self.assertRaises(KeyboardInterrupt):
            workflow.start(client, fixtures.config(), directory)
        record = read_value(directory / 'run.json')
        self.assertEqual(record['state'], 'submission_unknown')
        self.assertEqual(record['last_error']['message'], 'Interrupted during submission; inspect website jobs and attach before resuming.')
        self.assertEqual(record['last_error']['message_key'], 'workflow.post_interrupted')
        self.assertFalse(record['last_error']['retryable'])
        self.assertEqual(shlex.split(record['last_error']['next_step']), ['ls', '-l', str(directory / 'run.json')])
        client.submit.assert_called_once()

    def test_managed_child_recovery_points_to_its_full_task_without_api_root_leak(self):
        directory, _ = self.task(managed_root=True)
        command = shlex.split(workflow._local_action(directory))
        self.assertEqual(command[:3], ['charmm-gui-cli', 'jobs', 'status'])
        self.assertEqual(command[3], str(directory.parent.parent))
        parser().parse_args(command[1:])
        command = shlex.split(workflow._local_action(directory, resume=True))
        self.assertEqual(command[:3], ['charmm-gui-cli', 'jobs', 'resume'])
        self.assertEqual(command[3], str(directory.parent.parent))
        parser().parse_args(command[1:])

    def test_waiting_archive_uses_bilingual_messages_and_exact_retry_metadata_without_get(self):
        directory, manifest = self.task()
        manifest['archive_retry_at_unix'] = time.time() + 600
        client = Mock()
        for wait, deadline, key in ((False, time.monotonic()+60, 'workflow.archive_waiting'),
                                    (True, time.monotonic()-1, 'workflow.archive_wait_limit')):
            with self.assertRaises(ToolError) as caught:
                workflow._download_archive(client, directory, manifest, directory / 'missing.tgz', wait=wait, deadline=deadline, progress=None)
            self.assert_bilingual(caught.exception, '归档', 'archive')
            self.assertEqual(caught.exception.key, key)
            self.assertEqual(caught.exception.category, 'archive_preparing')
            self.assertTrue(caught.exception.retryable)
            self.assertGreater(caught.exception.retry_after_seconds, 500)
            self.assertEqual(shlex.split(caught.exception.next_step), ['charmm-gui-cli', 'resume', str(directory), '--wait'])
        client.download.assert_not_called()
        client.status.assert_not_called()
        client.submit.assert_not_called()

    def test_remote_failure_and_wait_timeout_keep_distinct_categories_and_never_post(self):
        for remote, category, key in (('error', 'remote_failure', 'workflow.remote_failed'),
                                       ('running', 'wait_timeout', 'workflow.wait_limit')):
            directory, _ = self.task(name=remote)
            client = Mock()
            client.status.return_value = {'status': remote}
            with patch('charmm_gui_cli.pipeline.workflow.time.monotonic', side_effect=[0, 2]), self.assertRaises(ToolError) as caught:
                workflow.resume(client, directory, wait=True, max_wait=1)
            self.assertEqual(caught.exception.category, category)
            self.assertEqual(caught.exception.key, key)
            self.assertTrue(caught.exception.next_step)
            self.assertEqual(caught.exception.retryable, remote == 'running')
            self.assertEqual(read_value(directory / 'run.json')['last_error']['message_key'], key)
            client.status.assert_called_once_with('456')
            client.submit.assert_not_called()

    def test_attach_refusal_remains_local_and_includes_inspection_for_the_right_directory(self):
        for state, job, identifier, key in (('submitted', '456', '789', 'workflow.attach_ineligible'),
                                           ('submission_unknown', None, '123', 'workflow.attach_source')):
            directory, _ = self.task(name=key, state=state, job=job)
            client = Mock()
            with self.assertRaises(ToolError) as caught:
                workflow.attach(client, directory, identifier)
            self.assertEqual(caught.exception.key, key)
            self.assertEqual(shlex.split(caught.exception.next_step), ['ls', '-l', str(directory / 'run.json')])
            client.status.assert_not_called()
            client.submit.assert_not_called()

    def test_lineage_and_incomplete_input_errors_preserve_legacy_payloads(self):
        root = self.root / 'incomplete build'
        directory = root / '.charmm-gui'
        directory.mkdir(parents=True)
        archive = root / 'charmm-gui.tgz'
        with self.assertRaises(ToolError) as caught:
            managed._archive_binding({}, archive)
        self.assert_bilingual(caught.exception, '缺少完整', 'requires complete')
        self.assertEqual(caught.exception.key, 'validation.lineage_incomplete')
        self.assertEqual(caught.exception.as_dict()['message'], 'Archive provenance requires complete source/build job IDs and readable bilayer job lineage.')
        archive.write_bytes(b'retained but not validated')
        atomic_json(directory / 'full-run.json', {'schema_version': 2, 'state': 'ligands_present'})
        with self.assertRaises(ToolError) as caught:
            managed.finalize_managed_build(directory)
        self.assert_bilingual(caught.exception, '缺少最终验收', 'lacks readable inputs')
        self.assertEqual(caught.exception.key, 'validation.inputs_unreadable')
        self.assertIn(str(root), caught.exception.localized())
        self.assertEqual(caught.exception.as_dict()['message'], 'Completed build is missing readable input files/configuration required for final validation.')
        self.assertEqual(archive.read_bytes(), b'retained but not validated')

    def test_staging_and_blocked_validation_are_localized_without_repairing_evidence(self):
        output = self.root / 'old staging'
        staging = self.root / '.old staging.sources'
        staging.mkdir()
        marker = staging / 'original.txt'
        marker.write_text('keep')
        with self.assertRaises(ToolError) as caught:
            managed.create_managed_build(output=output)
        self.assert_bilingual(caught.exception, '输入暂存目录已存在', 'staging directory already exists')
        self.assertEqual(marker.read_text(), 'keep')
        self.assertFalse(output.exists())
        directory, _ = self.task(name='blocked')
        for state in ('submission_unknown', 'remote_error', 'preparation_failed'):
            atomic_json(directory / 'full-run.json', {'schema_version': 2, 'state': state})
            with self.assertRaises(ToolError) as caught:
                managed.finalize_managed_build(directory)
            self.assert_bilingual(caught.exception, '需先处理该失败', 'recover this failure')
            self.assertEqual(caught.exception.params, {'state': state})
            self.assertEqual(caught.exception.category, 'operation_error')

    def test_recovery_orchestrators_require_message_keys_including_dynamic_wrappers(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('pipeline/workflow.py', 'pipeline/managed.py', 'pipeline/full_build.py', 'pipeline/build_command.py'):
            path = root / 'charmm_gui_cli' / name
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('ToolError', 'AuthError', 'SubmissionUnknown'):
                    self.assertIn('key', {keyword.arg for keyword in node.keywords}, f'{name}:{node.lineno}')

    def test_full_manifest_errors_are_specific_bilingual_and_do_not_repair_the_record(self):
        directory = self.root / 'manifest with spaces'
        directory.mkdir()
        path = directory / 'full-run.json'
        for value, key, zh, en in ((None, 'build.manifest_unreadable', '无法读取任务记录', 'Cannot read'),
                                   ({'schema_version': 999}, 'build.manifest_version', '版本不受支持', 'version is unsupported'),
                                   ({'schema_version': 2}, 'build.inputs_incomplete', '准备尚未完成', 'preparation is incomplete')):
            if value is not None:
                atomic_json(path, value)
            before = path.read_bytes() if path.exists() else None
            with self.assertRaises(ToolError) as caught:
                full_build._read(directory)
            self.assert_bilingual(caught.exception, zh, en)
            self.assertEqual(caught.exception.key, key)
            self.assertEqual(path.read_bytes() if path.exists() else None, before)

    def test_every_component_refusal_still_happens_before_authentication_or_upload(self):
        sdf = self.root / 'chemistry.sdf'
        sdf.write_text('retained fixture; not submitted')
        group = {'resname': 'LIG', 'mode': 'cgenff_required', 'instances': [{}], 'files': {'sdf': str(sdf)}}
        cases = [(None, None, 'build.groups_missing', '缺少明确', 'lacks explicit'),
                 ([None], None, 'build.group_invalid', '定义无效', 'definition is invalid'),
                 ([{**group, 'resname': 'RAW_SECRET'}], None, 'build.group_names_invalid', '互不重复', 'unique residue names'),
                 ([{**group, 'mode': 'trusted_charmm'}], None, 'build.trusted_upload_unsupported', '不支持远端上传', 'upload is unsupported'),
                 ([{**group, 'mode': 'RAW_SECRET'}], None, 'build.group_mode_invalid', 'CGenFF 化学定义', 'explicit CGenFF chemistry'),
                 ([{**group, 'instances': []}], None, 'build.group_instances_unsupported', '一个实例', 'one instance'),
                 ([{**group, 'files': {}}], None, 'build.group_sdf_missing', 'SDF 文件', 'SDF file'),
                 ([{**group, 'files': {'sdf': str(self.root / 'missing.sdf')}}], None, 'build.group_sdf_unrecorded', 'SDF 文件缺失', 'SDF is missing'),
                 ([group], ['OTHER'], 'build.group_names_mismatch', '残基名与', 'residue names disagree')]
        for index, (groups, declared, key, zh, en) in enumerate(cases):
            directory = self.root / ('component-' + str(index))
            directory.mkdir()
            config = {'components_mode': True}
            if declared is not None:
                config['component_resnames'] = declared
            manifest = {'schema_version': 2, 'state': 'inputs_ready', 'config': config,
                        'inputs': {'manifest': {'chemistry_groups': deepcopy(groups)}, 'files': {'ligand.sdf': str(sdf)}}}
            atomic_json(directory / 'full-run.json', manifest)
            web, api = Mock(), Mock()
            with self.assertRaises(ToolError) as caught:
                full_build.advance(directory, web, api)
            self.assert_bilingual(caught.exception, zh, en)
            self.assertEqual(caught.exception.key, key)
            self.assertEqual(caught.exception.category, 'unsupported_feature' if key == 'build.trusted_upload_unsupported' else 'operation_error')
            self.assertEqual(web.method_calls, [])
            self.assertEqual(api.method_calls, [])
            self.assertEqual(read_value(directory / 'full-run.json')['last_error']['message_key'], key)

    def test_uncertain_full_build_post_precedes_new_input_checks_and_never_sends_request(self):
        directory = self.root / 'unknown full task'
        directory.mkdir()
        manifest = {'schema_version': 2, 'state': 'submission_unknown',
                    'config': {'input_options': {'split_chain_breaks': True}},
                    'inputs': {'manifest': {}, 'files': {}}}
        with self.assertRaises(SubmissionUnknown) as caught:
            full_build.require_protein_state_support(directory, manifest)
        self.assert_bilingual(caught.exception, '提交结果尚未确认', 'unresolved submission intent')
        self.assertEqual(caught.exception.key, 'build.post_unknown')
        self.assertEqual(caught.exception.category, 'submission_unknown')
        self.assertFalse(caught.exception.retryable)
        target = directory / 'preparation/parameterization.html'
        atomic_json(target.with_suffix('.intent.json'), {'state': 'submitting'})
        web = Mock()
        with self.assertRaises(SubmissionUnknown) as caught:
            full_build._submit(web, 'unused', target, {})
        self.assertEqual(caught.exception.key, 'build.post_unknown')
        self.assertIn(str(target.with_suffix('.intent.json')), caught.exception.next_step)
        web.submit_snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
