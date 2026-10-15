"""Independent terminal-site facts, synthetic actual files and mutation gates.

Synthetic PDB coordinates are not a physically accepted remote model. No HTTP.
"""

from copy import deepcopy
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.chem.fragment_preparation import prepare_fragmented_lines
from charmm_gui_cli.chem.fragment_proof import verify_fragment_artifact, verify_fragment_archive
from charmm_gui_cli.chem.terminal_topology import read_terminal_topology, terminal_profile
from charmm_gui_cli.locales.i18n import set_language
from charmm_gui_cli.pipeline.build_config import validate_preparation
from charmm_gui_cli.pipeline.full_build import _preparation_fields
from charmm_gui_cli.store.auth import ToolError
from tests.test_fragment_preparation import residue, ssbond
from tests.test_pdb_boundaries import atom


def sites(text):
    return {name: (kind, float(charge)) for name, kind, charge in (part.split() for part in text.split(";"))}


# Test-owned atom/type/charge/bond facts, independently transcribed from
# CHARMM36 terminal definitions; never generated from the verifier's profile.
BASE = {
    "ALA": (sites("N NH1 -.47;HN H .31;CA CT1 .07;HA HB1 .09;CB CT3 -.27;HB1 HA3 .09;HB2 HA3 .09;HB3 HA3 .09;C C .51;O O -.51"),
            "N HN N CA CA HA CA CB CB HB1 CB HB2 CB HB3 CA C C O C +N"),
    "GLY": (sites("N NH1 -.47;HN H .31;CA CT2 -.02;HA1 HB2 .09;HA2 HB2 .09;C C .51;O O -.51"),
            "N HN N CA CA HA1 CA HA2 CA C C O C +N"),
    "PRO": (sites("N N -.29;CA CP1 .02;HA HB1 .09;CD CP3 0;HD1 HA2 .09;HD2 HA2 .09;CB CP2 -.18;HB1 HA2 .09;HB2 HA2 .09;CG CP2 -.18;HG1 HA2 .09;HG2 HA2 .09;C C .51;O O -.51"),
            "N CA N CD CA HA CA CB CB HB1 CB HB2 CB CG CG HG1 CG HG2 CG CD CD HD1 CD HD2 CA C C O C +N"),
    "CYS": (sites("N NH1 -.47;HN H .31;CA CT1 .07;HA HB1 .09;CB CT2 -.11;HB1 HA2 .09;HB2 HA2 .09;SG S -.23;HG HS .16;C C .51;O O -.51"),
            "N HN N CA CA HA CA CB CB HB1 CB HB2 CB SG SG HG CA C C O C +N"),
    "ILE": (sites("N NH1 -.47;HN H .31;CA CT1 .07;HA HB1 .09;CB CT1 -.09;HB HA1 .09;CG2 CT3 -.27;HG21 HA3 .09;HG22 HA3 .09;HG23 HA3 .09;CG1 CT2 -.18;HG11 HA2 .09;HG12 HA2 .09;CD CT3 -.27;HD1 HA3 .09;HD2 HA3 .09;HD3 HA3 .09;C C .51;O O -.51"),
            "N HN N CA CA HA CA CB CB HB CB CG1 CB CG2 CG2 HG21 CG2 HG22 CG2 HG23 CG1 HG11 CG1 HG12 CG1 CD CD HD1 CD HD2 CD HD3 CA C C O C +N"),
}
PATCHES = {
    "NTER": (sites("N NH3 -.30;HT1 HC .33;HT2 HC .33;HT3 HC .33;CA CT1 .21;HA HB1 .10"), ["HN"], "N HT1 N HT2 N HT3"),
    "GLYP": (sites("N NH3 -.30;HT1 HC .33;HT2 HC .33;HT3 HC .33;CA CT2 .13;HA1 HB2 .09;HA2 HB2 .09"), ["HN"], "N HT1 N HT2 N HT3"),
    "PROP": (sites("N NP -.07;HN1 HC .24;HN2 HC .24;CD CP3 .16;HD1 HA2 .09;HD2 HA2 .09;CA CP1 .16;HA HB1 .09"), [], "N HN1 N HN2"),
    "ACE": (sites("CAY CT3 -.27;HY1 HA3 .09;HY2 HA3 .09;HY3 HA3 .09;CY C .51;OY O -.51"), [], "CY CAY CY N CAY HY1 CAY HY2 CAY HY3 OY CY"),
    "ACP": (sites("CAY CT3 -.27;HY1 HA3 .09;HY2 HA3 .09;HY3 HA3 .09;CY C .51;OY O -.51"), [], "CY CAY CY N CAY HY1 CAY HY2 CAY HY3 OY CY"),
    "NNEU": (sites("N NH2 -.96;HT1 H .34;HT2 H .34;CA CT1 .19;HA HB1 .09"), ["HN"], "N HT1 N HT2"),
    "CTER": (sites("C CC .34;OT1 OC -.67;OT2 OC -.67"), ["O"], "C OT2 C OT1"),
    "CNEU": (sites("C CD .72;OT1 OB -.55;OT2 OH1 -.61;HT2B H .44"), ["O"], "C OT2 OT2 HT2B C OT1"),
    "CT1": (sites("N NH1 -.47;HN H .31;CA CT1 .17;HA HB1 .09;C CD .63;OT1 OB -.52;OT2 OS -.34;CT CT3 -.14;HT1 HA3 .09;HT2 HA3 .09;HT3 HA3 .09"), ["O"], "C OT2 OT2 CT CT HT1 CT HT2 CT HT3 C OT1"),
    "CT2": (sites("C CC .55;O O -.55;NT NH2 -.62;HT1 H .32;HT2 H .30"), [], "C NT NT HT1 NT HT2"),
    "CT3": (sites("C C .51;O O -.51;NT NH1 -.47;HNT H .31;CAT CT3 -.11;HT1 HA3 .09;HT2 HA3 .09;HT3 HA3 .09"), [], "C NT NT HNT NT CAT CAT HT1 CAT HT2 CAT HT3"),
}
MASSES = {"H": 1.008, "C": 12.011, "N": 14.007, "O": 15.9994, "S": 32.06}


def pairs(text):
    words = text.split()
    return {tuple(sorted(pair)) for pair in zip(words[::2], words[1::2])}


def topology_text():
    all_sites = [entry[0] for entry in [*BASE.values(), *PATCHES.values()]]
    types = {kind for group in all_sites for kind, _ in group.values()}
    lines = ["* Synthetic terminal fact fixture", "36 1"]
    lines += [f"MASS -1 {kind} {MASSES[kind[0]]}" for kind in sorted(types)]
    for name, (atoms, bonds) in BASE.items():
        lines += [f"RESI {name} 0", *[f"ATOM {n} {k} {q}" for n, (k, q) in atoms.items()], "BOND " + bonds]
    for name, (atoms, deleted, bonds) in PATCHES.items():
        lines += [f"PRES {name} 0", *[f"ATOM {n} {k} {q}" for n, (k, q) in atoms.items()],
                  *["DELETE ATOM " + name for name in deleted], "BOND " + bonds]
    return "\n".join(lines) + "\nEND\n"


class FragmentProofTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-fragment-proof-"))
        self.counter = 0
        self.topology = self.root / "protein.rtf"
        self.topology.write_text(topology_text())
        self.network = patch("requests.Session.request", side_effect=AssertionError("No HTTP"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(set_language)
        self.setup_input()

    def setup_input(self, first="ALA", *, n="NTER", c="CTER", linkage=None, single=False):
        rows = [(1, first, 0), (2, "ALA", 2.3), (3, first, 20), (4, "ALA", 22.3)]
        if single:
            rows = [(1, first, 0), (2, first, 20)]
        self.source = []
        for index, (rid, name, x) in enumerate(rows):
            self.source += residue(1 + index * 10, rid, x, name=name)
        if linkage == "conect":
            self.source += ["CONECT    6   26"]
        elif linkage == "ssbond":
            self.source.insert(0, ssbond(left=1, right=3))
        self.source += ["END"]
        self.prepared, self.plan = prepare_fragmented_lines(self.source, accept_breaks=True)
        source_path, prepared_path = self.root / "source.pdb", self.root / "prepared.pdb"
        source_path.write_text("\n".join(self.source) + "\n")
        prepared_path.write_text("\n".join(self.prepared) + "\n")
        self.preparation = validate_preparation({"n_terminal": n, "c_terminal": c})
        catalogue, controls = {}, '<input name="jobid" value="1234567890"><input type="file" name="sdf_cgenff[LIG]">'
        for f in self.plan["fragments"]:
            name = "PRO" + f["prepared_chain"]
            catalogue[name] = {str(r["resid"]) + r["icode"]: r["resname"] for r in f["source_residues"]}
            for end, values in (("first", ["NTER", "NNEU", "ACE", "ACP", "GLYP", "PROP", "NONE"]),
                                ("last", ["CTER", "CNEU", "CT1", "CT2", "CT3", "NONE"])):
                controls += '<select name="terminal[' + name + '][' + end + ']">' + ''.join(
                    '<option value="' + value + '">' + value + '</option>' for value in values) + '</select>'
        page = self.root / "selection.html"
        page.write_text('<script>var prot_chains=' + json.dumps(catalogue) + ';</script><form>' + controls + '</form>')
        self.receipt = {}
        _preparation_fields(page, {"ligand_resname": "LIG", "preparation": self.preparation}, fragment_plan=self.plan,
            fragment_source_pdb=source_path, fragment_protein_pdb=prepared_path, fragment_mapping=self.receipt)

    def model(self, *, mutate=None, cross=False):
        self.counter += 1
        directory = self.root / ("model-" + str(self.counter))
        directory.mkdir()
        atoms, edges, lookup = [], set(), {}
        for fragment, binding in zip(self.plan["fragments"], self.receipt["fragments"]):
            entries = fragment["source_residues"]
            for index, entry in enumerate(entries):
                values, base_bonds = BASE[entry["resname"]]
                values, local = dict(values), pairs(base_bonds)
                for end, match in (("n_terminal", index == 0), ("c_terminal", index == len(entries)-1)):
                    patch_name = binding["terminals"][end]["form_value"]
                    if not match or patch_name == "NONE":
                        continue
                    additions, removed, extra = PATCHES[patch_name]
                    for name in removed:
                        values.pop(name, None)
                        local = {edge for edge in local if name not in edge}
                    values.update(additions)
                    local |= pairs(extra)
                if cross and entry["resname"] == "CYS":
                    values["SG"] = ("SM", -.08)
                    values.pop("HG", None)
                    local = {edge for edge in local if "HG" not in edge}
                segment, rid = binding["server_chain"], str(entry["resid"]) + entry["icode"]
                for name, (kind, charge) in values.items():
                    serial = len(atoms) + 1
                    atoms.append({"serial": serial, "segment": segment, "resid": rid, "resname": entry["resname"],
                                  "name": name, "type": kind, "charge": charge, "mass": MASSES[kind[0]]})
                    lookup[segment, rid, name] = serial
                for a, b in local:
                    if not a.startswith(("+", "-")) and not b.startswith(("+", "-")):
                        edges.add(tuple(sorted((lookup[segment, rid, a], lookup[segment, rid, b]))))
            for left, right in zip(entries, entries[1:]):
                edges.add(tuple(sorted((lookup[segment, str(left["resid"]), "C"], lookup[segment, str(right["resid"]), "N"]))))
        if cross:
            edges.add(tuple(sorted((lookup["PROA", "1", "SG"], lookup["PROB", "3", "SG"]))))
        if mutate:
            mutate(atoms, edges, lookup)
        return self.write_model(directory, atoms, edges)

    def write_model(self, directory, atoms, edges):
        pdb, psf = directory / "model.pdb", directory / "model.psf"
        lines = []
        for item in atoms:
            line = atom(item["serial"], item["resname"], int(item["resid"]), (float(item["serial"]), 0., 0.),
                        name=item["name"], element=item["type"][0], chain=item["segment"][-1], segment=item["segment"])
            lines.append(line)
        pdb.write_text("\n".join(lines) + "\nEND\n")
        psf.write_text("PSF EXT\n\n" + str(len(atoms)) + " !NATOM\n" + "\n".join(
            '{serial} {segment} {resid} {resname} {name} {type} {charge:.6f} {mass:.6f} 0'.format(**row)
            for row in atoms) + "\n\n" + str(len(edges)) + " !NBOND\n" +
            " ".join(str(value) for edge in sorted(edges) for value in edge) + "\n\n0 !NTHETA\n")
        return pdb, psf

    def verify(self, files=None, **kwargs):
        pdb, psf = files or self.model()
        return verify_fragment_artifact(self.preparation, self.source, self.prepared, self.plan, self.receipt, pdb, psf, self.topology, **kwargs)

    def refused(self, action):
        with self.assertRaises(ToolError) as caught:
            action()
        self.assertEqual(caught.exception.category, "fragment_topology_unverified")
        self.assertTrue(caught.exception.next_step)
        return caught.exception

    def restage_source(self):
        self.prepared, self.plan = prepare_fragmented_lines(self.source, accept_breaks=True)
        source_path, prepared_path = self.root / 'source.pdb', self.root / 'prepared.pdb'
        source_path.write_text('\n'.join(self.source) + '\n')
        prepared_path.write_text('\n'.join(self.prepared) + '\n')
        self.receipt = {}
        _preparation_fields(self.root / 'selection.html', {'ligand_resname': 'LIG', 'preparation': self.preparation},
            fragment_plan=self.plan, fragment_source_pdb=source_path, fragment_protein_pdb=prepared_path, fragment_mapping=self.receipt)

    def test_ile_cd1_mapping_uses_actual_rtf_types_and_complete_methyl_branch(self):
        self.setup_input(first='ILE')
        result = self.verify()
        renamed = [row for row in result['retained_atom_name_changes'] if row['from'] == 'CD1']
        self.assertEqual(len(renamed), 2)
        self.assertTrue(all(row['to'] == 'CD' and row['basis'] == 'same_source_CHARMM_protein_RTF' for row in renamed))

    def test_ile_alias_refuses_wrong_type_charge_mass_element_and_branch_connectivity(self):
        self.setup_input(first='ILE')
        for field, value in (('type', 'CT2'), ('charge', -.18), ('mass', 1.008)):
            def mutate(atoms, edges, lookup):
                atoms[lookup['PROA', '1', 'CD']-1][field] = value
            self.refused(lambda: self.verify(self.model(mutate=mutate)))
        def branch(atoms, edges, lookup):
            a, b, c = (lookup['PROA', '1', name] for name in ('CD', 'CG1', 'CB'))
            edges.remove(tuple(sorted((a, b))))
            edges.add(tuple(sorted((a, c))))
        self.refused(lambda: self.verify(self.model(mutate=branch)))
        pdb, psf = self.model()
        pdb.write_text('\n'.join(line[:76] + ' N' + line[78:] if line.startswith('ATOM') and line[12:16].strip() == 'CD'
                                  else line for line in pdb.read_text().splitlines()) + '\n')
        self.refused(lambda: self.verify((pdb, psf)))

    def test_ile_source_name_collision_and_non_ile_missing_atoms_are_not_aliases(self):
        self.setup_input(first='ILE')
        self.source.insert(8, atom(90, 'ILE', 1, (0., -3., 0.), name='CD', element='C'))
        self.restage_source()
        self.refused(self.verify)
        self.setup_input()
        self.source.insert(5, atom(90, 'ALA', 1, (0., -3., 0.), name='CD1', element='C'))
        self.restage_source()
        self.refused(self.verify)

    def test_explicit_ct2_ct3_can_replace_only_the_terminal_extra_carboxyl_oxygen(self):
        for cap in ('CT2', 'CT3'):
            self.setup_input(c=cap)
            self.source.insert(-1, atom(90, 'ALA', 4, (23., 1.8, 0.), name='OXT', element='O'))
            self.restage_source()
            result = self.verify()
            self.assertEqual(result['requested_terminal_atom_deletions'], [{'source_serial': 90,
                'segment': 'PROB', 'resid': '4', 'resname': 'ALA', 'atom': 'OXT', 'charmm_atom': 'OT2', 'patch': cap,
                'basis': 'explicit_requested_amide_terminal_RTF_profile'}])

    def test_carboxyl_patch_retains_oxt_as_ot2_and_none_does_not_authorize_oxygen_loss(self):
        self.source.insert(-1, atom(90, 'ALA', 4, (23., 1.8, 0.), name='OXT', element='O'))
        self.restage_source()
        result = self.verify()
        self.assertNotIn('requested_terminal_atom_deletions', result)
        self.assertTrue(any(row['from'] == 'OXT' and row['to'] == 'OT2' for row in result['retained_atom_name_changes']))
        self.setup_input(c='NONE')
        self.source.insert(-1, atom(90, 'ALA', 4, (23., 1.8, 0.), name='OXT', element='O'))
        self.restage_source()
        self.refused(self.verify)

    def test_amide_request_does_not_excuse_internal_missing_atoms_or_drop_declared_cross_links(self):
        self.setup_input(c='CT2')
        self.source.insert(5, atom(90, 'ALA', 1, (0., 1.8, 0.), name='OXT', element='O'))
        self.restage_source()
        self.refused(self.verify)
        self.setup_input(c='CT2')
        self.source[-1:-1] = [atom(90, 'ALA', 4, (23., 1.8, 0.), name='OXT', element='O'), 'CONECT   90    5']
        self.restage_source()
        self.refused(self.verify)

    def test_two_source_names_cannot_be_folded_onto_one_retained_actual_atom(self):
        self.source.insert(-1, atom(90, 'ALA', 4, (23., .8, 0.), name='OT1', element='O'))
        self.restage_source()
        self.refused(self.verify)

    def test_actual_sites_connectivity_pass_without_asserting_origin_or_science(self):
        proof = self.verify()
        self.assertTrue(proof["passed"])
        self.assertEqual(proof["fragment_count"], 2)
        self.assertEqual(len(proof["terminals"]), 4)
        self.assertEqual(proof["peptide_bonds_checked"], 2)
        self.assertFalse(proof["server_origin_verified"])
        self.assertFalse(proof["scientific_correctness_verified"])

    def test_every_supported_n_and_c_terminal_on_compatible_residues(self):
        for n, first in (("NTER", "ALA"), ("NNEU", "ALA"), ("ACE", "ALA"), ("ACP", "PRO"),
                         ("GLYP", "GLY"), ("PROP", "PRO"), ("NONE", "ALA")):
            for c in ("CTER", "CNEU", "CT1", "CT2", "CT3", "NONE"):
                with self.subTest(n=n, c=c):
                    self.setup_input(first, n=n, c=c)
                    proof = self.verify()
                    self.assertEqual({end["patch"] for end in proof["terminals"]}, {n, c})

    def test_real_residue_fallback_form_values_drive_actual_topology_checks(self):
        for residue_name, patch_name in (("GLY", "GLYP"), ("PRO", "PROP")):
            self.setup_input(residue_name)
            page = self.root/"selection.html"
            page.write_text(page.read_text().replace('<option value="NTER">NTER</option>', ''))
            self.receipt = {}
            with redirect_stderr(io.StringIO()):
                _preparation_fields(page, {"ligand_resname":"LIG","preparation":self.preparation}, fragment_plan=self.plan,
                    fragment_source_pdb=self.root/"source.pdb", fragment_protein_pdb=self.root/"prepared.pdb", fragment_mapping=self.receipt)
            self.assertTrue(all(row["terminals"]["n_terminal"]["value"]=="NTER" and
                                row["terminals"]["n_terminal"]["form_value"]==patch_name for row in self.receipt["fragments"]))
            proof = self.verify(self.model())
            self.assertEqual({end["patch"] for end in proof["terminals"] if end["end"]=="n_terminal"}, {patch_name})

    def test_wrong_types_or_compensated_charges_cannot_pass_by_total_charge(self):
        for field, value in (("type", "NH1"), ("charge", -.20), ("mass", 12.011)):
            def mutate(atoms, edges, lookup):
                atoms[lookup["PROB", "3", "N"]-1][field] = value
                if field == "charge":
                    atoms[lookup["PROB", "3", "CA"]-1][field] -= .10
            self.refused(lambda: self.verify(self.model(mutate=mutate)))

    def test_hmr_mass_is_not_mistaken_for_native_charmm_terminal_proof(self):
        def mutate(atoms, edges, lookup):
            atoms[lookup["PROA", "1", "HT1"]-1]["mass"] = 3.024
        self.refused(lambda: self.verify(self.model(mutate=mutate)))

    def test_orphan_double_bonded_and_wrong_partner_terminal_hydrogens_refused(self):
        for kind in ("orphan", "double", "wrong"):
            def mutate(atoms, edges, lookup):
                h, n, ca = (lookup["PROA", "1", name] for name in ("HT1", "N", "CA"))
                if kind != "double":
                    edges.remove(tuple(sorted((h, n))))
                if kind != "orphan":
                    edges.add(tuple(sorted((h, ca))))
            self.refused(lambda: self.verify(self.model(mutate=mutate)))

    def test_missing_terminal_oxygen_or_old_carbonyl_name_refused(self):
        for name in ("XOT2", "O"):
            def mutate(atoms, edges, lookup):
                atoms[lookup["PROB", "4", "OT2" if name == "XOT2" else "OT1"]-1]["name"] = name
            self.refused(lambda: self.verify(self.model(mutate=mutate)))

    def test_extra_unrequested_cap_atom_refused(self):
        def mutate(atoms, edges, lookup):
            extra = dict(atoms[lookup["PROA", "1", "HT1"]-1], serial=len(atoms)+1, name="HN", type="H", charge=.31)
            atoms.append(extra)
            edges.add(tuple(sorted((extra["serial"], lookup["PROA", "1", "N"]))))
        self.refused(lambda: self.verify(self.model(mutate=mutate)))

    def test_missing_internal_peptide_and_new_gap_peptide_are_refused(self):
        for kind in ("missing", "cross"):
            def mutate(atoms, edges, lookup):
                if kind == "missing":
                    edges.remove(tuple(sorted((lookup["PROA", "1", "C"], lookup["PROA", "2", "N"]))))
                else:
                    edges.add(tuple(sorted((lookup["PROA", "2", "C"], lookup["PROB", "3", "N"]))))
            self.refused(lambda: self.verify(self.model(mutate=mutate)))

    def test_merged_segments_wrong_residue_or_unexpected_protein_copy_refused(self):
        for kind in ("merge", "residue", "copy"):
            def mutate(atoms, edges, lookup):
                if kind == "merge":
                    for row in atoms:
                        if row["segment"] == "PROB":
                            row["segment"] = "PROA"
                elif kind == "residue":
                    for row in atoms:
                        if row["segment"] == "PROB" and row["resid"] == "4":
                            row["resname"] = "GLY"
                else:
                    atoms.append(dict(atoms[lookup["PROA", "1", "CA"]-1], serial=len(atoms)+1, segment="PROC"))
            self.refused(lambda: self.verify(self.model(mutate=mutate)))

    def test_pdb_psf_identity_mismatch_or_malformed_bond_list_refused(self):
        pdb, psf = self.model()
        pdb.write_text(pdb.read_text().replace("PROA", "WRNG", 1))
        self.refused(lambda: self.verify((pdb, psf)))
        pdb, psf = self.model()
        psf.write_text(psf.read_text().replace("!NBOND", "!WRONG"))
        self.refused(lambda: self.verify((pdb, psf)))

    def test_edited_receipt_field_server_or_override_layer_refused(self):
        original = deepcopy(self.receipt)
        for kind in ("server", "patch", "origin", "plan", "confirmed"):
            self.receipt = deepcopy(original)
            if kind == "server": self.receipt["fragments"][1]["server_chain"] = "PROX"
            if kind == "patch": self.receipt["fragments"][1]["terminals"]["n_terminal"]["form_value"] = "ACE"
            if kind == "origin": self.receipt["fragments"][0]["terminals"]["c_terminal"]["origin"] = "fragment"
            if kind == "plan": self.receipt["plan_fingerprint"] = "0"*64
            if kind == "confirmed": self.receipt["submission_confirmed"] = True
            self.refused(lambda: self.verify(self.model()))

    def test_retained_conect_and_ssbond_disulfides_have_real_bond_and_state(self):
        for linkage in ("conect", "ssbond"):
            self.setup_input("CYS", linkage=linkage)
            proof = self.verify(self.model(cross=True))
            self.assertEqual(proof["retained_cross_fragment_bonds"], 1)
            self.assertEqual(proof["disulfide_bonds_checked"], 1)
            self.refused(lambda: self.verify(self.model(cross=False)))

    def test_disulfide_bond_without_correct_sulfur_state_is_refused(self):
        self.setup_input("CYS", linkage="conect")
        for field, value in (("type", "S"), ("charge", -.23), ("mass", 12.011)):
            def mutate(atoms, edges, lookup):
                atoms[lookup["PROA", "1", "SG"]-1][field] = value
            self.refused(lambda: self.verify(self.model(cross=True, mutate=mutate)))

    def test_explicit_cyx_state_pair_is_retained_without_conect_or_ssbond(self):
        self.setup_input("CYS")
        left = {"polymer_block": 0, "chain": "A", "segment": "", "resid": 1, "icode": ""}
        right = dict(left, resid=3)
        states = {"schema_version": 1, "pH_selected": False, "remote_verified": False,
                  "residues": [{**left, "source_resname": "CYX", "pdb_resname": "CYS", "state": "DISU",
                                "method": "disulfide", "hydrogen_renames": [], "remote_verified": False}],
                  "disulfides": [{"left": left, "right": right, "distance_A": 2.05,
                                  "basis": "explicit_CYX_unique_distance_pair", "remote_verified": False}]}
        self.assertEqual(self.verify(self.model(cross=True), state_intent=states)["disulfide_bonds_checked"], 1)
        self.refused(lambda: self.verify(self.model(cross=False), state_intent=states))

    def test_missing_original_heavy_atom_and_wrong_terminal_pdb_element_refused(self):
        def mutate(atoms, edges, lookup):
            atoms[lookup["PROB", "3", "CB"]-1]["name"] = "CX"
        self.refused(lambda: self.verify(self.model(mutate=mutate)))
        pdb, psf = self.model()
        rows = pdb.read_text().splitlines()
        rows[0] = rows[0][:76] + " C" + rows[0][78:]
        pdb.write_text("\n".join(rows) + "\n")
        self.refused(lambda: self.verify((pdb, psf)))

    def test_new_undeclared_cross_fragment_disulfide_is_not_accepted(self):
        self.setup_input("CYS")
        self.refused(lambda: self.verify(self.model(cross=True)))

    def test_disulfide_cannot_keep_thiol_hydrogen_or_extra_sulfur_partner(self):
        self.setup_input("CYS", linkage="conect")
        for kind in ("hydrogen", "sulfur"):
            def mutate(atoms, edges, lookup):
                group = atoms[lookup["PROA", "1", "SG"]-1]
                extra = dict(group, serial=len(atoms)+1, name="HG" if kind=="hydrogen" else "SX",
                             type="HS" if kind=="hydrogen" else "SM", mass=1.008 if kind=="hydrogen" else 32.06)
                atoms.append(extra)
                edges.add(tuple(sorted((group["serial"], extra["serial"]))))
            self.refused(lambda: self.verify(self.model(cross=True, mutate=mutate)))

    def test_unrequested_protein_ligand_covalent_link_is_refused(self):
        def mutate(atoms, edges, lookup):
            atoms.append({"serial":len(atoms)+1,"segment":"HETA","resid":"1","resname":"LIG", "name":"C1",
                          "type":"CT3","charge":0.,"mass":12.011})
            edges.add(tuple(sorted((atoms[-1]["serial"], lookup["PROA","1","CA"])) ))
        self.refused(lambda: self.verify(self.model(mutate=mutate)))

    def test_explicit_sidechain_crosslink_cannot_disappear_or_be_replaced(self):
        self.source.insert(-1, "CONECT    5   25")
        self.prepared, self.plan = prepare_fragmented_lines(self.source, accept_breaks=True)
        # Refresh saved-form mapping through the actual helper with these bytes.
        source_path, prepared_path = self.root/"source.pdb", self.root/"prepared.pdb"
        source_path.write_text("\n".join(self.source)+"\n")
        prepared_path.write_text("\n".join(self.prepared)+"\n")
        self.receipt = {}
        _preparation_fields(self.root/"selection.html", {"ligand_resname":"LIG", "preparation":self.preparation},
            fragment_plan=self.plan, fragment_source_pdb=source_path, fragment_protein_pdb=prepared_path, fragment_mapping=self.receipt)
        self.refused(lambda: self.verify(self.model()))
        def mutate(atoms, edges, lookup):
            edges.add(tuple(sorted((lookup["PROA","1","CB"], lookup["PROB","3","CB"])) ))
        self.assertEqual(self.verify(self.model(mutate=mutate))["retained_cross_fragment_bonds"], 1)

    def test_single_residue_disjoint_caps_pass_but_overlapping_caps_refuse(self):
        self.setup_input(single=True)
        self.assertEqual(len(self.verify()["terminals"]), 4)
        for c in ("CT1", "CT2", "CT3"):
            self.setup_input(single=True, c=c)
            self.refused(lambda: self.verify(self.model()))

    def test_template_unknown_directive_duplicate_atom_or_nonfinite_charge_refused(self):
        for text in (topology_text().replace("PRES NTER 0", "PRES NTER 0\nEXEC UNKNOWN"),
                     topology_text().replace("PRES NTER 0", "PRES NTER 0\nATOM N NH3 -.30"),
                     topology_text().replace("ATOM N NH3 -0.3", "ATOM N NH3 nan")):
            self.topology.write_text(text)
            self.refused(lambda: self.verify(self.model()))

    def test_template_profile_includes_double_bonds_and_deleted_atoms(self):
        text = topology_text().replace("BOND C OT2 C OT1", "BOND C OT2\nDOUBLE C OT1")
        self.topology.write_text(text)
        profile = terminal_profile(read_terminal_topology(self.topology), "ALA", ["CTER"])
        self.assertNotIn("O", profile["atoms"])
        self.assertIn(("C", "OT1"), profile["bonds"])
        self.verify()

    def test_input_artifacts_are_not_modified_by_verification(self):
        files = self.model()
        before = [path.read_bytes() for path in [*files, self.topology]]
        self.verify(files)
        self.assertEqual(before, [path.read_bytes() for path in [*files, self.topology]])

    def test_changing_artifact_cannot_receive_a_stale_hash_receipt(self):
        files = self.model()
        with patch("charmm_gui_cli.chem.fragment_proof._digest", side_effect=["a"*64]*3+["b"*64]*3):
            self.refused(lambda: self.verify(files))

    def test_topology_read_is_bounded_before_parsing(self):
        self.topology.write_bytes(b"*"*(4*1024*1024+1))
        self.refused(lambda: read_terminal_topology(self.topology))

    def test_archive_requires_source_and_assembly_and_rejects_changed_final(self):
        root = self.root/"archive"
        root.mkdir()
        (root/"toppar").mkdir()
        (root/"toppar/top_all36_prot.rtf").write_text(topology_text())
        pdb, psf = self.model()
        for step in ("step1_pdbreader", "step5_assembly"):
            (root/(step+".pdb")).write_bytes(pdb.read_bytes())
            (root/(step+".psf")).write_bytes(psf.read_bytes())
        def verify():
            return verify_fragment_archive(root, self.preparation, self.source, self.prepared, self.plan, self.receipt)
        proof = verify()
        self.assertTrue(proof["source"]["passed"] and proof["assembly"]["passed"])
        final = root/"step5_assembly.psf"
        final.write_text(final.read_text().replace("NH3 -0.300000", "NH3 -0.200000", 1))
        self.refused(verify)

    def test_archive_does_not_choose_arbitrary_engine_copy_or_symlink(self):
        root = self.root/"incomplete-archive"
        (root/"gromacs").mkdir(parents=True)
        pdb, psf = self.model()
        for step in ("step1_pdbreader", "step5_assembly"):
            (root/"gromacs"/(step+".pdb")).symlink_to(pdb)
            (root/"gromacs"/(step+".psf")).symlink_to(psf)
        self.refused(lambda: verify_fragment_archive(root, self.preparation, self.source, self.prepared, self.plan, self.receipt))

    def test_archive_read_permission_failure_is_localized_not_raw_oserror(self):
        with patch.object(Path, "is_file", return_value=True), \
                patch("charmm_gui_cli.chem.fragment_proof._digest", side_effect=PermissionError("private details")):
            error = self.refused(lambda: verify_fragment_archive(self.root/"unreadable", self.preparation,
                self.source, self.prepared, self.plan, self.receipt))
            self.assertNotIn("private details", error.localized())

    def test_archive_source_changed_during_assembly_check_is_refused(self):
        root = self.root/"changing-archive"
        (root/"toppar").mkdir(parents=True)
        (root/"toppar/top_all36_prot.rtf").write_text(topology_text())
        pdb, psf = self.model()
        for step in ("step1_pdbreader", "step5_assembly"):
            (root/(step+".pdb")).write_bytes(pdb.read_bytes())
            (root/(step+".psf")).write_bytes(psf.read_bytes())
        def check(*args, **kwargs):
            result = verify_fragment_artifact(*args, **kwargs)
            if Path(args[5]).name == "step1_pdbreader.pdb":
                path = root/"step1_pdbreader.psf"
                path.write_text(path.read_text().replace("NH3 -0.300000", "NH3 -0.200000", 1))
            return result
        with patch("charmm_gui_cli.chem.fragment_proof.verify_fragment_artifact", side_effect=check):
            self.refused(lambda: verify_fragment_archive(root, self.preparation, self.source, self.prepared, self.plan, self.receipt))

    def test_archive_rejects_real_engine_files_and_mixed_model_roots(self):
        root = self.root/"engine-archive"
        (root/"gromacs/toppar").mkdir(parents=True)
        pdb, psf = self.model()
        for step in ("step1_pdbreader", "step5_assembly"):
            (root/"gromacs"/(step+".pdb")).write_bytes(pdb.read_bytes())
            (root/"gromacs"/(step+".psf")).write_bytes(psf.read_bytes())
        (root/"gromacs/toppar/top_all36_prot.rtf").write_text(topology_text())
        self.refused(lambda: verify_fragment_archive(root, self.preparation, self.source, self.prepared, self.plan, self.receipt))
        root = self.root/"mixed-archive"
        for name, step in (("first", "step1_pdbreader"), ("second", "step5_assembly")):
            (root/name).mkdir(parents=True)
            (root/name/(step+".pdb")).write_bytes(pdb.read_bytes())
            (root/name/(step+".psf")).write_bytes(psf.read_bytes())
        (root/"first/toppar").mkdir()
        (root/"first/toppar/top_all36_prot.rtf").write_text(topology_text())
        self.refused(lambda: verify_fragment_archive(root, self.preparation, self.source, self.prepared, self.plan, self.receipt))

    def test_bilingual_error_is_actionable_without_file_contents(self):
        for language, fragment in (("zh", "片段拓扑"), ("en", "Fragment topology")):
            set_language(language)
            self.topology.write_text("password=never-display-contents")
            error = self.refused(lambda: self.verify(self.model()))
            self.assertIn(fragment, error.localized())
            self.assertNotIn("never-display", error.localized())


if __name__ == "__main__":
    unittest.main()
