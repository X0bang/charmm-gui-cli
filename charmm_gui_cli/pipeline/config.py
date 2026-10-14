"""Strict user configuration mapped only to known remote form fields."""

import math
from difflib import get_close_matches
from pathlib import Path
import re

import yaml

from charmm_gui_cli.store.identifiers import job_id
from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.chem.structure import analyze_pdb
from ..chem.lipids import KNOWN_LIPIDS
from ..store.configuration import UniqueLoader, unique_mapping


from charmm_gui_cli.store.configuration import mapping, number, composition, validate_config

def load_config(path):
    path = Path(path)
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except yaml.YAMLError:
        raise ToolError("Invalid YAML configuration.") from None
    config = validate_config(value)
    if config["input_pdb"]:
        config["input_pdb"] = str((path.parent / config["input_pdb"]).resolve())
    return config


def payload_for(config):
    membrane, prep, ions = config["membrane"], config["preparation"], config["ions"]
    payload = {
        "jobid": config["source_job_id"], "upper": membrane["upper"], "lower": membrane["lower"],
        "margin": str(membrane["margin_A"]), "wdist": str(membrane["water_padding_A"]),
        "ion_conc": str(ions["concentration_M"]), "ion_type": ions["type"],
        "heteroatoms": "true", "clone_job": "true", "run_ffconverter": "true",
    }
    if prep["orientation"] == "ppm":
        payload["ppm"] = "true"
    return payload


def pdb_residues(lines):
    residues = {}
    for line in lines:
        if line.startswith("ENDMDL"):
            break
        if line.startswith(("ATOM  ", "HETATM")):
            name = line[17:21].strip()
            key = (line[21:27], line[72:76])
            residues.setdefault(name, set()).add(key)
    return {name: len(items) for name, items in residues.items()}


def plan(config):
    blockers = []
    if not config["source_job_id"]:
        blockers.append("Finish PDB Reader/ligand preparation in the website and supply source_job_id.")
    if not config["preparation"]["ligand_parameters_ready"]:
        blockers.append("Confirm ligand topology/parameters exist in the source job, then set ligand_parameters_ready: true.")
    local = None
    if config["input_pdb"]:
        local = analyze_pdb(config["input_pdb"], config["expected_ligands"])
        if local["missing_ligands"]:
            blockers.append("Expected ligands are absent from the local PDB (first model).")
    return {
        "ready_to_submit": not blockers, "blockers": blockers,
        "support": "experimental: prepared source job only; ligand retention requires validation",
        "preparation_url": "https://www.charmm-gui.org/?doc=input/membrane.bilayer",
        "local_pdb": local, "request": {"method": "POST", "endpoint": "/quick_bilayer", "form": payload_for(config)},
        "notes": ["input_pdb is inspected locally; it is not uploaded by this API workflow.",
                  "A job ID alone does not prove that ligand parameters are ready.",
                  "Use lowercase ion_conc/ion_type as in the upstream client. Live testing showed uppercase documented names silently yielded default NaCl 0.15 M; inspect actual output settings."],
    }
