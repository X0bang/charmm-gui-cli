"""Credential/session lifecycle fixtures; no live accounts or OS vault writes."""

import base64
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli.cli import main
from charmm_gui_cli.remote import sessions
from charmm_gui_cli.remote.api import Client, AuthError, SubmissionUnknown
from charmm_gui_cli.remote.web import WebClient
from charmm_gui_cli.store.auth import ToolError, atomic_write


def jwt(expiry, label="old"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry, "sub": "fixture-user", "test_label": label}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + payload + ".fixture-signature"


class Vault:
    priority = 5
    __module__ = "keyring.backends.SecretService"

    def __init__(self):
        self.passwords = {}

    def get_password(self, service, user):
        return self.passwords.get((service, user))

    def set_password(self, service, user, value):
        self.passwords[service, user] = value

    def delete_password(self, service, user):
        self.passwords.pop((service, user))


class Terminal(io.StringIO):
    def isatty(self):
        return True


class SessionRenewalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-session-renewal-")).resolve()
        self.token = self.root / "session.token"
        self.cookies = self.root / "session.cookies.json"
        self.email, self.password = "fixture@example.invalid", "fixture-password-not-for-output"
        self.old = jwt(time.time() - 10)
        self.new = jwt(time.time() + 36000, "new")
        self.vault = Vault()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch("pathlib.Path.home", return_value=self.root))
        self.stack.enter_context(patch("requests.Session.request", side_effect=AssertionError("No live network")))
        self.original_backend = sessions._backend
        self.stack.enter_context(patch.object(sessions, "_backend", return_value=self.vault))
        self.stack.enter_context(patch.object(sessions, "_WARNED", set()))

    def profile(self, *, token=None, source="keyring"):
        web = WebClient()
        web.session.cookies.set("sid", "fixture-old-cookie", domain="www.charmm-gui.org", secure=True)
        with sessions.session_lock(self.token, self.cookies):
            sessions.save_pair(self.token, self.cookies, token or self.old, web, self.email, source)
        self.vault.set_password(sessions._service(self.token), self.email, self.password)
        return sessions.SessionManager(str(self.token), self.cookies)

    def login_mocks(self, *, web_error=None):
        api = self.stack.enter_context(patch.object(Client, "login", return_value=self.new))
        def website(web, email, password):
            if web_error:
                raise web_error
            web.session.cookies.set("sid", "fixture-new-cookie", domain="www.charmm-gui.org", secure=True)
            return {"web_authenticated": True}
        web = self.stack.enter_context(patch.object(WebClient, "login", autospec=True, side_effect=website))
        return api, web

    def call(self, args, *, tty=False):
        output, error = Terminal() if tty else io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(args)
        text = output.getvalue() + error.getvalue()
        for secret in (self.password, self.old, self.new, "fixture-old-cookie", "fixture-new-cookie"):
            self.assertNotIn(secret, text)
        return code, output.getvalue(), error.getvalue()

    def test_cli_remember_saves_os_secret_but_no_password_in_profile_or_output(self):
        self.login_mocks()
        with patch("getpass.getpass", return_value=self.password):
            code, output, error = self.call(["--lang", "zh", "login", "--email", self.email, "--save-to", str(self.token), "--remember"], tty=True)
        self.assertEqual(code, 0, error)
        self.assertIn("系统凭据库", output)
        profile = json.loads(sessions.account_path(self.token).read_text())
        self.assertEqual(profile["credential_source"], "keyring")
        self.assertNotIn(self.password, json.dumps(profile))
        self.assertEqual(self.vault.get_password(sessions._service(self.token), self.email), self.password)
        for path in (self.token, self.cookies, sessions.account_path(self.token)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(set(json.loads(self.cookies.read_text())), {"version", "cookies"})

    def test_cli_headless_password_command_and_command_not_stored(self):
        self.login_mocks()
        command = shlex.join([sys.executable, "-c", f"print({self.password!r})"])
        with patch.dict(os.environ, {"CHARMM_GUI_PASSWORD_COMMAND": command}), patch("getpass.getpass") as prompt:
            code, output, error = self.call(["login", "--email", self.email, "--save-to", str(self.token), "--json"])
        self.assertEqual(code, 0, error)
        prompt.assert_not_called()
        self.assertTrue(json.loads(output)["password_command"])
        profile = sessions.account_path(self.token).read_text()
        self.assertNotIn(command, profile)
        self.assertNotIn(self.password, profile)

    def test_expired_token_renews_both_transports_before_first_get(self):
        manager = self.profile()
        api_login, web_login = self.login_mocks()
        client = Client(self.old)
        sessions.bind(client, str(self.token), self.cookies, manager)
        response = Mock(status_code=200)
        response.json.return_value = {"jobid": "123", "status": "done"}
        client.session.request = Mock(return_value=response)
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(client.status("123")["status"], "done")
        api_login.assert_called_once_with(self.email, self.password)
        web_login.assert_called_once()
        self.assertEqual(client.session.request.call_count, 1)
        self.assertEqual(client.session.request.call_args.args[0], "GET")
        self.assertNotIn(self.password, error.getvalue())
        self.assertEqual(self.token.read_text().strip(), self.new)
        self.assertIn("fixture-new-cookie", self.cookies.read_text())

    def test_http_401_get_can_refresh_once_but_403_cannot(self):
        for code in (401, 403):
            with self.subTest(code=code):
                manager = self.profile(token=jwt(time.time() + 3600, str(code)))
                client = Client(self.token.read_text().strip(), renewer=manager)
                login = self.stack.enter_context(patch.object(Client, "login", return_value=self.new))
                web_login = self.stack.enter_context(patch.object(WebClient, "login", return_value={"web_authenticated": True}))
                bad, good = Mock(status_code=code), Mock(status_code=200)
                good.json.return_value = {"jobid": "123", "status": "done"}
                client.session.request = Mock(side_effect=[bad, good])
                with redirect_stderr(io.StringIO()):
                    if code == 401:
                        self.assertEqual(client.status("123")["status"], "done")
                        login.assert_called_once()
                        self.assertEqual(client.session.request.call_count, 2)
                    else:
                        with self.assertRaises(AuthError):
                            client.status("123")
                        login.assert_not_called()
                        self.assertEqual(client.session.request.call_count, 1)

    def test_post_auth_failure_remains_unknown_without_login_or_replay(self):
        manager = self.profile(token=jwt(time.time() + 3600))
        api_login, web_login = self.login_mocks()
        client = Client(self.token.read_text().strip(), renewer=manager)
        client.session.request = Mock(return_value=Mock(status_code=401))
        with self.assertRaises(SubmissionUnknown) as error:
            client.submit({"fixture": "modeling"})
        self.assertEqual(error.exception.category, "submission_unknown")
        api_login.assert_not_called()
        web_login.assert_not_called()
        self.assertEqual(client.session.request.call_count, 1)
        self.assertEqual(client.session.request.call_args.args[0], "POST")

    def test_web_modeling_post_401_preserves_durable_unknown_intent(self):
        manager = self.profile(token=jwt(time.time() + 3600))
        api_login, web_login = self.login_mocks()
        web = WebClient(self.cookies)
        sessions.bind(web, str(self.token), self.cookies, manager)
        web.session.request = Mock(return_value=Mock(status_code=401, is_redirect=False))
        intent, path = {"state": "submitting"}, self.root / "fixture.intent.json"
        with self.assertRaises(SubmissionUnknown):
            web._modeling_post("?doc=input/membrane.bilayer", path, intent, data={"fixture": "modeling"})
        self.assertEqual(json.loads(path.read_text())["state"], "submission_unknown")
        self.assertEqual(web.session.request.call_count, 1)
        api_login.assert_not_called()
        web_login.assert_not_called()

    def test_failed_web_renewal_keeps_previous_pair_and_profile_unchanged(self):
        manager = self.profile()
        before = {path: path.read_bytes() for path in (self.token, self.cookies, sessions.account_path(self.token))}
        self.login_mocks(web_error=ToolError("must never expose " + self.password))
        with self.assertRaises(ToolError) as error:
            manager.token(self.old)
        self.assertEqual(error.exception.key, "session.renew_failed")
        self.assertNotIn(self.password, error.exception.localized())
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_expired_cookie_can_renew_with_a_still_valid_api_token_before_intent(self):
        web = WebClient()
        web.session.cookies.set("sid", "fixture-old-cookie", domain="www.charmm-gui.org", expires=int(time.time()) - 10)
        current = jwt(time.time() + 3600)
        with sessions.session_lock(self.token, self.cookies):
            sessions.save_pair(self.token, self.cookies, current, web, self.email, "keyring")
        self.vault.set_password(sessions._service(self.token), self.email, self.password)
        client = WebClient(self.cookies)
        sessions.bind(client, str(self.token), self.cookies)
        login, website = self.login_mocks()
        with redirect_stderr(io.StringIO()):
            client.validate_auth()
        login.assert_called_once()
        website.assert_called_once()
        self.assertTrue(any(not cookie.is_expired() for cookie in client.session.cookies))

    def test_paired_publication_failure_and_interrupt_roll_back_both_files(self):
        self.profile(token=self.new)
        before = {path: path.read_bytes() for path in (self.token, self.cookies, sessions.account_path(self.token))}
        web = WebClient()
        web.session.cookies.set("sid", "different-fixture-cookie", domain="www.charmm-gui.org")
        original = sessions.atomic_write
        for exception in (OSError("simulated failure"), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__):
                fired = False
                def write(path, data):
                    nonlocal fired
                    if Path(path) == self.token and not fired:
                        fired = True
                        raise exception
                    return original(path, data)
                with patch.object(sessions, "atomic_write", side_effect=write), self.assertRaises(type(exception) if isinstance(exception, KeyboardInterrupt) else ToolError):
                    sessions.save_pair(self.token, self.cookies, self.old, web, self.email, None)
                self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_failed_renewal_cooldown_is_shared_with_another_process_context(self):
        manager = self.profile()
        login, website = self.login_mocks(web_error=ToolError("rejected"))
        with redirect_stderr(io.StringIO()), self.assertRaises(ToolError):
            manager.token(self.old)
        another = sessions.SessionManager(str(self.token), self.cookies)
        with self.assertRaises(ToolError):
            another.token(self.old)
        login.assert_called_once()
        website.assert_called_once()

    def test_actual_status_cli_renews_before_query_without_credential_output(self):
        self.profile()
        login, website = self.login_mocks()
        response = Mock(status_code=200)
        response.json.return_value = {"jobid": "123", "status": "running", "lastInpStep": "input", "lastOutFile": "output"}
        with patch("requests.Session.request", return_value=response) as request:
            code, output, error = self.call(["--token-file", str(self.token), "status", "123", "--json"])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["job_id"], "123")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")
        login.assert_called_once()

    def test_actual_foreground_build_binds_the_same_renewal_to_both_transports(self):
        from test_precheck import PrecheckTests
        fixture = PrecheckTests()
        fixture.setUp()
        self.profile()
        login, website = self.login_mocks()
        def advance(directory, web, api, **kwargs):
            self.assertIs(api.renewer, web.renewer)
            self.assertEqual(api.validate_auth(), self.new)
            web.validate_auth()
            self.assertEqual(web.session.cookies.get("sid"), "fixture-new-cookie")
            return {"state": "bilayer_submitted", "source_job_id": "123", "build_job_id": "456"}
        with patch("charmm_gui_cli.pipeline.full_build.advance", side_effect=advance):
            code, output, error = self.call(["--foreground", "--token-file", str(self.token), "build", "--protein", str(fixture.protein),
                "--ligand", str(fixture.ligand), "--out", str(self.root / "foreground-task"), "--hydrogens", "preserve", "--no-grompp", "--no-preview", "--json"])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["state"], "bilayer_submitted")
        login.assert_called_once()
        website.assert_called_once()

    def test_second_manager_adopts_concurrent_renewal_without_a_second_login(self):
        first = self.profile()
        second = sessions.SessionManager(str(self.token), self.cookies)
        login, web = self.login_mocks()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(first.token(self.old), self.new)
            self.assertEqual(second.token(self.old), self.new)
        login.assert_called_once()
        web.assert_called_once()

    def test_read_only_reconcile_context_never_logs_in_even_when_expired(self):
        self.profile()
        login, website = self.login_mocks()
        manager = sessions.SessionManager(str(self.token), self.cookies, allow_renew=False)
        web = WebClient(self.cookies)
        sessions.bind(web, str(self.token), self.cookies, manager)
        web.session.request = Mock()
        with self.assertRaises(ToolError):
            web.request("GET", "?doc=input/pdbreader")
        login.assert_not_called()
        website.assert_not_called()
        web.session.request.assert_not_called()

    def test_manual_login_and_environment_tokens_do_not_enable_hidden_renewal(self):
        manager = self.profile(source=None)
        login, web = self.login_mocks()
        with self.assertRaises(ToolError) as error:
            manager.token(self.old)
        self.assertEqual(error.exception.key, "session.not_configured")
        login.assert_not_called()
        env = sessions.SessionManager("CHARMMGUI_TOKEN")
        self.assertIsNone(env.profile)
        self.assertEqual(env.token(self.old), self.old)

    def test_changed_cookie_or_account_is_not_silently_replaced(self):
        manager = self.profile()
        login, web = self.login_mocks()
        atomic_write(self.cookies, '{"cookies": []}\n')
        with self.assertRaises(ToolError) as error:
            manager.token(self.old)
        self.assertEqual(error.exception.key, "session.changed")
        login.assert_not_called()

    def test_insecure_keyring_backend_is_rejected(self):
        class Insecure:
            priority = 100
            __module__ = "keyrings.alt.file"
        fake = Mock()
        fake.get_keyring.return_value = Insecure()
        with patch("importlib.import_module", return_value=fake), self.assertRaises(ToolError):
            self.original_backend()

    def test_password_command_bounds_output_and_hides_failure_stdout_and_stderr(self):
        for body in (f"import sys; print({self.password!r}); sys.exit(1)", "print('x'*5000)", "print('a\\nb')"):
            command = shlex.join([sys.executable, "-c", body])
            with patch.dict(os.environ, {"CHARMM_GUI_PASSWORD_COMMAND": command}), self.assertRaises(ToolError) as error:
                sessions.command_password()
            self.assertEqual(error.exception.key, "session.command")
            self.assertNotIn(self.password, error.exception.localized())
            self.assertNotIn(command, error.exception.localized())

    def test_expiry_warning_is_localized_and_once_per_profile(self):
        token = jwt(time.time() + 3600)
        output = io.StringIO()
        with redirect_stderr(output):
            sessions.warn_expiry(token, str(self.token))
            sessions.warn_expiry(token, str(self.token))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertNotIn(token, output.getvalue())

    def test_doctor_decodes_remaining_lifetime_without_network_or_keyring_access(self):
        self.profile(token=self.new)
        with patch.object(sessions, "_backend") as backend:
            code, output, error = self.call(["doctor", "--token-file", str(self.token), "--json"])
        report = json.loads(output)
        self.assertGreater(report["session"]["remaining_seconds"], 35000)
        self.assertFalse(report["session"]["signature_verified_locally"])
        self.assertFalse(report["session"]["server_checked"])
        backend.assert_not_called()

    def test_logout_only_removes_selected_credentials_and_leaves_tasks_and_env(self):
        self.profile(token=self.new)
        task = self.root / "existing-task"
        task.mkdir()
        (task / "run.log").write_text("retained")
        with patch.dict(os.environ, {"CHARMMGUI_TOKEN": self.new}):
            code, output, error = self.call(["--token-file", str(self.token), "logout", "--json"])
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(len(report["removed_files"]), 3)
        self.assertFalse(report["remote_revoked"])
        self.assertTrue(report["environment_token_active"])
        self.assertEqual((task / "run.log").read_text(), "retained")
        self.assertEqual(self.vault.passwords, {})

    def test_logout_rejects_symlink_and_foreign_file_without_deleting_it(self):
        self.profile(token=self.new)
        foreign = self.root / "important.txt"
        foreign.write_text("retain this file")
        with self.assertRaises(ToolError):
            sessions.logout(self.token, foreign)
        self.assertEqual(foreign.read_text(), "retain this file")
        self.assertTrue(self.token.is_file())

    def test_real_concurrent_processes_only_perform_one_paired_login(self):
        self.profile()
        context = multiprocessing.get_context("fork")
        count, results = context.Value("i", 0), context.Queue()
        def api_login(api, email, password):
            with count.get_lock():
                count.value += 1
            time.sleep(.05)
            return self.new
        def run():
            manager = sessions.SessionManager(str(self.token), self.cookies)
            with redirect_stderr(io.StringIO()):
                results.put(manager.token(self.old) == self.new)
        with patch.object(Client, "login", autospec=True, side_effect=api_login), \
                patch.object(WebClient, "login", return_value={"web_authenticated": True}):
            workers = [context.Process(target=run) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
        self.assertEqual(count.value, 1)
        self.assertTrue(results.get(timeout=2))
        self.assertTrue(results.get(timeout=2))

    def test_command_timeout_terminates_only_the_credential_helper(self):
        command = shlex.join([sys.executable, "-c", "import time; time.sleep(120)"])
        with patch.dict(os.environ, {"CHARMM_GUI_PASSWORD_COMMAND": command}), \
                patch.object(sessions.time, "monotonic", side_effect=[0., 31.]), self.assertRaises(ToolError) as error:
            sessions.command_password()
        self.assertEqual(error.exception.key, "session.command")

    def test_custom_cookie_destination_is_followed_without_repeating_the_flag(self):
        custom = self.root / "custom folder" / "web.json"
        self.cookies = custom
        manager = self.profile()
        auto = sessions.SessionManager(str(self.token))
        self.assertEqual(auto.cookies, custom)
        self.assertEqual(sessions.session_cookie_path(None, str(self.token)), custom)
        self.login_mocks()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(auto.token(self.old), self.new)

    def test_web_only_login_cannot_overwrite_a_paired_session(self):
        self.profile(token=self.new)
        before = self.cookies.read_bytes()
        with patch("getpass.getpass") as password:
            code, output, error = self.call(["--token-file", str(self.token), "web-login", "--email", self.email])
        self.assertEqual(code, 2)
        password.assert_not_called()
        self.assertEqual(self.cookies.read_bytes(), before)

    def test_logout_locked_keyring_does_not_falsely_claim_success(self):
        self.profile(token=self.new)
        with patch.object(sessions, "_backend", side_effect=ToolError("never echo " + self.password)):
            code, output, error = self.call(["--lang", "zh", "--token-file", str(self.token), "logout"])
        self.assertEqual(code, 2)
        self.assertTrue(self.token.is_file())
        self.assertIn("logout", error)
        self.assertNotIn(self.password, error)

    def test_default_logout_selects_a_legacy_project_token(self):
        from test_session_login import chdir
        self.profile(token=self.new, source=None)
        # A legacy login has no account metadata and may live in the cwd.
        sessions.account_path(self.token).rename(self.root / "retained-account.json")
        with chdir(self.root):
            code, output, error = self.call(["logout", "--json"])
        self.assertEqual(code, 0, error)
        self.assertFalse(self.token.exists())
        self.assertFalse(self.cookies.exists())

    def test_logout_help_and_doctor_terminal_are_bilingual_without_passwords(self):
        self.profile(token=self.new)
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                code, output, error = self.call(["--lang", lang, "--token-file", str(self.token), "doctor"], tty=True)
                self.assertNotIn("\"token_saved\"", output)
                self.assertIn("秒" if lang == "zh" else "seconds", output)
                self.assertNotIn(self.new, output)


if __name__ == "__main__":
    unittest.main()
