"""Only isolated synthetic indexes are archived; task data is never removed."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from charmm_gui_cli.cli import main
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.store import tasks
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.discovery import relink
from charmm_gui_cli.store.lifecycle import forget
from charmm_gui_cli.store.paths import read_value
from charmm_gui_cli.store.workers import claim
import test_single_directory as fixtures


class ForgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SingleDirectoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base, self.root, self.run = self.fixture.fixture.root, self.fixture.root, self.fixture.run
        self.primary, self.legacy = self.base/'index', self.base/'legacy-config'
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(set_language)
        self.stack.enter_context(patch('charmm_gui_cli.store.tasks._registries', return_value=(self.primary,self.legacy)))
        self.stack.enter_context(patch('pathlib.Path.cwd', return_value=self.base))
        self.stack.enter_context(patch('requests.Session.request', side_effect=AssertionError('No cleanup network')))
        self.legacy.mkdir()
        (self.legacy/'session.token').write_text('TOKEN_SECRET')
        (self.legacy/'session.cookies.json').write_text('COOKIE_SECRET')

    def data(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*')
                if p.is_file() and p.name not in ('run.lock','.lock')}

    def indexes(self):
        return {str(p):p.read_bytes() for registry in (self.primary,self.legacy) for p in registry.rglob('*') if p.is_file()}

    def call(self,args,*,tty=False,language='en'):
        output,errors=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), patch.object(output,'isatty',return_value=tty):
            code=main(['--lang',language,*args])
        return code,output.getvalue(),errors.getvalue()

    def test_default_and_explicit_dry_run_are_readonly_and_bilingual(self):
        before, data = self.indexes(), self.data()
        for language,text in (('zh','尚未执行'),('en','not executed')):
            for options in ([],['--dry-run']):
                code,output,errors=self.call(['jobs','forget','receptor-a',*options],tty=True,language=language)
                self.assertEqual(code,0,errors)
                self.assertIn(text,output)
                self.assertIn('--yes',output)
                self.assertEqual(before,self.indexes())
                self.assertEqual(data,self.data())
        self.assertFalse((self.run/'run.lock').exists())

    def test_yes_removes_only_matching_indexes_and_preserves_every_task_and_session_byte(self):
        record=read_value(self.primary/'names/receptor-a.json')
        atomic_json(self.legacy/'names/receptor-a.json',record)
        atomic_json(self.legacy/'run-history/old.json',record)
        atomic_json(self.legacy/'latest-run.json',record)
        other={**record,'name':'other','task_id':'f'*32}
        atomic_json(self.primary/'names/other.json',other)
        unrelated=(self.primary/'names/other.json').read_bytes()
        before=self.data()
        result=forget('receptor-a',yes=True)
        self.assertEqual(result['state'],'forgotten')
        self.assertEqual(len(result['backup_directories']),2)
        self.assertEqual(before,self.data())
        self.assertEqual((self.legacy/'session.token').read_text(),'TOKEN_SECRET')
        self.assertEqual((self.legacy/'session.cookies.json').read_text(),'COOKIE_SECRET')
        self.assertEqual((self.primary/'names/other.json').read_bytes(),unrelated)
        self.assertFalse((self.primary/'names/receptor-a.json').exists())
        self.assertFalse((self.legacy/'names/receptor-a.json').exists())
        self.assertEqual(tasks.list_records(),[])
        for backup in result['backup_directories']:
            folder=Path(backup)
            receipt=read_value(folder/'forget.json')
            self.assertEqual(receipt['state'],'forgotten')
            for entry in receipt['files']:
                self.assertEqual(hashlib.sha256((folder/entry['path']).read_bytes()).hexdigest(),entry['sha256'])
            self.assertEqual(folder.stat().st_mode&0o777,0o700)
        self.assertNotIn('SECRET',json.dumps(result))

    def test_relink_from_outside_restores_the_exact_forgotten_task_after_a_move(self):
        original=self.data()
        forget('receptor-a',yes=True)
        moved=self.root.with_name('moved 中文 task')
        self.root.rename(moved)
        self.root,self.run=moved,moved/'.charmm-gui'
        outside=self.base/'outside'
        outside.mkdir()
        with patch('pathlib.Path.cwd',return_value=outside):
            code,output,errors=self.call(['jobs','relink','receptor-a',str(self.root),'--json'])
            self.assertEqual(code,0,errors)
            self.assertEqual(json.loads(output)['state'],'relinked')
            self.assertEqual(tasks.resolve_run('receptor-a'),self.run)
        self.assertEqual(original,self.data())
        self.assertEqual(len(tasks.list_records()),1)

    def test_foreign_identity_or_reused_name_cannot_be_overwritten_by_restore(self):
        forget('receptor-a',yes=True)
        job=read_value(self.run/'job.json')
        original=dict(job)
        job['task_id']='f'*32
        atomic_json(self.run/'job.json',job)
        with self.assertRaises(ToolError): relink('receptor-a',self.root)
        atomic_json(self.run/'job.json',original)
        foreign={'name':'receptor-a','task_id':'f'*32,'directory':str(self.base/'other/.charmm-gui')}
        atomic_json(self.primary/'names/receptor-a.json',foreign)
        before=(self.primary/'names/receptor-a.json').read_bytes()
        with self.assertRaises(ToolError): relink('receptor-a',self.root)
        self.assertEqual((self.primary/'names/receptor-a.json').read_bytes(),before)

    def test_damaged_backup_is_not_used_to_restore_registration(self):
        result=forget('receptor-a',yes=True)
        backup=Path(result['backup_directories'][0])/'names/receptor-a.json'
        backup.write_text(backup.read_text()+' ')
        with self.assertRaises(ToolError) as error: relink('receptor-a',self.root)
        self.assertEqual(error.exception.key,'cleanup.backup_changed')
        self.assertFalse((self.primary/'names/receptor-a.json').exists())

    def test_active_worker_and_unknown_or_queued_submissions_are_never_hidden(self):
        before=self.indexes()
        fd=claim(self.run)
        try:
            with self.assertRaises(ToolError) as error: forget('receptor-a',yes=True)
            self.assertEqual(error.exception.key,'cleanup.running')
        finally: os.close(fd)
        original=read_value(self.run/'full-run.json')
        remote=read_value(self.run/'bilayer/run.json')
        for state in ('submitting','submission_unknown','pending','running'):
            atomic_json(self.run/'full-run.json',{**original,'state':state})
            atomic_json(self.run/'bilayer/run.json',{**remote,'state':state})
            with self.assertRaises(ToolError) as error: forget('receptor-a',yes=True)
            self.assertEqual(error.exception.key,'cleanup.uncertain' if state in ('submitting','submission_unknown') else 'cleanup.remote_active')
            self.assertEqual(before,self.indexes())

    def test_unreadable_intent_and_symlinked_index_do_not_change_any_records(self):
        before=self.indexes()
        intent=self.run/'bilayer/submit.intent.json'
        intent.write_text('invalid JSON TOKEN_SECRET')
        with self.assertRaises(ToolError) as error: forget('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'cleanup.uncertain')
        self.assertEqual(before,self.indexes())
        intent.rename(intent.with_suffix('.retained'))
        names=self.primary/'names'
        names.rename(self.primary/'original-names')
        names.symlink_to(self.primary/'original-names',target_is_directory=True)
        with self.assertRaises(ToolError) as error: forget('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'cleanup.unsafe_index')
        self.assertTrue((self.primary/'original-names/receptor-a.json').is_file())

    def test_partial_move_failure_rolls_back_without_losing_records(self):
        before=self.indexes()
        rename=Path.rename
        calls=0
        def interrupted(source,destination):
            nonlocal calls
            if 'forgotten' in Path(destination).parts:
                calls+=1
                if calls==2: raise OSError('RAW_SECRET')
            return rename(source,destination)
        with patch.object(Path,'rename',interrupted), self.assertRaises(ToolError) as error:
            forget('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'cleanup.rolled_back')
        self.assertNotIn('RAW_SECRET',error.exception.localized())
        for path,data in before.items(): self.assertEqual(Path(path).read_bytes(),data)
        self.assertTrue(list((self.primary/'forgotten').glob('*/forget.json')))

    def test_name_and_history_writer_share_one_real_cross_process_critical_section(self):
        ready, release = self.base/'writer-ready', self.base/'writer-release'
        code = '''
import sys,time
from pathlib import Path
from charmm_gui_cli.store import tasks
directory,index,ready,release=map(Path,sys.argv[1:])
tasks._registry=lambda:index
tasks._registries=lambda:(index,)
original=tasks._publish_run
def held(registry,record):
    ready.touch()
    deadline=time.monotonic()+10
    while not release.exists():
        if time.monotonic()>deadline: raise AssertionError('test release missing')
        time.sleep(.02)
    return original(registry,record)
tasks._publish_run=held
tasks.assign_name(directory,'receptor-a')
'''
        environment=dict(os.environ)
        environment['PYTHONPATH']=str(Path(tasks.__file__).resolve().parents[2])+os.pathsep+environment.get('PYTHONPATH','')
        process=subprocess.Popen([sys.executable,'-c',code,str(self.run),str(self.primary),str(ready),str(release)],
                                 env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+8
            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            with self.assertRaises(ToolError) as error:
                forget('receptor-a',yes=True)
            self.assertEqual(error.exception.category,'lock_busy')
            release.touch()
            self.assertEqual(process.wait(timeout=10),0)
        finally:
            release.touch()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        result=forget('receptor-a',yes=True)
        self.assertGreaterEqual(result['index_records'],3)
        self.assertEqual(tasks.list_records(),[])

    def test_submission_state_is_rechecked_after_the_process_lease_is_acquired(self):
        original=read_value(self.run/'full-run.json')
        remote=read_value(self.run/'bilayer/run.json')
        before=self.indexes()
        def changed(directory):
            fd=claim(directory)
            atomic_json(self.run/'full-run.json',{**original,'state':'pending'})
            atomic_json(self.run/'bilayer/run.json',{**remote,'state':'pending'})
            return fd
        with patch('charmm_gui_cli.store.lifecycle.claim',side_effect=changed), self.assertRaises(ToolError) as error:
            forget('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'cleanup.remote_active')
        for path,data in before.items(): self.assertEqual(Path(path).read_bytes(),data)

    def test_saved_unknown_recovery_is_not_hidden_even_without_an_intent_file(self):
        atomic_json(self.run/'recovery.json',{'active':True,'error':{'category':'submission_unknown'}})
        with self.assertRaises(ToolError) as error: forget('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'cleanup.uncertain')
        self.assertTrue((self.primary/'names/receptor-a.json').exists())

    def test_ctrl_c_rolls_back_index_moves_and_uses_local_command_guidance(self):
        before=self.indexes()
        rename=Path.rename
        count=0
        def interrupted(source,destination):
            nonlocal count
            if 'forgotten' in Path(destination).parts:
                count+=1
                if count==2: raise KeyboardInterrupt
            return rename(source,destination)
        with patch.object(Path,'rename',interrupted):
            code,output,error=self.call(['jobs','forget','receptor-a','--yes'])
        self.assertEqual(code,130)
        self.assertIn('jobs forget receptor-a --dry-run',error)
        self.assertNotIn('build-resume',error)
        for path,data in before.items(): self.assertEqual(Path(path).read_bytes(),data)

    def test_hard_killed_local_archiver_can_restore_from_pre_recorded_receipt(self):
        before=self.data()
        ready=self.base/'archiver-ready'
        script='''
import sys,time
from pathlib import Path
from charmm_gui_cli.store import tasks
from charmm_gui_cli.store.lifecycle import forget
directory,index,ready=map(Path,sys.argv[1:])
tasks._registry=lambda:index
tasks._registries=lambda:(index,)
original=Path.rename
def paused(source,destination):
    result=original(source,destination)
    if source==index/'names/receptor-a.json':
        ready.touch()
        time.sleep(20)
    return result
Path.rename=paused
forget(str(directory),yes=True)
'''
        environment=dict(os.environ)
        environment['PYTHONPATH']=str(Path(tasks.__file__).resolve().parents[2])+os.pathsep+environment.get('PYTHONPATH','')
        process=subprocess.Popen([sys.executable,'-c',script,str(self.run),str(self.primary),str(ready)],
                                 env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+8
            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.assertFalse((self.primary/'names/receptor-a.json').exists())
        result=relink('receptor-a',self.root)
        self.assertEqual(result['state'],'relinked')
        self.assertEqual(before,self.data())

    def test_rollback_never_overwrites_a_foreign_record_that_appeared_mid_operation(self):
        rename=Path.rename
        moved=[]
        def conflicted(source,destination):
            if 'forgotten' in Path(destination).parts:
                if moved:
                    moved[0].write_text('FOREIGN_RECORD')
                    raise OSError('test failure')
                result=rename(source,destination)
                moved.append(source)
                return result
            return rename(source,destination)
        with patch.object(Path,'rename',conflicted), self.assertRaises(ToolError) as error:
            forget('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'cleanup.partial')
        self.assertEqual(moved[0].read_text(),'FOREIGN_RECORD')
        receipts=list((self.primary/'forgotten').glob('*/forget.json'))
        self.assertTrue(receipts)
        self.assertEqual(read_value(receipts[0])['state'],'partial')

    def test_unnamed_legacy_task_can_be_forgotten_and_remembered_without_modeling(self):
        legacy=self.base/'legacy-unnamed'
        legacy.mkdir()
        atomic_json(legacy/'full-run.json',{'schema_version':2,'state':'inputs_ready'})
        tasks.remember_run(legacy)
        before=(legacy/'full-run.json').read_bytes()
        result=forget(str(legacy),yes=True)
        self.assertIn('jobs remember',result['next_step'])
        self.assertNotIn(str(legacy),[r['directory'] for r in tasks.list_records()])
        with patch('charmm_gui_cli.pipeline.build_command.execute',side_effect=AssertionError('No modeling')):
            code,output,error=self.call(['jobs','remember',str(legacy),'--json'])
        self.assertEqual(code,0,error)
        self.assertEqual(json.loads(output)['state'],'remembered')
        self.assertEqual(before,(legacy/'full-run.json').read_bytes())
        self.assertIn(str(legacy),[r['directory'] for r in tasks.list_records()])

    def test_named_remember_uses_forgotten_binding_and_is_bilingual(self):
        before=self.data()
        forget('receptor-a',yes=True)
        for language,text in (('en','re-registered'),('zh','重新收录')):
            code,output,error=self.call(['jobs','remember',str(self.root)],tty=True,language=language)
            self.assertEqual(code,0,error)
            self.assertIn(text,output)
        self.assertEqual(before,self.data())

    def test_explicit_missing_name_archives_only_indexes_and_retained_task_can_relink(self):
        original=self.data()
        retained=self.base/'.retained-task'
        self.root.rename(retained)
        old=self.root
        self.root,self.run=retained,retained/'.charmm-gui'
        before=self.indexes()
        for language,text in (('en','cannot be checked'),('zh','无法核对')):
            code,output,error=self.call(['jobs','forget','receptor-a','--missing'],tty=True,language=language)
            self.assertEqual(code,0,error)
            self.assertIn(text,output)
            self.assertEqual(before,self.indexes())
        code,output,error=self.call(['jobs','forget','receptor-a','--missing','--yes','--json'])
        self.assertEqual(code,0,error)
        self.assertTrue(json.loads(output)['missing_directory'])
        self.assertFalse(old.exists())
        self.assertEqual(original,self.data())
        self.assertEqual(relink('receptor-a',self.root)['state'],'relinked')

    def test_missing_option_refuses_existing_directories_and_unindexed_or_malformed_targets(self):
        before=self.indexes()
        with self.assertRaises(ToolError) as error: forget('receptor-a',yes=True,missing=True)
        self.assertEqual(error.exception.key,'cleanup.missing_present')
        self.assertEqual(before,self.indexes())
        code,_,error=self.call(['jobs','forget','UNREGISTERED_SECRET','--missing','--yes'])
        self.assertEqual(code,2)
        self.assertNotIn('UNREGISTERED_SECRET',error)
        record=read_value(self.primary/'names/receptor-a.json')
        record['task_id']=[]
        atomic_json(self.primary/'names/receptor-a.json',record)
        with self.assertRaises(ToolError) as error: forget('receptor-a',yes=True,missing=True)
        self.assertEqual(error.exception.key,'cleanup.missing_not_indexed')
        record['task_id']='a'*32
        record['name']='other-task'
        atomic_json(self.primary/'names/receptor-a.json',record)
        with self.assertRaises(ToolError) as error: forget('receptor-a',yes=True,missing=True)
        self.assertEqual(error.exception.key,'cleanup.missing_not_indexed')

    def test_missing_unnamed_path_and_conflicting_historical_identities_are_explicit(self):
        old=self.base/'legacy-anonymous'
        old.mkdir()
        atomic_json(old/'full-run.json',{'schema_version':2,'state':'inputs_ready'})
        tasks.remember_run(old)
        old.rename(self.base/'.retained-legacy')
        result=forget(str(old),yes=True,missing=True)
        self.assertTrue(result['missing_directory'])
        self.assertFalse(old.exists())
        self.assertEqual(json.loads((self.base/'.retained-legacy/full-run.json').read_text())['state'],'inputs_ready')
        atomic_json(self.primary/'run-history/conflict-a.json',{'directory':str(old),'task_id':'a'*32})
        atomic_json(self.primary/'run-history/conflict-b.json',{'directory':str(old),'task_id':'b'*32})
        with self.assertRaises(ToolError) as error: forget(str(old),yes=True,missing=True)
        self.assertEqual(error.exception.key,'cleanup.missing_ambiguous')

    def test_internal_borrowed_lease_is_validated_and_not_closed_by_index_archival(self):
        foreign=os.open(self.base/'foreign-lock',os.O_CREAT|os.O_RDWR,0o600)
        try:
            with self.assertRaises(ToolError) as error: forget('receptor-a',yes=True,_lease=foreign)
            self.assertEqual(error.exception.key,'cleanup.lease_invalid')
        finally: os.close(foreign)
        fd=claim(self.run)
        try:
            result=forget('receptor-a',yes=True,_lease=fd)
            self.assertEqual(result['state'],'forgotten')
            os.fstat(fd)
        finally: os.close(fd)

    def test_unrelated_malformed_directory_field_is_preserved_not_rendered_or_archived(self):
        invalid=self.primary/'run-history/invalid-directory.json'
        atomic_json(invalid,{'directory':[],'body':'BODY_SECRET'})
        before=invalid.read_bytes()
        result=forget('receptor-a',yes=True)
        self.assertEqual(before,invalid.read_bytes())
        self.assertNotIn('BODY_SECRET',json.dumps(result))


if __name__=='__main__':
    unittest.main()
