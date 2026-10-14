"""Polymer boundaries must survive local preparations and same-chain ligands."""

import json
from pathlib import Path
import tempfile
import unittest

from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.chem.inputs import Atom, _pdb, _protein_pdb_lines, _sdf, prepare_inputs, split_complex
from charmm_gui_cli.chem.input_sources import prepare_sources
from charmm_gui_cli.chem.multicomponent import prepare_build_components
from charmm_gui_cli.remote.web import assert_nonpolymer_parameterization, soup_for
from charmm_gui_cli.pipeline.full_build import _preparation_fields


def atom(serial, resname, resid, xyz, *, chain="A", segment="", record="ATOM", name="CA", element="C"):
    return (f"{record:<6}{serial:5d} {name:>4} {resname:>3} {chain}{resid:4d}    "
            + "".join(f"{v:8.3f}" for v in xyz) + f"  1.00  0.00      {segment:<4}{element:>2}  ")


def ssbond():
    line = list(" " * 80)
    for start, text in ((0, "SSBOND"), (7, "  1"), (11, "CYS"), (15, "A"), (17, "   1"),
                        (25, "CYS"), (29, "A"), (31, "   2")):
        line[start:start + len(text)] = text
    return "".join(line)


class PdbBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-pdb-boundaries-")).resolve()
        self.source = self.root / "source.pdb"
        self.protein = [atom(1, "CYS", 1, (0, 0, 0)), atom(2, "CYS", 2, (2, 0, 0)),
                        atom(3, "ALA", 3, (0, 2, 0))]
        self.sdf = self.root / "ligand.sdf"
        self.sdf.write_text(_sdf([Atom(1, "C1", "C", (4.4, 0, 0)), Atom(2, "O1", "O", (5.6, 0, 0))], {(0, 1): 2}, "LIG"))

    def test_existing_same_chain_ter_and_chain_segment_changes_are_retained(self):
        lines = [self.protein[0], "TER", self.protein[1],
                 atom(3, "ALA", 3, (0, 2, 0), chain="B"),
                 atom(4, "ALA", 4, (0, 4, 0), chain="B", segment="P2"), "TER", "TER"]
        self.source.write_text("\n".join(lines) + "\nEND\n")
        atoms, _ = _pdb(self.source)
        result = _protein_pdb_lines(self.source, atoms, {a.serial: 10 + a.serial for a in atoms})
        self.assertEqual([line[:6].strip() for line in result], ["ATOM", "TER", "ATOM", "TER", "ATOM", "TER", "ATOM", "TER"])
        self.assertEqual([int(line[6:11]) for line in result if line.startswith("ATOM")], [11, 12, 13, 14])
        self.assertEqual([line[11:] for line in result if line.startswith("ATOM")], [a.line[11:] for a in atoms])

    def test_numbering_gap_alone_does_not_create_a_polymer_break(self):
        self.source.write_text(self.protein[0] + "\n" + atom(2, "ALA", 99, (2, 0, 0)) + "\nEND\n")
        atoms, _ = _pdb(self.source)
        self.assertNotIn("TER", _protein_pdb_lines(self.source, atoms))

    def test_single_ligand_pipeline_preserves_ssbond_conect_and_exact_boundaries(self):
        original = ssbond() + "\n" + self.protein[0] + "\nTER\n" + "\n".join(self.protein[1:]) + "\nTER\nCONECT    1    2\nEND\n"
        self.source.write_text(original)
        prepared = prepare_inputs(self.source, self.sdf, self.root / "single", ligand_resname="LIG")
        text = Path(prepared["files"]["complex.pdb"]).read_text()
        self.assertIn(ssbond(), text)
        self.assertNotIn("TER\nTER", text)
        self.assertEqual(text.splitlines().count("TER"), 2)
        atoms, bonds = _pdb(prepared["files"]["complex.pdb"])
        self.assertEqual([a.serial for a in atoms], [1, 2, 3, 4, 5])
        self.assertEqual(bonds, {(1, 2): 1, (4, 5): 2})
        first_ligand = next(i for i, line in enumerate(text.splitlines()) if line.startswith("HETATM"))
        self.assertEqual(text.splitlines()[first_ligand - 1], "TER")
        self.assertNotEqual(atoms[-1].line[21], "A")
        self.assertEqual(self.source.read_text(), original)

    def test_split_and_bound_complex_sources_retain_polymer_metadata(self):
        ligand = [atom(10, "LIG", 500, (4, 0, 0), record="HETATM", name="C1"),
                  atom(11, "LIG", 500, (5.2, 0, 0), record="HETATM", name="O1", element="O")]
        self.source.write_text(ssbond() + "\n" + self.protein[0] + "\nTER\n" + "\n".join(self.protein[1:] + ["TER"] + ligand)
                               + "\nCONECT    1    2\nCONECT   10   11   11\nEND\n")
        split = split_complex(self.source, self.root / "split", ligand_resname="LIG", accept_conect_bond_orders=True)
        sources = prepare_sources(out=self.root / "sources", complex_pdb=self.source, ligand=self.sdf, ligand_resname="LIG")
        for path in (split["files"]["protein.pdb"], sources["protein"]):
            text = Path(path).read_text()
            self.assertIn(ssbond(), text)
            self.assertEqual(text.splitlines().count("TER"), 2)
            self.assertNotIn("TER\nTER", text)

    def test_same_chain_two_components_have_explicit_separators_without_atom_or_bond_changes(self):
        ligand = [atom(10, "DXC", 500, (4.4, 0, 0), record="HETATM", name="C1"),
                  atom(11, "DXC", 500, (5.6, 0, 0), record="HETATM", name="O1", element="O"),
                  atom(20, "LDA", 501, (0, 4.4, 0), record="HETATM", name="C1"),
                  atom(21, "LDA", 501, (0, 5.6, 0), record="HETATM", name="O1", element="O")]
        original = ssbond() + "\n" + self.protein[0] + "\nTER\n" + "\n".join(self.protein[1:] + ligand) + "\nCONECT    1    2\nEND\n"
        self.source.write_text(original)
        config = self.root / "components.yaml"
        config.write_text(json.dumps({"version": 1, "water_policy": "reject", "ion_policy": "reject", "components": [
            {"resname": "DXC", "chain": "A", "resid": 500, "sdf": str(self.sdf)},
            {"resname": "LDA", "chain": "A", "resid": 501, "sdf": str(self.sdf)}]}))
        result = prepare_build_components(components_config=config, complex_pdb=self.source,
                                          out=self.root / "multi", hydrogens="preserve")
        text = Path(result["files"]["complex.pdb"]).read_text()
        lines = text.splitlines()
        self.assertIn(ssbond(), text)
        self.assertEqual(lines.count("TER"), 3)
        self.assertNotIn("TER\nTER", text)
        atoms, bonds = _pdb(result["files"]["complex.pdb"])
        source_atoms, _ = _pdb(self.source)
        self.assertEqual([a.xyz for a in atoms], [a.xyz for a in source_atoms])
        self.assertEqual([a.serial for a in atoms], list(range(1, 8)))
        self.assertEqual(bonds, {(1, 2): 1, (4, 5): 2, (6, 7): 2})
        self.assertEqual([a.line[21:27] for a in atoms], [a.line[21:27] for a in source_atoms])
        for name in ("DXC", "LDA"):
            first = next(i for i, line in enumerate(lines) if line.startswith("HETATM") and line[17:20] == name)
            self.assertEqual(lines[first - 1], "TER")
        self.assertEqual(self.source.read_text(), original)

    def test_precise_engineered_residue_diagnostic_and_no_normal_form_false_positive(self):
        engineered = '<div id="prot_rename_option"><input name="rename[DXC]" value="rename"></div>'
        html = '<form><input name="jobid" value="123">' + engineered + '</form>'
        path = self.root / "selection.html"
        path.write_text(html)
        with self.assertRaises(ToolError) as caught:
            _preparation_fields(path, {"ligand_resname": "DXC"}, [{"resname": "DXC"}])
        self.assertEqual(caught.exception.category, "component_classification")
        self.assertIn("TER", caught.exception.next_step)
        self.assertIn("当前源无法", caught.exception.next_step)
        assert_nonpolymer_parameterization(soup_for(html), ["LIG"])
        with_sdf = html.replace("</form>", '<input name="sdf_cgenff[DXC]" type="file"></form>')
        assert_nonpolymer_parameterization(soup_for(with_sdf), ["DXC"])
        disabled = with_sdf.replace('type="file"', 'type="file" disabled')
        with self.assertRaises(ToolError):
            assert_nonpolymer_parameterization(soup_for(disabled), ["DXC"])


if __name__ == "__main__":
    unittest.main()
