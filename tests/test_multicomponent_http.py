"""Offline form/HTTP orchestration tests; never submit real remote models."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from charmm_gui_cli import full_build
from charmm_gui_cli.auth import ToolError, atomic_json, atomic_write
from charmm_gui_cli.inputs import Atom, _sdf


def form(names=("LIG", "COF")):
    controls = '<input name="rename_checked" type="checkbox" value="1" checked>'
    for name in names:
        controls += (f'<input name="rename[{name}]" type="radio" value="cgenff">'
                     f'<input name="rename_option[cgenff][{name}]" type="radio" value="sdf">'
                     f'<input name="sdf_cgenff[{name}]" type="file">'
                     f'<input name="newname[{name}]" value="{name}">'
                     f'<input name="comment[{name}]" type="hidden" value="cgenff">')
    return ('<form action="?doc=input/pdbreader&step=3" method="post"><input name="jobid" value="123">'
            + controls + '<select name="terminal[PROA][first]"><option value="NTER">NTER</option></select>'
            '<select name="terminal[PROA][last]"><option value="CTER">CTER</option></select></form>')


def ready(names=("LIG", "COF"), stop=0):
    artifacts = ['step1_pdbreader.pdb', 'step1_pdbreader.psf']
    artifacts += [name.lower() + '/' + name.lower() + suffix for name in names for suffix in ('.rtf', '.prm')]
    return ('<script>var formstop=' + str(stop) + ';var jobid="123";var project="membrane_bilayer";</script>'
            '<form action="?doc=input/membrane.bilayer&step=2" method="post"><input name="jobid" value="123"></form>'
            + ''.join(f'<a href="uploaded_pdb/123/{name}">{name}</a>' for name in artifacts))


UPLOAD = ('<form action="?doc=input/pdbreader&step=2" method="post"><input name="jobid" value="123">'
          '<input type="checkbox" name="chains[PROA][checked]" value="1" checked>'
          '<input type="checkbox" name="chains[HETA][checked]" value="1">'
          '<input type="checkbox" name="chains[HETB][checked]" value="1"></form>')


class MultiComponentHttpTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-multicomponent-http-")).resolve()
        (self.root / "inputs").mkdir()
        groups = []
        files = {"complex.pdb": str(self.root / "inputs/complex.pdb")}
        atomic_write(files["complex.pdb"], "END\n")
        for index, name in enumerate(("LIG", "COF"), 1):
            filename = f"component-{index:03d}.sdf"
            path = self.root / "inputs" / filename
            atomic_write(path, _sdf([Atom(1, "C1", "C", (3., 0., 0.))], {}, name))
            files[filename] = str(path)
            groups.append({"resname": name, "mode": "cgenff_required", "instances": [f"component-{index:03d}"],
                           "representative_sdf": str(path), "files": {"sdf": str(path)}})
        self.groups = groups
        self.config = {"version": 2, "components_mode": True, "ligand_resname": "LIG",
                       "component_resnames": ["LIG", "COF"],
                       "preparation": {"n_terminal": "NTER", "c_terminal": "CTER", "orientation": "ppm"},
                       "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20},
                       "ions": {"type": "KCl", "concentration_M": 0.15}}
        self.manifest = {"schema_version": 2, "state": "inputs_ready", "config": self.config,
                         "inputs": {"files": files, "manifest": {"chemistry_groups": self.groups}}}
        self.web, self.api = Mock(), Mock()
        self.prep = self.root / "preparation"
        self.selected = self.prep / "selection.html"
        self.final = self.prep / "parameterization.html"

    def saved_form(self, html=None):
        self.prep.mkdir(exist_ok=True)
        atomic_write(self.selected, html or form())
        return self.selected

    def test_every_group_discovers_and_sets_its_actual_controls(self):
        result = full_build._preparation_fields(self.saved_form(), self.config, self.groups)
        for name in ("LIG", "COF"):
            self.assertEqual(result[f"rename[{name}]"], "cgenff")
            self.assertEqual(result[f"rename_option[cgenff][{name}]"], "sdf")
            self.assertEqual(result[f"newname[{name}]"], name)
            self.assertEqual(result[f"comment[{name}]"], "cgenff")
        self.assertEqual(result["terminal[PROA][first]"], "NTER")

    def test_missing_group_cgenff_option_cannot_be_invented(self):
        html = form().replace('name="rename[COF]" type="radio" value="cgenff"', 'name="rename[COF]" type="radio" value="rename"')
        with self.assertRaises(ToolError):
            full_build._preparation_fields(self.saved_form(html), self.config, self.groups)

    def test_disabled_sdf_field_is_not_accepted(self):
        html = form().replace('name="sdf_cgenff[COF]" type="file"', 'name="sdf_cgenff[COF]" type="file" disabled')
        with self.assertRaises(ToolError):
            full_build._preparation_fields(self.saved_form(html), self.config, self.groups)

    def test_controls_in_unrelated_forms_do_not_satisfy_discovery(self):
        html = form(("LIG",)) + form(("COF",)).replace('<input name="jobid" value="123">', "")
        with self.assertRaises(ToolError):
            full_build._preparation_fields(self.saved_form(html), self.config, self.groups)

    def test_duplicate_groups_and_missing_prepared_sdf_refused_before_upload(self):
        for change in ("duplicate", "missing"):
            with self.subTest(change=change):
                groups = [dict(group) for group in self.groups]
                if change == "duplicate":
                    groups[1]["resname"] = "LIG"
                else:
                    groups[1]["files"] = {"sdf": str(self.root / "absent.sdf")}
                self.manifest["inputs"]["manifest"]["chemistry_groups"] = groups
                atomic_json(self.root / "full-run.json", self.manifest)
                with self.assertRaises(ToolError):
                    full_build.advance(self.root, self.web, self.api)
                self.web.upload_structure.assert_not_called()

    def test_declared_component_list_must_match_prepared_groups(self):
        self.config["component_resnames"] = ["LIG"]
        atomic_json(self.root / "full-run.json", self.manifest)
        with self.assertRaisesRegex(ToolError, "disagree"):
            full_build.advance(self.root, self.web, self.api)
        self.web.upload_structure.assert_not_called()

    def test_ready_requires_rtf_and_prm_for_all_components(self):
        self.saved_form()
        self.manifest["source_job_id"] = "123"
        atomic_write(self.final, ready(("LIG",)))
        with self.assertRaises(ToolError):
            full_build._await_preparation(self.web, self.root, self.manifest, self.final, False, 10, 60)
        self.assertIn("/uploaded_pdb/123/cof/cof.rtf", self.manifest["preparation_missing_artifacts"])
        self.assertIn("/uploaded_pdb/123/cof/cof.prm", self.manifest["preparation_missing_artifacts"])
        self.web.submit_snapshot.assert_not_called()

    def test_all_parameter_links_establish_preparation(self):
        self.saved_form()
        self.manifest["source_job_id"] = "123"
        atomic_write(self.final, ready())
        full_build._await_preparation(self.web, self.root, self.manifest, self.final, False, 10, 60)
        self.assertEqual(self.manifest["state"], "prepared")

    def test_trusted_topology_refused_before_remote_upload(self):
        self.groups[1]["mode"] = "trusted_charmm"
        atomic_json(self.root / "full-run.json", self.manifest)
        with self.assertRaisesRegex(ToolError, "trusted|topology|CHARMM"):
            full_build.advance(self.root, self.web, self.api)
        self.web.upload_structure.assert_not_called()
        self.web.submit_snapshot.assert_not_called()
        self.web.request.assert_not_called()
        self.api.submit.assert_not_called()

    def test_full_round_uploads_each_sdf_clones_all_groups_and_never_reposts(self):
        atomic_json(self.root / "full-run.json", self.manifest)
        def upload(path, directory):
            directory.mkdir()
            atomic_write(directory / "upload-response.html", UPLOAD)
        def submit(source, destination, fields, uploads=None):
            if destination == self.selected:
                self.assertEqual(fields["chains[HETA][checked]"], "1")
                self.assertEqual(fields["chains[HETB][checked]"], "1")
                atomic_write(destination, form())
            else:
                self.assertEqual(uploads, {f"sdf_cgenff[{g['resname']}]": g["files"]["sdf"] for g in self.groups})
                atomic_write(destination, ready())
        def start(api, config, directory, *, existing_configuration=False):
            self.assertEqual(set(config["expected_ligands"]), {"LIG", "COF"})
            directory.mkdir()
            atomic_json(directory / "run.json", {"job_id": "456"})
            return {"job_id": "456", "state": "submitted"}
        self.web.upload_structure.side_effect = upload
        self.web.submit_snapshot.side_effect = submit
        with patch.object(full_build, "start_api", side_effect=start) as start_mock:
            result = full_build.advance(self.root, self.web, self.api)
            self.assertEqual(result["state"], "bilayer_submitted")
            with patch.object(full_build, "resume_api", return_value={"state": "running", "job_id": "456"}):
                self.assertEqual(full_build.advance(self.root, None, self.api)["state"], "running")
            start_mock.assert_called_once()
        self.web.upload_structure.assert_called_once()
        self.assertEqual(self.web.submit_snapshot.call_count, 2)
        stored = json.loads((self.root / "api-config.json").read_text())
        self.assertEqual(set(stored["expected_ligands"]), {"LIG", "COF"})

    def test_unsupported_group_field_stops_before_parameterization_post(self):
        self.saved_form(form(("LIG",)))
        atomic_write(self.prep / "upload-response.html", UPLOAD)
        atomic_json(self.root / "full-run.json", self.manifest)
        with patch.object(full_build, "start_api") as start:
            with self.assertRaises(ToolError):
                full_build.advance(self.root, self.web, self.api)
            start.assert_not_called()
        self.web.submit_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
