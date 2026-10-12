"""Input-oriented configuration for the HTTP preparation + API build workflow."""

from pathlib import Path
import re

import yaml

from .auth import ToolError
from .config import UniqueLoader, mapping, validate_config
from .pdb_normalization import N_TERMINALS, C_TERMINALS


def load_build_config(path):
    path = Path(path).expanduser()
    context = {"path": str(path), "cwd": str(Path.cwd())}
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
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
    raw = mapping(raw, ["version", "protein", "ligand", "ligand_resname", "ligand_hydrogens",
                        "preparation", "membrane", "ions"], "build configuration")
    if type(raw.get("version")) is not int or raw["version"] != 2:
        raise ToolError("Input-oriented build configuration must use version: 2.")
    result = dict(raw)
    for key in ("protein", "ligand"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ToolError(f"{key} must specify an input file.")
        result[key] = str((path.parent / value).resolve())
        if not Path(result[key]).is_file():
            raise ToolError(f"The configured {key} file does not exist.")
    result["ligand_resname"] = raw.get("ligand_resname", "LIG")
    if (not isinstance(result["ligand_resname"], str)
            or not re.fullmatch(r"[A-Z][A-Z0-9]{0,2}", result["ligand_resname"])):
        raise ToolError("ligand_resname must be 1-3 uppercase letters/digits beginning with a letter, e.g. LIG.")
    result["ligand_hydrogens"] = raw.get("ligand_hydrogens", "preserve")
    if not isinstance(result["ligand_hydrogens"], str) or result["ligand_hydrogens"] not in ("preserve", "add_missing"):
        raise ToolError("ligand_hydrogens must be preserve or add_missing.")
    prep = mapping(raw.get("preparation", {}), ["n_terminal", "c_terminal", "orientation"], "preparation")
    result["preparation"] = {"n_terminal": prep.get("n_terminal", "NTER"),
                             "c_terminal": prep.get("c_terminal", "CTER"),
                             "orientation": prep.get("orientation", "ppm")}
    if (not isinstance(result["preparation"]["n_terminal"], str)
            or result["preparation"]["n_terminal"] not in N_TERMINALS):
        raise ToolError("Unsupported N-terminal patch.")
    if (not isinstance(result["preparation"]["c_terminal"], str)
            or result["preparation"]["c_terminal"] not in C_TERMINALS):
        raise ToolError("Unsupported C-terminal patch.")
    # Reuse the tested membrane/ion validators without treating a dummy job as
    # evidence of remote preparation. A real ID is supplied only after step 3.
    normalized = api_config(result, None)
    result["membrane"], result["ions"] = normalized["membrane"], normalized["ions"]
    return result


def api_config(config, source_job_id, input_pdb=None):
    return validate_config({
        "version": 1, "source_job_id": source_job_id, "input_pdb": input_pdb,
        "expected_ligands": [config["ligand_resname"]],
        "preparation": {"ligand_parameters_ready": source_job_id is not None,
                        "orientation": config["preparation"]["orientation"]},
        "membrane": config.get("membrane"), "ions": config.get("ions", {}),
    })
