"""Preserve explicit input residue-state intent; never choose a pH state.

ASPP/GLUP/LSN are CHARMM PRES patches, not RESI residue names. Their upload
residues remain ASP/GLU/LYS and require explicit remote patch verification.
"""

import math
import re

from ..store.auth import ToolError
from ..locales.i18n import t
from .structure_policy import residue, label

# (PDB base residue, intended CHARMM state, application mechanism)
STATES = {"HID": ("HSD", "HSD", "residue"), "HIE": ("HSE", "HSE", "residue"),
          "HIP": ("HSP", "HSP", "residue"), "HSD": ("HSD", "HSD", "residue"),
          "HSE": ("HSE", "HSE", "residue"), "HSP": ("HSP", "HSP", "residue"),
          "ASH": ("ASP", "ASPP", "patch"), "ASPP": ("ASP", "ASPP", "patch"),
          "GLH": ("GLU", "GLUP", "patch"), "GLUP": ("GLU", "GLUP", "patch"),
          "LYN": ("LYS", "LSN", "patch"), "LSN": ("LYS", "LSN", "patch"),
          "CYX": ("CYS", "DISU", "disulfide"), "CYM": ("CYM", "CYM", "residue")}


def identity(value):
    return {"polymer_block": value[0], "chain": value[1], "segment": value[2],
            "resid": int(value[3]), "icode": value[4]}


def identity_key(value):
    return (value.get("polymer_block", 0), value["chain"], value["segment"], value["resid"], value["icode"])


def intent_for(manifest):
    normalization = manifest.get("input_normalization", {})
    if not isinstance(normalization, dict):
        raise ToolError(key="state.intent_invalid", next_step=t("state.intent_next"))
    return normalization.get("protein_state_intent", {})


def require_remote_state_verification(manifest, *, next_command="charmm-gui-cli check -h"):
    """Temporary fail-closed gate until actual PDB/PSF proof is integrated."""
    if validate_intent(intent_for(manifest)):
        raise ToolError(key="state.pipeline_pending", category="unsupported_feature",
                        next_step=t("state.pipeline_next", command=next_command))


def validate_intent(plan):
    """Reject edited/unknown state recipes rather than inventing form fields."""
    if plan is None or plan == {}:
        return {}
    def valid_identity(value):
        return (isinstance(value, dict) and type(value.get("polymer_block")) is int and value["polymer_block"] >= 0
                and isinstance(value.get("chain"), str) and re.fullmatch(r"[A-Za-z0-9]", value["chain"])
                and isinstance(value.get("segment"), str) and re.fullmatch(r"[A-Za-z0-9_-]{0,4}", value["segment"])
                and type(value.get("resid")) is int and -999 <= value["resid"] <= 9999
                and isinstance(value.get("icode"), str) and re.fullmatch(r"[A-Za-z0-9]?", value["icode"]))
    try:
        if (set(plan) != {"schema_version", "residues", "disulfides", "pH_selected", "remote_verified"}
                or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
                or plan["pH_selected"] is not False or plan["remote_verified"] is not False
                or not isinstance(plan["residues"], list) or not 0 < len(plan["residues"]) <= 20000
                or not isinstance(plan["disulfides"], list)):
            raise ValueError
        keys = set()
        for entry in plan["residues"]:
            if (not valid_identity(entry) or set(entry) != {"polymer_block", "chain", "segment", "resid", "icode",
                    "source_resname", "pdb_resname", "state", "method", "hydrogen_renames", "remote_verified"}
                    or STATES[entry["source_resname"]] != (entry["pdb_resname"], entry["state"], entry["method"])
                    or entry["remote_verified"] is not False or not isinstance(entry["hydrogen_renames"], list)
                    or entry["hydrogen_renames"] not in ([], [{"from": "HZ3", "to": "HZ1"}], [{"from": "HZ3", "to": "HZ2"}])
                    or entry["hydrogen_renames"] and entry["source_resname"] != "LYN" or identity_key(entry) in keys):
                raise ValueError
            keys.add(identity_key(entry))
        paired = set()
        declared = {identity_key(entry) for entry in plan["residues"] if entry["method"] == "disulfide"}
        for pair in plan["disulfides"]:
            if (not isinstance(pair, dict) or set(pair) != {"left", "right", "distance_A", "basis", "remote_verified"}
                    or pair["basis"] != "explicit_CYX_unique_distance_pair" or pair["remote_verified"] is not False
                    or not all(valid_identity(pair[side]) and set(pair[side]) == {"polymer_block", "chain", "segment", "resid", "icode"}
                               for side in ("left", "right"))
                    or isinstance(pair["distance_A"], bool) or not isinstance(pair["distance_A"], (int, float))
                    or not 1.8 <= pair["distance_A"] <= 2.3):
                raise ValueError
            left, right = identity_key(pair["left"]), identity_key(pair["right"])
            if left == right or left in paired or right in paired or not {left, right} & declared:
                raise ValueError
            paired.update((left, right))
        if not declared <= paired:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ToolError(key="state.intent_invalid", next_step=t("state.intent_next")) from None
    return plan


def _xyz(line):
    return tuple(float(line[start:start + 8]) for start in (30, 38, 46))


def normalize_states(lines, repairs):
    """Map names after model/altloc/chain selection; retain all coordinates."""
    from .pdb_normalization import finding
    groups, block = {}, 0
    for index, line in enumerate(lines):
        if line[:6].strip() == "TER":
            block += 1
        if line.startswith("ATOM  "):
            groups.setdefault(residue(line, block), []).append(index)
    requested = {key: STATES[key[5]] for key in groups if key[5] in STATES}
    plan = {"schema_version": 1, "residues": [], "disulfides": [], "pH_selected": False,
            "remote_verified": False}
    if not requested:
        return lines, plan
    # CYX explicitly declares a disulfide state; distances only identify its
    # possible partner. Ordinary CYS pairs are not converted by proximity.
    sulfurs = {}
    for key, indexes in groups.items():
        if key[5] not in ("CYS", "CYX"):
            continue
        sg = [index for index in indexes if lines[index][12:16].strip() == "SG"]
        if len(sg) == 1 and lines[sg[0]][76:78].strip().upper() == "S":
            sulfurs[key] = _xyz(lines[sg[0]])
    partners, pairs = {}, set()
    for key, (_, _, method) in requested.items():
        if method != "disulfide":
            continue
        neighbors = [other for other, xyz in sulfurs.items() if other != key and key in sulfurs
                     and 1.8 <= math.dist(sulfurs[key], xyz) <= 2.3]
        if len(neighbors) != 1:
            raise ToolError(key="state.disulfide_ambiguous", params={"residue": label(key), "count": len(neighbors)},
                            next_step=t("state.disulfide_next"))
        other = neighbors[0]
        if other in partners and partners[other] != key or key in partners and partners[key] != other:
            raise ToolError(key="state.disulfide_ambiguous", params={"residue": label(key), "count": len(neighbors)},
                            next_step=t("state.disulfide_next"))
        partners[key], partners[other] = other, key
        pairs.add(tuple(sorted((key, other))))
    for left, right in sorted(pairs):
        for key in (left, right):
            if any(lines[i][12:16].strip() in ("HG", "HG1", "HSG") for i in groups[key]):
                raise ToolError(key="state.hydrogen_conflict", params={"residue": label(key), "state": "DISU"},
                                next_step=t("state.hydrogen_next"))
        plan["disulfides"].append({"left": identity(left), "right": identity(right),
            "distance_A": math.dist(sulfurs[left], sulfurs[right]), "basis": "explicit_CYX_unique_distance_pair",
            "remote_verified": False})
        repairs.append(finding("state.disulfide_recorded", left=label(left), right=label(right),
                               distance=f"{math.dist(sulfurs[left], sulfurs[right]):.3f}"))
    if pairs:
        for line in lines:
            if not line.startswith("SSBOND"):
                continue
            endpoints = []
            try:
                for chain_start, resid_start, icode_start in ((15, 17, 21), (29, 31, 35)):
                    matches = [key for key in groups if key[5] in ("CYS", "CYX")
                        and key[1] == line[chain_start:chain_start + 1].strip()
                        and int(key[3]) == int(line[resid_start:resid_start + 4])
                        and key[4] == line[icode_start:icode_start + 1].strip()]
                    if len(matches) != 1:
                        raise ValueError
                    endpoints.append(matches[0])
                left, right = endpoints
                if (left in partners and partners[left] != right or right in partners and partners[right] != left):
                    raise ValueError
            except ValueError:
                raise ToolError(key="state.ssbond_conflict", next_step=t("state.disulfide_next")) from None
    for key, (base, target, method) in requested.items():
        indexes = groups[key]
        names = {lines[index][12:16].strip() for index in indexes}
        forbidden = {"HSD": {"HE2"}, "HSE": {"HD1"}, "ASPP": {"HD1"}, "GLUP": {"HE1"},
                     "CYM": {"HG", "HG1", "HSG"}}.get(target, set())
        if names & forbidden or target == "LSN" and ({"HZ1", "HZ2", "HZ3"} <= names or key[5] == "LSN" and "HZ3" in names):
            raise ToolError(key="state.hydrogen_conflict", params={"residue": label(key), "state": target},
                            next_step=t("state.hydrogen_next"))
        hydrogen_renames = []
        # Amber LYN commonly has HZ2/HZ3; CHARMM LSN keeps HZ1/HZ2.
        # Rename only an unambiguous existing atom; never remove/add a proton.
        if key[5] == "LYN" and "HZ3" in names:
            hydrogen_renames = [{"from": "HZ3", "to": "HZ1" if "HZ1" not in names else "HZ2"}]
            if any(lines[index][76:78].strip().upper() not in ("H", "D", "T")
                   for index in indexes if lines[index][12:16].strip() == "HZ3"):
                raise ToolError(key="state.hydrogen_conflict", params={"residue": label(key), "state": target},
                                next_step=t("state.hydrogen_next"))
        for index in indexes:
            line = lines[index]
            if hydrogen_renames and line[12:16].strip() == "HZ3":
                line = line[:12] + f"{hydrogen_renames[0]['to']:>4}" + line[16:]
            if key[5] != base:
                line = line[:17] + f"{base:<4}" + line[21:]
            lines[index] = line
        entry = {**identity(key), "source_resname": key[5], "pdb_resname": base, "state": target,
                 "method": method, "hydrogen_renames": hydrogen_renames, "remote_verified": False}
        plan["residues"].append(entry)
        if key[5] != base or hydrogen_renames:
            repair = finding("state.mapped", residue=label(key), original=key[5], base=base, state=target)
            repair["state_intent"] = entry
            repairs.append(repair)
        for rename in hydrogen_renames:
            repairs.append(finding("state.hydrogen_renamed", residue=label(key), original=rename["from"], target=rename["to"]))
    # Existing SSBOND identities are retained; renamed CYX endpoints must still
    # identify the same input cysteines, not a new guessed chain or residue.
    for index, line in enumerate(lines):
        if line.startswith("SSBOND"):
            if line[11:14] == "CYX":
                line = line[:11] + "CYS" + line[14:]
            if line[25:28] == "CYX":
                line = line[:25] + "CYS" + line[28:]
            lines[index] = line
    return lines, validate_intent(plan)
