import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
from argparse import Namespace
import os
import subprocess
import sys
import contextlib

from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.store.workers import claim, metadata, write_lock
from charmm_gui_cli.pipeline.background import watch
from charmm_gui_cli.store.auth import atomic_json


class BackgroundTests(unittest.TestCase):
    def test_inherited_lock_survives_parent_close(self):
        directory = Path(tempfile.mkdtemp()).resolve()
        fd = claim(directory)
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"], pass_fds=(fd,), start_new_session=True)
        os.close(fd)
        self.assertTrue(metadata(directory)["running"])
        process.wait(timeout=5)
        self.assertFalse(metadata(directory)["running"])

    def test_foreground_returns_final_exit_code(self):
        from charmm_gui_cli.cli import main
        with patch("charmm_gui_cli.pipeline.build_command.execute", return_value=({"state": "validation_failed"}, 2)), \
                patch("charmm_gui_cli.pipeline.background.launch") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--foreground", "build", "--protein", "p.pdb", "--ligand", "l.sdf"]), 2)
        launch.assert_not_called()

    def test_macos_notification_is_best_effort_and_reports_failure(self):
        from charmm_gui_cli.pipeline.background import worker
        directory = Path(tempfile.mkdtemp()).resolve()
        atomic_json(directory / "worker-options.json", {"lang": "en"})
        fd = claim(directory)
        with patch("charmm_gui_cli.pipeline.build_command.execute", return_value=({"state": "validation_failed"}, 2)), \
                patch("charmm_gui_cli.pipeline.background.sys.platform", "darwin"), \
                patch("charmm_gui_cli.pipeline.background.subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker(directory, fd), 2)
        self.assertEqual(run.call_args.args[0][0], "osascript")
        self.assertIn("failed", run.call_args.args[0][-1])
        self.assertEqual(metadata(directory)["exit_code"], 2)

    def test_launch_detaches_after_local_checks_and_authentication(self):
        from charmm_gui_cli.pipeline.background import launch
        directory = Path(tempfile.mkdtemp()).resolve()
        atomic_json(directory / "full-run.json", {"schema_version": 2, "state": "inputs_ready"})
        args = Namespace(command="build", token_file=None, cookies=None, post_timeout=900, lang="en")
        web = Mock()
        web.request.return_value.text = '<form name="pdb"></form>'
        with patch("charmm_gui_cli.pipeline.build_command.execute", return_value=({"directory": str(directory), "state": "inputs_ready"}, 0)) as execute, \
                patch("charmm_gui_cli.pipeline.background.load_token", return_value=("TOKEN_SECRET", "mock")), \
                patch("charmm_gui_cli.remote.api.Client") as api, \
                patch("charmm_gui_cli.remote.web.WebClient", return_value=web), \
                patch("charmm_gui_cli.pipeline.background.subprocess.Popen", return_value=Mock(pid=12345)) as popen, \
                patch("charmm_gui_cli.pipeline.background.sys.platform", "darwin"):
            result, code = launch(args)
        self.assertEqual(code, 0)
        self.assertTrue(execute.call_args.args[0].dry_run)
        api.return_value.validate_auth.assert_called_once()
        self.assertEqual(web.request.call_args.args[0], "GET")
        self.assertTrue(popen.call_args_list[0].kwargs["start_new_session"])
        self.assertEqual(popen.call_args_list[1].args[0], ["caffeinate", "-i", "-w", "12345"])
        self.assertEqual(result["background"]["pid"], 12345)
        self.assertEqual(metadata(directory)["pid"], 12345)
        self.assertNotIn("TOKEN_SECRET", (directory / "worker-options.json").read_text())
        self.assertEqual((directory / "worker-options.json").stat().st_mode & 0o777, 0o600)

    def test_real_fast_worker_sees_published_pid_and_keeps_its_final_record(self):
        from charmm_gui_cli.pipeline.background import launch
        directory = Path(tempfile.mkdtemp(prefix='cgui-worker-handoff-')).resolve()
        atomic_json(directory / 'full-run.json', {'schema_version': 2, 'state': 'inputs_ready'})
        args = Namespace(command='build', token_file=None, cookies=None, post_timeout=900, lang='en')
        web = Mock()
        web.request.return_value.text = '<form name="pdb"></form>'
        spawn = subprocess.Popen
        processes = []
        script = '''
import os,sys
from pathlib import Path
from unittest.mock import patch
from charmm_gui_cli.pipeline.background import worker
from charmm_gui_cli.store.workers import metadata
from charmm_gui_cli.store.auth import atomic_json
root=Path(sys.argv[1]); fd=int(sys.argv[2]); gate=int(sys.argv[3])
os.read(gate,1); os.close(gate)
assert metadata(root)['pid']==os.getpid(), 'launcher PID was not handed off'
atomic_json(root/'observed-pid.json', {'pid':os.getpid()})
with patch('charmm_gui_cli.pipeline.build_command.execute', return_value=({'state':'inputs_ready'},2)), \
     patch('charmm_gui_cli.presentation.notifications.notify_completion'), \
     patch('requests.Session.request', side_effect=AssertionError('Offline worker')):
    sys.exit(worker(root,fd))
'''
        def child(command, **kwargs):
            self.assertEqual(command[1:3], ['-m', 'charmm_gui_cli.pipeline.background'])
            self.assertEqual(tuple(map(int, command[-2:])), kwargs['pass_fds'])
            process = spawn([sys.executable, '-c', script, *command[-3:]], **kwargs)
            processes.append(process)
            return process
        with patch('charmm_gui_cli.pipeline.build_command.execute', return_value=({'directory':str(directory)},0)), \
                patch('charmm_gui_cli.pipeline.background.load_token', return_value=('TOKEN_SECRET','mock')), \
                patch('charmm_gui_cli.remote.api.Client'), patch('charmm_gui_cli.remote.web.WebClient', return_value=web), \
                patch('charmm_gui_cli.pipeline.background.subprocess.Popen', side_effect=child), \
                patch('charmm_gui_cli.pipeline.background.sys.platform', 'linux'):
            result, code = launch(args)
        self.assertEqual(code, 0)
        process = processes[0]
        try:
            self.assertEqual(metadata(directory)['pid'], result['background']['pid'])
            self.assertEqual(process.wait(timeout=15), 2, (directory/'run.log').read_text())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        state = metadata(directory)
        self.assertEqual(state['pid'], process.pid)
        self.assertFalse(state['running'])
        self.assertEqual(state['exit_code'], 2)
        self.assertIn('finished_at', state)
        self.assertEqual(__import__('json').loads((directory/'observed-pid.json').read_text())['pid'], process.pid)

    def test_spawn_failure_closes_startup_pipe_and_releases_lock(self):
        from charmm_gui_cli.pipeline.background import launch
        directory = Path(tempfile.mkdtemp(prefix='cgui-worker-spawn-failure-')).resolve()
        atomic_json(directory/'full-run.json', {'schema_version':2,'state':'validated'})
        (directory/'bilayer').mkdir()
        (directory/'bilayer/charmm-gui.tgz').touch()
        args = Namespace(command='build', token_file=None, cookies=None, post_timeout=900)
        descriptors = []
        def fail(command, **kwargs):
            descriptors.extend(kwargs['pass_fds'])
            raise OSError('not displayed')
        with patch('charmm_gui_cli.pipeline.build_command.execute', return_value=({'directory':str(directory)},0)), \
                patch('charmm_gui_cli.pipeline.background.subprocess.Popen', side_effect=fail), self.assertRaises(OSError):
            launch(args)
        self.assertEqual(len(descriptors), 2)
        for fd in descriptors:
            with self.assertRaises(OSError): os.fstat(fd)
        self.assertFalse(metadata(directory)['running'])

    def test_expired_login_fails_before_worker_spawn(self):
        from charmm_gui_cli.pipeline.background import launch
        directory = Path(tempfile.mkdtemp()).resolve()
        atomic_json(directory / "full-run.json", {"schema_version": 2, "state": "inputs_ready", "inputs": {"files": {}}})
        args = Namespace(command="build-resume", directory=directory, token_file=None, cookies=None, post_timeout=900)
        with patch("charmm_gui_cli.pipeline.background.load_token", side_effect=ToolError("expired", category="authentication")), \
                patch("charmm_gui_cli.pipeline.background.subprocess.Popen") as popen, self.assertRaises(ToolError):
            launch(args)
        popen.assert_not_called()

    def test_invalid_resume_runtime_fails_before_auth_or_spawn(self):
        from charmm_gui_cli.pipeline.background import launch
        args = Namespace(command="build-resume", directory="unused", interval=1, max_wait=60, post_timeout=900)
        with patch("charmm_gui_cli.pipeline.background.load_token") as token, \
                patch("charmm_gui_cli.pipeline.background.subprocess.Popen") as popen, self.assertRaises(ToolError) as error:
            launch(args)
        self.assertEqual(error.exception.key, "background.runtime_invalid")
        token.assert_not_called()
        popen.assert_not_called()

    def test_active_lock_rejects_duplicate(self):
        directory = Path(tempfile.mkdtemp()).resolve()
        fd = claim(directory)
        try:
            write_lock(fd, {"pid": os.getpid()})
            self.assertTrue(metadata(directory)["running"])
            with self.assertRaises(ToolError):
                claim(directory)
        finally:
            os.close(fd)
        self.assertFalse(metadata(directory)["running"])

    def test_stale_lock_can_be_reused(self):
        directory = Path(tempfile.mkdtemp()).resolve()
        fd = claim(directory)
        write_lock(fd, {"pid": 999999999})
        os.close(fd)
        fd = claim(directory)
        write_lock(fd, {"pid": os.getpid()})
        os.close(fd)
        self.assertEqual(metadata(directory)["pid"], os.getpid())

    def test_watch_interrupt_never_signals_worker(self):
        directory = Path(tempfile.mkdtemp()).resolve()
        with patch("charmm_gui_cli.store.workers.metadata", return_value={"running": True}), \
                patch("charmm_gui_cli.pipeline.background.time.sleep", side_effect=KeyboardInterrupt), \
                patch("os.kill") as kill, patch("sys.stderr", io.StringIO()):
            self.assertEqual(watch(directory), 130)
            kill.assert_not_called()
