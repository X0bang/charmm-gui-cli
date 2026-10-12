import contextlib
import io
import os
import re
import unittest
from unittest.mock import patch

from charmm_gui_cli import cli, i18n, progress, reporting


class LocalizedHelpTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language()

    def help(self, command, language):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            cli.main(["--lang", language, *command, "-h"])
        self.assertEqual(caught.exception.code, 0)
        return output.getvalue()

    def test_root_and_every_subcommand_have_localized_help(self):
        commands = next(a for a in cli.parser()._actions if isinstance(a, cli.argparse._SubParsersAction)).choices
        for command in [[], *([name] for name in commands), ["jobs", "status"], ["jobs", "report"], ["jobs", "resume"]]:
            with self.subTest(command=command):
                english = self.help(command, "en")
                self.assertIsNone(re.search(r"[\u4e00-\u9fff]", english))
                self.assertIn("options:", english)
                chinese = self.help(command, "zh")
                self.assertIn("选项", chinese)
                self.assertIn("显示帮助并退出", chinese)
        for language in ("zh", "en"):
            text = self.help([], language)
            self.assertIn("--lang", text)
            self.assertIn("--json", text)

    def test_explicit_help_language_precedes_environment(self):
        with patch.dict(os.environ, {"CHARMM_GUI_CLI_LANG": "zh"}, clear=True):
            self.assertIn("Quick start", self.help([], "en"))

    def test_invalid_arguments_do_not_echo_secrets(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as caught:
            cli.main(["--lang", "en", "--TOKEN_SECRET"])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn("TOKEN_SECRET", output.getvalue())

    def test_progress_labels_translate_but_json_presentation_does_not(self):
        event = {"state": "running ffconverter", "job_id": "123", "lastOutFile": "step5_input.out"}
        i18n.set_language("zh")
        before = progress.describe_progress(event)
        i18n.set_language("en")
        self.assertEqual(progress.describe_progress(event), before)
        english = progress.format_progress(event)
        self.assertIn("Force-field conversion", english)
        self.assertNotRegex(english, r"[\u4e00-\u9fff]")

    def test_english_text_and_html_labels(self):
        i18n.set_language("en")
        result = {"passed": True, "name": "sample", "simulation_note": "旧文案", "warnings": []}
        text = reporting.render_text(result)
        self.assertIn("Automatic validation: passed", text)
        self.assertNotRegex(text, r"[\u4e00-\u9fff]")
        html = reporting.render_html(result)
        self.assertIn('lang="en"', html)
        self.assertIn("Coordinate previews", html)
        self.assertNotIn("<script", html)
