import contextlib
import io
import json
import unittest
from unittest.mock import patch

from charmm_gui_cli import cli, i18n
from charmm_gui_cli.terminal_output import format_build


class TerminalOutputTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language()

    def capture(self, value, command, *, tty, force=False, **context):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(output, "isatty", return_value=tty), patch.object(cli, "_JSON_OUTPUT", force):
            cli.emit(value, command=command, **context)
        return output.getvalue()

    def test_non_tty_and_forced_json_are_byte_identical_to_old_emit(self):
        value = {"logged_in": True, "message": "中文", "category": "submission_unknown", "nested": [None, False, 0.15]}
        expected = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        for language in ("zh", "en"):
            i18n.set_language(language)
            for command in (None, "login", "build", "status", "unregistered"):
                self.assertEqual(self.capture(value, command, tty=False), expected)
                self.assertEqual(self.capture(value, command, tty=True, force=True), expected)

    def test_unregistered_command_still_emits_json_on_tty(self):
        value = {"passed": True}
        self.assertEqual(self.capture(value, "check", tty=True), json.dumps(value, indent=2, ensure_ascii=False) + "\n")

    def test_login_summary_uses_only_allowlisted_fields(self):
        value = {"logged_in": True, "api_authenticated": True, "web_authenticated": True,
                 "saved_to": "/tmp/session.token", "cookie_file": "/tmp/session.cookies.json", "permissions": "0600",
                 "password": "PASSWORD_SECRET", "token": "TOKEN_SECRET", "cookie": "COOKIE_SECRET", "body": "BODY_SECRET"}
        for language, expected in (("zh", "已登录"), ("en", "Logged in")):
            i18n.set_language(language)
            output = self.capture(value, "login", tty=True, email="user@example.invalid")
            self.assertIn(expected, output)
            self.assertIn("user@example.invalid", output)
            self.assertIn("0600", output)
            self.assertIn("charmm-gui-cli build", output)
            for secret in ("PASSWORD_SECRET", "TOKEN_SECRET", "COOKIE_SECRET", "BODY_SECRET"):
                self.assertNotIn(secret, output)

    def test_status_does_not_reflect_unknown_server_state_or_messages(self):
        value = {"status": "BODY_SECRET", "lastOutFile": "COOKIE_SECRET", "token": "TOKEN_SECRET"}
        output = self.capture(value, "status", tty=True)
        self.assertNotIn("SECRET", output)

    def test_build_and_status_never_conflate_remote_done_and_validation(self):
        i18n.set_language("en")
        done = format_build({"status": "done", "job_id": "123"})
        self.assertIn("Local validation is not complete", done)
        passed = format_build({"state": "validated", "validation": {"passed": True, "review_required": True},
                               "name": "receptor-a", "gromacs_compiled": True})
        self.assertIn("State: passed", passed)
        self.assertIn("Review items", passed)
        self.assertIn("production MD", passed)
        self.assertIn("jobs report receptor-a", passed)
        self.assertIn("GROMACS input compilation: passed", passed)

    def test_main_json_option_and_repeated_invocation_do_not_leak_settings(self):
        value = {"state": "pending"}
        def dispatch(args):
            cli.emit(value, command="build")
            return 0
        for force in (True, False):
            output = io.StringIO()
            with patch.object(cli, "dispatch", side_effect=dispatch), contextlib.redirect_stdout(output), patch.object(output, "isatty", return_value=True):
                cli.main(["--lang", "en", *(["--json"] if force else []), "doctor"])
            if force:
                self.assertEqual(output.getvalue(), json.dumps(value, indent=2, ensure_ascii=False) + "\n")
            else:
                self.assertIn("CHARMM-GUI job", output.getvalue())
