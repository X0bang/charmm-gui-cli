import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli import cli
from charmm_gui_cli.locales import i18n
from charmm_gui_cli.remote.api import AuthError, Client, SubmissionUnknown
from charmm_gui_cli.store.auth import ToolError, recovery_guidance


class LocalizedErrorTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language()

    def test_legacy_message_and_dictionary_remain_compatible(self):
        error = ToolError("Legacy message", category="example")
        self.assertEqual(error.localized(), "Legacy message")
        self.assertEqual(error.as_dict(), {"message": "Legacy message", "category": "example", "next_step": "charmm-gui-cli -h", "retryable": False})

    def test_key_error_localizes_without_changing_machine_category(self):
        error = AuthError(key="api.auth_http", params={"code": 401})
        for language, prefix in (("zh", "API 返回"), ("en", "API returned")):
            i18n.set_language(language)
            self.assertTrue(error.localized().startswith(prefix))
            self.assertEqual(error.category, "authentication")
            self.assertEqual(str(error), "API returned HTTP 401.")

    def test_bad_credentials_signature_is_safe_and_login_specific(self):
        for text in ("No user or incorrect password!", "NO USER OR INCORRECT PASSWORD!"):
            response = requests.Response()
            response.status_code = 404
            response._content = text.encode()
            session = Mock()
            session.request.return_value = response
            with self.assertRaises(AuthError) as caught:
                Client(session=session).login("user@example.invalid", "PASSWORD_SECRET")
            error = caught.exception
            self.assertEqual(error.key, "login.bad_creds")
            self.assertEqual(error.next_step, "charmm-gui-cli login")
            self.assertEqual(session.request.call_count, 1)
            for language in ("zh", "en"):
                i18n.set_language(language)
                output = error.localized()
                self.assertNotIn(text, output)
                self.assertNotIn("PASSWORD_SECRET", output)
                self.assertNotIn("resubmit", output)

    def test_generic_404_is_not_bad_credentials(self):
        response = requests.Response()
        response.status_code = 404
        response._content = b"BODY_SECRET not found"
        session = Mock()
        session.request.return_value = response
        with self.assertRaises(ToolError) as caught:
            Client(session=session).login("user", "secret")
        self.assertEqual(caught.exception.key, "api.http")
        self.assertNotIn("BODY_SECRET", caught.exception.localized())

    def test_main_bad_credentials_has_no_modeling_guidance(self):
        for language, expected in (("zh", "邮箱或密码错误"), ("en", "Incorrect email or password")):
            output = io.StringIO()
            with patch.object(cli, "dispatch", side_effect=AuthError(key="login.bad_creds")), contextlib.redirect_stderr(output):
                self.assertEqual(cli.main(["--lang", language, "login"]), 2)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn("No user or", output.getvalue())
            self.assertIn("charmm-gui-cli login", output.getvalue())
            self.assertNotIn("resume", output.getvalue())

    def test_recovery_semantics_in_both_languages(self):
        categories = ("authentication", "auth_required", "access_denied", "transport_error", "archive_preparing",
                      "transient_network", "transient_http", "rate_limited", "interrupted", "wait_timeout")
        for language in ("zh", "en"):
            i18n.set_language(language)
            for category in categories:
                self.assertTrue(recovery_guidance(category))
            unknown = SubmissionUnknown("Uncertain", cause_category="authentication")
            self.assertEqual(unknown.category, "submission_unknown")
            self.assertIn("login", unknown.next_step)
            self.assertIn("attach", unknown.next_step)
            self.assertIn("POST", unknown.next_step)
            self.assertFalse(unknown.retryable)
            self.assertIsNone(recovery_guidance("other"))

    def test_filesystem_and_interruption_do_not_echo_exception(self):
        for language in ("zh", "en"):
            for exception, code in ((OSError("SECRET"), 2), (KeyboardInterrupt(), 130), (EOFError(), 130)):
                output = io.StringIO()
                with patch.object(cli, "dispatch", side_effect=exception), contextlib.redirect_stderr(output):
                    self.assertEqual(cli.main(["--lang", language, "doctor"]), code)
                self.assertNotIn("SECRET", output.getvalue())
