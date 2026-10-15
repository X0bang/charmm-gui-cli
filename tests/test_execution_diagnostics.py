"""Historical execution status remains read-only and distinct from remote state."""

import contextlib
import io
import json
import os
import shlex
import unittest
from unittest.mock import patch

from charmm_gui_cli.cli import main
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.pipeline import managed
from charmm_gui_cli.presentation.execution import describe_execution
from charmm_gui_cli.presentation.terminal_output import format_build, format_job_status
from charmm_gui_cli.store import tasks
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.execution_evidence import submission_evidence
from charmm_gui_cli.store.paths import read_value
from charmm_gui_cli.store.workers import claim, metadata, write_lock
import test_jobs as fixtures


class ExecutionDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.JobsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.base,self.run=self.fixture.root,self.fixture.directory
        self.stack=contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(set_language)
        self.stack.enter_context(patch('charmm_gui_cli.store.tasks._registry',return_value=self.base/'index'))
        self.stack.enter_context(patch('charmm_gui_cli.store.tasks._registries',return_value=(self.base/'index',)))
        tasks.assign_name(self.run,'example')

    def save(self,state='bilayer_submitted',**fields):
        atomic_json(self.run/'full-run.json',{'schema_version':2,'state':state,**fields})

    def call(self,args,*,tty=False,language='en'):
        output,errors=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors),patch.object(output,'isatty',return_value=tty):
            code=main(['--lang',language,*args])
        return code,output.getvalue(),errors.getvalue()

    def snapshot(self):
        return {str(p.relative_to(self.base)):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def test_legacy_remote_failure_overrides_old_local_classification_without_rewriting_it(self):
        self.save(state='bilayer_submitted',execution_state='failed_local')
        event={'active':True,'error':{'category':'remote_failure','message':'Remote building failed.',
                                     'retryable':False,'next_step':None},'execution_state':'failed_local'}
        atomic_json(self.run/'recovery.json',event)
        before=self.snapshot()
        for language,label in (('en','failed-remote'),('zh','远端失败')):
            for args in (['jobs','status','example'],['jobs','list']):
                code,output,error=self.call(args,tty=True,language=language)
                self.assertEqual(code,0,error)
                self.assertIn('Remote failure' if language=='en' and args[1]=='status' else label,output)
                self.assertNotIn('NAME',output)
                self.assertNotIn('Local failure',output)
        code,output,error=self.call(['jobs','status','example','--json'])
        self.assertEqual(code,0,error)
        result=json.loads(output)
        self.assertEqual(result['state'],'bilayer_submitted')
        self.assertEqual(result['execution_state'],'failed_local')
        self.assertEqual(result['recovery'],event)
        self.assertEqual(result['execution_summary']['outcome'],'failed_remote')
        self.assertEqual(before,self.snapshot())

    def test_legacy_local_failure_and_unobserved_running_stages_are_not_running(self):
        self.save(state='parameterizing',last_error={'message':'Unsupported component.', 'category':'operation_error','retryable':False})
        status=managed.inspect_run('example')
        self.assertEqual(status['execution_summary']['outcome'],'failed_local')
        self.save(state='running',build_job_id='123')
        (self.run/'bilayer').mkdir()
        atomic_json(self.run/'bilayer/run.json',{'state':'running','job_id':'123','remote_status':'running'})
        before=self.snapshot()
        code,output,error=self.call(['jobs','status','example'],tty=True)
        self.assertEqual(code,0,error)
        self.assertIn('stopped (local)',output)
        self.assertIn('Last recorded remote state (not queried now)',output)
        self.assertIn('jobs resume example',output)
        self.assertEqual(managed.inspect_run('example')['state'],'running')
        self.assertEqual(before,self.snapshot())

    def test_current_remote_failure_is_saved_correctly_but_http_failure_does_not_prove_model_failure(self):
        self.save()
        tasks.record_recovery(self.run,ToolError('Remote failed',category='remote_failure'))
        self.assertEqual(read_value(self.run/'recovery.json')['execution_state'],'failed_remote')
        self.assertEqual(managed.inspect_run('example')['execution_summary']['outcome'],'failed_remote')
        self.save()
        tasks.record_recovery(self.run,ToolError('HTTP request failed',category='http_error'))
        result=managed.inspect_run('example')
        self.assertEqual(result['execution_summary']['outcome'],'paused')
        self.assertEqual(result['execution_summary']['reason_key'],'execution.reason.request_failed')

    def test_pending_post_with_live_lease_is_running_but_dead_or_unknown_is_not(self):
        self.save(state='parameterizing')
        path=self.run/'preparation/parameterization.intent.json'
        atomic_json(path,{'state':'submitting','body':'BODY_SECRET','token':'TOKEN_SECRET'})
        self.assertEqual(managed.inspect_run('example')['execution_summary']['outcome'],'submission_unknown')
        fd=claim(self.run)
        try:
            write_lock(fd,{'pid':os.getpid(),'started_at':'2026-10-15T00:00:00+00:00'})
            value=managed.inspect_run('example')
            self.assertTrue(metadata(self.run)['running'])
            self.assertEqual(value['execution_summary']['outcome'],'running')
            atomic_json(path,{'state':'submission_unknown','password':'PASSWORD_SECRET'})
            for language,label in (('en','submission-unknown'),('zh','提交结果待核对')):
                code,output,error=self.call(['jobs','status','example'],tty=True,language=language)
                self.assertEqual(code,0,error)
                self.assertIn(label,output)
                self.assertIn('jobs reconcile example',output)
                self.assertNotIn('SECRET',output+error)
        finally: os.close(fd)
        evidence=submission_evidence(self.run)
        self.assertTrue(evidence['unknown'])
        self.assertNotIn('SECRET',json.dumps(evidence))

    def test_unreadable_intent_and_symlink_are_conservative_bounded_and_readonly(self):
        self.save()
        path=self.run/'preparation/selection.intent.json'
        path.parent.mkdir()
        path.write_text('BODY_SECRET not JSON')
        before=self.snapshot()
        value=managed.inspect_run('example')
        self.assertEqual(value['execution_summary']['outcome'],'submission_unknown')
        self.assertTrue(value['submission_evidence']['unreadable'])
        self.assertIn('ls -ld',value['execution_summary']['next_step'])
        self.assertNotIn('BODY_SECRET',json.dumps(value))
        self.assertEqual(before,self.snapshot())
        path.rename(path.with_suffix('.retained'))
        path.symlink_to(self.base/'outside')
        self.assertTrue(submission_evidence(self.run)['unreadable'])

    def test_prior_worker_failure_does_not_mask_current_worker_but_new_failure_is_kept(self):
        value={'name':'example','directory':str(self.run),'state':'running','execution_state':'failed_local',
               'recovery':{'updated_at':'2026-10-15T00:00:00+00:00','error':{'category':'operation_error','retryable':False,'message':'Prior failure'}},
               'background':{'running':True,'pid':123,'started_at':'2026-10-15T01:00:00+00:00'}}
        prior=describe_execution(value)
        self.assertTrue(prior['prior_error'])
        self.assertEqual(prior['outcome'],'running')
        set_language('en')
        self.assertNotIn('Prior failure',format_job_status(value))
        value['recovery']['updated_at']='2026-10-15T02:00:00+00:00'
        self.assertEqual(describe_execution(value)['outcome'],'failed_local')
        self.assertIn('Prior failure',format_job_status(value))

    def test_explicit_unknown_wins_over_a_pass_and_cached_pass_over_obsolete_remote_stage(self):
        for evidence in ({'pending':True,'unknown':True},{'pending':True}):
            value={'state':'validated','passed':True,'submission_evidence':evidence,'background':{'running':False}}
            self.assertEqual(describe_execution(value)['outcome'],'submission_unknown')
        value={'state':'validated','passed':True,'remote_status':'archive_preparing'}
        self.assertEqual(describe_execution(value)['outcome'],'passed')
        self.assertNotIn('Remote state:',format_job_status(value))
        value['passed']=None
        self.assertEqual(describe_execution(value)['outcome'],'needs_validation')

    def test_stale_worker_elapsed_is_not_claimed_as_execution_duration(self):
        value={'state':'running','name':'example','background':{'running':False,'pid':123,'started_at':'2026-10-15T00:00:00+00:00','elapsed_seconds':1367}}
        self.assertEqual(describe_execution(value)['outcome'],'interrupted')
        self.assertFalse(describe_execution(value)['elapsed_is_duration'])
        set_language('en')
        self.assertIn('since last start (not execution duration)',format_job_status(value))

    def test_diagnostic_values_do_not_depend_on_language_and_raw_json_is_unchanged(self):
        self.save(state='running',build_job_id='123')
        before=self.snapshot()
        values=[]
        for language in ('en','zh'):
            code,output,error=self.call(['jobs','status','example','--json'],language=language)
            self.assertEqual(code,0,error)
            value=json.loads(output)
            self.assertEqual(output,json.dumps(value,indent=2,ensure_ascii=False)+'\n')
            self.assertEqual(value['state'],'running')
            values.append((value['execution_summary'],value['submission_evidence']))
        self.assertEqual(values[0],values[1])
        self.assertEqual(before,self.snapshot())

    def test_direct_build_remote_error_is_not_relabeled_as_local_failure(self):
        set_language('en')
        text=format_build({'state':'running','last_error':{'category':'remote_failure','retryable':False,'message':'Remote failure'}})
        self.assertIn('State: Remote failure',text)
        self.assertNotIn('State: Local failure',text)

    def test_manifest_only_error_is_classified_without_surfacing_an_untrusted_legacy_body(self):
        self.save(state='parameterizing',last_error={'message':'BODY_SECRET','category':'operation_error','retryable':False})
        for tty in (True,False):
            code,output,error=self.call(['jobs','status','example'],tty=tty)
            self.assertEqual(code,0,error)
            self.assertNotIn('BODY_SECRET',output+error)
        self.assertEqual(managed.inspect_run('example')['execution_summary']['error_origin'],'legacy_manifest')

    def test_name_metadata_errors_are_bilingual_actionable_and_do_not_echo_record_values(self):
        path=self.base/'index/names/example.json'
        atomic_json(path,{'directory':[],'body':'BODY_SECRET','token':'TOKEN_SECRET'})
        for language,label in (('en','valid directory field'),('zh','有效目录字段')):
            code,output,error=self.call(['jobs','status','example'],language=language)
            self.assertEqual(code,2)
            self.assertIn(label,error)
            self.assertIn(str(path),error)
            self.assertIn('ls -l',error)
            self.assertNotIn('SECRET',output+error)
            self.assertNotIn('Traceback',output+error)

    def test_common_task_errors_keep_legacy_strings_but_have_localized_keys_and_concrete_commands(self):
        with self.assertRaises(ToolError) as caught: tasks.check_name_available('example')
        self.assertEqual(caught.exception.key,'jobs.name_taken')
        self.assertIn('already registered',str(caught.exception))
        self.assertEqual(shlex.split(caught.exception.next_step),['charmm-gui-cli','jobs','status','example'])
        for language,label in (('en','registered'),('zh','已登记')):
            set_language(language)
            self.assertIn(label,caught.exception.localized())
        target=self.run/'occupied.json'
        atomic_json(target,{'body':'BODY_SECRET'})
        with self.assertRaises(ToolError) as caught: tasks._new_json(target,{'replacement':True})
        self.assertEqual(caught.exception.key,'jobs.output_exists')
        self.assertIn(str(target),caught.exception.localized())
        self.assertNotIn('BODY_SECRET',caught.exception.localized())
        with self.assertRaises(ToolError) as caught: tasks.assign_name(self.run,'different')
        self.assertEqual(caught.exception.key,'jobs.name_mismatch')
        self.assertIn('different name',str(caught.exception))

    def test_unnamed_recovery_paths_with_spaces_are_shell_quoted(self):
        (self.run/'job.json').rename(self.run/'retained-job-record')
        moved=self.base/'run space 中文'
        self.run.rename(moved)
        saved=tasks.record_recovery(moved,ToolError('Network',category='transient_network',retryable=True))
        self.assertEqual(shlex.split(saved['next_step']),['charmm-gui-cli','jobs','resume',str(moved)])

    def test_early_flat_received_responses_need_matching_downstream_lineage_and_never_override_unknown(self):
        snapshot=self.run/'preparation/parameterization.html'
        snapshot.parent.mkdir()
        snapshot.write_text('retained ready-page fixture')
        self.save(source_job_id='123',build_job_id='456',preparation_page_state='ready',preparation_snapshot=str(snapshot))
        atomic_json(self.run/'bilayer/run.json',{'state':'running','job_id':'456','config':{'source_job_id':'123'}})
        path=self.run/'preparation/selection.intent.json'
        atomic_json(path,{'state':'submitting'})
        before=self.snapshot()
        value=managed.inspect_run('example')
        self.assertEqual(value['execution_summary']['outcome'],'stopped')
        self.assertFalse(value['submission_evidence']['pending'])
        self.assertEqual(value['submission_evidence']['historical_receipts'][0]['state'],'submitting')
        self.assertEqual(before,self.snapshot())
        for defect in ('foreign_build','foreign_source','missing_snapshot','explicit_unknown','unrelated_intent'):
            atomic_json(path,{'state':'submitting'})
            atomic_json(self.run/'bilayer/run.json',{'state':'running','job_id':'789' if defect=='foreign_build' else '456',
                'config':{'source_job_id':'789' if defect=='foreign_source' else '123'}})
            if defect=='missing_snapshot': snapshot.rename(snapshot.with_suffix('.retained'))
            if defect=='explicit_unknown': atomic_json(path,{'state':'submission_unknown'})
            extra=self.run/'preparation/other.intent.json'
            if defect=='unrelated_intent': atomic_json(extra,{'state':'submitting'})
            self.assertEqual(managed.inspect_run('example')['execution_summary']['outcome'],'submission_unknown',defect)
            if defect=='missing_snapshot': snapshot.with_suffix('.retained').rename(snapshot)
            if defect=='unrelated_intent': extra.rename(extra.with_suffix('.retained'))

    def test_live_anonymous_watch_commands_quote_user_root_not_internal_directory(self):
        root=self.base/'task space 中文'
        value={'state':'running','directory':str(root/'.charmm-gui'),
               'background':{'running':True,'pid':123,'log':str(root/'run.log')}}
        set_language('en')
        for render in (format_job_status,format_build):
            output=render(value)
            next_line=next(line for line in output.splitlines() if line.startswith('Next: '))
            self.assertEqual(shlex.split(next_line[len('Next: '):]),['charmm-gui-cli','jobs','watch',str(root)])
            self.assertNotIn('.charmm-gui',output)


if __name__=='__main__':
    unittest.main()
