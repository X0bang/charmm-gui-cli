"""Public CLI chain/charge review and explicit unsupported-complex decisions."""

import json
import unittest

import yaml

from charmm_gui_cli.chem.inputs import Atom, _pdb, _sdf
from charmm_gui_cli.chem.precheck import inspect_protein
from charmm_gui_cli.chem.structure_inventory import inspect_chains
from charmm_gui_cli.store.paths import read_value
import test_input_policy as fixtures


def linked(left, right):
    # wwPDB LINK columns, including separate altloc and insertion-code fields.
    def endpoint(value):
        name, residue, chain, number, insertion = value
        return f"{name:>4} {residue:>3} {chain}{number:4d}{insertion}"
    return "LINK        " + endpoint(left) + "               " + endpoint(right)


class StructureInventoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InputPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.protein, self.ligand = self.fixture.root, self.fixture.protein, self.fixture.ligand

    def add_chain(self, chain="B", segment="", *, resid=10):
        lines = []
        for index, name in enumerate(("N", "CA", "C", "O", "CB"), 20):
            line = fixtures.atom_line(index, name, chain=chain, resid=resid, xyz=(10. + index * .1, 0., 0.))
            lines.append(line.ljust(80)[:72] + f"{segment:<4}" + line.ljust(80)[76:])
        self.fixture.lines.extend(["TER", *lines])
        self.fixture.write()

    def test_bilingual_chain_summary_retains_receptor_and_declared_nanobody_without_inference(self):
        self.add_chain()
        header = ["COMPND    MOL_ID: 1;", "COMPND   2 MOLECULE: RECEPTOR WITH BRIL INSERT;",
                  "COMPND   3 CHAIN: A;", "COMPND   4 MOL_ID: 2;", "COMPND   5 MOLECULE: NANOBODY;",
                  "COMPND   6 CHAIN: B;"]
        self.protein.write_text("\n".join(header + self.fixture.lines) + "\nEND\n")
        original = self.protein.read_bytes()
        for language, expected in (("zh", "链组成"), ("en", "Chain inventory")):
            code, report, output, _ = self.fixture.check(lang=language, tty=True)
            self.assertEqual(code, 0, report.get("error"))
            self.assertIn(expected, output)
            self.assertIn("RECEPTOR WITH BRIL INSERT", output)
            self.assertIn("NANOBODY", output)
            inventory = report["protein_inventory"]["structure_inventory"]
            self.assertEqual([(c["chain"], c["residue_count"], c["kind_counts"]) for c in inventory["chains"]],
                             [("A", 1, {"protein": 1}), ("B", 1, {"protein": 1})])
            self.assertFalse(inventory["sequence_assignment_performed"])
            self.assertFalse(inventory["domain_identity_verified"])
            atoms, _ = _pdb(report["prepared_files"]["protein.pdb"])
            self.assertEqual(len(atoms), 10)
            self.assertEqual({a.line[21] for a in atoms}, {"A", "B"})
            manifest = read_value(report["prepared_files"]["input-manifest.json"])
            self.assertEqual(manifest["input_normalization"]["structure_inventory"], inventory)
            self.assertEqual(self.protein.read_bytes(), original)

    def test_ter_segment_and_insertion_identities_are_not_collapsed(self):
        self.fixture.lines = [fixtures.atom_line(1, "CA", resid=59, insertion="A", chain="A")]
        self.add_chain(chain="A", segment="PR02", resid=59)
        inventory = inspect_chains(self.protein.read_text().splitlines())
        self.assertEqual(len(inventory["chains"]), 2)
        first, second = inventory["chains"]
        self.assertEqual((first["polymer_block"], first["segment"], first["residues"][0]["insertion_code"]), (0, "", "A"))
        self.assertEqual((second["polymer_block"], second["segment"], second["residue_count"]), (1, "PR02", 1))

    def test_altloc_records_count_as_one_residue_without_deleting_diagnostic_atoms(self):
        lines = [fixtures.atom_line(1, "CA", altloc="A"), fixtures.atom_line(2, "CA", altloc="B")]
        inventory = inspect_chains(lines)
        entry = inventory["chains"][0]
        self.assertEqual(entry["residue_count"], 1)
        self.assertEqual(entry["coordinate_records"], 2)
        self.assertEqual(entry["residues"][0]["unique_atom_names"], 1)
        self.assertFalse(inventory["coordinates_modified"])

    def test_chain_ids_are_kept_separate_across_models_and_selected_model_is_authoritative(self):
        other = [line[:21] + "B" + line[22:] for line in self.fixture.lines]
        self.protein.write_text("MODEL        1\n" + "\n".join(self.fixture.lines) + "\nENDMDL\nMODEL        2\n" +
                                "\n".join(other) + "\nENDMDL\n")
        code, report, _, _ = self.fixture.check("--model", "2")
        self.assertEqual(code, 0, report.get("error"))
        original = report["original_protein_inventory"]["structure_inventory"]["chains"]
        self.assertEqual([(c["model"], c["chain"]) for c in original], [(1, "A"), (2, "B")])
        self.assertEqual([c["chain"] for c in report["protein_inventory"]["structure_inventory"]["chains"]], ["B"])

    def test_unannotated_chains_have_no_invented_domain_or_antibody_assignment(self):
        code, report, output, _ = self.fixture.check(tty=True)
        self.assertEqual(code, 0, report.get("error"))
        inventory = report["protein_inventory"]["structure_inventory"]
        self.assertEqual(inventory["chains"][0]["declared_molecules"], [])
        self.assertNotIn("COMPND declaration", output)
        self.assertNotIn("nanobody", output.lower())

    def test_compnd_multiple_chains_and_fragment_text_are_declarations_only(self):
        lines = ["COMPND    MOL_ID: 1; MOLECULE: RECEPTOR;", "COMPND   2 CHAIN: A, B; FRAGMENT: T4 LYSOZYME INSERT;"]
        lines.extend([fixtures.atom_line(1, "CA", chain="A"), "TER", fixtures.atom_line(2, "CA", chain="B")])
        inventory = inspect_chains(lines)
        for chain in inventory["chains"]:
            self.assertEqual(chain["declared_molecules"][0]["molecules"], ["RECEPTOR"])
            self.assertEqual(chain["declared_molecules"][0]["fragments"], ["T4 LYSOZYME INSERT"])
        self.assertFalse(inventory["domain_identity_verified"])

    def test_oversized_header_is_bounded_and_marked_incomplete(self):
        lines = ["COMPND    " + "X" * 70] * 1900 + self.fixture.lines
        inventory = inspect_chains(lines)
        self.assertTrue(inventory["header_truncated"])
        self.assertEqual(inventory["chains"][0]["declared_molecules"], [])

    def test_dna_and_rna_are_identified_and_blocked_without_peptide_terminal_or_fake_gaps(self):
        for name, record in (("DA", "ATOM"), ("U", "ATOM"), ("DA", "HETATM")):
            with self.subTest(name=name, record=record):
                self.fixture.lines = self.fixture.lines[:5] + ["TER",
                    fixtures.atom_line(900, "P", name, chain="D", resid=501, xyz=(10., 0., 0.), element="P", record=record),
                    fixtures.atom_line(901, "C1'", name, chain="D", resid=501, xyz=(11., 0., 0.), element="C", record=record)]
                self.fixture.write()
                original = self.protein.read_bytes()
                for language, expected in (("zh", "核酸"), ("en", "nucleic-acid")):
                    code, report, output, _ = self.fixture.check(tty=True, lang=language)
                    self.assertEqual(code, 2)
                    self.assertEqual(report["error"]["message_key"], "input.nucleic_chains")
                    self.assertIn(expected, output)
                    self.assertIn(name + " D 501", output)
                    self.assertIn(str(self.protein), report["error"]["next_step"])
                    self.assertFalse(report["server_submitted"])
                    self.assertEqual(report["protein_inventory"]["protein_residues_all_models"], 1)
                    self.assertEqual(report["protein_inventory"]["numbering_gaps"], [])
                    self.assertNotIn("D", [item["chain"] for item in report["terminal_predictions"]])
                    chains = report["protein_inventory"]["structure_inventory"]["chains"]
                    self.assertEqual(chains[-1]["kind_counts"], {"nucleic_acid": 1})
                self.assertEqual(self.protein.read_bytes(), original)

    def glycan(self):
        self.fixture.lines.extend([fixtures.atom_line(900, "C1", "NAG", chain="G", resid=501,
                                  xyz=(12., 0., 0.), record="HETATM"),
                                  fixtures.atom_line(901, "C1", "MAN", chain="G", resid=502,
                                  xyz=(14., 0., 0.), record="HETATM"),
                                  linked(("ND2", "ASN", "A", 1, " "), ("C1", "NAG", "G", 501, " ")),
                                  linked(("O4", "NAG", "G", 501, " "), ("C1", "MAN", "G", 502, " "))])
        self.fixture.write()

    def test_glycan_links_are_reported_bilingually_before_removal_advice(self):
        self.glycan()
        original = self.protein.read_bytes()
        for language, expected in (("zh", "糖连接候选"), ("en", "Possible glycan linkage")):
            code, report, output, _ = self.fixture.check(tty=True, lang=language)
            self.assertEqual(code, 2)
            self.assertEqual(report["error"]["message_key"], "input.linkage")
            self.assertIn(expected, output)
            self.assertIn("ASN A 1 ND2", output)
            self.assertIn("NAG G 501 C1", output)
            self.assertIn("MAN G 502 C1", output)
            self.assertNotIn("--drop-hetero", report["error"]["next_step"])
            annotations = report["protein_inventory"]["structure_inventory"]["link_annotations"]
            self.assertEqual(len(annotations), 2)
            self.assertTrue(all(item["glycan_name_hint"] and not item["connectivity_verified"] for item in annotations))
        self.assertEqual(self.protein.read_bytes(), original)

    def test_explicit_glycan_removal_does_not_erase_linkage_or_pass_noncovalent_upload(self):
        self.glycan()
        code, report, _, _ = self.fixture.check("--drop-hetero", "NAG,MAN")
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["message_key"], "input.linkage")
        self.assertTrue(any(d["key"] == "input.glycan_link" for d in report["error"]["details"]))
        self.assertFalse(report["server_submitted"])

    def test_atom_glycan_is_not_counted_as_protein_or_a_numbering_gap(self):
        self.fixture.lines.append(fixtures.atom_line(900, "C1", "NAG", resid=501, record="ATOM", xyz=(12., 0., 0.)))
        self.fixture.write()
        inventory = inspect_protein(self.protein)
        self.assertEqual(inventory["protein_residues_all_models"], 1)
        self.assertEqual(inventory["numbering_gaps"], [])
        self.assertEqual(inventory["structure_inventory"]["chains"][0]["kind_counts"], {"protein": 1, "glycan": 1})
        code, report, _, _ = self.fixture.check()
        self.assertEqual(code, 2)
        self.assertFalse(report["server_submitted"])

    def test_same_named_bound_copies_are_listed_without_choosing_or_combining_them(self):
        self.fixture.lines.extend(["TER", fixtures.atom_line(900, "C1", "LIG", chain="L", resid=501,
            xyz=(0., 3.2, 0.), record="HETATM"), fixtures.atom_line(901, "C1", "LIG", chain="M", resid=502,
            xyz=(0., 5.2, 0.), record="HETATM")])
        self.fixture.write()
        original = self.protein.read_bytes()
        for language, expected in (("zh", "2 个残基"), ("en", "2 residue/TER-block instances")):
            out = self.root / ("copies-" + language)
            code, output, _ = self.fixture.call(["check", "--complex", str(self.protein), "--ligand", str(self.ligand),
                                                "--out", str(out), "--no-preview"], tty=True, lang=language)
            report = read_value(out / "check.json")
            self.assertEqual(code, 2)
            self.assertEqual(report["error"]["message_key"], "input.ligand_copies")
            self.assertIn(expected, output)
            self.assertIn("LIG L 501", output)
            self.assertIn("LIG M 502", output)
            self.assertFalse(report["server_submitted"])
            self.assertIn(str(self.protein), report["error"]["next_step"])
        self.assertEqual(self.protein.read_bytes(), original)

    def test_repeated_identity_across_ter_blocks_is_not_one_ligand_instance(self):
        from charmm_gui_cli.chem.input_sources import _one_ligand
        from charmm_gui_cli.store.auth import ToolError
        self.protein.write_text("\n".join([fixtures.atom_line(900, "C1", "LIG", chain="L", record="HETATM"),
                                            "TER", fixtures.atom_line(901, "O1", "LIG", chain="L", record="HETATM")]) + "\n")
        atoms, _ = _pdb(self.protein)
        with self.assertRaises(ToolError) as error:
            _one_ligand(atoms, resname="LIG")
        self.assertEqual(error.exception.key, "input.ligand_copies")
        self.assertEqual(error.exception.params["count"], 2)

    def test_charge_review_distinguishes_carboxylic_acid_and_anion_without_picking_ph(self):
        for charge in (0, -1):
            self.ligand.write_text(_sdf([Atom(1, "C1", "C", (0., 3.2, 0.)), Atom(2, "O1", "O", (1.2, 3.2, 0.)),
                Atom(3, "O2", "O", (-1.2, 3.2, 0.), charge=charge)], {(0, 1): 2, (0, 2): 1}, "LIG"))
            original = self.ligand.read_bytes()
            for policy in ("preserve", "add_missing"):
                for language, expected in (("zh", "请核对配体形式电荷"), ("en", "Review ligand formal charges")):
                    code, report, output, _ = self.fixture.check("--hydrogens", policy, tty=True, lang=language)
                    self.assertEqual(code, 0, report.get("error"))
                    self.assertEqual(report["chemistry"][0]["net_formal_charge"], charge)
                    self.assertIn(expected, output)
                    self.assertFalse(report["scientific_correctness_verified"])
                    self.assertEqual(self.ligand.read_bytes(), original)

    def test_build_dry_run_and_json_check_retain_inventory_and_old_public_fields(self):
        self.add_chain()
        code, output, errors = self.fixture.call(["build", "--protein", str(self.protein), "--ligand", str(self.ligand),
            "--dry-run", "--out", str(self.root / "dry-build"), "--no-preview", "--json"])
        self.assertEqual(code, 0, errors)
        value = json.loads(output)
        self.assertEqual(value["state"], "inputs_ready")
        self.assertFalse(value["server_submitted"])
        self.assertEqual([c["chain"] for c in value["structure_inventory"]["chains"]], ["A", "B"])
        code, report, output, _ = self.fixture.check()
        self.assertEqual(code, 0, report.get("error"))
        summary = json.loads(output)
        for key in ("passed", "status", "directory", "check_file", "protein", "chemistry", "review_required", "server_submitted", "task_registered"):
            self.assertIn(key, summary)
        self.assertEqual(summary["structure_inventory"], report["protein_inventory"]["structure_inventory"])
        self.assertEqual(output, json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

    def test_yaml_batch_and_sweep_keep_the_same_multichain_inventory_and_upload_bytes(self):
        self.add_chain()
        inputs = {"protein": str(self.protein), "ligand": str(self.ligand), "hydrogens": "preserve"}
        config = self.root / "build.yaml"
        config.write_text(yaml.safe_dump({"version": 2, "protein": str(self.protein), "ligand": str(self.ligand),
            "ligand_hydrogens": "preserve", "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20,
            "water_padding_A": 22.5}, "ions": {"type": "KCl", "concentration_M": .15}}))
        out = self.root / "yaml-build"
        code, output, error = self.fixture.call(["build", str(config), "--out", str(out), "--dry-run", "--json"])
        self.assertEqual(code, 0, error + output)
        expected = read_value(out / "inputs/input-manifest.json")["input_normalization"]["structure_inventory"]
        for command, data in (("batch", {"version": 1, "defaults": inputs, "jobs": [{"name": "a"}, {"name": "b"}]}),
                              ("sweep", {"version": 1, "inputs": inputs, "axes": {"salt_concentration": [.1, .2]}})):
            config = self.root / (command + ".yaml")
            config.write_text(yaml.safe_dump(data))
            directory = self.root / command
            code, output, error = self.fixture.call([command, str(config), "--out", str(directory), "--dry-run", "--json"])
            self.assertEqual(code, 0, error + output)
            children = directory / "batch" if command == "sweep" else directory
            manifests = list(children.glob("jobs/*/inputs/input-manifest.json"))
            self.assertEqual(len(manifests), 2)
            for path in manifests:
                self.assertEqual(read_value(path)["input_normalization"]["structure_inventory"], expected)
                self.assertEqual((path.parent / "protein.pdb").read_bytes(), (out / "inputs/protein.pdb").read_bytes())

    def test_unsupported_glycan_links_cannot_pass_component_preparation_or_build(self):
        self.glycan()
        config = self.root / "glycans.yaml"
        config.write_text(yaml.safe_dump({"version": 1, "water_policy": "reject", "ion_policy": "reject", "components": [
            {"resname": "NAG", "chain": "G", "resid": 501, "sdf": str(self.ligand)},
            {"resname": "MAN", "chain": "G", "resid": 502, "sdf": str(self.ligand)}]}))
        for command in ("check", "build"):
            out = self.root / (command + "-glycan")
            args = [command, "--complex", str(self.protein), "--components", str(config), "--out", str(out), "--json"]
            args.extend(["--dry-run"] if command == "build" else ["--no-preview"])
            code, output, errors = self.fixture.call(args)
            self.assertEqual(code, 2)
            if command == "check":
                report = read_value(out / "check.json")
                self.assertEqual(report["error"]["message_key"], "input.linkage")
                self.assertIn("--components", report["error"]["next_step"])
                self.assertIn(str(config), report["error"]["next_step"])
            else:
                self.assertIn("LINK", errors)
                self.assertIn("Possible glycan linkage", errors)
                self.assertIn(str(config), errors)
                self.assertFalse((out / ".charmm-gui/full-run.json").exists())

    def test_summary_caps_many_chains_without_changing_full_json_inventory(self):
        from charmm_gui_cli.presentation.terminal_output import _chain_lines
        lines = []
        for index, chain in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            lines.append(fixtures.atom_line(index + 1, "CA", chain=chain))
        inventory = inspect_chains(lines)
        self.assertEqual(len(inventory["chains"]), 26)
        rendered = _chain_lines(inventory)
        self.assertEqual(len(rendered), 21)
        self.assertIn("6 chain/segment entries", rendered[-1])
        self.assertFalse(any("1 residues" in line for line in rendered))

    def test_component_selectors_cannot_fuse_same_identity_across_ter_blocks(self):
        from charmm_gui_cli.chem.multicomponent import prepare_multicomponent
        from charmm_gui_cli.store.auth import ToolError
        self.fixture.lines.extend(["TER", fixtures.atom_line(900, "C1", "LIG", chain="L", resid=501,
            xyz=(0., 3.2, 0.), record="HETATM"), "TER", fixtures.atom_line(901, "O1", "LIG", chain="L", resid=501,
            xyz=(1.2, 3.2, 0.), record="HETATM", element="O")])
        self.fixture.write()
        components = [{"resname": "LIG", "chain": "L", "resid": 501, "sdf": str(self.ligand)}]
        with self.assertRaises(ToolError) as error:
            prepare_multicomponent(complex_pdb=self.protein, components=components, out=self.root / "ambiguous-components",
                                   water_policy="reject", ion_policy="reject")
        self.assertEqual(error.exception.key, "input.component_ter_ambiguous")
        self.assertEqual(error.exception.params["count"], 2)
        config = self.root / "ambiguous-components.yaml"
        config.write_text(yaml.safe_dump({"version": 1, "components": components, "water_policy": "reject", "ion_policy": "reject"}))
        for command in ("check", "build"):
            out = self.root / ("ter-" + command)
            args = [command, "--complex", str(self.protein), "--components", str(config), "--out", str(out), "--json"]
            args.extend(["--dry-run"] if command == "build" else ["--no-preview"])
            code, output, errors = self.fixture.call(args)
            self.assertEqual(code, 2)
            if command == "check":
                report = read_value(out / "check.json")
                self.assertEqual(report["error"]["message_key"], "input.component_ter_ambiguous")
                self.assertIn(str(config), report["error"]["next_step"])
            else:
                self.assertIn("TER blocks", errors)
                self.assertIn(str(config), errors)


if __name__ == "__main__":
    unittest.main()
