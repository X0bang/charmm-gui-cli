"""Public help, compatibility parsing and value-free option errors."""

from contextlib import redirect_stdout, redirect_stderr
import io
import os
import re
import unittest
from unittest.mock import patch

from charmm_gui_cli import cli
from charmm_gui_cli.locales.i18n import set_language, t
from charmm_gui_cli.presentation.arguments import COMMON_COMMANDS
from charmm_gui_cli.pipeline.build_config import validate_preparation


class GuidedHelpTests(unittest.TestCase):
    def tearDown(self):
        set_language()

    def call(self, args, language="en"):
        output, error = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"COLUMNS": "100"}), redirect_stdout(output), redirect_stderr(error), \
                patch.object(cli, "dispatch", side_effect=AssertionError("Help/errors must not dispatch commands")):
            try:
                code = cli.main(["--lang", language, *args])
            except SystemExit as result:
                code = result.code
        return code, output.getvalue(), error.getvalue()

    def test_default_help_is_short_curated_and_has_executable_workflow(self):
        for language in ("zh", "en"):
            code, output, error = self.call(["-h"], language)
            self.assertEqual(code, 0, error)
            self.assertLessEqual(len(output.splitlines()), 70)
            choices = re.findall(r"^    ([a-z][a-z-]+)\s", output, re.M)
            self.assertEqual(choices, list(COMMON_COMMANDS))
            for command in ("login", "check --protein protein.pdb --ligand bound.sdf",
                            "jobs watch receptor-a", "jobs status receptor-a", "jobs resume receptor-a"):
                self.assertIn("charmm-gui-cli " + command, output)
            for old in ("--no-wait", "Do not build again", "不要重新 build", "web-upload", "build-resume"):
                self.assertNotIn(old, output)
            self.assertIn("--help-all", output)
            self.assertIn("--foreground", output)
            self.assertIn("./receptor-a/", output)

    def test_full_help_lists_every_command_without_mutating_the_parser(self):
        for language in ("zh", "en"):
            set_language(language)
            parser = cli.parser()
            before = parser.format_help()
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as result:
                parser.parse_args(["--help-all"])
            self.assertEqual(result.exception.code, 0)
            commands = next(a for a in parser._actions if isinstance(a, cli.argparse._SubParsersAction))
            for name in commands.choices:
                self.assertRegex(output.getvalue(), r"(?m)^    " + re.escape(name) + r"\s")
            self.assertEqual(parser.format_help(), before)
            self.assertGreater(len(output.getvalue()), len(before))

    def test_global_help_language_and_literal_separator(self):
        for args in (["--help-all"], ["build", "--help-all"]):
            code, output, error = self.call(args, "zh")
            self.assertEqual(code, 0, error)
            self.assertIn("全部命令", output)
            self.assertIn("web-upload", output)
        code, output, error = self.call(["build", "--", "--help-all", "VALUE_SECRET"])
        self.assertEqual(code, 2)
        self.assertFalse(output)
        self.assertNotIn("VALUE_SECRET", error)
        self.assertNotIn("All commands", error)

    def test_runtime_help_units_defaults_and_compatibility_are_explicit(self):
        for language in ("zh", "en"):
            for command in (["build"], ["build-resume"], ["jobs", "resume"], ["batch"], ["batch-resume"], ["sweep"], ["sweep-resume"]):
                code, output, error = self.call([*command, "-h"], language)
                self.assertEqual(code, 0, error)
                self.assertIn("--interval", output)
                self.assertIn("--max-wait", output)
                self.assertIn("21600", output)
                self.assertIn("Retry-After", output)
                self.assertNotIn("--no-wait", output)
                self.assertNotRegex(output, r"(?m)^\s+--wait\b")
            for command in ("batch", "sweep"):
                _, output, _ = self.call([command, "-h"], language)
                for suffix in ("-resume DIR", "-status DIR"):
                    self.assertIn(command + suffix, output)

    def test_hidden_wait_flags_and_existing_namespaces_still_parse(self):
        for command in (["build"], ["jobs", "resume", "task"], ["batch", "b.yaml"], ["sweep", "s.yaml"]):
            for flag, expected in (("--no-wait", False), ("--wait", True)):
                args = cli.parser().parse_args([*command, flag, "--interval", "45", "--max-wait", "90"])
                self.assertEqual(args.wait, expected)
                self.assertEqual((args.interval, args.max_wait), (45, 90))
                self.assertNotIn("help_all", vars(args))
        args = cli.parser().parse_args(["build", "--foreground", "--json", "--lang", "zh", "--protein", "p", "--ligand", "l"])
        self.assertEqual((args.foreground, args.json, args.lang), (True, True, "zh"))
        code, _, error = self.call(["build", "--wait", "--no-wait"])
        self.assertEqual(code, 2)
        self.assertIn("option combination", error)
        self.assertIn("--no-wait", error)

    def test_typo_hints_are_command_specific_and_never_echo_values(self):
        cases = [(["build", "--protien", "FILE_SECRET", "--ligand", "LIGAND_SECRET"], "--protien", "--protein", "build -h"),
                 (["build", "--protien=TOKEN_SECRET"], "--protien", "--protein", "build -h"),
                 (["jobs", "status", "TASK_SECRET", "--remote-chek=BODY_SECRET"], "--remote-chek", "--remote-check", "jobs status -h"),
                 (["build", "--prot", "FILE_SECRET", "--protien=BODY_SECRET"], "--protien", "--protein", "build -h")]
        for args, typo, suggested, command in cases:
            for language in ("zh", "en"):
                with self.subTest(args=args, language=language):
                    code, output, error = self.call(args, language)
                    self.assertEqual(code, 2)
                    self.assertFalse(output)
                    self.assertIn(typo, error)
                    self.assertIn(suggested, error)
                    self.assertIn("charmm-gui-cli " + command, error)
                    self.assertNotIn("SECRET", error)
                    self.assertLessEqual(len(error.splitlines()), 3)

    def test_flag_shaped_values_and_unknown_option_tails_stay_private(self):
        cases = [(["build", "--protein", "--protien"], "--protein"),
                 (["login", "--email", "--protien"], "--email"),
                 (["--token-file", "--protien", "build"], "--token-file"),
                 (["--lang", "TOKEN_SECRET", "build"], "--lang"),
                 (["build", "--margin", "TOKEN_SECRET"], "--margin"),
                 (["build", "--json=TOKEN_SECRET"], "--json")]
        for args, known in cases:
            code, _, error = self.call(args)
            self.assertEqual(code, 2, error)
            self.assertIn(known, error)
            self.assertNotIn("--protien", error)
            self.assertNotIn("TOKEN_SECRET", error)
        for args in (["--TOKEN_SECRET"], ["build", "--", "--protien", "TOKEN_SECRET"],
                     ["build", "--unknown=TOKEN_SECRET", "--protien=BODY_SECRET"]):
            code, _, error = self.call(args)
            self.assertEqual(code, 2)
            self.assertNotIn("SECRET", error)
            self.assertNotIn("--protien", error)

    def test_ambiguous_abbreviation_names_only_registered_options(self):
        code, _, error = self.call(["build", "--p", "TOKEN_SECRET"])
        self.assertEqual(code, 2)
        self.assertIn("--protein", error)
        self.assertIn("--post-timeout", error)
        self.assertNotIn("TOKEN_SECRET", error)

    def test_check_help_and_terminal_examples_match_supported_inputs(self):
        for language in ("zh", "en"):
            _, output, _ = self.call(["check", "-h"], language)
            self.assertIn("SDF/mol2/PDBQT", output)
            self.assertIn("--ligand-pose", output)
            self.assertIn("--open", output)
            set_language(language)
            example = t("help.c_terminal_mapping")
            self.assertIn("A=CTER,B=CT2", example)
            self.assertNotIn("GLYP", example)
            self.assertNotIn("NTER", example)
        args = cli.parser().parse_args(["build", "--c-terminal", "A=CTER,B=CT2"])
        self.assertEqual(validate_preparation({"c_terminal": args.c_terminal})["terminals"],
                         {"A": {"c_terminal": "CTER"}, "B": {"c_terminal": "CT2"}})
