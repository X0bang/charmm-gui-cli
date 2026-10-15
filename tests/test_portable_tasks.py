"""Moved task regression tests retain evidence and never replay unknown POSTs."""

import charmm_gui_cli.store.tasks as _task_store

from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch

from charmm_gui_cli.pipeline import managed, delivery
from charmm_gui_cli.store.auth import ToolError, atomic_json, atomic_write
from charmm_gui_cli.store.paths import read_value, encode, decode
from charmm_gui_cli.store.discovery import relink
from charmm_gui_cli.cli import main
from charmm_gui_cli.pipeline import full_build, sweep
from charmm_gui_cli.remote.api import SubmissionUnknown
from tests import test_single_directory as single, test_precheck as precheck
from tests import test_sweep as fixtures


class PortableTaskTests(unittest.TestCase):
    def setUp(self):
        fixture = single.SingleDirectoryTests()
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.stack.close)
        self.base, self.root, self.run = fixture.fixture.root, fixture.root, fixture.run
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("pathlib.Path.cwd", return_value=self.base))
        self.stack.enter_context(patch("requests.Session.request", side_effect=AssertionError("No live network")))
        self.stack.enter_context(patch("charmm_gui_cli.store.tasks._registries", return_value=(self.base / "index",)))

    def portable(self):
        atomic_json(self.run / "path-mode.json", {"version": 1})
        manifest = read_value(self.run / "full-run.json")
        manifest["config"].update(protein=str(self.root / "inputs/protein.pdb"), ligand=str(self.root / "inputs/ligand.sdf"))
        atomic_json(self.run / "full-run.json", manifest)
        atomic_json(self.run / "job.json", read_value(self.run / "job.json"))
        report = read_value(self.run / "results/validation.json")
        evidence = managed._evidence(manifest, self.root / "charmm-gui.tgz", False, "gmx")
        report.update(evidence=evidence, fingerprint=managed._canonical_hash(evidence))
        atomic_json(self.run / "results/validation.json", report)
        return report

    def move(self):
        destination = self.base / "新位置 with spaces/receptor-renamed"
        destination.parent.mkdir()
        self.root.rename(destination)
        self.root, self.run = destination, destination / ".charmm-gui"

    def call(self, args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(args)
        return code, output.getvalue(), error.getvalue()

    def test_portable_records_contain_relative_paths_and_unchanged_proof_after_move(self):
        original = self.portable()
        raw = json.loads((self.run / "full-run.json").read_text())
        self.assertEqual(raw["inputs"]["files"]["protein.pdb"], "inputs/protein.pdb")
        self.assertEqual(raw["config"]["protein"], "inputs/protein.pdb")
        self.assertEqual(json.loads((self.run / "job.json").read_text())["directory"], ".charmm-gui")
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*.json")}
        self.move()
        self.assertEqual(_task_store.resolve_run("receptor-a"), self.run)
        self.assertTrue(managed.inspect_run("receptor-a")["passed"])
        self.assertEqual(managed.list_runs()[0]["directory"], str(self.run))
        self.assertEqual(_task_store._read_json(self.run / "results/validation.json")["fingerprint"], original["fingerprint"])
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*.json")})

    def test_actual_status_report_export_and_offline_resume_commands_after_move(self):
        self.portable()
        self.move()
        for target in ("receptor-a", str(self.root)):
            code, output, error = self.call(["jobs", "status", target, "--json"])
            self.assertEqual(code, 0, error)
            self.assertTrue(json.loads(output)["passed"])
            code, output, error = self.call(["jobs", "report", target, "--format", "text"])
            self.assertEqual(code, 0, error)
            self.assertIn("200", output)
        with patch.object(managed, "extract_for_validation", side_effect=AssertionError("Must reuse verified model")):
            code, output, error = self.call(["--foreground", "jobs", "resume", "receptor-a", "--no-preview", "--no-grompp", "--json"])
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["validation"]["passed"])
        exported = self.base / "exported"
        code, output, error = self.call(["jobs", "export", "receptor-a", "--to", str(exported), "--json"])
        self.assertEqual(code, 0, error)
        self.assertEqual((exported / "charmm-gui.tgz").read_bytes(), (self.root / "charmm-gui.tgz").read_bytes())
        self.assertTrue(managed.inspect_run("receptor-a")["passed"])

    def test_existing_owned_export_can_refresh_after_source_moves(self):
        self.portable()
        target = self.base / "export-before-moving"
        delivery.export_run(self.run, to=target)
        self.move()
        result = delivery.export_run(self.run, to=target)
        self.assertEqual(result["directory"], str(target))
        self.assertTrue(managed.inspect_run("receptor-a")["passed"])

    def test_legacy_absolute_paths_read_without_rewriting_or_resealing_evidence(self):
        old = self.run
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*.json")}
        fingerprint = _task_store._read_json(self.run / "results/validation.json")["fingerprint"]
        self.move()
        self.assertTrue(managed.inspect_run(str(self.root))["passed"])
        self.assertEqual(_task_store._read_json(self.run / "results/validation.json")["fingerprint"], fingerprint)
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*.json")})
        self.assertIn(str(old), (self.run / "job.json").read_text())
        code, _, error = self.call(["--foreground", "jobs", "resume", "receptor-a", "--no-preview", "--no-grompp"])
        self.assertEqual(code, 0, error)

    def test_relink_outside_discovery_tree_updates_only_index(self):
        self.portable()
        self.move()
        outside = self.base / "unrelated"
        outside.mkdir()
        with patch("pathlib.Path.cwd", return_value=outside):
            with self.assertRaises(ToolError) as caught:
                _task_store.resolve_run("receptor-a")
            self.assertIn("jobs relink receptor-a", caught.exception.next_step)
            before = {p: p.read_bytes() for p in self.root.rglob("*.json")}
            code, output, error = self.call(["jobs", "relink", "receptor-a", str(self.root), "--json"])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["state"], "relinked")
            self.assertEqual(_task_store.resolve_run("receptor-a"), self.run)
            self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*.json")})

    def test_two_copies_are_ambiguous_and_foreign_task_id_is_rejected(self):
        self.portable()
        self.move()
        duplicate = self.base / "duplicate"
        shutil.copytree(self.root, duplicate)
        with self.assertRaises(ToolError) as caught:
            _task_store.resolve_run("receptor-a")
        self.assertEqual(caught.exception.key, "jobs.discovery_ambiguous")
        updated = relink("receptor-a", self.root)
        self.assertEqual(updated["directory"], str(self.root))
        with self.assertRaises(ToolError) as caught:
            relink("receptor-a", duplicate)
        self.assertEqual(caught.exception.key, "jobs.original_present")
        original_index = (self.base / "index/names/receptor-a.json").read_bytes()
        job = read_value(duplicate / ".charmm-gui/job.json")
        job["task_id"] = "f" * 32
        atomic_json(duplicate / ".charmm-gui/job.json", job)
        with self.assertRaises(ToolError) as caught:
            relink("receptor-a", duplicate)
        self.assertEqual(caught.exception.key, "jobs.relink_mismatch")
        self.assertEqual((self.base / "index/names/receptor-a.json").read_bytes(), original_index)

    def test_relink_refuses_a_worker_with_a_real_held_lock(self):
        import os
        from charmm_gui_cli.store.workers import claim
        self.portable()
        self.move()
        record = (self.base / "index/names/receptor-a.json").read_bytes()
        fd = claim(self.run)
        try:
            with self.assertRaises(ToolError) as caught:
                relink("receptor-a", self.root)
            self.assertEqual(caught.exception.key, "background.running")
        finally:
            os.close(fd)
        self.assertEqual(record, (self.base / "index/names/receptor-a.json").read_bytes())

    def test_discovery_does_not_choose_after_unreadable_or_incomplete_search(self):
        from charmm_gui_cli.store.discovery import discover
        record = {"name": "receptor-a", "directory": str(self.run), "task_id": "f" * 32}
        def unreadable(path, *, followlinks, onerror):
            onerror(PermissionError(13, "permission denied", "/fixture/unreadable"))
        with patch("charmm_gui_cli.store.discovery.os.walk", side_effect=unreadable), self.assertRaises(ToolError) as caught:
            discover(record)
        self.assertEqual(caught.exception.key, "jobs.discovery_unreadable")
        self.assertIn("jobs relink receptor-a", caught.exception.next_step)
        with patch("charmm_gui_cli.store.discovery.os.walk", return_value=((self.base / str(i), [], []) for i in range(10001))), self.assertRaises(ToolError) as caught:
            discover(record)
        self.assertEqual(caught.exception.key, "jobs.discovery_limit")

    def test_moved_model_and_input_edits_still_invalidate_validation(self):
        self.portable()
        self.move()
        (self.root / "inputs/protein.pdb").write_text("changed")
        status = managed.inspect_run(str(self.root))
        self.assertEqual(status["validation_status"], "stale_or_unreadable")
        self.assertIn("validation.input_changed", [issue["key"] for issue in status["validation_issues"]])

    def test_relative_metadata_rejects_traversal_absolute_paths_and_symlink_escape(self):
        self.portable()
        path = self.run / "bad.json"
        for value in ("../outside", "/outside"):
            with self.assertRaises(ToolError) as caught:
                decode(path, {"file": value, "_task_paths": {"version": 1, "fields": [["file"]]}})
            self.assertEqual(caught.exception.key, "paths.invalid")
        (self.root / "escape").symlink_to(self.base)
        with self.assertRaises(ToolError):
            decode(path, {"file": "escape/outside", "_task_paths": {"version": 1, "fields": [["file"]]}})
        for marker in ({"version": True, "fields": []}, {"version": 1, "fields": [[-1]]}, {"version": 1, "fields": [[]]}):
            with self.assertRaises(ToolError):
                decode(path, {"file": "inputs/protein.pdb", "_task_paths": marker})

    def test_metadata_lists_roundtrip_and_external_paths_are_not_rebased(self):
        self.portable()
        value = {"files": [{"file": str(self.root / "inputs/protein.pdb")}], "external": str(self.base / "external")}
        path = self.run / "arrays.json"
        encoded = encode(path, value)
        self.assertEqual(encoded["files"][0]["file"], "inputs/protein.pdb")
        self.assertEqual(decode(path, encoded), value)
        self.assertEqual(encoded["files"][0]["file"], "inputs/protein.pdb")

    def test_compiled_artifacts_and_immutable_receipt_remain_bound_after_move(self):
        report = self.portable()
        compiler = self.base / "fixture-gmx"
        compiler.write_text("fixture compiler identity")
        compiler.chmod(0o700)
        output = self.run / "results/checks/grompp"
        output.mkdir(parents=True)
        log, tpr = output / "grompp.log", output / "system.tpr"
        log.write_text("fixture compiler log")
        tpr.write_bytes(b"fixture tpr")
        manifest = read_value(self.run / "full-run.json")
        evidence = managed._evidence(manifest, self.root / "charmm-gui.tgz", True, str(compiler))
        report.update(evidence=evidence, fingerprint=managed._canonical_hash(evidence),
                      checks={"grompp": {"passed": True, "output_dir": str(output), "log": str(log)}},
                      compiled_artifacts_sha256={"log": managed._digest(log), "tpr": managed._digest(tpr)})
        atomic_json(self.run / "results/validation.json", report)
        receipt = (self.run / "bilayer/download-receipt.json").read_bytes()
        self.move()
        value = managed.inspect_run("receptor-a")
        self.assertTrue(value["gromacs_compiled"])
        self.assertEqual(value["compilation_status"], "passed")
        self.assertEqual(receipt, (self.run / "bilayer/download-receipt.json").read_bytes())
        (self.run / "results/checks/grompp/system.tpr").write_bytes(b"changed")
        stale = managed.inspect_run("receptor-a")
        self.assertIn("validation.compilation_changed", [entry["key"] for entry in stale["validation_issues"]])

    def test_old_name_index_without_uuid_can_discover_but_never_by_name_alone(self):
        index = self.base / "index/names/receptor-a.json"
        record = json.loads(index.read_text())
        record.pop("task_id")
        index.write_text(json.dumps(record))
        job = json.loads((self.run / "job.json").read_text())
        job.pop("task_id")
        (self.run / "job.json").write_text(json.dumps(job))
        for path in (self.base / "index/run-history").glob("*.json"):
            old = json.loads(path.read_text())
            old.pop("task_id", None)
            path.write_text(json.dumps(old))
        self.move()
        self.assertEqual(_task_store.resolve_run("receptor-a"), self.run)
        self.assertEqual(managed.list_runs()[0]["directory"], str(self.run))
        job["created_at"] = "different registration"
        (self.run / "job.json").write_text(json.dumps(job))
        with self.assertRaises(ToolError) as caught:
            _task_store.resolve_run("receptor-a")
        self.assertEqual(caught.exception.key, "jobs.directory_missing")


class PreparedPortableTests(unittest.TestCase):
    def setUp(self):
        self.fixture = precheck.PrecheckTests()
        self.fixture.setUp()
        self.base = self.fixture.root
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("charmm_gui_cli.store.tasks._registry", return_value=self.base / "index"))
        self.stack.enter_context(patch("charmm_gui_cli.store.tasks._registries", return_value=(self.base / "index",)))
        self.stack.enter_context(patch("pathlib.Path.cwd", return_value=self.base))

    def test_real_cli_dryrun_moved_then_upload_uses_new_local_inputs(self):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(["build", "--dry-run", "--protein", str(self.fixture.protein), "--ligand", str(self.fixture.ligand),
                         "--name", "portable", "--out", str(self.base / "portable"), "--hydrogens", "preserve", "--json"])
        self.assertEqual(code, 0, error.getvalue())
        root = self.base / "portable"
        raw_input = json.loads((root / "inputs/input-manifest.json").read_text())
        # The original external source is provenance, not a runtime dependency.
        # Only task-local file references must become relative.
        self.assertEqual(raw_input["protein"]["path"], str(self.fixture.protein))
        raw_manifest = json.loads((root / ".charmm-gui/full-run.json").read_text())
        self.assertTrue(all(not Path(value).is_absolute() for value in raw_manifest["inputs"]["files"].values()))
        retained = self.base / "original-inputs-retained"
        retained.mkdir()
        self.fixture.protein.rename(retained / "protein.pdb")
        self.fixture.ligand.rename(retained / "ligand.sdf")
        moved = self.base / "moved"
        root.rename(moved)
        run = moved / ".charmm-gui"
        self.assertEqual(_task_store.resolve_run("portable"), run)
        web, api = Mock(), Mock()
        api.submit.return_value = "456"

        def upload(pdb, destination):
            self.assertEqual(Path(pdb), moved / "inputs/complex.pdb")
            self.assertTrue(Path(pdb).is_file())
            Path(destination).mkdir()
            atomic_write(Path(destination) / "upload-response.html", fixtures.UPLOAD)

        def submit(source, destination, fields, uploads=None):
            self.assertTrue(Path(source).is_file())
            if uploads:
                self.assertEqual(Path(uploads["sdf_cgenff[LIG]"]), moved / "inputs/ligand.sdf")
            atomic_write(destination, fixtures.SELECTION if Path(destination).name == "selection.html" else fixtures.READY)

        web.upload_structure.side_effect = upload
        web.submit_snapshot.side_effect = submit
        result = full_build.advance(run, web, api)
        self.assertEqual(result["state"], "bilayer_submitted")
        web.upload_structure.assert_called_once()
        api.submit.assert_called_once()
        raw = json.loads((run / "full-run.json").read_text())
        self.assertEqual(raw["preparation_snapshot"], ".charmm-gui/preparation/parameterization.html")
        self.assertNotIn(str(root), (run / "full-run.json").read_text())

    def test_unknown_post_intent_survives_move_without_replay(self):
        directory = managed.create_managed_build(protein=self.fixture.protein, ligand=self.fixture.ligand,
                                                 output=self.base / "intent/.charmm-gui", hydrogens="preserve")
        _task_store.assign_name(directory, "intent")
        preparation = directory / "preparation"
        preparation.mkdir()
        atomic_json(preparation / "web-run.json", {"state": "submission_unknown", "input_pdb": str(self.base / "intent/inputs/complex.pdb")})
        (self.base / "intent").rename(self.base / "intent-moved")
        moved = self.base / "intent-moved/.charmm-gui"
        web, api = Mock(), Mock()
        with self.assertRaises(SubmissionUnknown):
            full_build.advance(moved, web, api)
        web.upload_structure.assert_not_called()
        web.submit_snapshot.assert_not_called()
        api.submit.assert_not_called()
        self.assertEqual(read_value(moved / "preparation/web-run.json")["state"], "submission_unknown")

    def test_bound_preparation_source_and_child_can_move_without_reupload_or_resealing(self):
        self._bound_preparation_relocation()

    def test_quarantined_bound_source_and_child_retain_real_seal_and_intent_checks(self):
        self._bound_preparation_relocation(quarantine=True)

    def _bound_preparation_relocation(self, *, quarantine=False):
        from charmm_gui_cli.remote.web import soup_for, form_data, modeling_target
        runs = {}
        for name in ("source", "child"):
            runs[name] = managed.create_managed_build(protein=self.fixture.protein, ligand=self.fixture.ligand,
                output=self.base / name / ".charmm-gui", hydrogens="preserve")
            _task_store.assign_name(runs[name], name)
        source, child = runs["source"], runs["child"]
        web, api = Mock(), Mock()
        api.submit.return_value = "456"

        def upload(pdb, destination):
            Path(destination).mkdir()
            atomic_write(Path(destination) / "upload-response.html", fixtures.UPLOAD)
            atomic_json(Path(destination) / "web-run.json", {"state": "upload_response_received", "job_id": "123", "input_pdb": str(pdb)})

        def submit(snapshot, destination, overrides, uploads=None):
            form = soup_for(Path(snapshot).read_text()).find("form")
            fields = [[key, value] for key, value in form_data(form) if key not in overrides and key not in (uploads or {})]
            fields.extend([key, value] for key, value in overrides.items())
            atomic_json(Path(destination).with_suffix(".intent.json"), {"state": "response_received", "fields": fields,
                "target": modeling_target(form.get("action")), "upload_fields": list(uploads or {})})
            atomic_write(destination, fixtures.SELECTION if Path(destination).name == "selection.html" else fixtures.READY)

        web.upload_structure.side_effect = upload
        web.submit_snapshot.side_effect = submit
        full_build.advance(source, web, api)
        binding = sweep.bind_source(child, source)
        original = binding["certificate"]["fingerprint"]
        self.assertIsNotNone(binding["certificate"]["source_task_id"])
        applied = read_value(child / "full-run.json")["applied_terminals"]
        self.assertEqual(applied["terminal[PROA][first]"], "NTER")
        if quarantine:
            from charmm_gui_cli.pipeline.cleanup import clean
            # The synthetic membrane job is terminal, while its verified
            # preparation source remains reusable. Active builds stay guarded.
            for path in (source / 'full-run.json', source / 'bilayer/run.json'):
                saved = read_value(path)
                saved['state'] = 'remote_error'
                atomic_json(path, saved)
            before = {name: {str(p.relative_to(run)): p.read_bytes() for p in run.rglob('*')
                       if p.is_file() and p.name not in ('.lock', 'run.lock')} for name, run in runs.items()}
            with patch('requests.Session.request', side_effect=AssertionError('No quarantine HTTP')):
                moved_child = clean('child', yes=True)
                moved_source = clean('source', yes=True)
            source = Path(moved_source['directory']) / '.charmm-gui'
            child = Path(moved_child['directory']) / '.charmm-gui'
            for name, run in (('source', source), ('child', child)):
                self.assertEqual(before[name], {str(p.relative_to(run)): p.read_bytes() for p in run.rglob('*')
                                 if p.is_file() and p.name not in ('.lock', 'run.lock')})
        else:
            (self.base / "source").rename(self.base / "source-moved")
            (self.base / "child").rename(self.base / "child-moved")
            source, child = self.base / "source-moved/.charmm-gui", self.base / "child-moved/.charmm-gui"
        self.assertEqual(sweep.validate_reuse(child), "123")
        self.assertEqual(sweep.certify_source(source)["fingerprint"], original)
        self.assertEqual(sweep.reuse_api_config(child)["source_job_id"], "123")
        web.upload_structure.assert_called_once()
        self.assertEqual(web.submit_snapshot.call_count, 2)
        (source / "preparation/parameterization.html").write_text(fixtures.READY + "changed")
        with self.assertRaises(ToolError):
            sweep.validate_reuse(child)


if __name__ == "__main__":
    unittest.main()
