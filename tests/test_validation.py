import io
import importlib.util
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.auth import ToolError
from charmm_gui_cli.validation import (extract_for_validation, inspect_coordinates, inspect_topology,
                                       run_grompp_check, validate_charmm_source, validate_system,
                                       compare_bound_pose, find_ion_settings, inspect_environment)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="charmm-validation-test-"))
        (self.root / "toppar").mkdir()
        (self.root / "topol.top").write_text('#include "toppar/ligand.itp"\n[ system ]\nExample\n[ molecules ]\nLIG 1\n')
        (self.root / "toppar/ligand.itp").write_text(
            "[ atomtypes ]\nCT 6 12.011 0.0 A 0.3 0.1\nHC 1 1.008 0.0 A 0.1 0.05\n"
            "[ moleculetype ]\nLIG 3\n[ atoms ]\n1 CT 1 LIG C1 1 0.0 12.011\n"
            "2 HC 1 LIG H1 1 0.0 1.008\n[ bonds ]\n1 2 1 0.1 1000\n")
        (self.root / "step5_input.gro").write_text("Example\n2\n"
            + f"{1:5d}{'LIG':<5}{'C1':>5}{1:5d}{0.1:8.3f}{0.2:8.3f}{0.3:8.3f}\n"
            + f"{1:5d}{'LIG':<5}{'H1':>5}{2:5d}{0.2:8.3f}{0.2:8.3f}{0.3:8.3f}\n"
            + "1.0 1.0 1.0\n")
        (self.root / "step6.0_minimization.mdp").write_text("integrator = steep\nnsteps = 1000\n")
        self.manifest = {"ligand": {"resname": "LIG", "heavy_atoms": 1, "bonds": 0,
                                    "atom_mapping": [{"pdb_atom_name": "C1", "element": "C"}]}}

    def test_static_valid_case_is_not_science_certification(self):
        result = validate_system(self.root, self.manifest)
        self.assertTrue(result["passed"], result)
        self.assertFalse(result["scientific_correctness_verified"])
        self.assertEqual(result["checks"]["ligand_coordinate_retention"]["heavy_atoms"], 1)
        self.assertEqual(len(result["files"]["includes"]), 2)

    def test_missing_heavy_atom_detected(self):
        self.manifest["ligand"]["heavy_atoms"] = 2
        result = validate_system(self.root, self.manifest)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["ligand_topology_heavy_atoms"]["passed"])

    def test_hmr_hydrogens_are_not_counted_as_heavy_atoms(self):
        path = self.root / "toppar/ligand.itp"
        path.write_text(path.read_text().replace("0.0 12.011", "0.0 5.963").replace("0.0 1.008", "0.0 3.024"))
        result = validate_system(self.root, self.manifest)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["checks"]["ligand_topology_heavy_atoms"]["observed_per_copy"], [1])
        self.assertTrue(result["ligand_mass_repartitioning"]["detected"])

    def test_atomtype_optional_fields_preserve_element_identity(self):
        path = self.root / "toppar/ligand.itp"
        original = path.read_text().replace("0.0 12.011", "0.0 5.963").replace("0.0 1.008", "0.0 3.024")
        for carbon, hydrogen, expected_z in (
            ("CT 12.011 0.0 A", "HC 1.008 0.0 A", None),
            ("CT CTYPE 12.011 0.0 A", "HC HTYPE 1.008 0.0 A", None),
            ("CT 6 12.011 0.0 A", "HC 1 1.008 0.0 A", 1),
            ("CT CTYPE 6 12.011 0.0 A", "HC HTYPE 1 1.008 0.0 A", 1),
        ):
            with self.subTest(hydrogen=hydrogen):
                path.write_text(original.replace("CT 6 12.011 0.0 A", carbon).replace("HC 1 1.008 0.0 A", hydrogen))
                topology = inspect_topology(self.root / "topol.top")
                self.assertEqual(topology["atomtypes"]["HC"]["atomic_number"], expected_z)
                self.assertFalse(topology["molecules"]["LIG"]["atoms"][1]["heavy"])
                self.assertTrue(validate_system(self.root, self.manifest)["passed"])

    def test_missing_ligand_detected(self):
        result = validate_system(self.root, self.manifest, ligand_resname="ABC")
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["ligand_topology_presence"]["passed"])

    def test_atom_order_mismatch_detected(self):
        path = self.root / "step5_input.gro"
        path.write_text(path.read_text().replace("   C1", "   XX"))
        result = validate_system(self.root, self.manifest)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["coordinate_topology_atom_order"]["passed"])
        self.assertIsNone(result["checks"]["ligand_coordinate_retention"]["heavy_atoms"])

    def test_atomtypes_required(self):
        path = self.root / "toppar/ligand.itp"
        path.write_text(path.read_text().replace("1 CT 1", "1 MISSING 1"))
        result = validate_system(self.root, self.manifest)
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["ligand_atomtypes_defined"]["undefined"], ["MISSING"])

    def test_missing_include_reported_readably(self):
        path = self.root / "topol.top"
        path.write_text(path.read_text().replace("ligand.itp", "missing.itp"))
        result = validate_system(self.root, self.manifest)
        self.assertFalse(result["passed"])
        self.assertIn("Missing topology include", result["errors"][0])

    def test_inactive_preprocessor_include_skipped(self):
        path = self.root / "topol.top"
        path.write_text('#ifdef ABSENT\n#include "nonexistent.itp"\n#endif\n' + path.read_text())
        self.assertEqual(inspect_topology(path)["total_atoms"], 2)

    def test_active_preprocessor_include_required(self):
        path = self.root / "topol.top"
        path.write_text('#ifdef PRESENT\n#include "nonexistent.itp"\n#endif\n' + path.read_text())
        with self.assertRaisesRegex(ToolError, "Missing topology include"):
            inspect_topology(path, defines={"PRESENT"})

    def test_gro_declared_count_checked(self):
        path = self.root / "step5_input.gro"
        path.write_text(path.read_text().replace("Example\n2\n", "Example\n3\n"))
        with self.assertRaisesRegex(ToolError, "Malformed"):
            inspect_coordinates(path)

    def test_charmm_five_digit_water_ids_are_not_merged(self):
        lines = []
        for residue_id in (" 9999", "10000", "10001", "10009", "10010"):
            for name in ("OH2", "H1", "H2"):
                line = list(" " * 80)
                line[:6] = "ATOM  "
                line[6:11] = "*****"
                line[12:16] = f"{name:>4}"
                line[17:21] = "TIP3"
                line[22:27] = residue_id
                line[30:54] = f"{0.0:8.3f}" * 3
                line[72:76] = "TIP3"
                lines.append("".join(line))
        path = self.root / "large-water.pdb"
        path.write_text("\n".join(lines) + "\nEND\n")
        result = inspect_coordinates(path)
        self.assertEqual(result["total_atoms"], 15)
        self.assertEqual(result["residue_counts"]["TIP3"], 5)

    def test_pdb_insertion_codes_distinguish_residues(self):
        lines = []
        for code in (" ", "A", "B"):
            line = list(" " * 80)
            line[:6] = "ATOM  "
            line[12:16] = "  CA"
            line[17:20] = "ALA"
            line[21] = "A"
            line[22:26] = "  25"
            line[26] = code
            line[30:54] = f"{0.0:8.3f}" * 3
            lines.append("".join(line))
        path = self.root / "insertions.pdb"
        path.write_text("\n".join(lines) + "\nEND\n")
        self.assertEqual(inspect_coordinates(path)["residue_counts"]["ALA"], 3)

    def test_grompp_zero_warning_policy_and_retained_output(self):
        def fake_run(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_bytes(b"test tpr")
            return subprocess.CompletedProcess(command, 0)
        with patch("charmm_gui_cli.validation.subprocess.run", side_effect=fake_run) as run:
            result = validate_system(self.root, self.manifest, run_grompp=True, gmx="gmx")
        self.assertTrue(result["passed"], result)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-maxwarn") + 1], "0")
        self.assertEqual(command[1], "grompp")
        self.assertTrue(Path(result["checks"]["grompp"]["output_dir"], "command.json").exists())

    def test_grompp_failure_not_ignored(self):
        with patch("charmm_gui_cli.validation.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            result = validate_system(self.root, self.manifest, run_grompp=True, gmx="gmx")
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["grompp"]["returncode"], 1)

    def test_nonminimization_mdp_refused(self):
        path = self.root / "production.mdp"
        path.write_text("integrator = md\n")
        with self.assertRaisesRegex(ToolError, "energy-minimization"):
            run_grompp_check(self.root / "topol.top", self.root / "step5_input.gro", path, self.root / "outputs", gmx="gmx")

    def make_archive(self, member):
        path = self.root / "archive.tgz"
        with tarfile.open(path, "w:gz") as archive:
            archive.addfile(member, io.BytesIO(b"hi") if member.isfile() else None)
        return path

    def test_safe_archive_extracts_exclusively(self):
        member = tarfile.TarInfo("system/topol.top")
        member.size = 2
        archive = self.make_archive(member)
        output = extract_for_validation(archive, self.root / "extracted")
        self.assertEqual((output / "system/topol.top").read_text(), "hi")
        with self.assertRaisesRegex(ToolError, "new destination"):
            extract_for_validation(archive, output)

    def test_archive_traversal_rejected(self):
        member = tarfile.TarInfo("../bad")
        member.size = 2
        archive = self.make_archive(member)
        with self.assertRaisesRegex(ToolError, "Unsafe"):
            extract_for_validation(archive, self.root / "extracted")
        self.assertFalse((self.root / "extracted").exists())

    def test_archive_symlink_rejected(self):
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/tmp"
        archive = self.make_archive(member)
        with self.assertRaisesRegex(ToolError, "links/devices"):
            extract_for_validation(archive, self.root / "extracted")

    def test_charmm_source_retention_and_penalty_reporting(self):
        pdb = self.root / "prepared.pdb"
        pdb.write_text("ATOM      1  C1  LIG L   0       1.000   2.000   3.000  1.00  0.00      HETA C\n"
                       "ATOM      2  H1  LIG L   0       2.000   2.000   3.000  1.00  0.00      HETA H\nEND\n")
        psf = self.root / "prepared.psf"
        psf.write_text("PSF EXT\n\n2 !NATOM\n1 HETA 0 LIG C1 CT 0.1 12.011 0\n2 HETA 0 LIG H1 HC -0.1 1.008 0\n")
        rtf = self.root / "lig.rtf"
        rtf.write_text("RESI LIG 0.000 ! param penalty= 1.4 ; charge penalty= 1.442\n"
                       "ATOM C1 CT 0.1\nATOM H1 HC -0.1\nBOND C1 H1\nEND\n")
        prm = self.root / "lig.prm"
        prm.write_text("BONDS\nCT HC 100.0 1.0 ! penalty= 1.4\nEND\n")
        result = validate_charmm_source(pdb, psf, rtf, prm, self.manifest)
        self.assertTrue(result["passed"], result)
        self.assertFalse(result["scientific_correctness_verified"])
        self.assertEqual(result["cgenff_penalties"]["parameter_max"], 1.4)
        self.assertEqual(result["cgenff_penalties"]["charge_max"], 1.442)
        self.assertEqual(result["ligand"]["hydrogen_atoms"], 1)
        self.manifest["ligand"]["heavy_atoms"] = 2
        self.assertFalse(validate_charmm_source(pdb, psf, rtf, prm, self.manifest)["passed"])

    def test_requested_kcl_distinguished_from_default_nacl(self):
        (self.root / "step4_ions.inp").write_text("set conc = 0.15 ! actual generated input\n")
        with patch("charmm_gui_cli.validation.inspect_coordinates", return_value={"residue_counts": {
                "POPC": 200, "TIP3": 55500, "SOD": 150, "CLA": 150}}):
            result = inspect_environment("unused.pdb", expected_lipids=["POPC"], ion_type="KCl", concentration_M=0.1, system_dir=self.root)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["ion_species"]["passed"])
        self.assertEqual(result["checks"]["concentration_direct_evidence"]["status"], "mismatch")

    def test_kcl_and_concentration_confirmed_with_counterions(self):
        (self.root / "step4_ions.inp").write_text("set conc 0.10\n")
        with patch("charmm_gui_cli.validation.inspect_coordinates", return_value={"residue_counts": {
                "POPC": 200, "TIP3": 55500, "POT": 108, "CLA": 100}}):
            result = inspect_environment("unused.pdb", expected_lipids=["POPC"], ion_type="KCl", concentration_M=0.1, system_dir=self.root)
        self.assertTrue(result["passed"], result)
        self.assertAlmostEqual(result["water_ratio_salt_estimate"]["concentration_M"], 0.1)
        self.assertEqual(result["water_ratio_salt_estimate"]["excess_counterions"], 8)

    def test_concentration_estimate_does_not_replace_direct_evidence(self):
        with patch("charmm_gui_cli.validation.inspect_coordinates", return_value={"residue_counts": {
                "POPC": 200, "TIP3": 55500, "POT": 100, "CLA": 100}}):
            result = inspect_environment("unused.pdb", expected_lipids=["POPC"], ion_type="KCl", concentration_M=0.1)
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["concentration_direct_evidence"]["status"], "not_found")
        self.assertTrue(result["water_ratio_salt_estimate"]["approximately_matches_request"])

    def test_referenced_single_salt_stream_resolves_only_active_index(self):
        (self.root / "step4.3_ion.inp").write_text("set ionind = 1\nstream step2.2_ions_count.str @ionind\n")
        (self.root / "step2.2_ions_count.str").write_text(
            "if @IN1 .eq. 1 set pos = SOD\nif @IN1 .eq. 1 set neg = CLA\n"
            "if @IN1 .eq. 1 set conc = 0.15\nif @IN1 .eq. 2 set conc = 0.10\nset niontypes = 1\n")
        records = find_ion_settings(self.root)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["concentration_M"], 0.15)
        self.assertEqual(records[0]["cation"], "SOD")
        self.assertEqual(records[0]["selected_index"], 1)
        with patch("charmm_gui_cli.validation.inspect_coordinates", return_value={"residue_counts": {
                "POPC": 200, "TIP3": 55500, "POT": 100, "CLA": 100}}):
            result = inspect_environment("unused.pdb", expected_lipids=["POPC"], ion_type="KCl", concentration_M=0.1, system_dir=self.root)
        self.assertFalse(result["checks"]["ion_species_direct_evidence"]["passed"])
        self.assertFalse(result["checks"]["concentration_direct_evidence"]["passed"])

    def test_unreferenced_ion_stream_is_not_evidence(self):
        (self.root / "unused_ions.str").write_text("if @IN1 .eq. 1 set conc = 0.10\nset niontypes = 1\n")
        self.assertEqual(find_ion_settings(self.root), [])

    def test_multisalt_conditionals_not_silently_interpreted(self):
        (self.root / "step4.3_ion.inp").write_text("set ionind = 1\nstream step2.2_ions_count.str @ionind\n")
        (self.root / "step2.2_ions_count.str").write_text(
            "if @IN1 .eq. 1 set conc = 0.10\nif @IN1 .eq. 2 set conc = 0.15\nset niontypes = 2\n")
        records = find_ion_settings(self.root)
        self.assertIsNone(records[0]["concentration_M"])
        with patch("charmm_gui_cli.validation.inspect_coordinates", return_value={"residue_counts": {
                "POPC": 200, "TIP3": 55500, "POT": 100, "CLA": 100}}):
            result = inspect_environment("unused.pdb", expected_lipids=["POPC"], ion_type="KCl", concentration_M=0.1, system_dir=self.root)
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["concentration_direct_evidence"]["status"], "unresolved")

    def pose_pdb(self, path, *, transform=False, ligand_shift=0):
        records = [("ALA", i + 1, "CA", xyz) for i, xyz in enumerate([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)])]
        records.extend([("LIG", 1, "C1", (2, 2, 2)), ("LIG", 1, "C2", (3, 2, 2))])
        lines = []
        for serial, (resname, resid, name, xyz) in enumerate(records, 1):
            x, y, z = xyz
            if transform:
                x, y, z = -y + 10, x + 3, z - 2
            if resname == "LIG":
                x += ligand_shift
            line = f"ATOM  {serial:5d} {name:>4} {resname:>3} A{resid:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C"
            lines.append(line)
        path.write_text("\n".join(lines) + "\nEND\n")

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional dependency is absent")
    def test_ppm_rigid_transform_preserves_bound_pose(self):
        source, target = self.root / "source.pdb", self.root / "target.pdb"
        self.pose_pdb(source)
        self.pose_pdb(target, transform=True)
        result = compare_bound_pose(source, target)
        self.assertTrue(result["passed"], result)
        self.assertLess(result["ligand_rmsd_after_protein_fit_A"], 1e-10)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional dependency is absent")
    def test_independent_ligand_motion_is_not_aligned_away(self):
        source, target = self.root / "source.pdb", self.root / "target.pdb"
        self.pose_pdb(source)
        self.pose_pdb(target, transform=True, ligand_shift=5)
        result = compare_bound_pose(source, target)
        self.assertFalse(result["passed"])
        self.assertAlmostEqual(result["ligand_rmsd_after_protein_fit_A"], 5)


if __name__ == "__main__":
    unittest.main()
