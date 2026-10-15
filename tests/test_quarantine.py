"""Recoverable cleanup uses isolated synthetic tasks and never provider calls."""

import contextlib
import errno
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
from charmm_gui_cli.pipeline import cleanup, managed
from charmm_gui_cli.store import quarantine as q, tasks
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.discovery import source_directory, task_id
from charmm_gui_cli.store.exclusive_move import move_exclusive
from charmm_gui_cli.store.lifecycle import forget, remember
from charmm_gui_cli.store.paths import read_value
from charmm_gui_cli.store.workers import claim
from charmm_gui_cli.locales.i18n import set_language
import test_single_directory as fixtures


class QuarantineTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SingleDirectoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base,self.root,self.run=self.fixture.fixture.root,self.fixture.root,self.fixture.run
        self.stack=contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(set_language)
        self.stack.enter_context(patch('pathlib.Path.cwd',return_value=self.base))
        self.stack.enter_context(patch('charmm_gui_cli.store.tasks._registries',return_value=(self.base/'index',)))
        self.stack.enter_context(patch('requests.Session.request',side_effect=AssertionError('No cleanup HTTP')))
        self.identifier=task_id(self.run)

    def snapshot(self,root=None):
        root=root or self.root
        return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*')
                if p.is_file() and p.name not in ('.lock','run.lock')}

    def call(self,args,*,language='en',tty=False):
        output,errors=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors),patch.object(output,'isatty',return_value=tty):
            code=main(['--lang',language,*args])
        return code,output.getvalue(),errors.getvalue()

    def test_plan_is_strictly_readonly_and_bilingual(self):
        before=self.snapshot(self.base)
        for language,text in (('en','not executed'),('zh','尚未执行')):
            for flags in ([],['--dry-run']):
                code,output,error=self.call(['jobs','clean','receptor-a',*flags],language=language,tty=True)
                self.assertEqual(code,0,error)
                self.assertIn(text,output)
                self.assertIn('--yes',output)
                self.assertEqual(before,self.snapshot(self.base))
        self.assertFalse(q.ledger().exists())
        self.assertFalse((self.run/'run.lock').exists())

    def test_actual_move_and_restore_preserve_inodes_bytes_and_final_evidence(self):
        before=self.snapshot()
        root_inode=self.root.stat().st_ino
        archive_inode=(self.root/'charmm-gui.tgz').stat().st_ino
        result=cleanup.clean('receptor-a',yes=True)
        retained=Path(result['directory'])
        self.assertEqual(result['state'],'quarantined')
        self.assertFalse(self.root.exists())
        self.assertEqual(retained.stat().st_ino,root_inode)
        self.assertEqual((retained/'charmm-gui.tgz').stat().st_ino,archive_inode)
        self.assertEqual(before,self.snapshot(retained))
        self.assertEqual(tasks.list_records(),[])
        self.assertTrue(managed.inspect_run(str(retained))['passed'])
        certificate={'source_directory':str(self.run),'source_task_id':self.identifier}
        self.assertEqual(source_directory(certificate),retained/'.charmm-gui')
        index_before=self.snapshot(self.base)
        plan=cleanup.restore(result['operation'])
        self.assertEqual(plan['state'],'restore_plan')
        self.assertEqual(index_before,self.snapshot(self.base))
        restored=cleanup.restore(result['operation'],yes=True)
        self.assertEqual(restored['state'],'restored')
        self.assertEqual(before,self.snapshot())
        self.assertEqual(self.root.stat().st_ino,root_inode)
        self.assertTrue(managed.inspect_run('receptor-a')['passed'])
        self.assertEqual(source_directory(certificate),self.run)
        self.assertEqual(len(list(self.base.rglob('*.tgz'))),1)

    def test_active_unknown_and_source_dependents_prevent_moves(self):
        fd=claim(self.run)
        try:
            with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
            self.assertEqual(error.exception.key,'cleanup.running')
        finally: os.close(fd)
        intent=self.run/'preparation/test.intent.json'
        atomic_json(intent,{'state':'submission_unknown'})
        with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'cleanup.uncertain')
        atomic_json(intent,{'state':'response_received'})
        child=self.base/'child/.charmm-gui'
        child.mkdir(parents=True)
        atomic_json(child/'full-run.json',{'schema_version':2,'state':'bilayer_submitted','build_job_id':'400',
            'reused_preparation':{'certificate':{'source_directory':str(self.run),'source_task_id':self.identifier}}})
        tasks.assign_name(child,'dependent')
        with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.dependent')
        self.assertTrue(self.root.is_dir())
        self.assertFalse(q.ledger().exists())

    def test_existing_restore_destination_and_reused_name_are_not_overwritten(self):
        result=cleanup.clean('receptor-a',yes=True)
        self.root.mkdir()
        inode=self.root.stat().st_ino
        with self.assertRaises(ToolError) as error: cleanup.restore(result['operation'],yes=True)
        self.assertEqual(error.exception.key,'quarantine.occupied')
        self.assertEqual(self.root.stat().st_ino,inode)
        destination=self.base/'restore 中文 alternate'
        atomic_json(self.base/'index/names/receptor-a.json',{'directory':str(self.base/'other'), 'name':'receptor-a','task_id':'f'*32})
        with self.assertRaises(ToolError) as error: cleanup.restore(result['operation'],yes=True,to=destination)
        self.assertEqual(error.exception.key,'quarantine.name_conflict')
        restored=cleanup.restore(result['operation'],yes=True,to=destination,register=False)
        self.assertFalse(restored['registered'])
        self.assertTrue(managed.inspect_run(str(destination))['passed'])
        self.assertEqual(read_value(self.base/'index/names/receptor-a.json')['task_id'],'f'*32)

    def test_no_replace_io_and_response_lost_after_actual_move_are_recoverable(self):
        before=self.snapshot()
        with patch.object(cleanup,'move_exclusive',side_effect=OSError(errno.EXDEV,'SECRET_BODY')):
            with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.move')
        self.assertNotIn('SECRET_BODY',error.exception.localized())
        self.assertEqual(before,self.snapshot())
        receipt=q.records()[0]
        # A pre-move intent can safely finish as an in-place restore.
        self.assertEqual(cleanup.restore(receipt['operation'],yes=True)['state'],'restored')
        def moved_but_error(source,destination):
            move_exclusive(source,destination)
            raise OSError(errno.EIO,'SECRET_BODY')
        with patch.object(cleanup,'move_exclusive',side_effect=moved_but_error):
            result=cleanup.clean('receptor-a',yes=True)
        self.assertTrue(result['move_response_uncertain'])
        self.assertFalse(self.root.exists())
        cleanup.restore(result['operation'],yes=True)
        self.assertEqual(before,self.snapshot())

    def test_interruption_after_move_retains_identity_and_cli_uses_restore_not_build(self):
        def interrupted(source,destination):
            move_exclusive(source,destination)
            raise KeyboardInterrupt
        with patch.object(cleanup,'move_exclusive',side_effect=interrupted):
            code,_,error=self.call(['jobs','clean','receptor-a','--yes'])
        self.assertEqual(code,130)
        self.assertIn('jobs restore',error)
        self.assertNotIn('jobs resume',error)
        receipt=q.records()[0]
        self.assertEqual(q.location(receipt),Path(receipt['quarantine_root']))
        self.assertTrue(managed.inspect_run(str(q.location(receipt)))['passed'])
        result=cleanup.restore(receipt['operation'],yes=True)
        self.assertEqual(result['state'],'restored')
        self.assertTrue(managed.inspect_run('receptor-a')['passed'])

    def test_tree_links_unsafe_roots_and_changed_identity_are_refused(self):
        link=self.root/'external-link'
        link.symlink_to(self.base/'not-a-task')
        with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.unsafe')
        link.rename(self.base/'retained-symlink')
        with patch('pathlib.Path.cwd',return_value=self.root):
            with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.unsafe')
        result=cleanup.clean('receptor-a',yes=True)
        embedded=Path(result['directory'])/'.charmm-gui/job.json'
        job=read_value(embedded)
        job['task_id']='f'*32
        atomic_json(embedded,job)
        with self.assertRaises(ToolError) as error: cleanup.restore(result['operation'],yes=True)
        self.assertEqual(error.exception.key,'quarantine.location')

    def test_named_unregistered_local_plan_can_be_remembered_without_new_identity(self):
        # Move index records recoverably outside the registry, without a forget receipt.
        index=self.base/'index'
        index.rename(self.base/'.retained-unindexed-records')
        before=self.snapshot()
        remember(str(self.root))
        self.assertEqual(task_id(self.run),self.identifier)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(tasks.resolve_run('receptor-a'),self.run)

    def test_genuinely_killed_mover_is_restorable_from_retained_intent(self):
        marker=self.base/'moved.marker'
        code='''
import os,signal,sys
from pathlib import Path
from unittest.mock import patch
from charmm_gui_cli.pipeline import cleanup
from charmm_gui_cli.store.exclusive_move import move_exclusive
base=Path(sys.argv[1]); marker=Path(sys.argv[2])
def stop(source,destination):
    move_exclusive(source,destination)
    marker.touch()
    signal.pause()
with patch('charmm_gui_cli.store.tasks._registry',return_value=base/'index'), \\
     patch('charmm_gui_cli.store.tasks._registries',return_value=(base/'index',)), \\
     patch('pathlib.Path.cwd',return_value=base), \\
     patch('requests.Session.request',side_effect=AssertionError('No cleanup HTTP')), \\
     patch.object(cleanup,'move_exclusive',side_effect=stop):
    cleanup.clean('receptor-a',yes=True)
'''
        environment={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])+os.pathsep+os.environ.get('PYTHONPATH','')}
        process=subprocess.Popen([sys.executable,'-c',code,str(self.base),str(marker)],env=environment,
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            for _ in range(200):
                if marker.exists() or process.poll() is not None: break
                time.sleep(.025)
            self.assertTrue(marker.exists(),process.stderr.read().decode() if process.poll() is not None else 'Move did not reach checkpoint')
            process.kill()
            self.assertLess(process.wait(timeout=10),0)
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=10)
            process.stderr.close()
        record=q.records()[0]
        self.assertEqual(record['state'],'moving')
        self.assertFalse(self.root.exists())
        self.assertTrue(q.location(record).is_dir())
        result=cleanup.restore(record['operation'],yes=True)
        self.assertEqual(result['state'],'restored')
        self.assertTrue(managed.inspect_run('receptor-a')['passed'])

    def test_index_archival_failure_and_interrupted_restore_can_finish_without_modeling(self):
        with patch.object(cleanup,'forget',side_effect=ToolError(key='cleanup.index_changed')):
            with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.index_pending')
        record=q.records()[0]
        self.assertEqual(record['state'],'index_pending')
        def moved(source,destination):
            move_exclusive(source,destination)
            raise KeyboardInterrupt
        with patch.object(cleanup,'move_exclusive',side_effect=moved):
            code,_,error=self.call(['jobs','restore',record['operation'],'--yes'])
        self.assertEqual(code,130)
        self.assertIn(record['operation'],error)
        self.assertTrue(self.root.exists())
        self.assertEqual(cleanup.restore(record['operation'],yes=True)['state'],'restored')
        self.assertTrue(managed.inspect_run('receptor-a')['passed'])

    def test_missing_indexed_tasks_and_forgotten_active_dependencies_are_not_ignored(self):
        missing=self.base/'index/run-history/missing.json'
        atomic_json(missing,{'directory':str(self.base/'unavailable/.charmm-gui'),'name':'lost','task_id':'e'*32})
        with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.index')
        missing.rename(self.base/'retained-missing-record.json')
        child=self.base/'dependent/.charmm-gui'
        child.mkdir(parents=True)
        full={'schema_version':2,'state':'inputs_ready',
              'reused_preparation':{'certificate':{'source_directory':str(self.run),'source_task_id':self.identifier}}}
        atomic_json(child/'full-run.json',full)
        tasks.assign_name(child,'dependent')
        forget('dependent',yes=True)
        atomic_json(child/'full-run.json',{**full,'state':'bilayer_submitted','build_job_id':'300'})
        with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.dependent')
        self.assertTrue(self.root.exists())

    def test_completed_batch_child_remains_validated_after_clean_and_restore(self):
        from charmm_gui_cli.pipeline import batch, build_command
        from charmm_gui_cli.store.discovery import relink
        parent=self.base/'batch'
        child=parent/'jobs/first'
        child.parent.mkdir(parents=True)
        self.root.rename(child)
        relink('receptor-a',child)
        self.root,self.run=child,child/'.charmm-gui'
        config={'version':1,'source':str(self.base/'batch.yaml'),'defaults':{},'jobs':[{'name':'first','options':{}}]}
        manifest={'version':1,'state':'complete','created_at':'2026-10-15T00:00:00+00:00','last_start_unix':0,
                  'config':config,'config_fingerprint':batch._canonical(config),
                  'jobs':[{'name':'first','directory':str(self.run),'state':'validated','started':True}]}
        atomic_json(parent/'batch.json',manifest)
        result=cleanup.clean('receptor-a',yes=True)
        row=batch.status(parent)['jobs'][0]
        self.assertEqual(row['state'],'validated')
        self.assertEqual(row['task_id'],self.identifier)
        self.assertEqual(row['directory'],str(self.run))
        self.assertEqual(row['retained_directory'],str(Path(result['directory'])/'.charmm-gui'))
        with patch.object(batch,'load_batch',return_value=config),patch.object(build_command,'execute',side_effect=AssertionError('Completed child must not rebuild')):
            summary,code=batch.advance(parent,wait=False)
        self.assertEqual(code,0)
        self.assertEqual(summary['jobs'][0]['state'],'validated')
        cleanup.restore(result['operation'],yes=True)
        self.assertEqual(batch.status(parent)['jobs'][0]['state'],'validated')

    def test_running_batch_prevents_child_clean_without_creating_a_ledger(self):
        from charmm_gui_cli.pipeline import batch
        parent=self.base/'batch'
        child=parent/'jobs/first'
        child.parent.mkdir(parents=True)
        self.root.rename(child)
        from charmm_gui_cli.store.discovery import relink
        relink('receptor-a',child)
        config={'source':str(self.base/'batch.yaml'),'jobs':[{'name':'first'}]}
        atomic_json(parent/'batch.json',{'version':1,'state':'running','last_start_unix':0,'config':config,
             'config_fingerprint':batch._canonical(config),
             'jobs':[{'name':'first','state':'validated','directory':str(child/'.charmm-gui'),'started':True}]})
        with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.batch_active')
        self.assertFalse(q.ledger().exists())

    def test_receipt_fields_and_reused_original_path_do_not_redirect_source_identity(self):
        result=cleanup.clean('receptor-a',yes=True)
        self.run.mkdir(parents=True)
        atomic_json(self.run/'full-run.json',{'schema_version':2,'state':'inputs_ready'})
        tasks.assign_name(self.run,'foreign-task')
        self.assertNotEqual(task_id(self.run),self.identifier)
        certificate={'source_directory':str(self.run),'source_task_id':self.identifier}
        self.assertEqual(source_directory(certificate),Path(result['directory'])/'.charmm-gui')
        path=q.ledger()/(result['operation']+'.json')
        receipt=json.loads(path.read_text())
        receipt['response_body']='BODY_SECRET'
        atomic_json(path,receipt)
        code,output,error=self.call(['jobs','restore',result['operation']])
        self.assertEqual(code,2)
        self.assertNotIn('BODY_SECRET',output+error)

    def test_retained_mirror_can_restore_after_the_global_index_was_moved(self):
        result=cleanup.clean('receptor-a',yes=True)
        mirror=Path(result['container'])/'restore.json'
        index=self.base/'index'
        index.rename(self.base/'.retained-global-index')
        self.assertEqual(cleanup.restore(mirror)['state'],'restore_plan')
        restored=cleanup.restore(mirror,yes=True)
        self.assertEqual(restored['state'],'restored')
        self.assertTrue(managed.inspect_run('receptor-a')['passed'])

    def test_unfinished_bound_child_blocks_source_but_quarantined_idle_child_retains_resolution(self):
        child=self.base/'dependent/.charmm-gui'
        child.mkdir(parents=True)
        full={'schema_version':2,'state':'prepared_reused',
              'reused_preparation':{'certificate':{'source_directory':str(self.run),'source_task_id':self.identifier}}}
        atomic_json(child/'full-run.json',full)
        tasks.assign_name(child,'dependent')
        with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.dependent')
        quarantined_child=cleanup.clean('dependent',yes=True)
        source=cleanup.clean('receptor-a',yes=True)
        certificate=read_value(Path(quarantined_child['directory'])/'.charmm-gui/full-run.json')['reused_preparation']['certificate']
        self.assertEqual(source_directory(certificate),Path(source['directory'])/'.charmm-gui')
        with self.assertRaises(ToolError) as error: cleanup.clean(source['directory'],yes=True)
        self.assertEqual(error.exception.key,'quarantine.already')

    def test_corrupt_unrelated_quarantine_record_does_not_break_normal_task_queries(self):
        q.ledger().mkdir()
        atomic_json(q.ledger()/('f'*32+'.json'),{'response_body':'BODY_SECRET'})
        code,output,error=self.call(['jobs','status','receptor-a','--json'])
        self.assertEqual(code,0,error)
        self.assertTrue(json.loads(output)['passed'])
        self.assertNotIn('BODY_SECRET',output+error)

    def test_active_quarantined_worker_blocks_even_the_restore_plan(self):
        result=cleanup.clean('receptor-a',yes=True)
        directory=Path(result['directory'])/'.charmm-gui'
        before=self.snapshot(self.base)
        fd=claim(directory)
        try:
            with self.assertRaises(ToolError) as error: cleanup.restore(result['operation'])
            self.assertEqual(error.exception.key,'cleanup.running')
        finally: os.close(fd)
        self.assertEqual(before,self.snapshot(self.base))

    def test_destination_created_during_move_is_not_replaced(self):
        destination_seen=[]
        def occupied(source,destination):
            destination.mkdir()
            destination_seen.append((destination,destination.stat().st_ino))
            move_exclusive(source,destination)
        before=self.snapshot()
        with patch.object(cleanup,'move_exclusive',side_effect=occupied):
            with self.assertRaises(ToolError) as error: cleanup.clean('receptor-a',yes=True)
        self.assertEqual(error.exception.key,'quarantine.move')
        destination,inode=destination_seen[0]
        self.assertEqual(destination.stat().st_ino,inode)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(q.location(q.records()[0]),self.root)

    @unittest.skipUnless(hasattr(os,'mkfifo'),'POSIX FIFO required')
    def test_special_receipts_are_rejected_without_blocking_normal_queries(self):
        from charmm_gui_cli.store.lifecycle import _record
        q.ledger().mkdir()
        fifo=q.ledger()/('f'*32+'.json')
        os.mkfifo(fifo)
        with self.assertRaises(ValueError): _record(fifo)
        self.assertTrue(managed.inspect_run('receptor-a')['passed'])

    def test_interruption_during_index_archival_gives_the_actual_restore_operation(self):
        with patch.object(cleanup,'forget',side_effect=KeyboardInterrupt):
            code,_,error=self.call(['jobs','clean','receptor-a','--yes'])
        self.assertEqual(code,130)
        record=q.records()[0]
        self.assertIn(record['operation'],error)
        self.assertNotIn('jobs resume',error)
        self.assertEqual(cleanup.restore(record['operation'],yes=True)['state'],'restored')


if __name__=='__main__':
    unittest.main()
