"""Check coordinate-column boundaries before interpreting IDs or duplicates.

CHARMM's four-character residue names are retained. No whitespace-token repair,
hybrid-36 decoding or residue renumbering is attempted here.
"""

import math
import re

from ..store.auth import ToolError
from ..locales.i18n import t

FIELD_LABELS = {"record": "ATOM/HETATM", "serial": "serial (7–11)", "residue": "resSeq (23–26)",
                "identity": "name/resName (13–21)", "coordinates": "x/y/z (31–54)"}


def validate_layout(lines, *, line_numbers=None):
    for index, line in enumerate(lines):
        record = line.split(maxsplit=1)[0] if line.strip() else ""
        if record not in ("ATOM", "HETATM") and not line.lstrip().startswith(("ATOM", "HETATM")):
            continue
        number = line_numbers[index] if line_numbers is not None else index + 1
        def reject(field):
            raise ToolError(key="input.pdb_columns", params={"line": number, "field": FIELD_LABELS[field]},
                            next_step=t("input.pdb_columns_next"))
        if len(line) < 54 or not line.startswith(("ATOM  ", "HETATM")):
            reject("record")
        if line[11] != " ":
            if re.fullmatch(r"[0-9]{6}", line[6:12]):
                raise ToolError(key="input.serial_limit", next_step=t("input.pdb_large_next"))
            reject("serial")
        serial, resid = line[6:11].strip(), line[22:26].strip()
        for field, value in (("serial", serial), ("residue", resid)):
            width = 5 if field == "serial" else 4
            if len(value) == width and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", value):
                raise ToolError(key="input.pdb_hybrid36", params={"line": number, "field": FIELD_LABELS[field]},
                                next_step=t("input.pdb_large_next"))
            if not re.fullmatch(r"[+-]?[0-9]+", value) and not (field == "serial" and value == "*****"):
                reject(field)
        if line[26:27].isdigit() and len(line[22:26].strip()) == 4:
            raise ToolError(key="input.pdb_residue_width", params={"line": number}, next_step=t("input.pdb_large_next"))
        if not line[12:16].strip() or not re.fullmatch(r"[A-Za-z0-9]{1,4}", line[17:21].strip()) or line[27:30].strip():
            reject("identity")
        try:
            values = [float(line[left:left + 8]) for left in (30, 38, 46)]
        except ValueError:
            reject("coordinates")
        if not all(math.isfinite(value) for value in values):
            raise ToolError(key="input.pdb_coordinates", params={"line": number}, next_step=t("input.pdb_columns_next"))
