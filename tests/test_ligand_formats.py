"""Public ligand-format/pose workflow tests with no network or original edits."""

import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import yaml

from charmm_gui_cli.chem.inputs import Atom, _sdf, _pdb, read_ligand, _apply_hydrogen_policy
from charmm_gui_cli.chem.input_sources import _chemical_sdf_in_complex
from charmm_gui_cli.store.auth import ToolError, atomic_write
from charmm_gui_cli.store.paths import read_value
from charmm_gui_cli.pipeline import full_build
import test_input_policy as fixtures
import test_sweep as web_fixtures


MOL2 = """@<TRIPOS>MOLECULE
bound-carbonyl
2 1 0 0 0
SMALL
USER_CHARGES

@<TRIPOS>ATOM
1 C1 0.0000 3.2000 0.0000 C.2 1 LIG 0.2000
2 O1 1.2000 3.2000 0.0000 O.2 1 LIG -0.2000
@<TRIPOS>BOND
1 1 2 2
"""


class LigandFormatTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InputPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.protein, self.ligand = self.fixture.root, self.fixture.protein, self.fixture.ligand

    def poses(self):
        records = []
        for index, score in enumerate((-6.0, -9.5, -4.0), 1):
            sdf = _sdf([Atom(1, "C1", "C", (index * .2, 3.2, 0.)), Atom(2, "O1", "O", (1.2 + index * .2, 3.2, 0.))], {(0, 1): 2}, f"pose-{index}")
            records.append(sdf.replace("$$$$", f"> <minimizedAffinity>\n{score}\n\n$$$$"))
        self.ligand.write_text("".join(records))

    def v3000(self, *, stereo=False, names=True):
        from rdkit import Chem
        from rdkit.Chem import AllChem
        if stereo:
            molecule = Chem.MolFromSmiles("F[C@H](Cl)Br")
            AllChem.Compute2DCoords(molecule)
            conf = molecule.GetConformer()
            for index in range(molecule.GetNumAtoms()):
                xyz = conf.GetAtomPosition(index)
                xyz.y += 6
                conf.SetAtomPosition(index, xyz)
        else:
            molecule = Chem.MolFromMolBlock(self.ligand.read_text(), removeHs=False)
        block = Chem.MolToMolBlock(molecule, forceV3000=True)
        if names and not stereo:
            block += "> <PDB_ATOM_NAMES>\nC1 O1\n\n"
        self.ligand.write_text(block + "$$$$\n")
        return molecule

    def test_multi_pose_rejects_with_names_scores_and_an_executable_choice_in_both_languages(self):
        self.poses()
        original = self.ligand.read_bytes()
        for lang in ("zh", "en"):
            code, report, output, error = self.fixture.check(tty=True, lang=lang)
            self.assertEqual(code, 2)
            self.assertEqual(report["error"]["message_key"], "input.ligand_poses")
            for value in ("pose-1", "pose-2", "pose-3", "minimizedAffinity=-9.5"):
                self.assertIn(value, output)
            self.assertIn("--ligand-pose 1", report["error"]["next_step"])
            self.assertEqual(self.ligand.read_bytes(), original)

    def test_explicit_pose_preserves_atom_coordinates_bonds_and_scores_not_lowest_score(self):
        self.poses()
        original = self.ligand.read_bytes()
        code, report, output, _ = self.fixture.check("--ligand-pose", "3", tty=True)
        self.assertEqual(code, 0, report.get("error"))
        self.assertIn("3/3", output)
        manifest = read_value(report["prepared_files"]["input-manifest.json"])
        selection = manifest["ligand_selection"]
        self.assertEqual(selection["selected_pose"], 3)
        self.assertEqual(selection["pose_inventory"][1]["scores"]["minimizedAffinity"], -9.5)
        prepared = read_ligand(report["prepared_files"]["ligand.sdf"])
        self.assertEqual(prepared.atoms[0].xyz, (.6, 3.2, 0.))
        self.assertEqual({edge: order for edge, order in prepared.bonds.items() if max(edge) < 2}, {(0, 1): 2})
        self.assertEqual(manifest["source_evidence"]["ligand"]["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self.ligand.read_bytes(), original)

    def test_invalid_pose_indices_are_localized_and_never_submitted(self):
        self.poses()
        for index in ("0", "-1", "4"):
            code, report, output, _ = self.fixture.check("--ligand-pose", index, tty=True, lang="zh")
            self.assertEqual(code, 2)
            self.assertFalse(report["server_submitted"])
            self.assertIn("ligand-pose", report["error"]["next_step"])

    def test_v3000_cli_normalizes_without_original_edits_or_graph_changes(self):
        molecule = self.v3000()
        original = self.ligand.read_bytes()
        code, report, output, _ = self.fixture.check(tty=True, lang="zh")
        self.assertEqual(code, 0, report.get("error"))
        self.assertIn("sdf_v3000", output)
        sdf = Path(report["prepared_files"]["ligand.sdf"])
        self.assertIn("V2000", sdf.read_text())
        parsed = read_ligand(sdf)
        self.assertEqual({edge: order for edge, order in parsed.bonds.items() if max(edge) < 2}, {(0, 1): 2})
        self.assertEqual([atom.name for atom in parsed.atoms[:2]], ["C1", "O1"])
        self.assertTrue(report["chemistry"][0]["ligand_source"]["conversion"]["graph_and_stereo_roundtrip_verified"])
        self.assertEqual(self.ligand.read_bytes(), original)

    def test_declared_chirality_survives_v3000_conversion_and_hydrogen_addition(self):
        from rdkit import Chem
        molecule = self.v3000(stereo=True, names=False)
        parsed = read_ligand(self.ligand)
        prepared = _apply_hydrogen_policy(parsed, "add_missing")
        roundtrip = Chem.MolFromMolBlock(prepared.sdf, removeHs=False)
        self.assertEqual(Chem.MolToSmiles(Chem.RemoveHs(roundtrip)), Chem.MolToSmiles(molecule))
        self.assertTrue(parsed.provenance["conversion"]["graph_and_stereo_roundtrip_verified"])

    def test_3d_coordinates_do_not_invent_undeclared_stereo(self):
        from rdkit import Chem
        from rdkit.Chem import AllChem
        molecule = Chem.AddHs(Chem.MolFromSmiles("F[C@H](Cl)Br"))
        AllChem.EmbedMolecule(molecule, randomSeed=7)
        block = Chem.MolToMolBlock(molecule, forceV3000=True)
        import re
        block = re.sub(r" CFG=[123]", "", block)
        self.ligand.write_text(block + "$$$$\n")
        parsed = read_ligand(self.ligand)
        lines = parsed.sdf.splitlines()
        self.assertTrue(all(int(line[39:42].strip() or "0") == 0 for line in lines[4:4 + len(parsed.atoms)]))
        self.assertTrue(all(int(line[9:12].strip() or "0") == 0 for line in lines[4 + len(parsed.atoms):4 + len(parsed.atoms) + len(parsed.bonds)]))

    def test_mol2_cli_shows_charge_review_and_keeps_partial_charges_distinct(self):
        self.fixture.ligand = self.root / "bound.mol2"
        self.fixture.ligand.write_text(MOL2)
        for lang, text in (("zh", "部分电荷"), ("en", "partial-charge")):
            code, report, output, _ = self.fixture.check(tty=True, lang=lang)
            self.assertEqual(code, 0, report.get("error"))
            self.assertTrue(report["review_required"])
            self.assertIn(text, output)
            conversion = report["chemistry"][0]["ligand_source"]["conversion"]
            self.assertEqual(conversion["input_partial_charges"], [.2, -.2])
            self.assertEqual(conversion["net_formal_charge"], 0)
            self.assertEqual(conversion["implicit_valence_hydrogens"], 2)
            self.assertEqual(self.fixture.ligand.read_text(), MOL2)

    def test_mol2_long_names_are_mapped_and_cannot_fake_complex_atom_identity(self):
        path = self.root / "long-names.mol2"
        path.write_text(MOL2.replace(" C1 ", " CARBONYL-C ").replace(" O1 ", " CARBONYL-O "))
        molecule = read_ligand(path)
        self.assertEqual([atom.name for atom in molecule.atoms], ["C1", "O1"])
        self.assertFalse(molecule.provenance["explicit_atom_names"])
        with self.assertRaisesRegex(ToolError, "explicit"):
            _chemical_sdf_in_complex(path, molecule.atoms, molecule.bonds)

    def test_query_isotope_enhanced_stereo_and_special_bonds_are_not_discarded(self):
        from rdkit import Chem
        molecules = [Chem.MolFromSmiles(smiles) for smiles in ("[13CH3]O", "[CH2]O", "[NH3]->[Cu+2]", "F[C@H](Cl)Br |&1:1|")]
        molecules.append(Chem.MolFromSmarts("C~O"))
        for molecule in molecules:
            conf = Chem.Conformer(molecule.GetNumAtoms())
            molecule.AddConformer(conf)
            self.ligand.write_text(Chem.MolToMolBlock(molecule, forceV3000=True) + "$$$$\n")
            with self.assertRaises(ToolError) as error:
                read_ligand(self.ligand)
            self.assertEqual(error.exception.key, "input.ligand_semantics")

    def test_pdbqt_error_is_actionable_without_guessing_chemical_bonds(self):
        self.fixture.ligand = self.root / "pose.pdbqt"
        self.fixture.ligand.write_text("ROOT\nENDROOT\n")
        code, report, output, _ = self.fixture.check(tty=True, lang="zh")
        self.assertEqual(code, 2)
        self.assertIn("PDBQT", output)
        self.assertIn("SMILES", output)
        self.assertFalse(report["server_submitted"])

    def test_yaml_batch_and_sweep_select_the_same_record_as_the_cli(self):
        self.poses()
        for command in ("build", "yaml", "batch", "sweep"):
            directory = self.root / command
            if command == "build":
                args = ["build", "--protein", str(self.protein), "--ligand", str(self.ligand), "--ligand-pose", "2"]
            else:
                inputs = {"protein": str(self.protein), "ligand": str(self.ligand), "ligand_pose": 2, "hydrogens": "preserve"}
                raw = {"version": 2, "protein": str(self.protein), "ligand": str(self.ligand), "input_options": {"ligand_pose": 2}, "ligand_hydrogens": "preserve", "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20}} if command == "yaml" else \
                    {"version": 1, "defaults": inputs, "jobs": [{"name": "a"}, {"name": "b"}]} if command == "batch" else \
                    {"version": 1, "inputs": inputs, "axes": {"salt_concentration": [.1, .2]}}
                config = self.root / (command + ".yaml")
                config.write_text(yaml.safe_dump(raw))
                args = ["build" if command == "yaml" else command, str(config)]
            code, output, error = self.fixture.call([*args, "--out", str(directory), "--dry-run", "--json"])
            self.assertEqual(code, 0, error + output)
            paths = [directory / "inputs/input-manifest.json"] if command in ("build", "yaml") else list(directory.glob("jobs/*/inputs/input-manifest.json")) if command == "batch" else list(directory.glob("batch/jobs/*/inputs/input-manifest.json"))
            self.assertEqual(len(paths), 1 if command in ("build", "yaml") else 2)
            for path in paths:
                self.assertEqual(read_value(path)["ligand_selection"]["selected_pose"], 2)
                self.assertEqual(read_ligand(path.parent / "ligand.sdf").atoms[0].xyz, (.4, 3.2, 0.))

    def test_selected_record_is_the_only_ligand_uploaded(self):
        self.poses()
        root = self.root / "upload"
        code, output, error = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
            "--out", str(root), "--dry-run", "--ligand-pose", "2", "--hydrogens", "preserve", "--json"])
        self.assertEqual(code, 0, error)
        web, api = Mock(), Mock()
        api.submit.return_value = "456"
        def upload(pdb, destination):
            atoms, _ = _pdb(pdb)
            self.assertEqual(next(a.xyz for a in atoms if a.line.startswith("HETATM")), (.4, 3.2, 0.))
            Path(destination).mkdir()
            atomic_write(Path(destination) / "upload-response.html", web_fixtures.UPLOAD)
        def submit(source, destination, fields, uploads=None):
            if uploads:
                sdf = Path(uploads["sdf_cgenff[LIG]"])
                self.assertEqual(sdf.read_text().count("$$$$"), 1)
                self.assertEqual(read_ligand(sdf).name, "pose-2")
            atomic_write(destination, web_fixtures.SELECTION if Path(destination).name == "selection.html" else web_fixtures.READY)
        web.upload_structure.side_effect = upload
        web.submit_snapshot.side_effect = submit
        full_build.advance(root / ".charmm-gui", web, api)
        web.upload_structure.assert_called_once()
