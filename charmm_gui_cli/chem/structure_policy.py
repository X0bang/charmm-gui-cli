"""Residue-level input selection, preserving retained coordinates and chemistry."""

from collections import Counter
import math

from ..store.auth import ToolError
from .residues import ALIASES, BACKBONE, SIDECHAINS, WATERS, ADDITIVES, atom_alias


def residue(line, block=0):
    return (block, line[21:22].strip(), line[72:76].strip(), line[22:26].strip(), line[26:27].strip(), line[17:21].strip())


def label(identity):
    return " ".join((identity[5], identity[1] or identity[2] or "-", identity[3] + identity[4]))


def select_model(lines, number, repairs):
    from .pdb_normalization import finding
    starts = [index for index, line in enumerate(lines) if line[:6].strip() == "MODEL"]
    count = len(starts) or 1
    if number is None:
        if count > 1:
            raise ToolError(key="input.models", params={"count": count}, next_step="charmm-gui-cli check -h")
        return lines
    if number > count:
        raise ToolError(key="input.model_range", params={"count": count, "number": number}, next_step="charmm-gui-cli check -h")
    if not starts:
        return lines
    selected, current, active = [], 0, False
    for line in lines:
        record = line[:6].strip()
        if record == "MODEL":
            current += 1
            active = True
        elif record == "ENDMDL":
            active = False
        elif active and current == number or not active and record not in ("ATOM", "HETATM", "TER"):
            selected.append(line)
    repairs.append(finding("input.model_selected", number=number, count=count))
    return selected


def apply_policy(lines, options, role, repairs):
    from .pdb_normalization import finding
    if role not in ("protein", "complex", "components", "ligand"):
        return lines
    groups, block = {}, 0
    for index, line in enumerate(lines):
        if line[:6].strip() == "TER":
            block += 1
        if line.startswith(("ATOM  ", "HETATM")):
            groups.setdefault(residue(line, block), []).append(index)
    removed, converted, water, dropped = set(), [], [], []
    for identity, indexes in groups.items():
        name = identity[5]
        if role in ("protein", "complex") and name in WATERS and all(lines[i].startswith("HETATM") for i in indexes):
            removed.update(indexes)
            water.append(identity)
        elif role != "ligand" and (name in options["drop_hetero"] or options["drop_additives"] and name in ADDITIVES) and all(lines[i].startswith("HETATM") for i in indexes):
            removed.update(indexes)
            dropped.append(identity)
        elif role != "ligand" and name == "MSE" and not options["keep_mse"]:
            for index in indexes:
                line = lines[index].ljust(80)
                atom = line[12:16].strip()
                line = "ATOM  " + line[6:17] + "MET " + line[21:]
                if atom == "SE":
                    line = line[:12] + " SD " + line[16:76] + " S" + line[78:]
                lines[index] = line
            converted.append(identity)
    for key, identities in (("input.waters_removed", water), ("input.hetero_removed", dropped), ("input.mse_converted", converted)):
        if identities:
            entry = finding(key, count=len(identities), residues=", ".join(label(value) for value in identities[:20]))
            entry["residues"] = [{"resname": value[5], "chain": value[1], "segment": value[2], "resid": value[3], "icode": value[4]} for value in identities]
            repairs.append(entry)
    for identity, indexes in groups.items():
        indexes = [i for i in indexes if i not in removed]
        alternates = sorted({lines[i][16:17] for i in indexes if lines[i][16:17].strip()})
        if not alternates:
            continue
        shared = [i for i in indexes if not lines[i][16:17].strip()]
        name = lines[indexes[0]][17:21].strip()
        template = ALIASES.get(name, name)
        required = BACKBONE | SIDECHAINS[template] if template in SIDECHAINS else {
            lines[i][12:16].strip() for i in indexes if lines[i][76:78].strip().upper() not in ("H", "D", "T")}
        candidates = []
        missing_by_label = {}
        for alternate in alternates:
            chosen = shared + [i for i in indexes if lines[i][16:17] == alternate]
            names = [atom_alias(lines[i][12:16].strip(), name) for i in chosen if lines[i][76:78].strip().upper() not in ("H", "D", "T")]
            missing = sorted(required - set(names))
            duplicates = [atom for atom, count in Counter(names).items() if count > 1]
            missing_by_label[alternate] = ", ".join(missing or duplicates)
            score = 0
            try:
                for i in chosen:
                    occupancy = float(lines[i][54:60].strip() or "1")
                    if not math.isfinite(occupancy) or not 0 <= occupancy <= 1:
                        raise ValueError
                    score += occupancy
            except ValueError:
                raise ToolError(key="input.occupancy", params={"residue": label(identity)}, next_step="charmm-gui-cli check -h") from None
            if not missing and not duplicates:
                candidates.append((-score, alternate, chosen))
        forced = options["altloc"]
        if forced != "highest":
            candidates = [entry for entry in candidates if entry[1] == forced]
        if not candidates:
            missing = missing_by_label.get(forced) if forced != "highest" else "; ".join(f"{key}: {value}" for key, value in missing_by_label.items())
            raise ToolError(key="input.altloc_incomplete", params={"residue": label(identity), "choice": forced, "missing": missing or ", ".join(sorted(required))},
                            next_step="charmm-gui-cli check -h")
        _, alternate, chosen = sorted(candidates)[0]
        removed.update(set(indexes) - set(chosen))
        for index in chosen:
            line = lines[index].ljust(80)
            lines[index] = line[:16] + " " + line[17:54] + "  1.00" + line[60:]
        entry = finding("input.altloc_selected", residue=label(identity), altloc=alternate, policy=forced)
        entry.update(residue={"resname": name, "chain": identity[1], "segment": identity[2], "resid": identity[3], "icode": identity[4]},
                     removed_atoms=len(indexes) - len(chosen), shared_atoms=len(shared), independent_residue_choice=True)
        repairs.append(entry)
    dropped_serials = {lines[i][6:11] for i in removed} - {line[6:11] for i, line in enumerate(lines) if i not in removed and line.startswith(("ATOM  ", "HETATM"))}
    selected = []
    for index, line in enumerate(lines):
        if index in removed:
            continue
        if line.startswith("CONECT"):
            ids = [line[i:i + 5] for i in range(6, len(line), 5) if line[i:i + 5].strip()]
            if ids and ids[0] in dropped_serials:
                continue
            ids = [item for item in ids if item not in dropped_serials]
            if len(ids) < 2:
                continue
            line = "CONECT" + "".join(ids)
        selected.append(line)
    return selected
