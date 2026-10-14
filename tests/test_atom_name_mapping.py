"""Evidence-bound naming adaptation, with no fitting-based name inference."""

import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.chem.multicomponent import validate_components
from charmm_gui_cli.chem.pose_validation import derive_server_atom_mapping
from charmm_gui_cli.chem.validation import compare_bound_pose, validate_atom_name_map
from charmm_gui_cli.pipeline import workflow


def pdb_atom(serial, name, resname, resid, xyz, element, segment):
    line = list(" " * 80)
    line[:6] = "ATOM  "
    line[6:11] = f"{serial:5d}"
    line[12:16] = f"{name:>4}"
    line[17:20] = f"{resname:>3}"
    line[21] = "A"
    line[22:26] = f"{resid:4d}"
    line[30:54] = "".join(f"{value:8.3f}" for value in xyz)
    line[54:66] = "  1.00  0.00"
    line[72:76] = f"{segment:<4}"
    line[76:78] = f"{element:>2}"
    return "".join(line) + "\n"


class AtomNameMappingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-name-map-")).resolve()
        self.run = self.root / "bilayer"
        self.run.mkdir()
        self.archive = self.run / "charmm-gui.tgz"
        self.system = self.root / "system"
        self.canonical = self.system / "charmm-gui-234"
        self.canonical.mkdir(parents=True)
        self.reference = self.root / "reference.pdb"
        self.atoms = [
            ["CA", "ALA", 1, (0, 0, 0), "C", "PROA"],
            ["CA", "ALA", 2, (3, 0, 0), "C", "PROA"],
            ["CA", "ALA", 3, (0, 4, 0), "C", "PROA"],
            ["CA", "ALA", 4, (0, 0, 5), "C", "PROA"],
            ["CM1", "LDA", 500, (1, 1, 2), "C", "HETA"],
            ["O1", "LDA", 500, (2, 1, 2), "O", "HETA"],
            ["CM2", "LDA", 500, (1, 2, 2), "C", "HETA"],
        ]
        self.mapping = {"CM1": "C1", "O1": "O", "CM2": "C2"}
        self.expected = {"resname": "LDA", "heavy_atoms": 3, "bonds": 2,
                         "atom_mapping": [{"pdb_atom_name": row[0], "element": row[4]} for row in self.atoms[4:]],
                         "heavy_atom_bonds": [{"atoms": ["CM1", "O1"]}, {"atoms": ["CM1", "CM2"]}]}
        self.manifest = {"components": [{"id": "component-001", "resname": "LDA", "expected_ligand": self.expected}],
                         "chemistry_groups": [{"resname": "LDA", "instances": ["component-001"]}]}
        self.write_reference()
        self.write_source()
        self.write_final()

    def write_reference(self):
        self.reference.write_text("".join(pdb_atom(i, *row) for i, row in enumerate(self.atoms, 1)))

    def write_source(self, *, drift=0, ambiguous=False, mass=12.011, mismatch_psf=False,
                     extra_copy=False, wrong_element=False, missing=False):
        rows = [list(row) for row in self.atoms]
        for row in rows[4:]:
            row[0] = self.mapping[row[0]]
        if drift:
            rows[4][3] = (1 + drift, 1, 2)
        if ambiguous:
            rows[6][3] = rows[4][3]
        if extra_copy:
            rows += [[r[0], r[1], 501, r[3], r[4], r[5]] for r in rows[4:]]
        if missing:
            rows = rows[:-1]
        pdb = "".join(pdb_atom(i, row[0], row[1], row[2], row[3], "N" if wrong_element and i == 5 else "", row[5])
                      for i, row in enumerate(rows, 1))
        masses = {"C": mass, "O": 15.9994}
        psf = f"PSF\n\n{len(rows):8d} !NATOM\n" + "".join(
            f"{i} {r[5]} {r[2]} {r[1]} {'WRNG' if mismatch_psf and i == 5 else r[0]} TYPE 0.0 {masses[r[4]]} 0\n"
            for i, r in enumerate(rows, 1))
        for name, body in (("step1_pdbreader.pdb", pdb), ("step1_pdbreader.psf", psf)):
            (self.canonical / name).write_text(body)
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, body in (("step1_pdbreader.pdb", pdb), ("step1_pdbreader.psf", psf)):
                payload = body.encode()
                member = tarfile.TarInfo("charmm-gui-234/" + name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        workflow.save(self.run, {
            "schema_version": 1, "state": "ligands_present", "remote_status": "done", "job_id": "234",
            "archive_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "config": {"version": 1, "source_job_id": "123", "expected_ligands": ["LDA"],
                       "preparation": {"ligand_parameters_ready": True, "orientation": "ppm"},
                       "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20}},
        })

    def write_final(self, *, drift=0, graph_changed=False, element_changed=False, extra_copy=False):
        rows = [list(row) for row in self.atoms]
        for row in rows:
            x, y, z = row[3]
            row[3] = (-y + 10 + (drift if row[1] == "LDA" else 0), x - 4, z + 7)
            if row[1] == "LDA":
                row[0] = self.mapping[row[0]]
        if extra_copy:
            rows += [[r[0], r[1], 501, r[3], r[4], r[5]] for r in rows[4:]]
        pdb = "".join(pdb_atom(i, *row) for i, row in enumerate(rows, 1))
        (self.canonical / "step5_assembly.pdb").write_text(pdb)
        gromacs = self.canonical / "gromacs"
        gromacs.mkdir(exist_ok=True)
        (gromacs / "step5_input.pdb").write_text(pdb)
        text = "[ atomtypes ]\nCT 6 12.011 0 A 0.3 0.1\nOT 8 15.9994 0 A 0.3 0.1\n[ moleculetype ]\nSYSTEM 3\n[ atoms ]\n"
        for i, row in enumerate(rows, 1):
            kind = "OT" if row[4] == "O" or (element_changed and i == 5) else "CT"
            text += f"{i} {kind} {row[2]} {row[1]} {row[0]} {i} 0 {15.9994 if kind == 'OT' else 12.011}\n"
        text += "[ bonds ]\n5 6 1\n" + ("6 7 1\n" if graph_changed else "5 7 1\n")
        if extra_copy:
            text += "8 9 1\n8 10 1\n"
        text += "[ system ]\nExample\n[ molecules ]\nSYSTEM 1\n"
        (gromacs / "topol.top").write_text(text)

    def derive(self, **kwargs):
        return derive_server_atom_mapping(self.system, self.reference, self.expected,
                                          archive_path=kwargs.get("archive_path", self.archive))

    def validate(self):
        return validate_components(self.system, self.manifest, self.reference, archive_path=self.archive)

    def test_exact_original_coordinates_prove_bijection_and_preserve_files(self):
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob("*") if path.is_file()}
        proof = self.derive()
        self.assertEqual(proof["mapping"], self.mapping)
        self.assertEqual(proof["archive_provenance"]["job_id"], "234")
        self.assertEqual(proof["reference_sha256"], before[self.reference])
        self.assertIn("no final-pose fit", proof["scope"])
        result = self.validate()
        self.assertTrue(result["passed"], result)
        component = result["components"]["LDA"]
        self.assertTrue(component["atom_name_mapping"]["final_graph_and_elements_passed"])
        self.assertLess(component["bound_pose"]["ligand_rmsd_after_protein_fit_A"], 1e-8)
        self.assertEqual(component["bound_pose"]["thresholds_A"], {"protein_ca_rmsd": 1.0, "ligand_rmsd": 2.0})
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob("*") if path.is_file()})

    def test_missing_provenance_is_not_optional(self):
        with self.assertRaisesRegex(ToolError, "job-bound"):
            self.derive(archive_path=None)

    def test_changed_extracted_source_rejected(self):
        path = self.canonical / "step1_pdbreader.pdb"
        path.write_text(path.read_text().replace("   1.000", "   1.001", 1))
        with self.assertRaisesRegex(ToolError, "differs from"):
            self.derive()

    def test_source_exactness_and_unique_elements_are_required(self):
        for options in ({"drift": 0.001}, {"ambiguous": True}, {"mass": 14.007},
                        {"wrong_element": True}, {"mass": 5.963}, {"extra_copy": True},
                        {"mismatch_psf": True}, {"missing": True}):
            with self.subTest(options=options):
                self.write_source(**options)
                with self.assertRaises(ToolError):
                    self.derive()

    def test_ambiguous_or_missing_canonical_source_rejected(self):
        other = self.system / "other"
        other.mkdir()
        (other / "step1_pdbreader.pdb").write_bytes((self.canonical / "step1_pdbreader.pdb").read_bytes())
        with self.assertRaisesRegex(ToolError, "Ambiguous"):
            self.derive()
        (other / "step1_pdbreader.pdb").rename(other / "saved-source.pdb")
        (self.canonical / "step1_pdbreader.psf").rename(self.canonical / "saved-source.psf")
        with self.assertRaisesRegex(ToolError, "Missing canonical"):
            self.derive()

    def test_archive_hash_change_fails_before_mapping(self):
        self.archive.write_bytes(self.archive.read_bytes() + b"changed")
        with self.assertRaisesRegex(ToolError, "SHA256 mismatch"):
            self.derive()

    def test_source_alternate_locations_and_multiple_models_rejected(self):
        original = self.reference.read_text()
        lines = original.splitlines(keepends=True)
        lines[4] = lines[4][:16] + "A" + lines[4][17:]
        self.reference.write_text("".join(lines))
        with self.assertRaisesRegex(ToolError, "alternate locations"):
            self.derive()
        self.reference.write_text("MODEL        1\n" + original + "ENDMDL\nMODEL        2\n" + original + "ENDMDL\n")
        with self.assertRaisesRegex(ToolError, "single PDB model"):
            self.derive()

    def test_final_graph_change_is_not_a_naming_fix(self):
        self.write_final(graph_changed=True)
        report = self.validate()
        self.assertFalse(report["passed"])
        self.assertFalse(report["components"]["LDA"]["atom_name_mapping"]["passed"])

    def test_final_element_change_fails_after_proven_source_map(self):
        self.write_final(element_changed=True)
        report = self.validate()
        self.assertFalse(report["passed"])
        self.assertFalse(report["components"]["LDA"]["topology_validation"]["checks"]["ligand_mapped_elements"]["passed"])

    def test_final_extra_copy_fails_even_when_individual_graphs_match(self):
        self.write_final(extra_copy=True)
        report = self.validate()
        self.assertFalse(report["passed"])
        self.assertFalse(report["components"]["LDA"]["exact_copy_count"]["passed"])

    def test_ligand_drift_still_fails_with_a_proven_map(self):
        self.write_final(drift=4)
        report = self.validate()
        self.assertFalse(report["passed"])
        component = report["components"]["LDA"]
        self.assertTrue(component["atom_name_mapping"]["passed"])
        self.assertFalse(component["bound_pose"]["passed"])
        self.assertAlmostEqual(component["bound_pose"]["ligand_rmsd_after_protein_fit_A"], 4)

    def test_changed_reference_element_or_multiple_copies_fail(self):
        self.atoms[4][4] = "N"
        self.write_reference()
        with self.assertRaises(ToolError):
            self.derive()
        self.atoms[4][4] = "C"
        self.atoms += [[r[0], r[1], 501, r[3], r[4], r[5]] for r in self.atoms[4:]]
        self.write_reference()
        with self.assertRaises(ToolError):
            self.derive()

    def test_mapping_requires_complete_unique_target_names(self):
        for mapping in ({"CM1": "C1"}, {"CM1": "C1", "CM2": "C1", "O1": "O"},
                        {"CM1": "LONGER", "CM2": "C2", "O1": "O"}):
            with self.assertRaises(ToolError):
                validate_atom_name_map(self.expected, mapping)
        with self.assertRaises(ToolError):
            compare_bound_pose(self.reference, self.canonical / "step5_assembly.pdb", ligand_resname="LDA",
                               ligand_atom_names=["CM1", "CM2", "O1"], final_ligand_atom_names=["C1", "C1", "O"])

    def test_original_name_failure_without_archive_stays_failed(self):
        report = validate_components(self.system, self.manifest, self.reference)
        self.assertFalse(report["passed"])
        self.assertIn("job-bound", report["components"]["LDA"]["atom_name_mapping"]["error"])

    def test_mapping_without_explicit_input_graph_does_not_pass(self):
        self.expected.pop("heavy_atom_bonds")
        report = self.validate()
        self.assertFalse(report["passed"])
        self.assertIn("complete input heavy-atom bond graph", report["components"]["LDA"]["atom_name_mapping"]["error"])

    def test_mapping_cannot_erase_or_retry_grompp_failure(self):
        (self.canonical / "gromacs/step6.0_minimization.mdp").write_text("integrator = steep\n")
        for failure in (ToolError("Compiler setup failed"), {"passed": False, "reason": "Compiler rejected topology"}):
            with self.subTest(failure=failure):
                options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch("charmm_gui_cli.chem.validation.run_grompp_check", **options) as compiler:
                    report = validate_components(self.system, self.manifest, self.reference,
                                                 archive_path=self.archive, run_grompp=True, gmx="gmx")
                self.assertFalse(report["passed"])
                checked = report["components"]["LDA"]["topology_validation"]
                self.assertFalse(checked["checks"]["grompp"]["passed"])
                compiler.assert_called_once()
                if isinstance(failure, Exception):
                    self.assertIn("Compiler setup failed", checked["errors"])


if __name__ == "__main__":
    unittest.main()
