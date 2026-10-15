"""Actual shared serialization/precheck paths; no remote split support implied."""

import hashlib
import json
from pathlib import Path
import tempfile
from copy import deepcopy
import unittest
from unittest.mock import patch, Mock

from charmm_gui_cli.chem.fragment_inputs import verify_prepared_fragments
from charmm_gui_cli.chem.fragment_preparation import verify_fragment_preparation
from charmm_gui_cli.chem.input_sources import prepare_sources
from charmm_gui_cli.chem.inputs import Atom, _pdb, _sdf, prepare_inputs
from charmm_gui_cli.chem.input_safety import validate_protein
from charmm_gui_cli.chem.multicomponent import prepare_build_components
from charmm_gui_cli.chem.pdb_normalization import attach_source_normalization
from charmm_gui_cli.chem.precheck import check_inputs
from charmm_gui_cli.locales.i18n import MESSAGES, set_language
from charmm_gui_cli.presentation.terminal_output import format_check
from charmm_gui_cli.pipeline import full_build, sweep
from charmm_gui_cli.remote.api import SubmissionUnknown
from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.store.paths import read_value
from tests.test_fragment_preparation import residue, ssbond
from tests.test_pdb_boundaries import atom


class FragmentInputTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-fragment-inputs-"))
        self.protein, self.ligand = self.root/"protein.pdb", self.root/"ligand.sdf"
        self.lines = residue(10, 1, 0, segment="PROA") + residue(40, 2, 9, segment="PROA", name="GLY")
        self.write()
        self.ligand.write_text(_sdf([Atom(1,"C1","C",(0.,3.5,0.)),Atom(2,"O1","O",(1.2,3.5,0.))],{(0,1):2},"LIG"))
        self.network = patch("requests.Session.request", side_effect=AssertionError("No HTTP"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(set_language)
        self.counter = 0

    def write(self):
        self.protein.write_text("\n".join(self.lines)+"\nEND\n")

    def out(self):
        self.counter += 1
        return self.root/("output-"+str(self.counter))

    def prepare(self, **kwargs):
        return prepare_inputs(self.protein,self.ligand,self.out(), **kwargs)

    def refused(self, key, action):
        with self.assertRaises(ToolError) as caught:
            action()
        self.assertEqual(caught.exception.key,key)
        return caught.exception

    def assert_record(self, prepared):
        record = verify_prepared_fragments(prepared)
        self.assertIsNotNone(record)
        source=Path(prepared["files"][record["source_file"]]).read_text().splitlines()
        actual=Path(prepared["files"][record["prepared_file"]]).read_text().splitlines()
        proof=verify_fragment_preparation(source,actual,record["plan"])
        self.assertTrue(proof["passed"])
        protein,_=_pdb(prepared["files"]["protein.pdb"])
        validate_protein(protein)
        self.assertFalse(record["remote_verified"])
        self.assertFalse(record["caps_generated_locally"])
        self.assertEqual(read_value(prepared["files"]["input-manifest.json"]),json.loads(json.dumps(prepared["manifest"])))
        return record

    def test_default_still_refuses_in_prepare_sources_and_check(self):
        self.refused("input.chain_breaks",lambda:self.prepare())
        self.refused("input.chain_breaks",lambda:prepare_sources(protein=self.protein,ligand=self.ligand,out=self.out()))
        report=check_inputs(protein=self.protein,ligand=self.ligand,out=self.out(),hydrogens="preserve")
        self.assertFalse(report["passed"])
        self.assertIn("geometric backbone breaks",report["error"]["message"])

    def test_final_full_complex_not_old_protein_hash_is_the_plan_reference(self):
        original=self.protein.read_bytes(),self.ligand.read_bytes()
        prepared=self.prepare(fragment_preparation=True)
        record=self.assert_record(prepared)
        self.assertEqual(record["prepared_file"],"complex.pdb")
        self.assertEqual(record["plan"]["prepared_sha256"],hashlib.sha256(Path(prepared["files"]["complex.pdb"]).read_bytes()).hexdigest())
        self.assertNotEqual(record["plan"]["prepared_sha256"],hashlib.sha256(Path(prepared["files"]["protein.pdb"]).read_bytes()).hexdigest())
        self.assertEqual(record["plan"]["fragments"][0]["atom_serials"],list(range(1,6)))
        self.assertEqual(record["plan"]["fragments"][1]["atom_serials"],list(range(6,10)))
        self.assertEqual(original,(self.protein.read_bytes(),self.ligand.read_bytes()))

    def test_all_retained_atoms_and_ligand_coordinates_are_unchanged(self):
        prepared=self.prepare(fragment_preparation=True)
        source,_=_pdb(prepared["files"]["fragment-source.pdb"])
        actual,_=_pdb(prepared["files"]["complex.pdb"])
        self.assertEqual([(a.serial,a.name,a.element,a.xyz) for a in source],[(a.serial,a.name,a.element,a.xyz) for a in actual])
        self.assertEqual([a.line for a in source if a.line.startswith("HETATM")],[a.line for a in actual if a.line.startswith("HETATM")])
        self.assertEqual({a.line[21:22] for a in actual if a.line.startswith("ATOM")},{"A","B"})
        self.assertEqual({a.line[21:22] for a in actual if a.line.startswith("HETATM")},{"L"})

    def test_explicit_option_with_no_break_does_not_add_spurious_files_or_repairs(self):
        self.lines=residue(10,1,0)+residue(40,2,2.3,name="GLY")
        self.write()
        first,second=self.prepare(),self.prepare(fragment_preparation=True)
        self.assertIsNone(verify_prepared_fragments(second))
        self.assertEqual(first["manifest"],second["manifest"])
        self.assertEqual(set(first["files"]),set(second["files"]))
        for name in first["files"]:
            self.assertEqual(Path(first["files"][name]).read_bytes(),Path(second["files"][name]).read_bytes())

    def test_local_check_writes_matching_repair_record_and_prepared_manifest(self):
        for lang in ("zh","en"):
            set_language(lang)
            report=check_inputs(protein=self.protein,ligand=self.ligand,out=self.out(),hydrogens="preserve",fragment_preparation=True)
            self.assertTrue(report["passed"],report.get("error"))
            self.assertTrue(report["review_required"])
            self.assertFalse(report["server_submitted"])
            self.assertEqual(json.loads(Path(report["check_file"]).read_text())["automatic_repairs"],report["automatic_repairs"])
            prepared={"files":report["prepared_files"],"manifest":read_value(report["prepared_files"]["input-manifest.json"])}
            self.assert_record(prepared)
            repairs=[x for x in report["automatic_repairs"] if x["key"]=="fragment.prepared"]
            self.assertEqual(len(repairs),1)
            self.assertEqual(repairs[0]["params"],{"fragments":2,"breaks":1})
            self.assertTrue(MESSAGES[repairs[0]["key"]][lang].format(**repairs[0]["params"]))
            summary=format_check(report)
            self.assertIn(MESSAGES[repairs[0]["key"]][lang].format(**repairs[0]["params"]),summary)
            self.assertIn("GLYP",summary)
            self.assertEqual({r["chain"] for r in report["prepared_terminal_predictions"]},{"A","B"})

    def test_state_names_are_remapped_after_final_serialization(self):
        self.lines=residue(10,1,0,segment="PROA")+residue(40,2,9,segment="PROA",name="HIE")
        self.write()
        prepared=self.prepare(fragment_preparation=True)
        record=self.assert_record(prepared)
        entry=prepared["manifest"]["input_normalization"]["protein_state_intent"]["residues"][0]
        self.assertEqual((entry["chain"],entry["segment"],entry["polymer_block"],entry["state"]),("B","",1,"HSE"))
        self.assertEqual(record["source_state_intent"]["residues"][0]["chain"],"A")

    def test_collapsed_ter_blocks_are_rebased_before_fragment_state_mapping(self):
        self.lines=residue(10,1,0)+["TER","TER"]+residue(40,1,9,name="HIE")
        self.write()
        prepared=self.prepare(fragment_preparation=True)
        record=self.assert_record(prepared)
        self.assertEqual(record["source_state_intent"]["residues"][0]["polymer_block"],1)
        self.assertEqual(record["source_blocks"][1]["input"]["polymer_block"],2)
        self.assertEqual(record["source_blocks"][1]["canonical"]["polymer_block"],1)
        self.assertEqual(prepared["manifest"]["input_normalization"]["protein_state_intent"]["residues"][0]["chain"],"B")

    def test_staged_base_residue_does_not_erase_ash_patch_intent(self):
        self.lines=residue(10,1,0)+residue(40,2,9,name="ASH")
        self.write()
        sources=prepare_sources(protein=self.protein,ligand=self.ligand,out=self.out(),input_options={},fragment_preparation=True)
        self.assertTrue(sources["report"]["fragment_preparation_requested"])
        self.assertNotIn("fragment_preparation",sources["report"]["input_normalization"])
        prepared=prepare_inputs(sources["protein"],sources["ligand"],self.out(),fragment_preparation=True)
        self.assertNotIn("protein_state_intent",prepared["manifest"]["input_normalization"])
        attach_source_normalization(prepared,sources["report"])
        record=self.assert_record(prepared)
        entry=prepared["manifest"]["input_normalization"]["protein_state_intent"]["residues"][0]
        self.assertEqual((entry["source_resname"],entry["pdb_resname"],entry["state"],entry["chain"]),("ASH","ASP","ASPP","B"))
        self.assertEqual(record["source_state_intent"]["residues"][0]["chain"],"A")

    def test_staged_ter_state_and_source_inventory_do_not_overwrite_prepared_identity(self):
        self.lines=residue(10,1,0)+["TER","TER"]+residue(40,1,9,name="HIE")
        self.write()
        sources=prepare_sources(protein=self.protein,ligand=self.ligand,out=self.out(),input_options={},fragment_preparation=True)
        self.assertEqual(sources["report"]["input_normalization"]["protein_state_intent"]["residues"][0]["polymer_block"],1)
        prepared=prepare_inputs(sources["protein"],sources["ligand"],self.out(),fragment_preparation=True)
        attach_source_normalization(prepared,sources["report"])
        self.assert_record(prepared)
        normal=prepared["manifest"]["input_normalization"]
        self.assertEqual(normal["protein_state_intent"]["residues"][0]["chain"],"B")
        self.assertIn("source_structure_inventory",normal)
        self.assertNotEqual(normal["source_structure_inventory"],normal["structure_inventory"])

    def test_snapshot_upload_protein_and_record_edits_fail_verification(self):
        for name in ("fragment-source.pdb","complex.pdb","protein.pdb"):
            prepared=self.prepare(fragment_preparation=True)
            path=Path(prepared["files"][name]);path.write_text(path.read_text()+"REMARK changed\n")
            self.refused("fragment.evidence_changed",lambda:verify_prepared_fragments(prepared))
        prepared=self.prepare(fragment_preparation=True)
        prepared["manifest"]["input_normalization"]["fragment_preparation"]["plan"]["fingerprint"]="0"*64
        self.refused("fragment.evidence_changed",lambda:verify_prepared_fragments(prepared))

    def test_plan_scalar_types_cannot_be_changed_using_python_numeric_equality(self):
        prepared=self.prepare(fragment_preparation=True)
        record=verify_prepared_fragments(prepared)
        source=Path(prepared["files"]["fragment-source.pdb"]).read_text().splitlines()
        actual=Path(prepared["files"]["complex.pdb"]).read_text().splitlines()
        for target,key in ((record["plan"],"schema_version"),(record["plan"]["fragments"][0],"index")):
            old=target[key];target[key]=True
            self.refused("fragment.evidence_changed",lambda:verify_fragment_preparation(source,actual,record["plan"]))
            target[key]=old

    def test_restaging_must_not_silently_drop_a_previous_plan(self):
        first=self.prepare(fragment_preparation=True)
        second=prepare_inputs(first["files"]["protein.pdb"],first["files"]["ligand.sdf"],self.out(),fragment_preparation=True)
        self.assertIsNone(verify_prepared_fragments(second))
        before=Path(second["files"]["input-manifest.json"]).read_bytes()
        self.refused("fragment.evidence_changed",lambda:attach_source_normalization(second,{"input_normalization":first["manifest"]["input_normalization"]}))
        self.assertEqual(Path(second["files"]["input-manifest.json"]).read_bytes(),before)

    def test_source_request_cannot_be_forgotten_for_reused_chain_ter_blocks(self):
        self.lines=residue(10,1,0)+["TER"]+residue(40,2,9,name="GLY")
        self.write()
        sources=prepare_sources(protein=self.protein,ligand=self.ligand,out=self.out(),fragment_preparation=True)
        prepared=prepare_inputs(sources["protein"],sources["ligand"],self.out())
        self.assertIsNone(verify_prepared_fragments(prepared))
        self.refused("fragment.evidence_changed",lambda:attach_source_normalization(prepared,sources["report"]))

    def test_complex_and_matching_protein_use_the_actual_bound_frame(self):
        complex_path=self.root/"bound.pdb"
        extra=[atom(80,"LIG",1,(0,3.5,0),record="HETATM",chain="B",name="C1"),
               atom(81,"LIG",1,(1.2,3.5,0),record="HETATM",chain="B",name="O1",element="O")]
        complex_path.write_text("\n".join(self.lines+["TER"]+extra)+"\nEND\n")
        sources=prepare_sources(protein=self.protein,complex_pdb=complex_path,ligand=self.ligand,
            out=self.out(),input_options={},fragment_preparation=True)
        self.assertTrue(sources["report"]["supplied_protein_checked_against_complex"])
        prepared=prepare_inputs(sources["protein"],sources["ligand"],self.out(),fragment_preparation=True)
        attach_source_normalization(prepared,sources["report"])
        self.assert_record(prepared)
        self.assertEqual([a.xyz for a in _pdb(prepared["files"]["complex.pdb"])[0] if a.line.startswith("HETATM")],[(0.,3.5,0.),(1.2,3.5,0.)])

    def test_explicit_fragment_mode_does_not_bypass_other_blocking_checks(self):
        original=list(self.lines)
        for kind in ("unknown","metal","conect","link"):
            self.lines=list(original)
            if kind=="unknown":self.lines[0]=self.lines[0][:17]+"UNK "+self.lines[0][21:]
            if kind=="metal":self.lines.append(atom(90,"ZN",1,(0,1,0),record="HETATM",name="ZN",element="Zn"))
            if kind=="conect":self.lines.append("CONECT   12   40")
            if kind=="link":self.lines.append("LINK        C1   LIG L   1                 CA  ALA A   1")
            self.write()
            with self.assertRaises(ToolError):self.prepare(fragment_preparation=True)

    def test_explicit_ssbond_is_retained_after_actual_upload_numbering(self):
        self.lines=[ssbond(left=1,right=2)]+residue(10,1,0,name="CYS")+residue(40,2,9,name="CYS")
        self.write()
        prepared=self.prepare(fragment_preparation=True)
        self.assert_record(prepared)
        line=next(row for row in Path(prepared["files"]["complex.pdb"]).read_text().splitlines() if row.startswith("SSBOND"))
        self.assertEqual((line[15],line[29]),("A","B"))

    def test_non_boolean_request_is_not_accepted(self):
        for value in (1,"true",None):
            self.refused("fragment.option_invalid",lambda:self.prepare(fragment_preparation=value))

    def test_malformed_prepared_envelope_never_leaks_a_python_traceback(self):
        for envelope in ({},{"manifest":None},{"manifest":{"input_normalization":[]}}):
            self.refused("fragment.evidence_changed",lambda:verify_prepared_fragments(envelope))

    def test_rehashed_invalid_block_mapping_is_not_trusted(self):
        prepared=self.prepare(fragment_preparation=True)
        original=deepcopy(prepared["manifest"]["input_normalization"]["fragment_preparation"])
        for rows in (None,[],[{"input":{},"canonical":{}}],original["source_blocks"]*2):
            record=deepcopy(original)
            record["source_blocks"]=rows
            body={k:v for k,v in record.items() if k!="fingerprint"}
            record["fingerprint"]=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()
            prepared["manifest"]["input_normalization"]["fragment_preparation"]=record
            self.refused("fragment.evidence_changed",lambda:verify_prepared_fragments(prepared))

    def workflow(self):
        prepared=self.prepare(fragment_preparation=True)
        directory=self.out();directory.mkdir()
        manifest={"schema_version":2,"state":"inputs_ready","inputs":prepared,"config":{}}
        return directory,manifest

    def test_invalid_fragment_configuration_cannot_upload_seal_or_clone(self):
        directory,manifest=self.workflow()
        api=Mock()
        self.refused("fragment.proof_invalid",lambda:full_build.require_protein_state_support(directory,manifest))
        self.refused("fragment.proof_invalid",lambda:sweep.preparation_identity(directory,manifest))
        self.refused("fragment.proof_invalid",lambda:full_build._start_prepared_bilayer(directory,manifest,api,{},False,30,60,None))
        api.assert_not_called()
        self.assertEqual(api.method_calls,[])
        self.assertFalse((directory/"api-config.json").exists())
        self.assertFalse((directory/"preparation").exists())

    def test_uncertain_post_takes_priority_over_stale_input_or_pending_support(self):
        directory,manifest=self.workflow()
        Path(manifest["inputs"]["files"]["fragment-source.pdb"]).write_text("REMARK changed\n")
        (directory/"preparation").mkdir()
        for action in (lambda:full_build.require_protein_state_support(directory,manifest),
                       lambda:sweep.preparation_identity(directory,manifest)):
            with self.assertRaises(SubmissionUnknown):action()

    def test_advance_never_uploads_an_invalid_manually_registered_fragment_manifest(self):
        directory,manifest=self.workflow()
        from charmm_gui_cli.store.auth import atomic_json
        atomic_json(directory/"full-run.json",manifest)
        web,api=Mock(),Mock()
        self.refused("fragment.proof_invalid",lambda:full_build.advance(directory,web,api))
        self.assertEqual(web.method_calls,[])
        self.assertEqual(api.method_calls,[])

    def components(self):
        complex_path=self.root/"complex.pdb"
        extra=[atom(80,"LIG",1,(0,3.5,0),record="HETATM",chain="B",name="C1"),
               atom(81,"LIG",1,(1.2,3.5,0),record="HETATM",chain="B",name="O1",element="O"),
               atom(90,"COF",2,(9,3.5,0),record="HETATM",chain="C",name="C1"),
               atom(91,"COF",2,(10.2,3.5,0),record="HETATM",chain="C",name="O1",element="O")]
        complex_path.write_text("\n".join(self.lines+extra)+"\nEND\n")
        config=self.root/"components.yaml"
        config.write_text(json.dumps({"version":1,"water_policy":"reject","ion_policy":"reject","components":[
            {"resname":"LIG","chain":"B","resid":1,"sdf":str(self.ligand)},
            {"resname":"COF","chain":"C","resid":2,"sdf":str(self.ligand)}]}))
        return complex_path,config

    def test_multicomponent_final_serialization_reserves_every_component_chain(self):
        complex_path,config=self.components()
        original=complex_path.read_bytes()
        prepared=prepare_build_components(complex_pdb=complex_path,components_config=config,out=self.out(),
            hydrogens="preserve",input_options={},fragment_preparation=True)
        record=self.assert_record(prepared)
        self.assertEqual([f["prepared_chain"] for f in record["plan"]["fragments"]],["A","D"])
        self.assertEqual(complex_path.read_bytes(),original)
        atoms,_=_pdb(prepared["files"]["complex.pdb"])
        self.assertEqual({a.line[21:22] for a in atoms if a.line.startswith("HETATM")},{"B","C"})
        self.assertEqual({c["resname"] for c in prepared["manifest"]["components"]},{"LIG","COF"})

    def test_component_precheck_uses_same_final_plan(self):
        complex_path,config=self.components()
        report=check_inputs(complex_pdb=complex_path,components=config,out=self.out(),hydrogens="preserve",fragment_preparation=True)
        self.assertTrue(report["passed"],report.get("error"))
        self.assertTrue(report["review_required"])
        self.assert_record({"files":report["prepared_files"],"manifest":read_value(report["prepared_files"]["input-manifest.json"])})


if __name__=="__main__":unittest.main()
