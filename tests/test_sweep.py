"""Offline scan expansion and sealed-source reuse; retained synthetic fixtures."""

import json
from charmm_gui_cli.store.layout import inputs_path
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from charmm_gui_cli.pipeline import batch, sweep
from charmm_gui_cli.store.auth import ToolError, atomic_json
from charmm_gui_cli.pipeline.config import payload_for
from charmm_gui_cli.pipeline.full_build import _chains, _preparation_fields
from charmm_gui_cli.chem.inputs import Atom, _sdf
from charmm_gui_cli.remote.web import soup_for, form_data, modeling_target


UPLOAD = '''<form method="post" action="?doc=input/pdbreader&step=2"><input name="jobid" value="123">
<input type="checkbox" name="chains[PROA][checked]" value="1">
<input type="checkbox" name="chains[HETA][checked]" value="1"></form>'''
SELECTION = '''<form method="post" action="?doc=input/pdbreader&step=3"><input name="jobid" value="123">
<input type="file" name="sdf_cgenff[LIG]">
<select name="terminal[PROA][first]"><option value="NTER">NTER</option></select>
<select name="terminal[PROA][last]"><option value="CTER">CTER</option></select></form>'''
READY = '''<script>var formstop=0;var jobid='123';</script>
<form method="post" action="?doc=input/membrane.bilayer&step=2"><input name="jobid" value="123"></form>''' + "".join(
    f'<a href="uploaded_pdb/123/{path}">{path}</a>' for path in
    ("step1_pdbreader.pdb", "step1_pdbreader.psf", "lig/lig.rtf", "lig/lig.prm"))


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-sweep-test-")).resolve()
        self.path = self.root / "sweep.yaml"
        self.raw = {"version": 1, "inputs": {"protein": "protein.pdb", "ligand": "ligand.sdf"},
                    "axes": {"salt_concentration": [0.1, 0.15], "margin": [20, 25]}}
        self.write()

    def write(self):
        self.path.write_text(yaml.safe_dump(self.raw))

    def make_run(self, name, seal=True):
        directory = self.root / name
        (directory / "inputs").mkdir(parents=True)
        protein = "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\nEND\n"
        ligand = _sdf([Atom(1, "C1", "C", (3., 2., 1.)), Atom(2, "O1", "O", (4.3, 2., 1.))], {(0, 1): 1}, "LIG")
        files = {"protein.pdb": protein, "ligand.pdb": "ligand fixture\n", "complex.pdb": protein + "bound fixture\n", "ligand.sdf": ligand}
        for key, value in files.items():
            (directory / "inputs" / key).write_text(value)
        config = {"version": 2, "protein": str(directory / "inputs/protein.pdb"), "ligand": str(directory / "inputs/ligand.sdf"),
                  "ligand_resname": "LIG", "ligand_hydrogens": "preserve",
                  "preparation": {"orientation": "ppm", "n_terminal": "NTER", "c_terminal": "CTER"},
                  "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
                  "ions": {"type": "KCl", "concentration_M": 0.1}}
        atomic_json(directory / "full-run.json", {"schema_version": 2, "state": "inputs_ready", "config": config,
                    "inputs": {"files": {key: str(directory / "inputs" / key) for key in files}}})
        if seal:
            sweep.seal_preparation(directory)
        return directory

    def complete_source(self, directory):
        prep = directory / "preparation"
        prep.mkdir()
        for name, text in (("upload-response.html", UPLOAD), ("selection.html", SELECTION), ("parameterization.html", READY)):
            (prep / name).write_text(text)
        value = json.loads((directory / "full-run.json").read_text())
        for source, output, overrides, uploads in (
            ("upload-response.html", "selection.html", _chains(prep / "upload-response.html"), []),
            ("selection.html", "parameterization.html", _preparation_fields(prep / "selection.html", value["config"]), ["sdf_cgenff[LIG]"])):
            form = soup_for((prep / source).read_text()).find("form")
            fields = [[key, val] for key, val in form_data(form) if key not in overrides and key not in uploads]
            fields.extend([key, val] for key, val in overrides.items())
            atomic_json((prep / output).with_suffix(".intent.json"), {"state": "response_received", "target": modeling_target(form.get("action")),
                        "fields": fields, "upload_fields": uploads})
        atomic_json(prep / "web-run.json", {"state": "upload_response_received", "job_id": "123", "input_pdb": str(inputs_path(directory) / "complex.pdb")})
        value.update(state="prepared", source_job_id="123", preparation_snapshot=str(prep / "parameterization.html"), preparation_page_state="ready")
        atomic_json(directory / "full-run.json", value)

    def test_cartesian_expansion_deterministic_and_relative_inputs(self):
        result = sweep.expand(self.path)
        self.assertEqual(result["conditions"], 4)
        self.assertEqual([job["name"] for job in result["batch"]["jobs"]], ["scan-0001", "scan-0002", "scan-0003", "scan-0004"])
        self.assertEqual(result["batch"]["defaults"]["protein"], str(self.root / "protein.pdb"))
        self.assertTrue(result["batch"]["reuse_preparation"])
        self.assertEqual(sweep.expand(self.path), result)

    def test_axes_reject_preparation_or_chemistry_changes(self):
        for key in ("hydrogens", "protein", "n_terminal", "orientation", "ligand_smiles"):
            self.raw["axes"] = {key: ["arbitrary"]}
            self.write()
            with self.subTest(key=key), self.assertRaises(ToolError):
                sweep.expand(self.path)

    def test_reject_bad_types_duplicates_and_large_product(self):
        for axes in ({"margin": []}, {"margin": 20}, {"salt_concentration": [0, 0.]}, {"margin": [True]},
                     {"margin": list(range(1, 258))}, {"salt": ["MgCl2"]}):
            self.raw["axes"] = axes
            self.write()
            with self.subTest(axes=axes), self.assertRaises(ToolError):
                sweep.expand(self.path)

    def test_bare_source_id_rejected(self):
        self.raw["source_run"] = "123"
        self.write()
        with self.assertRaisesRegex(ToolError, "bare"):
            sweep.expand(self.path)

    def test_identity_ignores_run_directory_and_membrane_axes(self):
        first, second = self.make_run("first"), self.make_run("second")
        value = json.loads((second / "full-run.json").read_text())
        value["config"]["membrane"]["margin_A"] = 35
        value["config"]["ions"]["concentration_M"] = 0.5
        atomic_json(second / "full-run.json", value)
        self.assertEqual(sweep.preparation_identity(first), sweep.preparation_identity(second))

    def test_seal_idempotent_but_mutation_refused(self):
        source = self.make_run("source")
        before = (source / "preparation-seal.json").read_bytes()
        sweep.seal_preparation(source)
        self.assertEqual(before, (source / "preparation-seal.json").read_bytes())
        (source / "inputs/complex.pdb").write_text("changed")
        with self.assertRaisesRegex(ToolError, "changed"):
            sweep.seal_preparation(source)

    def test_legacy_upload_cannot_be_retroactively_sealed(self):
        source = self.make_run("legacy", seal=False)
        self.complete_source(source)
        with self.assertRaisesRegex(ToolError, "retroactively"):
            sweep.seal_preparation(source)
        with self.assertRaises(ToolError):
            sweep.certify_source(source)

    def test_complete_source_certificate_and_clone_payload(self):
        source, child = self.make_run("source"), self.make_run("child")
        self.complete_source(source)
        with patch("requests.Session.request", side_effect=AssertionError("No network")):
            certificate = sweep.certify_source(source)
            record = sweep.bind_source(child, source)
            self.assertEqual(record["certificate"], certificate)
            self.assertEqual(sweep.validate_reuse(child), "123")
            self.assertEqual(payload_for(sweep.reuse_api_config(child))["clone_job"], "true")
        self.assertFalse((child / "preparation").exists())
        before = (child / "full-run.json").read_bytes()
        sweep.bind_source(child, source)
        self.assertEqual(before, (child / "full-run.json").read_bytes())

    def test_source_state_advances_without_invalidating_preparation(self):
        source, child = self.make_run("source"), self.make_run("child")
        self.complete_source(source)
        sweep.bind_source(child, source)
        value = json.loads((source / "full-run.json").read_text())
        value.update(state="ligands_present", build_job_id="456")
        atomic_json(source / "full-run.json", value)
        self.assertEqual(sweep.validate_reuse(child), "123")

    def test_changed_terminal_hydrogen_orientation_refused(self):
        source = self.make_run("source")
        self.complete_source(source)
        for key, changed in (("n_terminal", "ACE"), ("orientation", "prepared"), ("ligand_hydrogens", "add_missing")):
            child = self.make_run(key)
            value = json.loads((child / "full-run.json").read_text())
            target = value["config"] if key == "ligand_hydrogens" else value["config"]["preparation"]
            target[key] = changed
            atomic_json(child / "full-run.json", value)
            with self.subTest(key=key), self.assertRaisesRegex(ToolError, "differ"):
                sweep.bind_source(child, source)

    def test_bound_child_or_source_mutation_blocks_clone(self):
        source, child = self.make_run("source"), self.make_run("child")
        self.complete_source(source)
        sweep.bind_source(child, source)
        (child / "inputs/protein.pdb").write_text("changed")
        with self.assertRaises(ToolError):
            sweep.reuse_api_config(child)

    def test_changed_post_or_incomplete_ready_page_refused(self):
        for defect in ("intent", "ready", "job"):
            source = self.make_run(defect)
            self.complete_source(source)
            if defect == "intent":
                path = source / "preparation/parameterization.intent.json"
                value = json.loads(path.read_text())
                value["fields"].append(["unknown_preparation", "1"])
                atomic_json(path, value)
            elif defect == "ready":
                (source / "preparation/parameterization.html").write_text(READY.replace("lig/lig.prm", "lig/missing.prm"))
            else:
                (source / "preparation/upload-response.html").write_text(UPLOAD.replace('value="123"', 'value="999"'))
            with self.subTest(defect=defect), self.assertRaises(ToolError):
                sweep.certify_source(source)

    def test_attempted_child_not_rebound(self):
        source, child = self.make_run("source"), self.make_run("child")
        self.complete_source(source)
        (child / "preparation").mkdir()
        with self.assertRaisesRegex(ToolError, "untouched"):
            sweep.bind_source(child, source)

    def test_group_leader_then_bind_without_counting_shared_source_as_active(self):
        source, child = self.make_run("source"), self.make_run("child")
        rows = [{"name": path.name, "directory": str(path), "state": "inputs_ready", "started": False} for path in (source, child)]
        manifest = {"config": {"reuse_preparation": True}, "jobs": rows}
        self.assertEqual(sweep.bind_ready_sources(self.root, manifest)["waiting"], 1)
        self.assertTrue(rows[1]["reuse_waiting"])
        self.complete_source(source)
        self.assertEqual(sweep.bind_ready_sources(self.root, manifest)["bound"], 1)
        batch._local(rows[1])
        self.assertEqual(rows[1]["state"], "prepared_reused")
        self.assertFalse(rows[1]["started"])

    def test_local_sweep_preparation_without_credentials_and_resume_fingerprint(self):
        try:
            import rdkit  # noqa: F401
        except ImportError:
            self.skipTest("RDKit needed for local chemistry preparation")
        template = self.make_run("template")
        self.raw["inputs"] = {"protein": str(template / "inputs/protein.pdb"), "ligand": str(template / "inputs/ligand.sdf")}
        self.raw["axes"] = {"margin": [20, 25]}
        self.write()
        with patch("charmm_gui_cli.store.tasks._registry", return_value=self.root / "registry"), \
                patch("charmm_gui_cli.pipeline.build_command.load_token", side_effect=AssertionError("No auth")), \
                patch("requests.Session.request", side_effect=AssertionError("No network")):
            result = sweep.prepare(self.path, self.root / "scan")
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["conditions"], 2)
        self.assertFalse(result["server_submitted"])
        self.assertEqual(sweep.read(self.root / "scan")["state"], "local_ready")
        with self.assertRaisesRegex(ToolError, "already exists"):
            sweep.prepare(self.path, self.root / "scan")
        self.path.write_text(self.path.read_text() + "\n# changed\n")
        with self.assertRaises(ToolError):
            sweep.read(self.root / "scan")

    def test_batch_parallel_scan_waits_for_one_source_then_limits_clones(self):
        template = self.make_run("template")
        self.raw["inputs"] = {"protein": str(template / "inputs/protein.pdb"), "ligand": str(template / "inputs/ligand.sdf"), "hydrogens": "preserve"}
        self.raw["axes"] = {"margin": [20, 25, 30]}
        self.write()
        with patch("charmm_gui_cli.store.tasks._registry", return_value=self.root / "registry"), \
                patch("requests.Session.request", side_effect=AssertionError("No remote tasks")):
            result = sweep.prepare(self.path, self.root / "scan")
            batch_dir = Path(result["batch_directory"])
            with patch("charmm_gui_cli.pipeline.build_command.execute", return_value=({"state": "preparation_running"}, 0)) as execute:
                summary, code = batch.advance(batch_dir, wait=False, max_active=4, submit_interval=0)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(summary["counts"]["waiting_preparation"], 2)
            source = Path(result["leader"])
            self.complete_source(source)
            with patch("charmm_gui_cli.pipeline.build_command.execute", return_value=({"state": "running"}, 0)) as execute:
                summary, code = batch.advance(batch_dir, wait=False, max_active=2, submit_interval=0)
            self.assertEqual(execute.call_count, 2)
            third = json.loads((batch_dir / "jobs/scan-0003/.charmm-gui/full-run.json").read_text())
            self.assertEqual(third["state"], "prepared_reused")
            rows = json.loads((batch_dir / "batch.json").read_text())["jobs"]
            self.assertFalse(rows[2]["started"])

    def test_ready_source_cannot_bind_an_already_uploaded_follower(self):
        source, child = self.make_run("source"), self.make_run("child")
        self.complete_source(source)
        (child / "preparation").mkdir()
        atomic_json(child / "preparation/web-run.json", {"state": "upload_submitting"})
        rows = [{"name": path.name, "directory": str(path), "state": "inputs_ready", "started": False} for path in (source, child)]
        result = sweep.bind_ready_sources(self.root, {"config": {"reuse_preparation": True}, "jobs": rows})
        self.assertEqual(result["bound"], 0)
        self.assertNotIn("reused_preparation", json.loads((child / "full-run.json").read_text()))

    def test_multi_component_reuse_explicitly_refused(self):
        source = self.make_run("source", seal=False)
        value = json.loads((source / "full-run.json").read_text())
        value["config"]["components_mode"] = True
        atomic_json(source / "full-run.json", value)
        with self.assertRaisesRegex(ToolError, "multi-component"):
            sweep.seal_preparation(source)

    def test_unknown_preparation_configuration_refused(self):
        source = self.make_run("source", seal=False)
        value = json.loads((source / "full-run.json").read_text())
        value["config"]["unknown_protonation_rule"] = "guess"
        atomic_json(source / "full-run.json", value)
        with self.assertRaisesRegex(ToolError, "unknown"):
            sweep.seal_preparation(source)

    def test_already_bound_clones_do_not_recertify_source_during_status_round(self):
        source, child = self.make_run("source"), self.make_run("child")
        self.complete_source(source)
        sweep.bind_source(child, source)
        rows = [{"name": path.name, "directory": str(path), "state": "running", "started": True} for path in (source, child)]
        with patch("charmm_gui_cli.pipeline.sweep.certify_source", side_effect=AssertionError("No new binding needed")):
            result = sweep.bind_ready_sources(self.root, {"config": {"reuse_preparation": True}, "jobs": rows})
        self.assertEqual(result, {"bound": 0, "waiting": 0})

    def test_interrupted_explicit_source_binding_cannot_fallback_to_upload(self):
        self.raw["source_run"] = "existing-source"
        self.write()
        expanded = sweep.expand(self.path)
        directory = self.root / "interrupted"
        directory.mkdir()
        atomic_json(directory / "expanded-batch.json", expanded["batch"])
        atomic_json(directory / "sweep.json", {"version": 1, "expanded": expanded,
                    "fingerprint": batch._canonical(expanded), "state": "preparing_local",
                    "batch_directory": str(directory / "batch")})
        with self.assertRaisesRegex(ToolError, "fallback upload"):
            sweep.read(directory)


if __name__ == "__main__":
    unittest.main()
