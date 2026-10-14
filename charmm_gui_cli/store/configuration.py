"""Strict YAML and shared system-configuration validation."""

from difflib import get_close_matches
import math
from pathlib import Path
import re
import shlex
import yaml
from .auth import ToolError
from .identifiers import job_id
from ..chem.lipids import KNOWN_LIPIDS
from ..locales.i18n import t


def bind_config_error(error, path, *, command="build"):
    """Add file context without reflecting YAML values or changing recovery."""
    if error.category != "operation_error" or not error.key or not error.key.startswith(("config.", "terminal.", "build.input_options_")):
        return error
    if error.key in ("config.missing", "config.permission", "config.directory", "config.unreadable"):
        return error
    path = Path(path).expanduser().resolve()
    context = {"key": "config.location", "params": {"path": str(path), "cwd": str(Path.cwd())}}
    error.details = [item for item in error.details if not isinstance(item, dict) or item.get("key") != "config.location"] + [context]
    args = ["charmm-gui-cli", command, str(path)]
    if command == "build":
        args.append("--dry-run")
    error.next_step = t("config.recheck", path=str(path), command=shlex.join(args))
    return error


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ToolError(key="config.unique_keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def mapping(value, keys, name):
    if not isinstance(value, dict) or set(value) - set(keys):
        key = {"build configuration": "config.build_fields", "configuration": "config.root_fields"}.get(name, "config.fields")
        params = {"allowed": ", ".join(keys), **({"name": name} if key == "config.fields" else {})}
        raise ToolError(key=key, params=params)
    return value


def number(value, name, minimum=0, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum or (positive and value == 0)):
        raise ToolError(key="config.positive" if positive else "config.nonnegative", params={"name": name})
    return value


def composition(value, *, allow_unknown=False):
    if not isinstance(value, str) or value.count("=") != 1:
        raise ToolError(key="config.lipid_syntax", next_step=t("config.lipid_next"))
    names, amounts = (part.split(":") for part in value.split("="))
    if len(names) != len(amounts) or len(set(names)) != len(names):
        raise ToolError(key="config.lipid_lengths", next_step=t("config.lipid_next"))
    if not all(re.fullmatch(r"[A-Za-z0-9_+-]+", name) for name in names):
        raise ToolError(key="config.lipid_name", next_step=t("config.lipid_next"))
    for name in names:
        if name not in KNOWN_LIPIDS and not allow_unknown:
            candidates = ", ".join(get_close_matches(name.upper(), sorted(KNOWN_LIPIDS), n=4)) or "POPC, POPE"
            raise ToolError(key="config.unknown_lipid", params={"name": name, "candidates": candidates},
                            next_step=t("config.lipid_next"))
    try:
        for amount in amounts:
            number(float(amount), "Lipid ratio", positive=True)
    except ToolError as error:
        raise ToolError(str(error), key="config.lipid_positive", next_step=t("config.lipid_next")) from None
    except ValueError:
        raise ToolError(key="config.lipid_numeric", next_step=t("config.lipid_next")) from None
    return value


def validate_config(raw, *, existing=False):
    raw = mapping(raw, ["version", "source_job_id", "input_pdb", "expected_ligands",
                        "preparation", "membrane", "ions"], "configuration")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ToolError(key="config.version_one")
    source = raw.get("source_job_id")
    if source is not None:
        job_id(source)
    ligands = raw.get("expected_ligands")
    if (not isinstance(ligands, list) or not ligands
            or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9]{1,4}", x) for x in ligands)
            or len(set(ligands)) != len(ligands)):
        raise ToolError(key="config.expected_ligands")
    prep = mapping(raw.get("preparation"), ["ligand_parameters_ready", "orientation"], "preparation")
    if type(prep.get("ligand_parameters_ready")) is not bool:
        raise ToolError(key="config.parameters_ready")
    if prep.get("orientation") not in ("prepared", "ppm"):
        raise ToolError(key="config.orientation")
    membrane = mapping(raw.get("membrane"), ["upper", "lower", "margin_A", "water_padding_A", "allow_unknown_lipids"], "membrane")
    allow_unknown = membrane.get("allow_unknown_lipids", False)
    if type(allow_unknown) is not bool:
        raise ToolError(key="config.unknown_lipid_flag", next_step="charmm-gui-cli build -h")
    upper, lower = composition(membrane.get("upper"), allow_unknown=allow_unknown or existing), composition(membrane.get("lower"), allow_unknown=allow_unknown or existing)
    margin = number(membrane.get("margin_A"), "margin_A", positive=True)
    water = number(membrane.get("water_padding_A", 22.5), "water_padding_A", positive=True)
    ions = mapping(raw.get("ions", {}), ["type", "concentration_M"], "ions")
    ion_type = ions.get("type", "NaCl")
    if not isinstance(ion_type, str) or not re.fullmatch(r"[A-Za-z0-9+-]+", ion_type):
        raise ToolError(key="config.ion_type")
    concentration = number(ions.get("concentration_M", 0.15), "concentration_M")
    pdb = raw.get("input_pdb")
    if pdb is not None and (not isinstance(pdb, str) or not pdb):
        raise ToolError(key="config.input_pdb")
    return {
        "version": 1, "source_job_id": source, "input_pdb": pdb,
        "expected_ligands": ligands, "preparation": dict(prep),
        "membrane": {"upper": upper, "lower": lower, "margin_A": margin, "water_padding_A": water,
                     **({"allow_unknown_lipids": True} if allow_unknown else {})},
        "ions": {"type": ion_type, "concentration_M": concentration},
    }
