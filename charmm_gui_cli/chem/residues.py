"""Residue identities used by normalization, inventory and local safety checks."""

BACKBONE = {"N", "CA", "C", "O"}
SIDECHAINS = {name: set(atoms.split()) for name, atoms in {
    "ALA": "CB", "ARG": "CB CG CD NE CZ NH1 NH2", "ASN": "CB CG OD1 ND2", "ASP": "CB CG OD1 OD2",
    "CYS": "CB SG", "GLN": "CB CG CD OE1 NE2", "GLU": "CB CG CD OE1 OE2", "GLY": "",
    "HIS": "CB CG ND1 CD2 CE1 NE2", "ILE": "CB CG1 CG2 CD1", "LEU": "CB CG CD1 CD2",
    "LYS": "CB CG CD CE NZ", "MET": "CB CG SD CE", "PHE": "CB CG CD1 CD2 CE1 CE2 CZ",
    "PRO": "CB CG CD", "SER": "CB OG", "THR": "CB OG1 CG2", "TRP": "CB CG CD1 CD2 NE1 CE2 CE3 CZ2 CZ3 CH2",
    "TYR": "CB CG CD1 CD2 CE1 CE2 CZ OH", "VAL": "CB CG1 CG2",
}.items()}
ALIASES = {**{name: "HIS" for name in ("HSD", "HSE", "HSP", "HID", "HIE", "HIP")},
           "ASH": "ASP", "ASPP": "ASP", "GLH": "GLU", "GLUP": "GLU", "LYN": "LYS", "LSN": "LYS",
           "CYM": "CYS", "CYX": "CYS"}
MODIFIED = {"MSE": "MET", "SEP": "SER", "TPO": "THR", "PTR": "TYR", "HYP": "PRO", "MLY": "LYS",
            "M3L": "LYS", "KCX": "LYS", "CSO": "CYS", "CSD": "CYS", "CME": "CYS", "SEC": "CYS"}
WATERS = frozenset(("HOH", "WAT", "H2O", "DOD", "TIP", "TIP3", "SOL"))
ADDITIVES = frozenset(("GOL", "EDO", "SO4", "PO4", "ACT", "PEG", "PG4", "MPD", "DMS", "TRS", "MES", "EPE", "IMD", "FMT"))
DETERGENTS = frozenset(("OLC", "OLA", "LDA", "BOG", "LMT", "DMU", "PLM", "CLR"))
METALS = frozenset(("Li", "Na", "K", "Rb", "Cs", "Mg", "Ca", "Sr", "Ba", "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Cd", "Hg", "Al", "Cr"))
CGENFF_ELEMENTS = frozenset(("H", "C", "N", "O", "S", "P", "F", "Cl", "Br", "I"))
INPUT_FIELDS = ("altloc", "drop_hetero", "keep_mse", "drop_additives", "model", "ligand_pose", "split_chain_breaks")


def options_from_args(args):
    return {key: getattr(args, key) for key in INPUT_FIELDS if getattr(args, key, None) is not None}


def atom_alias(name, residue):
    if name == "OT1":
        return "O"
    if name == "CD" and ALIASES.get(residue, residue) == "ILE":
        return "CD1"
    return name


def validate_input_options(raw=None):
    from ..store.auth import ToolError
    if raw is None:
        raw = {}
    allowed = set(INPUT_FIELDS)
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ToolError(key="input.options_invalid", next_step="charmm-gui-cli check -h")
    result = {"altloc": "highest", "drop_hetero": [], "keep_mse": False, "drop_additives": False, **raw}
    import re
    if not isinstance(result["altloc"], str) or not (result["altloc"] == "highest" or re.fullmatch(r"[A-Za-z0-9]", result["altloc"])):
        raise ToolError(key="input.altloc_option", next_step="charmm-gui-cli check -h")
    names = result["drop_hetero"]
    if isinstance(names, str):
        names = names.split(",")
    if not isinstance(names, list) or any(not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,3}", name) for name in names):
        raise ToolError(key="input.drop_option", next_step="charmm-gui-cli check -h")
    if set(names) & (set(SIDECHAINS) | set(ALIASES) | set(MODIFIED)):
        raise ToolError(key="input.drop_polymer", next_step="charmm-gui-cli check -h")
    result["drop_hetero"] = sorted(set(names))
    if "ligand_pose" in result and (type(result["ligand_pose"]) is not int or result["ligand_pose"] < 1):
        raise ToolError(key="input.ligand_pose_option", next_step="charmm-gui-cli check -h")
    if (any(type(result[key]) is not bool for key in ("keep_mse", "drop_additives"))
            or "split_chain_breaks" in result and type(result["split_chain_breaks"]) is not bool
            or "model" in result and (type(result["model"]) is not int or result["model"] < 1)):
        raise ToolError(key="input.options_invalid", next_step="charmm-gui-cli check -h")
    return result
