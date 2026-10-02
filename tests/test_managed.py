import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli import managed
from charmm_gui_cli.auth import ToolError, atomic_json, atomic_write


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-managed-test-"))
        self.home = self.root / "private-home"
        self.home.mkdir()
        self.home_patch = patch("pathlib.Path.home", return_value=self.home)
        self.home_patch.start()
        self.directory = self.root / "run"

    def tearDown(self):
        self.home_patch.stop()  # Preserve all files and directories.

    def make_run(self, state="ligands_present", directory=None):
        directory = directory or self.directory
        directory.mkdir(parents=True)
        (directory / "inputs").mkdir()
        files = {}
        for name in ("input-manifest.json", "complex.pdb", "ligand.sdf", "protein.pdb"):
            path = directory / "inputs" / name
            atomic_write(path, '{"ligand": {}}' if name.endswith("json") else name + "\n")
            files[name] = str(path)
        config = {"membrane": {"upper": "POPC=1", "lower": "POPC:POPE=1:1"},
                  "ions": {"type": "KCl", "concentration_M": 0.15}}
        manifest = {"schema_version": 2, "inputs": {"files": files}, "config": config, "state": state}
        atomic_json(directory / "full-run.json", manifest)
        (directory / "bilayer").mkdir()
        atomic_write(directory / "bilayer" / "charmm-gui.tgz", "mock archive bytes")
        return manifest

    def validators(self):
        def extract(source, destination):
            destination.mkdir(parents=True)
            return destination
        def validate(system, manifest, **kwargs):
            checks = {}
            if kwargs.get("run_grompp"):
                compiled = kwargs["output_parent"] / "grompp-test"
                compiled.mkdir()
                atomic_write(compiled / "system.tpr", "compiled test system")
                atomic_write(compiled / "grompp.log", "mock compilation log")
                checks["grompp"] = {"passed": True, "output_dir": str(compiled), "log": str(compiled / "grompp.log")}
            return {"passed": True, "files": {"coordinates": str(system / "step5_input.gro")}, "checks": checks}
        return (patch.object(managed, "extract_for_validation", side_effect=extract),
                patch.object(managed, "validate_system", side_effect=validate),
                patch.object(managed, "inspect_environment", return_value={"passed": True}),
                patch.object(managed, "validate_bound_pose", return_value={"passed": True}))

    def fake_gmx(self):
        path = self.root / "test-gmx"
        atomic_write(path, "mock executable identity")
        path.chmod(0o700)
        return str(path)

    def test_pending_run_has_no_success_claim_or_extraction(self):
        self.make_run(state="running")
        with patch.object(managed, "extract_for_validation") as extract:
            report = managed.finalize_managed_build(self.directory)
        self.assertEqual(report["status"], "pending")
        self.assertIsNone(report["passed"])
        extract.assert_not_called()

    def test_missing_archive_returns_pending_even_for_complete_state(self):
        self.make_run(state="complete")
        archive = self.directory / "bilayer" / "charmm-gui.tgz"
        archive.rename(archive.with_name("retained-original.tgz"))
        report = managed.finalize_managed_build(self.directory)
        self.assertEqual(report["status"], "pending")
        self.assertIsNone(report["passed"])

    def test_complete_validation_is_cached_without_repeating_work(self):
        self.make_run()
        a, b, c, d = self.validators()
        with a as extract, b as validate, c as env, d:
            first = managed.finalize_managed_build(self.directory)
            second = managed.finalize_managed_build(self.directory)
        self.assertEqual(first, second)
        self.assertTrue(first["passed"])
        self.assertEqual(first["compilation"]["status"], "not_run")
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(env.call_args.kwargs["expected_lipids"], ["POPC", "POPE"])
        self.assertEqual(env.call_args.kwargs["ion_type"], "KCl")
        self.assertTrue(Path(first["archive"]).is_file())
        self.assertTrue(Path(first["report_file"]).is_file())

    def test_grompp_upgrade_never_reuses_no_grompp_report(self):
        self.make_run()
        a, b, c, d = self.validators()
        with a as extract, b as validate, c, d:
            first = managed.finalize_managed_build(self.directory)
            second = managed.finalize_managed_build(self.directory, grompp=True, gmx="test-gmx")
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(extract.call_count, 2)
        self.assertTrue(validate.call_args.kwargs["run_grompp"])
        self.assertEqual(validate.call_args.kwargs["gmx"], "test-gmx")
        historical = list((self.directory / "results").glob("previous-validation-*/validation.json"))
        self.assertEqual(len(historical), 1)
        self.assertEqual(json.loads(historical[0].read_text())["fingerprint"], first["fingerprint"])

    def test_changed_input_config_archive_or_tool_invalidates_cache(self):
        manifest = self.make_run()
        a, b, c, d = self.validators()
        with a as extract, b, c, d:
            reports = [managed.finalize_managed_build(self.directory)]
            atomic_write(self.directory / "inputs" / "complex.pdb", "changed reference")
            reports.append(managed.finalize_managed_build(self.directory))
            manifest["config"]["ions"]["concentration_M"] = 0.2
            atomic_json(self.directory / "full-run.json", manifest)
            reports.append(managed.finalize_managed_build(self.directory))
            atomic_write(self.directory / "bilayer" / "charmm-gui.tgz", "new archive")
            reports.append(managed.finalize_managed_build(self.directory))
            with patch.object(managed, "__version__", "test-new-version"):
                reports.append(managed.finalize_managed_build(self.directory))
        self.assertEqual(len({report["fingerprint"] for report in reports}), 5)
        self.assertEqual(extract.call_count, 5)

    def test_environment_failure_cannot_be_reported_as_passed(self):
        self.make_run()
        a, b, c, d = self.validators()
        with a, b, c as environment, d:
            environment.return_value = {"passed": False, "error": "wrong salt"}
            report = managed.finalize_managed_build(self.directory)
        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "validation_failed")
        self.assertEqual(report["environment_validation"]["error"], "wrong salt")

    def test_environment_toolerror_is_written_into_complete_failure_report(self):
        self.make_run()
        a, b, c, d = self.validators()
        with a, b, c as environment, d:
            environment.side_effect = ToolError("Unsupported ion type")
            report = managed.finalize_managed_build(self.directory)
        self.assertFalse(report["passed"])
        self.assertTrue(report["validation_complete"])
        self.assertEqual(report["environment_validation"]["error"], "Unsupported ion type")
        self.assertTrue(report["bound_pose_validation"]["passed"])
        self.assertTrue(Path(report["report_file"]).is_file())

    def test_compiled_cache_requires_tpr_and_log_to_remain_present(self):
        self.make_run()
        gmx = self.fake_gmx()
        a, b, c, d = self.validators()
        with a as extract, b, c, d:
            first = managed.finalize_managed_build(self.directory, grompp=True, gmx=gmx)
            self.assertEqual(managed.finalize_managed_build(self.directory, grompp=True, gmx=gmx), first)
            self.assertEqual(extract.call_count, 1)
            tpr = Path(first["checks"]["grompp"]["output_dir"]) / "system.tpr"
            tpr.rename(tpr.with_name("retained-system.tpr"))
            second = managed.finalize_managed_build(self.directory, grompp=True, gmx=gmx)
            self.assertEqual(extract.call_count, 2)
            log = Path(second["checks"]["grompp"]["log"])
            log.rename(log.with_name("retained-grompp.log"))
            managed.finalize_managed_build(self.directory, grompp=True, gmx=gmx)
            self.assertEqual(extract.call_count, 3)

    def test_compiler_binary_upgrade_invalidates_pass(self):
        self.make_run()
        gmx = self.fake_gmx()
        a, b, c, d = self.validators()
        with a as extract, b, c, d:
            first = managed.finalize_managed_build(self.directory, grompp=True, gmx=gmx)
            atomic_write(gmx, "upgraded mock executable has changed size")
            Path(gmx).chmod(0o700)
            second = managed.finalize_managed_build(self.directory, grompp=True, gmx=gmx)
        self.assertEqual(extract.call_count, 2)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["evidence"]["gmx_identity"], second["evidence"]["gmx_identity"])

    def test_unavailable_compiler_cannot_reuse_past_pass(self):
        self.make_run()
        gmx = self.fake_gmx()
        a, b, c, d = self.validators()
        with a as extract, b, c, d:
            first = managed.finalize_managed_build(self.directory, grompp=True, gmx=gmx)
            Path(gmx).rename(self.root / "retained-compiler")
            with patch.object(managed.shutil, "which", return_value=None):
                second = managed.finalize_managed_build(self.directory, grompp=True, gmx=gmx)
                managed.finalize_managed_build(self.directory, grompp=True, gmx=gmx)
        self.assertEqual(extract.call_count, 3)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertFalse(second["evidence"]["gmx_identity"]["available"])

    def test_runs_list_shows_trustworthy_final_status_and_artifact_paths(self):
        manifest = self.make_run()
        managed.remember_run(self.directory)
        a, b, c, d = self.validators()
        with a, b, c, d:
            report = managed.finalize_managed_build(self.directory)
        item = managed.list_runs()[0]
        self.assertEqual(item["state"], "validated")
        self.assertEqual(item["report_file"], report["report_file"])
        self.assertEqual(item["archive"], report["archive"])
        manifest["config"]["ions"]["concentration_M"] = 0.3
        atomic_json(self.directory / "full-run.json", manifest)
        stale = managed.list_runs()[0]
        self.assertEqual(stale["state"], "ligands_present")
        self.assertEqual(stale["validation_status"], "stale_or_unreadable")

    def test_runs_list_shows_live_local_bilayer_poll_state(self):
        self.make_run(state="bilayer_submitted")
        managed.remember_run(self.directory)
        atomic_json(self.directory / "bilayer" / "run.json", {
            "state": "running", "job_id": "123", "remote_status": "running",
            "last_output_file": "step4_lipid.out", "lastOutLine": "NOT_FOR_SUMMARY"})
        item = managed.list_runs()[0]
        self.assertEqual(item["state"], "running")
        self.assertEqual(item["remote_status"], "running")
        self.assertEqual(item["last_output_file"], "step4_lipid.out")
        self.assertEqual(item["build_job_id"], "123")
        self.assertNotIn("NOT_FOR_SUMMARY", json.dumps(item))

    def test_final_validation_precedes_intermediate_bilayer_state(self):
        self.make_run()
        managed.remember_run(self.directory)
        a, b, c, d = self.validators()
        with a, b, c, d:
            managed.finalize_managed_build(self.directory)
        atomic_json(self.directory / "bilayer" / "run.json", {"state": "ligands_present", "remote_status": "done"})
        item = managed.list_runs()[0]
        self.assertEqual(item["state"], "validated")
        self.assertEqual(item["remote_status"], "done")

    def test_validation_transport_error_is_not_swallowed(self):
        self.make_run()
        with patch.object(managed, "extract_for_validation", side_effect=ToolError("broken archive")):
            with self.assertRaisesRegex(ToolError, "broken archive"):
                managed.finalize_managed_build(self.directory)
        self.assertFalse((self.directory / "results" / "validation.json").exists())

    def test_recent_registry_is_private_and_selects_latest_readable_run(self):
        self.make_run()
        managed.remember_run(self.directory)
        self.assertEqual(managed.latest_run(), self.directory)
        registry = self.home / ".config" / "charmm-gui-cli"
        self.assertEqual(stat.S_IMODE((registry / "latest-run.json").stat().st_mode), 0o600)
        newer = self.root / "newer"
        self.make_run(directory=newer)
        managed.remember_run(newer)
        self.assertEqual(managed.latest_run(), newer)
        newer.rename(self.root / "moved-newer")
        self.assertEqual(managed.latest_run(), self.directory)
        text = json.dumps(managed.list_runs())
        self.assertNotIn("token", text)
        self.assertNotIn("cookies", text)

    def test_missing_recent_run_is_controlled_error(self):
        with self.assertRaisesRegex(ToolError, "No readable recent build"):
            managed.latest_run()

    def test_new_run_path_creates_parent_but_not_run(self):
        first, second = managed.new_run_path(), managed.new_run_path()
        self.assertNotEqual(first, second)
        self.assertTrue(first.parent.is_dir())
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_create_managed_run_uses_source_adapter_and_new_directory(self):
        def sources(**kwargs):
            out = kwargs["out"]
            out.mkdir(parents=True)
            atomic_write(out / "protein.pdb", "protein")
            atomic_write(out / "ligand.sdf", "ligand")
            return {"protein": str(out / "protein.pdb"), "ligand": str(out / "ligand.sdf"), "report": {"mode": "split"}}
        def initialize(config, destination):
            self.assertFalse(destination.exists())
            self.make_run(state="inputs_ready", directory=destination)
        with patch("charmm_gui_cli.input_sources.prepare_sources", side_effect=sources) as prepare, \
                patch.object(managed, "initialize", side_effect=initialize):
            directory = managed.create_managed_build(complex_pdb="complex.pdb", accept_conect_bond_orders=True)
        self.assertTrue(directory.is_relative_to(self.home / ".local" / "share" / "charmm-gui-cli" / "runs"))
        self.assertEqual(managed.latest_run(), directory)
        self.assertTrue(prepare.call_args.kwargs["accept_conect_bond_orders"])
        self.assertEqual(json.loads((directory / "managed.json").read_text())["source_report"]["mode"], "split")

    def test_create_does_not_overwrite_or_register_failed_initialization(self):
        self.directory.mkdir()
        with patch("charmm_gui_cli.input_sources.prepare_sources") as prepare:
            with self.assertRaisesRegex(ToolError, "already exists"):
                managed.create_managed_build(output=self.directory)
        prepare.assert_not_called()
        self.assertEqual(managed.list_runs(), [])

    def test_failed_initialization_is_not_registered(self):
        def sources(**kwargs):
            kwargs["out"].mkdir(parents=True)
            return {"protein": "/unused-protein.pdb", "ligand": "/unused-ligand.sdf", "report": {}}
        with patch("charmm_gui_cli.input_sources.prepare_sources", side_effect=sources), \
                patch.object(managed, "initialize", side_effect=ToolError("invalid input chemistry")):
            with self.assertRaisesRegex(ToolError, "invalid input chemistry"):
                managed.create_managed_build(output=self.directory)
        self.assertEqual(managed.list_runs(), [])
        self.assertTrue((self.root / ".run.sources" / "build-config.json").is_file())


if __name__ == "__main__":
    unittest.main()
