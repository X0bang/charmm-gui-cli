"""HTTP adapter regression checks; all requests are mocked and artifacts retained."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli.auth import ToolError, atomic_json
from charmm_gui_cli.api import SubmissionUnknown
from charmm_gui_cli.web import WEB_BASE, WebClient, describe_page, form_data, modeling_target, soup_for


def response(body="ok", status=200, location=None):
    result = requests.Response()
    result.status_code = status
    result.url = WEB_BASE
    result._content = body.encode()
    result._content_consumed = True
    if location:
        result.headers["Location"] = location
    return result


class FormTests(unittest.TestCase):
    def test_successful_controls_preserve_repeated_names(self):
        form = soup_for('''<form>
            <input name="jobid" value="123"><input name="chains[]" value="A" checked type="checkbox">
            <input name="chains[]" value="B" checked type="checkbox">
            <input name="chains[]" value="C" type="checkbox">
            <input name="disabled" value="bad" disabled><input value="unnamed">
            <input type="file" name="file"><input type="submit" name="submit" value="next">
            <input name="flag" type="checkbox" checked>
            <textarea name="note">literal &amp; text</textarea>
            <select name="first"><option value="a">A</option><option value="b">B</option></select>
            <select name="many" multiple><option value="x" selected>X</option><option selected>Y</option></select>
            <select name="empty" multiple><option value="ignored">Ignored</option></select>
        </form>''').form
        self.assertEqual(form_data(form), [("jobid", "123"), ("chains[]", "A"),
            ("chains[]", "B"), ("flag", "on"), ("note", "literal & text"),
            ("first", "a"), ("many", "x"), ("many", "Y")])

    def test_selected_option_and_radio_override_defaults(self):
        form = soup_for('''<form><select name="ff"><option>A</option><option selected value="c36">C</option></select>
            <input name="mode" type="radio" value="a"><input name="mode" type="radio" value="b" checked>
        </form>''').form
        self.assertEqual(form_data(form), [("ff", "c36"), ("mode", "b")])

    def test_describe_redacts_named_credentials(self):
        description = describe_page('''<form action="?doc=sign" method="POST">
            <input name="password" value="PASSWORD_SECRET" type="password">
            <input name="csrf_token" value="TOKEN_SECRET"><input name="email" value="EMAIL_SECRET">
            <input name="jobid" value="123"></form><script src="scripts/form.js"></script>''')
        serialized = json.dumps(description)
        for secret in ("PASSWORD_SECRET", "TOKEN_SECRET", "EMAIL_SECRET"):
            self.assertNotIn(secret, serialized)
        self.assertTrue(description["login_form_present"])
        self.assertIn("123", serialized)
        self.assertEqual(description["scripts"], ["scripts/form.js"])

    def test_password_type_is_sensitive_even_without_standard_name(self):
        value = describe_page('<form><input type="password" name="pass" value="TYPE_SECRET"></form>')
        self.assertNotIn("TYPE_SECRET", json.dumps(value))

    def test_sensitive_select_options_are_redacted(self):
        value = describe_page('<form><select name="session_token"><option value="VALUE_SECRET" selected>TEXT_SECRET</option></select></form>')
        for secret in ("VALUE_SECRET", "TEXT_SECRET"):
            self.assertNotIn(secret, json.dumps(value))


class WebClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(spec=requests.Session)
        self.session.headers = {}
        self.session.cookies = requests.cookies.RequestsCookieJar()
        self.client = WebClient(session=self.session)

    def test_allowed_request_has_timeout_and_manual_redirects(self):
        self.session.request.return_value = response()
        self.client.request("GET", "?doc=input/pdbreader")
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("GET", WEB_BASE + "?doc=input/pdbreader"))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (15, 90))

    def test_foreign_origins_and_http_are_never_requested(self):
        for target in ("http://charmm-gui.org/", "https://example.org/", "//example.org/",
                       "https://charmm-gui.org.example.org/"):
            with self.subTest(target=target), self.assertRaises(ToolError):
                self.client.request("GET", target)
        self.session.request.assert_not_called()

    def test_nonstandard_port_and_userinfo_are_rejected(self):
        for target in ("https://charmm-gui.org:8443/", "https://user:secret@charmm-gui.org/"):
            with self.subTest(target=target), self.assertRaises(ToolError):
                self.client.request("GET", target)
        self.session.request.assert_not_called()

    def test_external_redirect_stops_before_forwarding_credentials(self):
        self.session.request.return_value = response(status=307, location="https://example.org/")
        with self.assertRaises(ToolError):
            self.client.request("POST", "?doc=sign", data={"password": "secret"})
        self.assertEqual(self.session.request.call_count, 1)

    def test_303_changes_post_to_get_and_drops_form_data(self):
        self.session.request.side_effect = [response(status=303, location="?doc=input"), response()]
        self.client.request("POST", "?doc=sign", data={"password": "secret"})
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("GET", WEB_BASE + "?doc=input"))
        self.assertNotIn("data", kwargs)

    def test_307_preserves_get_on_official_origin(self):
        self.session.request.side_effect = [response(status=307, location="https://charmm-gui.org/"), response()]
        self.client.request("GET", "")
        self.assertEqual(self.session.request.call_args.args, ("GET", "https://charmm-gui.org/"))

    def test_307_and_308_never_replay_a_post(self):
        for status in (307, 308):
            self.session.request.reset_mock()
            self.session.request.return_value = response(status=status, location="https://charmm-gui.org/")
            with self.assertRaises(SubmissionUnknown):
                self.client.request("POST", "", data={"jobid": "123"})
            self.assertEqual(self.session.request.call_count, 1)

    def test_redirect_cycle_is_bounded(self):
        self.session.request.side_effect = [response(status=302, location="?doc=loop") for _ in range(10)]
        with self.assertRaisesRegex(ToolError, "redirects"):
            self.client.request("GET", "")
        self.assertLessEqual(self.session.request.call_count, 6)

    def test_errors_never_print_or_embed_secret_response(self):
        output = io.StringIO()
        for failure in (response("BODY_SECRET", 403), requests.ConnectionError("URL_SECRET")):
            self.session.request.side_effect = failure if isinstance(failure, Exception) else None
            self.session.request.return_value = failure
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                with self.assertRaises(ToolError) as caught:
                    self.client.request("POST", "?doc=sign", data={"password": "PASSWORD_SECRET"})
            for secret in ("BODY_SECRET", "URL_SECRET", "PASSWORD_SECRET"):
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, output.getvalue())

    def test_foreign_cookie_domain_is_rejected(self):
        root = Path(tempfile.mkdtemp(prefix="cgui-web-test-"))
        path = root / "cookies.json"
        atomic_json(path, {"cookies": [{"name": "session", "value": "SECRET", "domain": ".example.org"}]})
        with self.assertRaises(ToolError) as caught:
            WebClient(path, session=self.session)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_login_posts_only_to_official_form_and_reports_no_credentials(self):
        self.session.request.side_effect = [response('<form action="?doc=sign" method="POST"><input type="password" name="password"><input name="email"><input name="csrf" value="FORM_TOKEN"></form>'), response('<a href="?doc=logout">Logout</a>')]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = self.client.login("test@example.org", "LOGIN_SECRET")
        self.assertTrue(result["web_authenticated"])
        self.assertIn(("password", "LOGIN_SECRET"), self.session.request.call_args.kwargs["data"])
        self.assertIn(("csrf", "FORM_TOKEN"), self.session.request.call_args.kwargs["data"])
        self.assertNotIn("LOGIN_SECRET", output.getvalue() + json.dumps(result))

    def test_login_form_remaining_is_failure(self):
        html = '<form><input type="password" name="password"></form>'
        self.session.request.side_effect = [response(html), response(html)]
        with self.assertRaisesRegex(ToolError, "not confirmed"):
            self.client.login("test@example.org", "secret")

    def test_modeling_action_uses_unique_actual_doc_parameter(self):
        for action in (None, "", "?doc=sign&next=doc=input/pdbreader", "/other?doc=input/pdbreader",
                       "?doc=input/pdbreader&doc=sign", "https://example.org/?doc=input/pdbreader",
                       "?doc=input/../sign"):
            with self.subTest(action=action), self.assertRaises(ToolError):
                modeling_target(action)
        for action in ("?doc=input/pdbreader&step=2", "/index.php?doc=input/membrane.bilayer&step=3"):
            self.assertTrue(modeling_target(action).startswith(WEB_BASE))

    def test_two_concurrent_step_attempts_make_only_one_post(self):
        root = Path(tempfile.mkdtemp(prefix="cgui-web-claim-test-"))
        snapshot, output = root / "before.html", root / "after.html"
        snapshot.write_text('<form action="?doc=input/pdbreader&step=2"><input name="jobid" value="123"></form>')
        barrier = threading.Barrier(2)
        original = form_data

        def synchronized_form_data(form):
            barrier.wait(timeout=5)
            return original(form)

        self.session.request.return_value = response('<input name="jobid" value="123">')

        def attempt():
            try:
                self.client.submit_snapshot(snapshot, output)
                return "posted"
            except ToolError:
                return "blocked"

        with patch("charmm_gui_cli.web.form_data", side_effect=synchronized_form_data):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["posted", "blocked"])
        self.assertEqual(self.session.request.call_count, 1)
        self.assertTrue(output.with_suffix(".intent.json").is_file())


if __name__ == "__main__":
    unittest.main()
