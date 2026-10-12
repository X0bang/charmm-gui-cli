import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from charmm_gui_cli.auth import ToolError
from charmm_gui_cli.inputs import _pdb, _protein_pdb_lines
from charmm_gui_cli.full_build import _chains, _preparation_fields
from charmm_gui_cli.pdb_normalization import N_TERMINALS, normalize_lines
from charmm_gui_cli.web import snapshot_error
from tests.test_pdb_boundaries import atom


class RobustInputTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-robust-inputs-"))
        self.path = self.root / "input.pdb"

    def pdb(self, lines):
        self.path.write_text("\n".join(lines) + "\nEND\n")
        return self.path

    def test_duplicate_protein_serials_renumber_without_losing_atoms_or_coordinates(self):
        self.pdb([atom(1, "ALA", 1, (0, 0, 0)), atom(1, "ALA", 2, (2, 0, 0))])
        original = self.path.read_bytes()
        report = {}
        atoms, _ = _pdb(self.path, report=report)
        self.assertEqual([a.serial for a in atoms], [1, 2])
        self.assertEqual([a.xyz for a in atoms], [(0, 0, 0), (2, 0, 0)])
        self.assertEqual(len(_protein_pdb_lines(self.path, atoms)), 2)
        self.assertEqual(report["repairs"][0]["params"]["count"], 2)
        self.assertEqual(self.path.read_bytes(), original)

    def test_duplicate_conect_or_ligand_serials_are_rejected_with_identifiers(self):
        for records in ([atom(5, "ALA", 1, (0, 0, 0)), atom(5, "ALA", 2, (2, 0, 0)), "CONECT    5    6"],
                        [atom(5, "LIG", 1, (0, 0, 0), record="HETATM"), atom(5, "LIG", 1, (2, 0, 0), record="HETATM")]):
            with self.assertRaises(ToolError) as caught:
                _pdb(self.pdb(records))
            self.assertEqual(caught.exception.key, "input.duplicate_conect")
            self.assertIn("5", str(caught.exception))

    def test_protein_elements_and_single_atom_iron_are_inferred(self):
        lines = [atom(1, "GLY", 1, (0, 0, 0), element=""),
                 atom(2, "GLY", 1, (2, 0, 0), name="1H", element=""),
                 atom(3, "FE2", 2, (4, 0, 0), record="HETATM", name="FE", element="")]
        report = {}
        atoms, _ = _pdb(self.pdb(lines), report=report)
        self.assertEqual([a.element for a in atoms], ["C", "H", "Fe"])
        self.assertEqual([a.line[76:78].strip() for a in atoms], ["C", "H", "Fe"])
        self.assertEqual(report["repairs"][0]["params"]["count"], 3)

    def test_unknown_element_reports_line_and_name(self):
        with self.assertRaises(ToolError) as caught:
            _pdb(self.pdb([atom(1, "LIG", 1, (0, 0, 0), record="HETATM", element="", name="XX")]))
        self.assertEqual(caught.exception.params, {"line": 1, "atom": "XX"})

    def test_unique_hetero_serial_collision_can_be_avoided_without_conect(self):
        atoms, _ = _pdb(self.pdb([atom(1, "ALA", 1, (0, 0, 0)),
                                 atom(1, "FE", 2, (4, 0, 0), record="HETATM", element="Fe")]))
        self.assertEqual([a.serial for a in atoms], [2, 1])

    def test_all_requested_single_atom_ion_aliases(self):
        from charmm_gui_cli.pdb_normalization import _IONS
        for residue, element in _IONS.items():
            with self.subTest(residue=residue):
                atoms, _ = _pdb(self.pdb([atom(1, residue, 1, (0, 0, 0), record="HETATM", name=residue, element="")]))
                self.assertEqual(atoms[0].element, element)

    def test_blank_chain_segid_mapping_and_conflicts(self):
        self.pdb([atom(1, "GLY", 1, (0, 0, 0), chain=" ", segment="PROA"),
                  atom(2, "PRO", 1, (2, 0, 0), chain=" ", segment="SEGA")])
        report = {}
        atoms, _ = _pdb(self.path, report=report)
        self.assertEqual([a.line[21] for a in atoms], ["A", "B"])
        self.assertEqual(len(report["repairs"]), 2)
        self.assertEqual([entry["suggested_n_terminal"] for entry in report["terminal_predictions"]], ["GLYP", "PROP"])

    def test_blank_chain_without_segid_defaults_to_a(self):
        lines = normalize_lines(self.pdb([atom(1, "ALA", 1, (0, 0, 0), chain=" ")]))
        self.assertEqual(lines[0][21], "A")

    def test_terminal_patch_fallback_and_regular_nter(self):
        for options, expected in ((["GLYP", "ACE", "NONE"], "GLYP"), (["PROP", "ACE"], "PROP"), (["NTER", "GLYP"], "NTER")):
            path = self.root / "selection.html"
            path.write_text('<form><input name="jobid" value="123"><input type="file" name="sdf_cgenff[LIG]">'
                            '<select name="terminal[PROA][first]">' +
                            "".join(f'<option value="{option}">{option}</option>' for option in options) + '</select></form>')
            records = []
            with contextlib.redirect_stderr(io.StringIO()):
                result = _preparation_fields(path, {"ligand_resname": "LIG", "preparation": {"n_terminal": "NTER"}}, adjustments=records)
            self.assertEqual(result["terminal[PROA][first]"], expected)
            self.assertEqual(len(records), 0 if expected == "NTER" else 1)
            self.assertIn(expected, N_TERMINALS)

    def test_terminal_error_includes_chain_end_request_and_options(self):
        path = self.root / "selection.html"
        path.write_text('<form><input name="jobid" value="123"><input type="file" name="sdf_cgenff[LIG]">'
                        '<select name="terminal[PROA][first]"><option value="ACE">ACE</option><option value="NONE">NONE</option></select></form>')
        with self.assertRaises(ToolError) as caught:
            _preparation_fields(path, {"ligand_resname": "LIG", "preparation": {"n_terminal": "NTER"}})
        for text in ("PROA", "N", "NTER", "ACE", "NONE"):
            self.assertIn(text, str(caught.exception))

    def test_server_diagnostic_is_exposed_without_scripts_or_control_secrets(self):
        path = self.root / "response.html"
        path.write_text('<script>SCRIPT_SECRET</script><style>STYLE_SECRET</style>'
                        '<input name="csrf_token" value="FORM_SECRET"><input name="jobid" value="123">'
                        "<div id='error_msg'>Error parsing ATOM, expected chain ID at column 22 FORM_SECRET password=PASSWORD_SECRET</div>")
        with self.assertRaises(ToolError) as caught:
            _chains(path)
        message = str(caught.exception)
        self.assertIn("Error parsing ATOM", message)
        self.assertIn(str(path), message)
        self.assertIn("jobid=123", message)
        for secret in ("SCRIPT_SECRET", "STYLE_SECRET", "FORM_SECRET", "PASSWORD_SECRET"):
            self.assertNotIn(secret, message)

    def test_server_diagnostic_is_bounded(self):
        path = self.root / "error.html"
        path.write_text('<div class="error">Error ' + 'x' * 900 + '</div>')
        self.assertEqual(len(snapshot_error(path, "missing").params["diagnostic"]), 300)
