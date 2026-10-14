"""Common failures through public commands, without credentials or HTTP."""

from contextlib import redirect_stdout, redirect_stderr
import copy
import errno
import io
import json
import os
import shlex
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli.cli import main
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.pipeline.build_config import load_build_config, validate_preparation
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.store.configuration import bind_config_error
from charmm_gui_cli.store.diagnostics import filesystem_error
from charmm_gui_cli.store.local_build import local_initialization
from charmm_gui_cli.store.tasks import _read_json
import test_input_policy as fixtures


class ActionableErrorTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.InputPolicyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write()
        self.root, self.protein, self.ligand = fixture.root, fixture.protein, fixture.ligand
        self.config = self.root / "建模 settings.yaml"
        self.raw = {"version": 2, "protein": self.protein.name, "ligand": self.ligand.name,
                    "ligand_hydrogens": "preserve",
                    "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20}}
        self.addCleanup(set_language)

    def write(self, value=None):
        self.config.write_text(yaml.safe_dump(self.raw if value is None else value), encoding="utf-8")

    def call(self, args, *, language="zh", tty=False):
        output = fixtures.Terminal() if tty else io.StringIO()
        errors = io.StringIO()
        env = {"XDG_CONFIG_HOME": str(self.root / "config"), "XDG_DATA_HOME": str(self.root / "data")}
        with patch.dict(os.environ, env), patch("pathlib.Path.home", return_value=self.root / "home"), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("No HTTP expected")), \
                redirect_stdout(output), redirect_stderr(errors):
            code = main(["--lang", language, *args])
        return code, output.getvalue(), errors.getvalue()

    def test_yaml_field_type_and_value_errors_have_actual_file_and_local_command(self):
        cases = [({**self.raw, "unknown": "VALUE_SECRET"}, "允许的字段", "Allowed:"),
                 ({**self.raw, "membrane": []}, "允许的字段", "Allowed:"),
                 ({**self.raw, "version": True}, "version: 2", "version: 2"),
                 ({**self.raw, "ligand_hydrogens": "VALUE_SECRET"}, "必须为", "must be"),
                 ({**self.raw, "preparation": {"orientation": "VALUE_SECRET"}}, "必须为", "must be"),
                 ({**self.raw, "membrane": {**self.raw["membrane"], "margin_A": True}}, "有限的正数", "finite positive")]
        for raw, zh, en in cases:
            self.write(raw)
            for language, expected in (("zh", zh), ("en", en)):
                with self.subTest(raw=raw, language=language):
                    out = self.root / "not-created"
                    code, _, error = self.call(["build", str(self.config), "--dry-run", "--out", str(out)], language=language)
                    self.assertEqual(code, 2, error)
                    self.assertIn(expected, error)
                    self.assertIn(str(self.config), error)
                    self.assertIn(shlex.join(["charmm-gui-cli", "build", str(self.config), "--dry-run"]), error)
                    self.assertNotIn("VALUE_SECRET", error)
                    self.assertNotIn("charmm-gui-cli -h", error)
                    self.assertFalse(out.exists())

    def test_yaml_parser_and_duplicate_key_errors_never_echo_values(self):
        cases = [("version: 2\npassword: [VALUE_SECRET\n", "config.yaml"),
                 ("version: 2\nversion: VALUE_SECRET\n", "config.unique_keys")]
        for content, key in cases:
            self.config.write_text(content)
            for language in ("zh", "en"):
                code, _, error = self.call(["build", str(self.config), "--dry-run"], language=language)
                self.assertEqual(code, 2, error)
                self.assertNotIn("VALUE_SECRET", error)
                self.assertNotIn("password", error)
                with self.assertRaises(ToolError) as raised:
                    load_build_config(self.config)
                self.assertEqual(raised.exception.key, key)
                self.assertIn(str(self.config), raised.exception.localized())
        self.config.write_bytes(b"version: 2\n\xffVALUE_SECRET")
        code, _, error = self.call(["build", str(self.config), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("UTF-8", error)
        self.assertNotIn("VALUE_SECRET", error)

    def test_lipid_failures_preserve_legacy_messages_and_localize_with_context(self):
        cases = [("POPC:POPE", "Lipid composition must look like POPC:CHL1=3:1."),
                 ("POPC:POPE=1", "Lipid names and ratios must match, without duplicate names."),
                 ("POPC:POPC=1:1", "Lipid names and ratios must match, without duplicate names."),
                 ("POPC=VALUE_SECRET", "Lipid ratios must be numeric."),
                 ("POPC=0", "Lipid ratio must be a finite positive number."),
                 ("POPC=nan", "Lipid ratio must be a finite positive number.")]
        for value, legacy in cases:
            raw = copy.deepcopy(self.raw)
            raw["membrane"]["upper"] = value
            self.write(raw)
            with self.subTest(value=value), self.assertRaises(ToolError) as error:
                load_build_config(self.config)
            self.assertEqual(error.exception.as_dict()["message"], legacy)
            self.assertEqual(error.exception.category, "operation_error")
            self.assertFalse(error.exception.retryable)
            code, _, output = self.call(["build", str(self.config), "--dry-run"])
            self.assertEqual(code, 2)
            self.assertIn("--dry-run", output)
            self.assertNotIn("VALUE_SECRET", output)

    def test_corrected_yaml_next_command_really_runs_without_http(self):
        raw = copy.deepcopy(self.raw)
        raw["membrane"]["upper"] = "POPC:POPE=7"
        self.write(raw)
        with self.assertRaises(ToolError) as raised:
            load_build_config(self.config)
        # Obtain the exact quoted command from the file-bound English guidance.
        set_language("en")
        bind_config_error(raised.exception, self.config)
        command = shlex.split(raised.exception.next_step.split("check locally: ", 1)[1])
        self.assertEqual(command[:2], ["charmm-gui-cli", "build"])
        self.write()
        code, output, error = self.call(command[1:] + ["--out", str(self.root / "corrected")])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["state"], "inputs_ready")

    def test_direct_option_errors_offer_explicit_default_baseline_with_real_inputs(self):
        for language, expected in (("zh", "默认建模参数"), ("en", "default model options")):
            code, _, error = self.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
                "--upper", "POPC=VALUE_SECRET", "--dry-run"], language=language)
            self.assertEqual(code, 2)
            self.assertIn(expected, error)
            self.assertIn(shlex.quote(str(self.protein)), error)
            self.assertIn(shlex.quote(str(self.ligand)), error)
            self.assertIn("--dry-run", error)
            self.assertNotIn("VALUE_SECRET", error)
            self.assertNotIn("build -h", error)
            if language == "en":
                command = error.split("For a local baseline using default model options: ", 1)[1].split("; this does not submit", 1)[0]
                command = shlex.split(command)
                code, output, corrected_error = self.call(command[1:] + ["--out", str(self.root / "direct-baseline")])
                self.assertEqual(code, 0, corrected_error)
                self.assertEqual(json.loads(output)["state"], "inputs_ready")

    def test_plan_validation_and_mixed_yaml_flags_have_correct_command(self):
        self.write({"version": 1, "unknown": "VALUE_SECRET"})
        code, _, error = self.call(["plan", str(self.config)])
        self.assertEqual(code, 2)
        self.assertIn(shlex.join(["charmm-gui-cli", "plan", str(self.config)]), error)
        self.assertNotIn("--dry-run", error)
        self.assertNotIn("VALUE_SECRET", error)
        self.write()
        code, _, error = self.call(["build", str(self.config), "--protein", "VALUE_SECRET", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("不能混用", error)
        self.assertIn(shlex.join(["charmm-gui-cli", "build", str(self.config), "--dry-run"]), error)
        self.assertNotIn("VALUE_SECRET", error)

    def test_missing_inputs_and_files_have_localized_non_looping_routes(self):
        for language, expected in (("zh", "结构"), ("en", "Provide")):
            code, _, error = self.call(["build", "--protein", str(self.protein)], language=language)
            self.assertEqual(code, 2)
            self.assertIn(expected, error)
            self.assertIn(shlex.quote(str(self.protein)), error)
            self.assertIn("--ligand bound-ligand.sdf", error)
        missing = self.root / "缺少 ligand.sdf"
        for tty in (False, True):
            out = self.root / ("missing-tty" if tty else "missing-json")
            code, output, error = self.call(["check", "--protein", str(self.protein), "--ligand", str(missing),
                                           "--out", str(out)], tty=tty)
            self.assertEqual(code, 2, error)
            record = json.loads((out / "check.json").read_text())["error"]
            self.assertEqual(record["message"], "An input file does not exist or is not a regular file.")
            self.assertEqual(record["message_key"], "input.source_file")
            self.assertIn(str(missing), record["message_params"]["path"])
            self.assertIn("ls -ld", record["next_step"])
            if tty:
                self.assertIn("输入文件不存在", output)
                self.assertIn(str(missing), output)

    def test_c_terminal_failure_never_suggests_n_terminal_patch(self):
        for preparation in ({"c_terminal": "A=GLYP"}, {"terminals": {"A": {"c_terminal": "GLYP"}}}):
            for language in ("zh", "en"):
                set_language(language)
                with self.assertRaises(ToolError) as error:
                    validate_preparation(preparation)
                self.assertEqual(error.exception.key, "terminal.patch_invalid")
                self.assertEqual(error.exception.params["end"], "C")
                self.assertIn("CTER", error.exception.localized())
                self.assertNotIn("NTER", error.exception.localized())

    def test_filesystem_errno_path_and_privacy_in_cli_and_saved_check(self):
        for number, zh in ((errno.EACCES, "没有权限"), (errno.EROFS, "只读"), (errno.ENOSPC, "空间不足")):
            exception = OSError(number, "BODY_SECRET", str(self.protein))
            with patch("charmm_gui_cli.cli.dispatch", side_effect=exception):
                code, _, error = self.call(["doctor"])
            self.assertEqual(code, 2)
            self.assertIn(zh, error)
            self.assertIn(str(self.protein), error)
            self.assertNotIn("BODY_SECRET", error)
            self.assertNotIn("charmm-gui-cli -h", error)
        out = self.root / "unreadable-check"
        with patch("charmm_gui_cli.chem.precheck._file_evidence", side_effect=PermissionError(errno.EACCES, "BODY_SECRET", str(self.protein))):
            code, output, _ = self.call(["check", "--protein", str(self.protein), "--ligand", str(self.ligand), "--out", str(out)], tty=True)
        self.assertEqual(code, 2)
        self.assertIn("没有权限", output)
        record = json.loads((out / "check.json").read_text())["error"]
        self.assertEqual(record["category"], "local_io")
        self.assertEqual(record["message_key"], "filesystem.permission")
        self.assertNotIn("BODY_SECRET", json.dumps(record))
        hidden = filesystem_error(OSError("BODY_SECRET"))
        self.assertNotIn("BODY_SECRET", hidden.localized())
        self.assertIn("pwd", hidden.next_step)

    def test_foreign_path_is_never_described_as_resumable_or_changed(self):
        root = self.root / "foreign output"
        root.mkdir()
        user_file = root / "data.txt"
        user_file.write_text("USER_DATA")
        for language, expected in (("zh", "未识别"), ("en", "not recognized")):
            set_language(language)
            with self.assertRaises(ToolError) as error:
                with local_initialization(root / ".charmm-gui"):
                    self.fail("Foreign output was accepted")
            self.assertEqual(error.exception.key, "build.path_occupied")
            self.assertIn(expected, error.exception.localized())
            self.assertIn("--out", error.exception.next_step)
            self.assertNotIn("jobs resume", error.exception.next_step)
            self.assertEqual(user_file.read_text(), "USER_DATA")
            self.assertFalse((root / ".charmm-gui").exists())

    def test_filesystem_build_error_is_logged_and_saved_without_exception_text(self):
        from argparse import Namespace
        import sys
        from charmm_gui_cli.pipeline import build_command
        from charmm_gui_cli.store.run_logging import LogStream
        run = self.root / "logged-task/.charmm-gui"
        run.mkdir(parents=True)
        atomic_json(run / "full-run.json", {"schema_version": 2, "state": "inputs_ready"})
        atomic_json(run / "job.json", {"name": "logged-task"})
        def fail(args, holder):
            holder.append(run)
            sys.stderr = LogStream(sys.stderr, run.parent / "run.log")
            raise PermissionError(errno.EACCES, "BODY_SECRET", str(self.protein))
        set_language("zh")
        with redirect_stderr(io.StringIO()), patch.object(build_command, "_execute", side_effect=fail), self.assertRaises(PermissionError):
            build_command.execute(Namespace())
        log = (run.parent / "run.log").read_text()
        record = json.loads((run / "recovery.json").read_text())["error"]
        self.assertIn("没有权限", log)
        self.assertIn(str(self.protein), log)
        self.assertIn("ls -ld", log)
        self.assertEqual(record["message_key"], "filesystem.permission")
        self.assertEqual(record["category"], "filesystem_error")
        self.assertNotIn("BODY_SECRET", log + json.dumps(record))

    def test_existing_check_and_configured_missing_file_have_distinct_guidance(self):
        out = self.root / "existing-check"
        out.mkdir()
        user_file = out / "data.txt"
        user_file.write_text("USER_DATA")
        code, _, error = self.call(["check", "--protein", str(self.protein), "--ligand", str(self.ligand), "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("预检输出路径已存在", error)
        self.assertIn("--out", error)
        self.assertEqual(user_file.read_text(), "USER_DATA")
        raw = copy.deepcopy(self.raw)
        raw["ligand"] = "missing ligand.sdf"
        self.write(raw)
        code, _, error = self.call(["build", str(self.config), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn(str(self.root / raw["ligand"]), error)
        self.assertIn("相对路径", error)
        self.assertIn(str(self.config), error)

    def test_metadata_and_unknown_names_have_actionable_localized_errors(self):
        for language, expected in (("zh", "没有记录"), ("en", "Unknown job")):
            code, _, error = self.call(["jobs", "status", "unrecorded-job"], language=language)
            self.assertEqual(code, 2)
            self.assertIn(expected, error)
            self.assertIn("charmm-gui-cli jobs list", error)
        path = self.root / "full-run.json"
        for contents, key in (("invalid BODY_SECRET", "jobs.metadata"), ("[]", "jobs.metadata_type")):
            path.write_text(contents)
            set_language("zh")
            with self.assertRaises(ToolError) as error:
                _read_json(path)
            self.assertEqual(error.exception.key, key)
            self.assertIn(str(path), error.exception.localized())
            self.assertNotIn("BODY_SECRET", error.exception.localized())
            self.assertIn("ls -ld", error.exception.next_step)

    def test_config_context_cannot_change_unknown_submission_recovery(self):
        error = ToolError("Uncertain POST", key="config.fields", params={"name": "x", "allowed": "y"},
                          category="submission_unknown", next_step="charmm-gui-cli jobs reconcile real-job")
        old = copy.deepcopy(error.as_dict())
        bind_config_error(error, self.config)
        self.assertEqual(error.as_dict(), old)
