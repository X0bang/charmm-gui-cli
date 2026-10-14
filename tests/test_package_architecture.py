"""Canonical imports, command dispatch and explicit legacy compatibility."""

import ast
import contextlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli._compat import MODULE_ALIASES
from charmm_gui_cli.cli import main
from charmm_gui_cli.store.auth import ToolError, atomic_write
from test_client import archive_bytes, config, pdb, token
import test_managed_provenance as provenance

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "charmm_gui_cli"


def imports(path):
    package = ".".join(path.relative_to(ROOT).parts[:-1])
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module
            yield module
            yield from (module + "." + alias.name for alias in node.names)


class PackageArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-package-layers-")).resolve()

    def child(self, code):
        environment = dict(os.environ)
        for name in ("CHARMMGUI_TOKEN", "CHARMM_GUI_PASSWORD_COMMAND", "CHARMMGUI_COOKIE_FILE"):
            environment.pop(name, None)
        environment.update(XDG_CONFIG_HOME=str(self.root / "config"), XDG_DATA_HOME=str(self.root / "data"))
        isolated = ("import os\nfrom pathlib import Path\nfrom unittest.mock import patch\n"
                    "with patch('pathlib.Path.home', return_value=Path(os.environ['XDG_CONFIG_HOME']).parent):\n"
                    + textwrap.indent(textwrap.dedent(code), "    "))
        result = subprocess.run([sys.executable, "-c", isolated], cwd=ROOT,
                                env=environment, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_internal_imports_never_use_legacy_modules(self):
        for path in PACKAGE.rglob("*.py"):
            name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
            if name in MODULE_ALIASES or path.name == "_compat.py":
                continue
            for module in imports(path):
                with self.subTest(path=path.relative_to(ROOT), module=module):
                    self.assertNotIn(module, MODULE_ALIASES)

    def test_compatibility_registry_has_no_alias_chains_and_covers_each_shim(self):
        self.assertTrue(MODULE_ALIASES)
        for alias, canonical in MODULE_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertNotIn(canonical, MODULE_ALIASES)
                path = ROOT / (alias.replace(".", "/") + ".py")
                self.assertIn("install_alias", path.read_text())
                self.assertTrue((ROOT / (canonical.replace(".", "/") + ".py")).is_file())

    def test_commands_directory_contains_only_handlers(self):
        for path in (PACKAGE / "commands").glob("*.py"):
            if path.name == "__init__.py":
                continue
            with self.subTest(module=path.stem):
                handler = importlib.import_module("charmm_gui_cli.commands." + path.stem)
                self.assertTrue(callable(getattr(handler, "execute", None)))
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.ImportFrom) and node.module in ("cli", "charmm_gui_cli.cli"):
                        self.assertEqual([alias.name for alias in node.names], ["emit"])

    def test_lower_layers_and_views_do_not_import_cli_or_handlers(self):
        for folder in ("pipeline", "store", "chem", "remote", "locales", "presentation"):
            for path in (PACKAGE / folder).glob("*.py"):
                for module in imports(path):
                    with self.subTest(path=path.relative_to(ROOT), module=module):
                        self.assertNotEqual(module, "charmm_gui_cli.cli")
                        self.assertFalse(module.startswith("charmm_gui_cli.commands"))

    def test_presentation_and_locale_modules_do_not_depend_on_orchestration(self):
        for folder in ("presentation", "locales"):
            for path in (PACKAGE / folder).glob("*.py"):
                for module in imports(path):
                    with self.subTest(path=path.relative_to(ROOT), module=module):
                        self.assertFalse(module.startswith(("charmm_gui_cli.pipeline", "charmm_gui_cli.remote")))

    def test_data_chemistry_and_http_modules_never_import_upper_layers(self):
        for folder in ("store", "chem", "remote"):
            for path in (PACKAGE / folder).glob("*.py"):
                name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
                if name in MODULE_ALIASES:
                    continue  # Explicit external compatibility, never used internally.
                for module in imports(path):
                    with self.subTest(path=path.relative_to(ROOT), module=module):
                        self.assertFalse(module.startswith(("charmm_gui_cli.pipeline", "charmm_gui_cli.commands", "charmm_gui_cli.cli")))

    def test_pure_storage_and_chemistry_work_when_upper_layers_are_unavailable(self):
        self.child('''
            import importlib, importlib.abc, json, pathlib, sys, tempfile
            class NoUpperLayers(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.startswith(("charmm_gui_cli.pipeline", "charmm_gui_cli.commands", "charmm_gui_cli.cli")):
                        raise AssertionError("Upward dependency: " + fullname)
            sys.meta_path.insert(0, NoUpperLayers())
            for name in ("store.tasks", "store.workers", "store.result_files", "store.run_manifest",
                         "store.provenance", "store.configuration", "chem.inputs", "chem.precheck",
                         "chem.pose_validation", "chem.preview", "remote.api", "remote.reconcile"):
                importlib.import_module("charmm_gui_cli." + name)
            from charmm_gui_cli.store.tasks import _new_json, _read_json, remember_run, resolve_run, list_records
            from charmm_gui_cli.store.locking import run_lock
            from charmm_gui_cli.store.workers import claim, metadata
            import os
            root=pathlib.Path(tempfile.mkdtemp(prefix="cgui-pure-store-"))
            _new_json(root/"full-run.json", {"schema_version":2,"state":"inputs_ready"})
            remember_run(root)
            assert resolve_run(None)==root
            assert len(list_records())==1
            with run_lock(root):
                assert _read_json(root/"full-run.json")["state"]=="inputs_ready"
            fd=claim(root)
            try: assert metadata(root)["running"]
            finally: os.close(fd)
            assert not metadata(root)["running"]
        ''')

    def test_legacy_aliases_are_same_objects_in_a_fresh_process(self):
        output = self.child('''
            import importlib
            from charmm_gui_cli._compat import MODULE_ALIASES
            for alias, canonical in MODULE_ALIASES.items():
                implementation = importlib.import_module(canonical)
                assert importlib.import_module(alias) is implementation, alias
                implementation.__compat_test_marker = object()
                assert importlib.import_module(alias).__compat_test_marker is implementation.__compat_test_marker
                del implementation.__compat_test_marker
            print("all legacy imports preserve module identity")
        ''')
        self.assertIn("preserve module identity", output)

    def test_all_canonical_modules_and_readonly_cli_work_with_legacy_imports_blocked(self):
        output = self.child('''
            import contextlib, importlib, importlib.abc, io, json, pkgutil, sys
            from unittest.mock import patch
            import charmm_gui_cli
            from charmm_gui_cli._compat import MODULE_ALIASES
            class BlockLegacy(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname in MODULE_ALIASES:
                        raise AssertionError("Internal dependency on legacy import: " + fullname)
            sys.meta_path.insert(0, BlockLegacy())
            with patch("requests.Session.request", side_effect=AssertionError("No network in import/readonly smoke")):
                for item in pkgutil.walk_packages(charmm_gui_cli.__path__, "charmm_gui_cli."):
                    if item.name not in MODULE_ALIASES and item.name != "charmm_gui_cli.__main__":
                        importlib.import_module(item.name)
                from charmm_gui_cli.cli import main
                for language in ("zh", "en"):
                    for args in (["doctor"], ["jobs", "list"]):
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                            result = main(["--lang", language, "--json", "--token-file", str(Path(os.environ["XDG_CONFIG_HOME"])/"missing.token"), *args])
                        assert result in (0, 2), stderr.getvalue()
                        assert isinstance(json.loads(stdout.getvalue()), dict), stdout.getvalue()
            assert not set(MODULE_ALIASES).intersection(sys.modules)
            print("canonical modules and bilingual readonly commands do not load aliases")
        ''')
        self.assertIn("do not load aliases", output)

    def test_shared_lock_and_yaml_loader_preserve_public_objects_and_semantics(self):
        from charmm_gui_cli.pipeline import config as configuration, workflow
        from charmm_gui_cli.store.configuration import UniqueLoader
        from charmm_gui_cli.store.locking import now, run_lock
        self.assertIs(workflow.run_lock, run_lock)
        self.assertIs(workflow.now, now)
        self.assertIs(configuration.UniqueLoader, UniqueLoader)
        with run_lock(self.root):
            with self.assertRaises(ToolError) as error:
                with workflow.run_lock(self.root):
                    self.fail("The same task must stay locked")
        self.assertEqual(error.exception.category, "lock_busy")
        with run_lock(self.root):
            pass
        with self.assertRaises(ToolError):
            yaml.load("same: 1\nsame: 2\n", Loader=configuration.UniqueLoader)

    def test_readonly_inspect_and_plan_dispatch_use_actual_implementations(self):
        archive, path = self.root / "model.tgz", self.root / "plan.yaml"
        archive.write_bytes(archive_bytes({"step5_assembly.pdb": pdb("LIG")}))
        atomic_write(path, yaml.safe_dump(config()))
        for language in ("zh", "en"):
            for args, expected_key in ((["inspect", str(archive), "--ligand", "LIG"], "passed"),
                                       (["plan", str(path)], "ready_to_submit")):
                stdout, stderr = io.StringIO(), io.StringIO()
                with self.subTest(args=args, language=language), contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr), patch("requests.Session.request", side_effect=AssertionError("No POST or GET")):
                    code = main(["--lang", language, "--json", *args])
                self.assertEqual(code, 0, stderr.getvalue())
                self.assertTrue(json.loads(stdout.getvalue())[expected_key])

    def test_readonly_authenticated_status_dispatch_never_posts(self):
        from charmm_gui_cli.remote.api import Client
        session = self.root / "test.token"
        atomic_write(session, token())
        for language in ("zh", "en"):
            for args in (["auth", "--jobid", "234"], ["status", "234"], ["status", "234", "--diagnostic"]):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        patch("requests.Session.request", side_effect=AssertionError("No network")), \
                        patch.object(Client, "status", return_value={"jobid": "234", "status": "done", "hasTarFile": True}) as query:
                    code = main(["--lang", language, "--json", "--token-file", str(session), *args])
                self.assertEqual(code, 0, stderr.getvalue())
                query.assert_called_once_with("234")
                self.assertIsInstance(json.loads(stdout.getvalue()), dict)

    def test_completed_status_and_report_dispatch_are_local_and_nonmutating(self):
        fixture = provenance.ManagedProvenanceTests()
        fixture.setUp()
        fixture.publish_report()
        manifest = fixture.run / "full-run.json"
        before = manifest.read_bytes()
        for language in ("zh", "en"):
            for command in ("status", "report"):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        patch("requests.Session.request", side_effect=AssertionError("No remote calls")):
                    code = main(["--lang", language, "--json", "jobs", command, str(fixture.run)])
                self.assertEqual(code, 0, stderr.getvalue())
                self.assertIsInstance(json.loads(stdout.getvalue()), dict)
        self.assertEqual(manifest.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
