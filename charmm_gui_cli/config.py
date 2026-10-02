"""Strict user configuration mapped only to known remote form fields."""

import math
from pathlib import Path
import re

import yaml

from .api import job_id
from .auth import ToolError
from .structure import analyze_pdb


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ToolError("Configuration keys must be unique strings.")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def mapping(value, keys, name):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise ToolError(f"Invalid/unknown fields in {name}. Allowed: {', '.join(keys)}.")
    return value


def number(value, name, minimum=0, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum or (positive and value == 0)):
        raise ToolError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} number.")
    return value


def composition(value):
    if not isinstance(value, str) or value.count("=") != 1:
        raise ToolError("Lipid composition must look like POPC:CHL1=3:1.")
    names, amounts = (part.split(":") for part in value.split("="))
    if len(names) != len(amounts) or len(set(names)) != len(names):
        raise ToolError("Lipid names and ratios must match, without duplicate names.")
    if not all(re.fullmatch(r"[A-Za-z0-9_+-]+", name) for name in names):
        raise ToolError("Invalid lipid name in composition.")
    try:
        for amount in amounts:
            number(float(amount), "Lipid ratio", positive=True)
    except ValueError:
        raise ToolError("Lipid ratios must be numeric.") from None
    return value


def validate_config(raw):
    raw = mapping(raw, ["version", "source_job_id", "input_pdb", "expected_ligands",
                        "preparation", "membrane", "ions"], "configuration")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ToolError("Configuration version must be 1.")
    source = raw.get("source_job_id")
    if source is not None:
        job_id(source)
    ligands = raw.get("expected_ligands")
    if (not isinstance(ligands, list) or not ligands
            or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9]{1,4}", x) for x in ligands)
            or len(set(ligands)) != len(ligands)):
        raise ToolError("expected_ligands must list unique PDB residue names, e.g. [LIG].")
    prep = mapping(raw.get("preparation"), ["ligand_parameters_ready", "orientation"], "preparation")
    if type(prep.get("ligand_parameters_ready")) is not bool:
        raise ToolError("preparation.ligand_parameters_ready must be true or false.")
    if prep.get("orientation") not in ("prepared", "ppm"):
        raise ToolError("preparation.orientation must be prepared or ppm.")
    membrane = mapping(raw.get("membrane"), ["upper", "lower", "margin_A", "water_padding_A"], "membrane")
    upper, lower = composition(membrane.get("upper")), composition(membrane.get("lower"))
    margin = number(membrane.get("margin_A"), "margin_A", positive=True)
    water = number(membrane.get("water_padding_A", 22.5), "water_padding_A", positive=True)
    ions = mapping(raw.get("ions", {}), ["type", "concentration_M"], "ions")
    ion_type = ions.get("type", "NaCl")
    if not isinstance(ion_type, str) or not re.fullmatch(r"[A-Za-z0-9+-]+", ion_type):
        raise ToolError("Invalid ion type.")
    concentration = number(ions.get("concentration_M", 0.15), "concentration_M")
    pdb = raw.get("input_pdb")
    if pdb is not None and (not isinstance(pdb, str) or not pdb):
        raise ToolError("input_pdb must be a file path or null.")
    return {
        "version": 1, "source_job_id": source, "input_pdb": pdb,
        "expected_ligands": ligands, "preparation": dict(prep),
        "membrane": {"upper": upper, "lower": lower, "margin_A": margin, "water_padding_A": water},
        "ions": {"type": ion_type, "concentration_M": concentration},
    }


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
