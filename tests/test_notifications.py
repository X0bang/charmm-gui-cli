"""Desktop notifications are optional, bounded and independent of exit status."""

import contextlib
import io
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.pipeline.background import worker
from charmm_gui_cli.presentation.notifications import notify_completion
from charmm_gui_cli.store.auth import atomic_json
from charmm_gui_cli.store.workers import claim, metadata


class NotificationTests(unittest.TestCase):
    def tearDown(self):
        set_language()

    def test_linux_notifications_are_bilingual_bounded_and_contain_no_task_data(self):
        for language, text in (('zh','任务'), ('en','task')):
            set_language(language)
            for code in (0, 2, 130):
                with patch('sys.platform','linux'), patch('shutil.which', return_value='/usr/bin/notify-send'), \
                        patch('subprocess.run', return_value=Mock(returncode=0)) as run:
                    self.assertTrue(notify_completion(code))
                command = run.call_args.args[0]
                self.assertEqual(command[:2], ['/usr/bin/notify-send','--app-name=charmm-gui-cli'])
                self.assertIn('--', command)
                self.assertIn(text, command[-1])
                self.assertEqual(run.call_args.kwargs['timeout'], 5)
                self.assertNotIn('shell', run.call_args.kwargs)
                self.assertIs(run.call_args.kwargs['stderr'], subprocess.DEVNULL)

    def test_unavailable_desktop_is_silent_and_never_affects_worker_result(self):
        for failure in (OSError('SECRET'), subprocess.TimeoutExpired('SECRET',5), Mock(returncode=1)):
            directory = Path(tempfile.mkdtemp(prefix='cgui-notification-failure-'))
            atomic_json(directory/'worker-options.json', {'lang':'en'})
            fd = claim(directory)
            output,errors=io.StringIO(),io.StringIO()
            with patch('sys.platform','linux'), patch('shutil.which', return_value='/usr/bin/notify-send'), \
                    patch('subprocess.run', side_effect=failure if isinstance(failure,Exception) else None,
                          return_value=failure), \
                    patch('charmm_gui_cli.pipeline.build_command.execute', return_value=({'state':'validation_failed'},2)), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(worker(directory,fd),2)
            self.assertEqual(metadata(directory)['exit_code'],2)
            self.assertNotIn('SECRET',output.getvalue()+errors.getvalue())
            self.assertNotIn('Traceback',output.getvalue()+errors.getvalue())

    def test_missing_tool_and_unsupported_platform_do_not_spawn_anything(self):
        for platform in ('linux','freebsd'):
            with patch('sys.platform',platform), patch('shutil.which',return_value=None), patch('subprocess.run') as run:
                self.assertFalse(notify_completion(0))
                run.assert_not_called()

    def test_macos_uses_an_argv_argument_not_interpolated_applescript(self):
        set_language('en')
        with patch('sys.platform','darwin'), patch('subprocess.run',return_value=Mock(returncode=0)) as run:
            self.assertTrue(notify_completion(2))
        args=run.call_args.args[0]
        self.assertEqual(args[0],'osascript')
        self.assertIn('item 1 of argv',args[2])
        self.assertIn('failed',args[-1])


if __name__=='__main__':
    unittest.main()
