"""Input-oriented configuration for the HTTP preparation + API build workflow."""

from pathlib import Path
import re

import yaml

from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.pipeline.config import UniqueLoader
from charmm_gui_cli.store.configuration import mapping, validate_config
from charmm_gui_cli.chem.pdb_normalization import N_TERMINALS, C_TERMINALS


def validate_preparation(raw):
    prep = mapping(raw, ["n_terminal", "c_terminal", "orientation", "terminals"], "preparation")
    result = {"n_terminal": prep.get("n_terminal", "NTER"),
              "c_terminal": prep.get("c_terminal", "CTER"), "orientation": prep.get("orientation", "ppm")}
    chains = dict(prep.get("terminals", {})) if isinstance(prep.get("terminals", {}), dict) else None
    if chains is None:
        raise ToolError(key="terminal.mapping_invalid", next_step="charmm-gui-cli build -h")
    for end, choices in (("n_terminal", N_TERMINALS), ("c_terminal", C_TERMINALS)):
        value = result[end]
        if isinstance(value, str) and "=" in value:
            for item in value.split(","):
                pair = item.split("=")
                if len(pair) != 2 or not re.fullmatch(r"[A-Za-z0-9_]{1,4}", pair[0]) or pair[1] not in choices:
                    raise ToolError(key="terminal.mapping_invalid", next_step="charmm-gui-cli build -h")
                previous = chains.setdefault(pair[0], {})
                if not isinstance(previous, dict) or end in previous:
                    raise ToolError(key="terminal.mapping_invalid", next_step="charmm-gui-cli build -h")
                previous[end] = pair[1]
            result[end] = "NTER" if end == "n_terminal" else "CTER"
        elif not isinstance(value, str) or value not in choices:
            raise ToolError(key="terminal.patch_invalid", params={"end": "N" if end == "n_terminal" else "C",
                            "choices": ", ".join(choices)}, next_step="charmm-gui-cli build -h")
    for chain, patches in chains.items():
        if (not isinstance(chain, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,4}", chain)
                or not isinstance(patches, dict) or not patches or set(patches) - {"n_terminal", "c_terminal"}
                or any(not isinstance(value, str) or value not in (N_TERMINALS if key == "n_terminal" else C_TERMINALS)
                       for key, value in patches.items())):
            raise ToolError(key="terminal.mapping_invalid", next_step="charmm-gui-cli build -h")
    if chains:
        result["terminals"] = chains
    return result


def validate_model_options(membrane, ions, preparation, *, ligand_resname="LIG", hydrogens="add_missing"):
    """Validate options before creating task directories or preparing structures."""
    if hydrogens not in ("preserve", "add_missing") or not re.fullmatch(r"[A-Z][A-Z0-9]{0,2}", str(ligand_resname)):
        raise ToolError(key="build.input_options_invalid", next_step="charmm-gui-cli build -h")
    config = {"ligand_resname": ligand_resname, "membrane": membrane, "ions": ions,
              "preparation": validate_preparation(preparation)}
    normalized = api_config(config, None)
    config.update(membrane=normalized["membrane"], ions=normalized["ions"])
    return config


def validate_terminal_residues(preparation, predictions):
    """Reject explicit Gly/Pro patches on the wrong residue before uploading."""
    terminals = preparation.get("terminals", {})
    matched = set()
    for entry in predictions:
        chain = entry["chain"]
        aliases = {name for name in (chain, chain[-1:]) if name in terminals}
        if len(aliases) > 1:
            raise ToolError(key="terminal.alias_conflict", params={"chain": chain}, next_step="charmm-gui-cli build -h")
        matched.update(aliases)
        key = chain if chain in terminals else chain[-1:]
        patch = terminals.get(key, {}).get("n_terminal", preparation["n_terminal"])
        residue = entry["first_resname"].upper()
        if patch in ("GLYP", "PROP") and residue != {"GLYP": "GLY", "PROP": "PRO"}[patch]:
            raise ToolError(key="terminal.residue_mismatch", params={"chain": chain, "residue": residue, "patch": patch},
                            next_step="charmm-gui-cli build -h")
    if predictions and set(terminals) - matched:
        raise ToolError(key="terminal.chain_missing_local", params={"chains": ", ".join(sorted(set(terminals) - matched))},
                        next_step="charmm-gui-cli build -h")


def load_build_config(path):
    path = Path(path).expanduser()
    context = {"path": str(path), "cwd": str(Path.cwd())}
    try:
        from ..store.paths import decode
        raw = decode(path, yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader))
    except (yaml.YAMLError, UnicodeError):
        raise ToolError("Invalid build YAML.") from None
    except FileNotFoundError:
        raise ToolError(key="config.missing", params=context, next_step="curl -fL https://raw.githubusercontent.com/X0bang/charmm-gui-cli/main/examples/default-build.yaml -o default-build.yaml") from None
    except PermissionError:
        raise ToolError(key="config.permission", params=context, next_step="charmm-gui-cli build -h") from None
    except IsADirectoryError:
        raise ToolError(key="config.directory", params=context, next_step="charmm-gui-cli build -h") from None
    except OSError:
        raise ToolError(key="config.unreadable", params=context, next_step="charmm-gui-cli build -h") from None
    raw = mapping(raw, ["version", "protein", "ligand", "ligand_resname", "ligand_hydrogens", "ligand_smiles",
                        "preparation", "membrane", "ions", "input_options"], "build configuration")
    if type(raw.get("version")) is not int or raw["version"] != 2:
        raise ToolError("Input-oriented build configuration must use version: 2.")
    result = dict(raw)
    if 'ligand_smiles' in raw and (not isinstance(raw['ligand_smiles'], str) or not raw['ligand_smiles'].strip()):
        raise ToolError(key='input.pdbqt_definition', next_step='charmm-gui-cli build -h')
    if "input_options" in raw:
        from ..chem.residues import validate_input_options
        result["input_options"] = validate_input_options(raw["input_options"])
    for key in ("protein", "ligand"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ToolError(f"{key} must specify an input file.")
        result[key] = str((path.parent / value).resolve())
        if not Path(result[key]).is_file():
            raise ToolError(f"The configured {key} file does not exist.")
    if 'ligand_smiles' in result and Path(result['ligand']).suffix.lower() != '.pdbqt':
        raise ToolError(key='input.ligand_competing_definition', next_step='charmm-gui-cli build -h')
    result["ligand_resname"] = raw.get("ligand_resname", "LIG")
    if (not isinstance(result["ligand_resname"], str)
            or not re.fullmatch(r"[A-Z][A-Z0-9]{0,2}", result["ligand_resname"])):
        raise ToolError("ligand_resname must be 1-3 uppercase letters/digits beginning with a letter, e.g. LIG.")
    result["ligand_hydrogens"] = raw.get("ligand_hydrogens", "preserve")
    if not isinstance(result["ligand_hydrogens"], str) or result["ligand_hydrogens"] not in ("preserve", "add_missing"):
        raise ToolError("ligand_hydrogens must be preserve or add_missing.")
    result["preparation"] = validate_preparation(raw.get("preparation", {}))
    # Reuse the tested membrane/ion validators without treating a dummy job as
    # evidence of remote preparation. A real ID is supplied only after step 3.
    normalized = api_config(result, None)
    result["membrane"], result["ions"] = normalized["membrane"], normalized["ions"]
    return result


def api_config(config, source_job_id, input_pdb=None, *, existing=False):
    return validate_config({
        "version": 1, "source_job_id": source_job_id, "input_pdb": input_pdb,
        "expected_ligands": [config["ligand_resname"]],
        "preparation": {"ligand_parameters_ready": source_job_id is not None,
                        "orientation": config["preparation"]["orientation"]},
        "membrane": config.get("membrane"), "ions": config.get("ions", {}),
    }, existing=existing)
