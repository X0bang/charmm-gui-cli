"""Provider queue states must never become fresh input-staging requests."""

import json
import unittest
from unittest.mock import patch

from charmm_gui_cli.pipeline import batch, build_command
from charmm_gui_cli.store.auth import atomic_json
from charmm_gui_cli.store.paths import read_value
import test_batch_workflow as fixtures


STAGING_ERROR = "Input staging for this build already exists; inspect it before starting a new run."


class RemotePendingBatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BatchWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.assertEqual(self.fixture.plan()[0], 0)
        self.queue = self.fixture.directory
        self.manifest = self.queue / 'batch.json'
        self.first = self.queue / 'jobs/first/.charmm-gui'
        self.inputs = self.first.parent / 'inputs'
        self.original = {p.name: p.read_bytes() for p in self.inputs.iterdir() if p.is_file()}

    def submitted(self, *, failed=False, row_started=True):
        value = read_value(self.first / 'full-run.json')
        value.update(state='pending', source_job_id='123', build_job_id='456')
        atomic_json(self.first / 'full-run.json', value)
        (self.first / 'bilayer').mkdir(exist_ok=True)
        atomic_json(self.first / 'bilayer/run.json', {'state': 'pending', 'job_id': '456'})
        manifest = read_value(self.manifest)
        row = manifest['jobs'][0]
        row.update(state='local_failed' if failed else 'pending', started=row_started)
        if row_started:
            row.update(source_job_id='123', build_job_id='456')
        if failed:
            row.update(error=STAGING_ERROR, local_failure_phase='preparation',
                       error_detail={'message': STAGING_ERROR, 'category': 'operation_error', 'retryable': False})
        atomic_json(self.manifest, manifest)

    def continuing(self, args):
        self.assertEqual(args.command, 'build-resume')
        self.assertEqual(args.directory, self.first)
        self.assertFalse(args.dry_run)
        return {'state': 'pending', 'source_job_id': '123', 'build_job_id': '456'}, 0

    def test_multiple_public_resume_rounds_retain_pending_child_and_active_slot(self):
        self.submitted()
        with patch.object(build_command, 'execute', side_effect=self.continuing) as execute, \
                patch('requests.Session.request', side_effect=AssertionError('No network in scheduler regression')):
            for _ in range(3):
                code, output, errors = self.fixture.fixture.call(['batch-resume', str(self.queue), '--no-wait',
                    '--max-active', '1', '--submit-interval', '0'])
                self.assertEqual(code, 0, errors)
                value = json.loads(output)
                self.assertEqual(value['active'], 1)
                self.assertEqual(value['failed'], 0)
                self.assertEqual([r['state'] for r in value['jobs']], ['pending', 'inputs_ready'])
            self.assertEqual(execute.call_count, 3)
        self.assertEqual(self.original, {p.name: p.read_bytes() for p in self.inputs.iterdir() if p.is_file()})

    def test_durable_submission_prevents_staging_even_when_outer_row_is_old(self):
        self.submitted(row_started=False)
        with patch.object(build_command, 'execute', side_effect=self.continuing) as execute:
            result, code = batch.advance(self.queue, wait=False, max_active=1, submit_interval=0)
        self.assertEqual(code, 0)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(result['jobs'][0]['build_job_id'], '456')
        self.assertEqual(result['jobs'][1]['state'], 'inputs_ready')

    def test_specific_historical_misclassification_adopts_same_id_without_rewriting_evidence(self):
        self.submitted(failed=True)
        before = self.manifest.read_bytes()
        proof = {p: p.read_bytes() for p in (self.first / 'full-run.json', self.first / 'bilayer/run.json')}
        report = batch.status(self.queue)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(report['failed'], 0)
        self.assertEqual(report['active'], 1)
        self.assertEqual(report['jobs'][0]['scheduling_recoveries'][0]['previous_error'], STAGING_ERROR)
        for lang, text in (('zh', '继续已有建膜任务 456'), ('en', 'continuing existing membrane job 456')):
            code, output, error = self.fixture.fixture.call(['batch-status', str(self.queue)], lang=lang, tty=True)
            self.assertEqual(code, 0, error)
            self.assertIn(text, output)
        with patch.object(build_command, 'execute', side_effect=self.continuing):
            result, code = batch.advance(self.queue, wait=False, max_active=1, submit_interval=0)
        self.assertEqual(code, 0)
        self.assertEqual(result['jobs'][0]['state'], 'pending')
        self.assertEqual(len(result['jobs'][0]['scheduling_recoveries']), 1)
        self.assertEqual(proof, {p: p.read_bytes() for p in proof})

    def test_unrelated_or_uncertain_records_are_not_recovered_as_queued(self):
        for defect in ('row_id', 'full_id', 'unknown', 'intent', 'unreadable', 'unrelated', 'failed_remote'):
            with self.subTest(defect=defect):
                self.submitted(failed=True)
                row = read_value(self.manifest)['jobs'][0]
                if defect == 'row_id': row['build_job_id'] = '789'
                elif defect == 'unrelated': row['error'] = 'A different staging failure'
                elif defect == 'full_id':
                    full = read_value(self.first / 'full-run.json')
                    full['build_job_id'] = '789'
                    atomic_json(self.first / 'full-run.json', full)
                elif defect in ('unknown', 'failed_remote'):
                    atomic_json(self.first / 'bilayer/run.json', {'job_id': '456',
                        'state': 'submission_unknown' if defect == 'unknown' else 'remote_error'})
                elif defect == 'intent':
                    intent = self.first / 'bilayer/submit.intent.json'
                    atomic_json(intent, {'state': 'submission_unknown'})
                elif defect == 'unreadable':
                    (self.first / 'bilayer/run.json').write_text('not JSON')
                self.assertFalse(batch._recover_queued_child(row))
                self.assertEqual(row['state'], 'local_failed')
                self.assertNotIn('scheduling_recoveries', row)
                if defect == 'intent': intent.rename(intent.with_suffix('.retained'))


if __name__ == '__main__':
    unittest.main()
