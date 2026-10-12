"""Single-copy output and compatibility tests using tiny synthetic archives."""

import contextlib
import importlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from charmm_gui_cli import managed, delivery, workflow
from charmm_gui_cli.auth import ToolError, atomic_json
from charmm_gui_cli.cli import main
from charmm_gui_cli.layout import full_archive_path, log_path, inputs_path
from charmm_gui_cli.validation import extract_for_validation
from tests import test_managed_provenance as provenance


class SingleDirectoryTests(unittest.TestCase):
    def setUp(self):
        fixture = provenance.ManagedProvenanceTests()
        fixture.setUp()
        self.fixture = fixture
        self.root = fixture.root / "receptor-a"
        self.root.mkdir()
        original = fixture.run
        self.run = self.root / ".charmm-gui"
        original.rename(self.run)
        (self.run / "bilayer/charmm-gui.tgz").rename(self.root / "charmm-gui.tgz")
        (self.run / "inputs").rename(self.root / "inputs")
        fixture.run, fixture.bilayer, fixture.archive = self.run, self.run / "bilayer", self.root / "charmm-gui.tgz"
        fixture.manifest["inputs"]["files"] = {name: str(self.root / "inputs" / name)
                                              for name in fixture.manifest["inputs"]["files"]}
        fixture.manifest["terminal_adjustments"] = [{"key": "terminal.adjusted", "params": {"chain": "PROA", "patch": "GLYP", "residue": "Gly"}}]
        fixture.save()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("charmm_gui_cli.managed._registry", return_value=fixture.root / "index"))
        managed._evidence(fixture.manifest, fixture.archive, False, "gmx", create_receipt=True)
        self.report = fixture.publish_report()
        self.model = extract_for_validation(fixture.archive, self.root / "model")
        self.report["extracted_directory"] = str(self.model)
        atomic_json(self.run / "results/validation.json", self.report)
        managed.assign_name(self.run, "receptor-a")

    def test_publish_does_not_copy_archive_or_model(self):
        inode = self.fixture.archive.stat().st_ino
        model_file = self.model / "charmm-gui-200/step5_assembly.pdb"
        model_inode = model_file.stat().st_ino
        result = delivery.auto_export(self.run)
        self.assertEqual(result["directory"], str(self.root))
        self.assertEqual(inode, self.fixture.archive.stat().st_ino)
        self.assertEqual(model_inode, model_file.stat().st_ino)
        self.assertEqual(len(list(self.root.rglob("*.tgz"))), 1)
        self.assertEqual(len(list(self.root.rglob("step5_assembly.pdb"))), 1)
        self.assertTrue((self.root / "report.txt").is_file())
        self.assertTrue((self.root / "README.txt").is_file())
        self.assertEqual(log_path(self.run), self.root / "run.log")
        config = json.loads((self.root / "inputs/system-config.json").read_text())
        self.assertEqual(config["requested"], self.fixture.manifest["config"])
        self.assertEqual(config["applied"]["preparation"]["terminals"]["terminal[PROA][first]"], "GLYP")
        self.assertEqual(delivery.export_run(self.run)["directory"], str(self.root))

    def test_named_and_explicit_root_resolve_to_same_state(self):
        with patch("pathlib.Path.cwd", return_value=self.fixture.root):
            self.assertEqual(managed.resolve_run("receptor-a"), self.run)
        self.assertEqual(managed.resolve_run(str(self.root)), self.run)
        self.assertEqual(managed.inspect_run("receptor-a")["passed"], True)
        self.assertEqual(full_archive_path(self.run), self.fixture.archive)
        self.assertEqual(inputs_path(self.run), self.root / "inputs")
        receipt = workflow.verify_archive_provenance(self.fixture.bilayer, self.fixture.bilayer_manifest)
        self.assertEqual(receipt["archive_sha256"], managed._digest(self.fixture.archive))

    def test_edited_model_is_not_silently_overwritten(self):
        (self.model / "charmm-gui-200/step5_assembly.pdb").write_text("user edit")
        with self.assertRaises(ToolError) as caught:
            delivery.auto_export(self.run)
        self.assertEqual(caught.exception.key, "delivery.model_changed")

    def test_revalidation_reuses_model_instead_of_extracting_again(self):
        self.fixture.manifest["config"].update(
            membrane={"upper": "POPC=1", "lower": "POPC=1"}, ions={"type": "KCl", "concentration_M": 0.15})
        self.fixture.save()
        inode = (self.model / "charmm-gui-200/step5_assembly.pdb").stat().st_ino
        with patch.object(managed, "extract_for_validation") as extract, \
                patch.object(managed, "validate_system", return_value={"passed": True, "files": {}, "checks": {}}), \
                patch.object(managed, "validate_bound_pose", return_value={"passed": True}), \
                patch.object(managed, "assess_acceptance", return_value={"passed": True}):
            managed.finalize_managed_build(self.run, grompp=True, gmx="unavailable-test-gmx")
        extract.assert_not_called()
        self.assertEqual(inode, (self.model / "charmm-gui-200/step5_assembly.pdb").stat().st_ino)
        self.assertEqual(len(list(self.root.rglob("step5_assembly.pdb"))), 1)

    def test_readonly_jobs_report_works_at_creation_location(self):
        with patch("pathlib.Path.cwd", return_value=self.fixture.root), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["jobs", "report", "receptor-a"]), 0)
        self.assertTrue(json.loads(output.getvalue())["passed"])

    def test_old_imports_are_identical_not_duplicate_modules(self):
        for old, new in (("api", "remote.api"), ("managed", "store.managed"), ("inputs", "chem.inputs"),
                         ("i18n", "locales.i18n"), ("full_build", "pipeline.full_build")):
            self.assertIs(importlib.import_module("charmm_gui_cli." + old), importlib.import_module("charmm_gui_cli." + new))

    def test_no_managed_staging_file_is_left_after_index_write(self):
        self.assertFalse(list((self.fixture.root / "index").rglob(".managed-*")))

    def test_fresh_cli_build_uses_only_the_named_root(self):
        from tests import test_cli_features as features
        fixture = features.CLIFeatureTests()
        fixture.setUp()
        self.addCleanup(fixture.stack.close)
        fixture.require_chem()
        with patch("pathlib.Path.cwd", return_value=fixture.root):
            code, result, error = fixture.call(["build", "--name", "one-task", "--protein", str(fixture.protein),
                                              "--ligand", str(fixture.ligand), "--dry-run"])
        self.assertEqual(code, 0, error)
        private = fixture.root / "one-task/.charmm-gui"
        self.assertEqual(result["directory"], str(private))
        self.assertTrue((private / "full-run.json").is_file())
        self.assertTrue((fixture.root / "one-task/inputs/complex.pdb").is_file())
        self.assertTrue((private / "sources").is_dir())
        self.assertFalse((fixture.root / ".one-task.sources").exists())
        self.assertFalse((fixture.root / "charmm-gui-runs").exists())

    def test_foreign_root_is_not_overwritten(self):
        from charmm_gui_cli.full_build import initialize
        foreign = self.fixture.root / "foreign"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("user file")
        with patch("charmm_gui_cli.full_build.load_build_config", return_value={}), self.assertRaises(FileExistsError):
            initialize("unused.yaml", foreign / ".charmm-gui")
        self.assertEqual((foreign / "keep.txt").read_text(), "user file")
        self.assertFalse((foreign / ".charmm-gui").exists())
