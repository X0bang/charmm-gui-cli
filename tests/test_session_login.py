"""Unified CLI login/profile regressions; no live credentials or requests."""

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from charmm_gui_cli import auth
from charmm_gui_cli.auth import ToolError, atomic_write
from charmm_gui_cli.cli import main


@contextlib.contextmanager
def chdir(path):
    """Python 3.10-compatible cwd scope; restores state without deleting files."""
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def fake_token(label="global"):
    claims = json.dumps({"exp": time.time() + 3600, "test_profile": label}).encode()
    payload = base64.urlsafe_b64encode(claims).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + payload + ".testing_signature"


class SessionLoginTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-session-login-test-"))
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch("pathlib.Path.home", return_value=self.root))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch("requests.Session.request", side_effect=AssertionError("Unexpected network request")))
        self.value = fake_token()
        self.email = "test@example.invalid"
        self.password = "test-only-secret"

    def tearDown(self):
        self.stack.close()  # Restore mocks only; preserve every test artifact.

    def login(self, options=(), web_error=None):
        output, errors = io.StringIO(), io.StringIO()
        with patch("charmm_gui_cli.api.Client.login", return_value=self.value) as api_login, \
                patch("charmm_gui_cli.web.WebClient.login", return_value={"web_authenticated": True}, side_effect=web_error) as web_login, \
                patch("charmm_gui_cli.cli.getpass", return_value=self.password), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["login", "--email", self.email, *options])
        self.assertNotIn(self.password, output.getvalue() + errors.getvalue())
        self.assertNotIn(self.value, output.getvalue() + errors.getvalue())
        return code, api_login, web_login, output.getvalue(), errors.getvalue()

    def test_login_defaults_to_api_and_http_session_in_global_profile(self):
        code, api_login, web_login, output, _ = self.login()
        self.assertEqual(code, 0)
        api_login.assert_called_once_with(self.email, self.password)
        web_login.assert_called_once_with(self.email, self.password)
        self.assertEqual(auth.default_token_path().read_text().strip(), self.value)
        self.assertTrue(auth.default_cookie_path().is_file())
        self.assertEqual(stat.S_IMODE(auth.default_token_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(auth.default_cookie_path().stat().st_mode), 0o600)
        self.assertIn(str(auth.default_cookie_path()), output)

    def test_custom_token_destination_defaults_to_paired_cookie(self):
        target = self.root / "paired" / "account.token"
        code, api_login, web_login, _, _ = self.login(["--save-to", str(target)])
        self.assertEqual(code, 0)
        api_login.assert_called_once()
        web_login.assert_called_once()
        self.assertEqual(target.read_text().strip(), self.value)
        self.assertTrue(target.with_name("session.cookies.json").is_file())
        self.assertFalse(auth.default_cookie_path().exists())

    def test_http_login_failure_does_not_overwrite_previous_token(self):
        old = fake_token("previous")
        atomic_write(auth.default_token_path(), old + "\n")
        atomic_write(auth.default_cookie_path(), '{"cookies": []}\n')
        old_cookie = auth.default_cookie_path().read_bytes()
        code, _, web_login, _, _ = self.login(web_error=ToolError("HTTP login rejected"))
        self.assertEqual(code, 2)
        web_login.assert_called_once()
        self.assertEqual(auth.default_token_path().read_text().strip(), old)
        self.assertEqual(auth.default_cookie_path().read_bytes(), old_cookie)

    def test_explicit_login_token_and_cookie_destinations_remain_supported(self):
        token_path = self.root / "custom" / "account.token"
        cookie_path = self.root / "custom" / "account.cookies.json"
        code, _, _, _, _ = self.login(["--save-to", str(token_path), "--cookies", str(cookie_path)])
        self.assertEqual(code, 0)
        self.assertEqual(token_path.read_text().strip(), self.value)
        self.assertTrue(cookie_path.is_file())
        self.assertFalse(auth.default_token_path().exists())
        self.assertFalse(auth.default_cookie_path().exists())

    def test_global_profile_wins_over_working_directory_legacy_token(self):
        self.assertEqual(self.login()[0], 0)
        work = self.root / "different-project"
        work.mkdir()
        atomic_write(work / "session.token", fake_token("unrelated-old-project"))
        with chdir(work):
            token, source = auth.load_token()
        self.assertEqual(token, self.value)
        self.assertEqual(source, str(auth.default_token_path()))

    def test_cross_directory_build_uses_same_global_profile_without_path_flags(self):
        self.assertEqual(self.login()[0], 0)
        for project in ("first-project", "second-project"):
            work = self.root / project
            work.mkdir()
            atomic_write(work / "session.token", fake_token(project))
            with chdir(work), \
                    patch("charmm_gui_cli.full_build.initialize") as initialize, \
                    patch("charmm_gui_cli.full_build.advance", return_value={"state": "preparation_running"}) as advance, \
                    patch("charmm_gui_cli.web.WebClient") as web, contextlib.redirect_stdout(io.StringIO()):
                code = main(["build", "system.yaml", "--out", "run"])
            self.assertEqual(code, 0)
            initialize.assert_called_once()
            web.assert_called_once_with(auth.default_cookie_path())
            self.assertEqual(advance.call_args.args[2].token, self.value)

    def test_explicit_build_session_paths_override_global_profile(self):
        self.assertEqual(self.login()[0], 0)
        custom_token = self.root / "another.token"
        custom_cookie = self.root / "another.cookies.json"
        other_value = fake_token("explicit")
        atomic_write(custom_token, other_value)
        atomic_write(custom_cookie, '{"cookies": []}\n')
        with patch("charmm_gui_cli.full_build.initialize"), \
                patch("charmm_gui_cli.full_build.advance", return_value={"state": "preparation_running"}) as advance, \
                patch("charmm_gui_cli.web.WebClient") as web, contextlib.redirect_stdout(io.StringIO()):
            code = main(["--token-file", str(custom_token), "build", "system.yaml", "--out", str(self.root / "run"),
                         "--cookies", str(custom_cookie)])
        self.assertEqual(code, 0)
        web.assert_called_once_with(custom_cookie)
        self.assertEqual(advance.call_args.args[2].token, other_value)

    def test_missing_paired_global_cookie_never_falls_back_to_cwd_cookie(self):
        atomic_write(auth.default_token_path(), self.value)
        project = self.root / "legacy-project"
        project.mkdir()
        atomic_write(project / "session.cookies.json", '{"cookies": []}\n')
        with chdir(project), patch("charmm_gui_cli.full_build.initialize") as initialize, \
                contextlib.redirect_stderr(io.StringIO()):
            code = main(["build", "system.yaml", "--out", "run"])
        self.assertEqual(code, 2)
        initialize.assert_not_called()

    def test_cookie_resolution_tracks_token_source_and_explicit_override(self):
        custom = self.root / "selected" / "session.token"
        explicit = self.root / "specific.cookie"
        self.assertEqual(auth.resolve_cookie_path(token_source=str(custom)), custom.with_name("session.cookies.json"))
        self.assertEqual(auth.resolve_cookie_path(explicit, str(custom)), explicit)
        self.assertEqual(auth.resolve_cookie_path(token_source="CHARMMGUI_TOKEN"), auth.default_cookie_path())


if __name__ == "__main__":
    unittest.main()
