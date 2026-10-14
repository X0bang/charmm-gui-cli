"""Conservative fixed-column repairs; source structures are never overwritten."""

from collections import Counter
from pathlib import Path
import re
import string

from ..auth import ToolError
from ..i18n import MESSAGES, t


N_TERMINALS = ("NTER", "NNEU", "ACE", "ACP", "NONE", "GLYP", "PROP")
C_TERMINALS = ("CTER", "CNEU", "CT1", "CT2", "CT3", "NONE")
_IONS = {"FE": "Fe", "FE2": "Fe", "ZN": "Zn", "MG": "Mg", "MN": "Mn",
         "CA": "Ca", "CU": "Cu", "CO": "Co", "NI": "Ni", "SOD": "Na",
         "CLA": "Cl", "POT": "K", "CAL": "Ca"}
_CHAIN_IDS = string.ascii_uppercase + string.ascii_lowercase + string.digits


def finding(key, **params):
    return {"key": key, "params": params, "message": MESSAGES[key]["en"].format(**params)}


def normalize_lines(path, report=None, *, options=None, role="generic"):
    if Path(path).suffix.lower() in (".cif", ".mmcif"):
        raise ToolError(key="input.mmcif", next_step=t("input.convert_pdb_next"))
    lines = Path(path).read_text().splitlines()
    from .residues import validate_input_options, WATERS
    from .structure_policy import select_model, apply_policy
    policy = validate_input_options(options) if options is not None else None
    def polymer(line):
        return line.startswith("ATOM  ") or (policy is not None and role in ("protein", "complex", "components")
                                             and line[17:21].strip() == "MSE" and not policy["keep_mse"])
    def removed_water(line):
        return policy is not None and role in ("protein", "complex") and line.startswith("HETATM") and line[17:21].strip() in WATERS
    def removed_record(line):
        from .residues import ADDITIVES
        return removed_water(line) or (policy is not None and role in ("protein", "complex", "components")
                                      and line.startswith("HETATM") and (line[17:21].strip() in policy["drop_hetero"]
                                      or policy["drop_additives"] and line[17:21].strip() in ADDITIVES))
    repairs = []
    lines = select_model(lines, policy.get("model") if policy else None, repairs)
    indexes = [i for i, line in enumerate(lines) if line.startswith(("ATOM  ", "HETATM"))]
    serials = []
    overflow = 0
    for i in indexes:
        if lines[i][6:11] == "*****" and polymer(lines[i]):
            serials.append(None)
            overflow += 1
            continue
        try:
            serials.append(int(lines[i][6:11]))
        except ValueError:
            raise ToolError(key="input.atom_record", params={"line": i + 1}, next_step=t("input.inspect")) from None
    if overflow:
        if any(line.startswith("CONECT") for line in lines):
            raise ToolError(key="input.overflow_conect", next_step=t("input.fix_serials"))
        next_synthetic = max((serial for serial in serials if serial is not None), default=0) + 1
        for index, serial in enumerate(serials):
            if serial is None:
                serials[index] = next_synthetic
                next_synthetic += 1
    duplicated = {value for value, count in Counter(serials).items() if count > 1}
    if duplicated or overflow:
        conect_ids = set()
        for line in lines:
            if line.startswith("CONECT"):
                try:
                    conect_ids.update(int(line[j:j + 5]) for j in range(6, len(line), 5) if line[j:j + 5].strip())
                except ValueError:
                    raise ToolError(key="input.conect_invalid", next_step=t("input.inspect")) from None
        unsafe = duplicated & conect_ids
        hetero_counts = Counter(serial for i, serial in zip(indexes, serials)
                               if lines[i].startswith("HETATM") and not polymer(lines[i]) and not removed_water(lines[i]))
        unsafe.update(serial for serial, count in hetero_counts.items() if count > 1)
        if unsafe:
            raise ToolError(key="input.duplicate_conect", params={"serials": ", ".join(map(str, sorted(unsafe)[:10])) + (" …" if len(unsafe) > 10 else "")},
                            next_step=t("input.fix_serials"))
        occupied = {serial for i, serial in zip(indexes, serials) if not polymer(lines[i])}
        serial_map, next_serial, count = {}, 1, 0
        for i, old in zip(indexes, serials):
            if not polymer(lines[i]):
                serial_map[old] = old
                continue
            while next_serial in occupied:
                next_serial += 1
            if next_serial > 99999:
                raise ToolError(key="input.serial_limit", next_step=t("input.inspect"))
            lines[i] = lines[i][:6] + f"{next_serial:5d}" + lines[i][11:]
            serial_map[old] = next_serial
            next_serial += 1
            count += 1
        # Only unambiguous original serials can have CONECT references here.
        for i, line in enumerate(lines):
            if line.startswith("CONECT"):
                values = [int(line[j:j + 5]) for j in range(6, len(line), 5) if line[j:j + 5].strip()]
                lines[i] = "CONECT" + "".join(f"{serial_map.get(value, value):5d}" for value in values)
        repairs.append(finding("input.overflow_renumbered", count=count) if overflow else
                       finding("input.renumbered", count=count, serials=", ".join(map(str, sorted(duplicated)[:10])) + (" …" if len(duplicated) > 10 else ""), duplicate_count=len(duplicated)))

    retained_indexes = [i for i in indexes if not removed_record(lines[i])]
    occupied = {lines[i][21:22] for i in retained_indexes if lines[i][21:22].strip()}
    groups = list(dict.fromkeys(lines[i][72:76].strip() for i in retained_indexes if not lines[i][21:22].strip()))
    preferred = {segment: segment[-1:] if segment else "A" for segment in groups}
    conflicts = len(set(preferred.values())) != len(preferred)
    mapping = {}
    if "" in groups:
        mapping[""] = "A"
        occupied.add("A")
    for segment in groups:
        if not segment:
            continue
        chain = preferred[segment]
        if conflicts or chain not in _CHAIN_IDS or chain in occupied:
            chain = next((value for value in _CHAIN_IDS if value not in occupied), None)
        if chain is None:
            raise ToolError(key="input.chains_exhausted", next_step=t("input.inspect"))
        mapping[segment] = chain
        occupied.add(chain)
    elements = 0
    chain_counts = Counter()
    residue_atoms = Counter((lines[i][17:27], lines[i][72:76]) for i in indexes)
    for i in retained_indexes:
        line = lines[i].ljust(80)
        if not line[76:78].strip():
            name = line[12:16].strip()
            if line.startswith("ATOM  "):
                element = re.sub(r"[0-9]", "", name)[:1].upper()
                if element not in "CNOSH" or not element:
                    element = None
            else:
                element = _IONS.get(line[17:21].strip().upper()) if residue_atoms[(lines[i][17:27], lines[i][72:76])] == 1 else None
                if line[17:21].strip() in WATERS and name[:1].upper() in ("O", "H", "D"):
                    element = name[:1].upper()
                elif line[17:21].strip() == "MSE":
                    element = "Se" if name.upper() == "SE" else re.sub(r"[0-9]", "", name)[:1].upper()
                    if element not in ("C", "N", "O", "S", "H", "Se"):
                        element = None
            if element is None:
                raise ToolError(key="input.element_unknown", params={"line": i + 1, "atom": name}, next_step=t("input.inspect"))
            line = line[:76] + f"{element:>2}" + line[78:]
            elements += 1
        if not line[21:22].strip():
            segment = line[72:76].strip()
            line = line[:21] + mapping[segment] + line[22:]
            chain_counts[segment] += 1
        # Keep byte-identical atom text when no fixed-column repair is needed.
        lines[i] = line if line != lines[i].ljust(80) else lines[i]
    if elements:
        repairs.append(finding("input.elements_added", count=elements))
    for segment, count in chain_counts.items():
        repairs.append(finding("input.chains_added", segment=segment or "-", chain=mapping[segment], count=count))
    try:
        if policy is not None:
            lines = apply_policy(lines, policy, role, repairs)
    finally:
        if report is not None:
            report.setdefault("repairs", []).extend(repairs)
            report.setdefault("warnings", []).extend(item["message"] for item in repairs)
            report["automatic_repairs"] = report["repairs"]
            report["terminal_predictions"] = terminal_predictions(lines)
            report["hetatm_present"] = any(line.startswith("HETATM") for line in lines)
    return lines


def attach_source_normalization(prepared, source_report):
    """Preserve original-input repair evidence after a normalized staging pass."""
    from ..store.auth import atomic_json
    manifest = prepared["manifest"]
    normalization = manifest.setdefault("input_normalization", {})
    source = source_report.get("input_normalization", {})
    entries = list(source.get("repairs", []))
    for entry in normalization.get("repairs", []):
        if entry not in entries:
            entries.append(entry)
    normalization.update(repairs=entries, automatic_repairs=entries,
                         warnings=[entry["message"] for entry in entries])
    manifest["automatic_repairs"] = entries
    manifest["source_inputs"] = source_report.get("source_inputs", {})
    manifest["source_evidence"] = source_report.get("source_evidence", {})
    manifest["input_options"] = source_report.get("input_options", manifest.get("input_options", {}))
    if source_report.get("ligand_selection"):
        manifest["ligand_selection"] = source_report["ligand_selection"]
        manifest["ligand"]["ligand_source"] = source_report["ligand_selection"]
        conversion = source_report["ligand_selection"].get("conversion", {})
        if conversion.get("review_required"):
            warning = {"key": "input.mol2_review", "params": {"partial": f"{conversion['partial_charge_sum']:.4f}", "formal": conversion["net_formal_charge"]}}
            manifest["ligand"].setdefault("chemistry_warnings", []).append(warning)
    atomic_json(prepared["files"]["input-manifest.json"], manifest)


def terminal_predictions(lines):
    chains = {}
    for line in lines:
        if line.startswith("ATOM  "):
            chain = line[72:76].strip() or line[21:22].strip()
            resname = line[17:21].strip()
            if chain not in chains:
                chains[chain] = {"chain": chain, "first_resname": resname}
            chains[chain]["last_resname"] = resname
    for value in chains.values():
        value["suggested_n_terminal"] = {"GLY": "GLYP", "PRO": "PROP"}.get(value["first_resname"], "NTER")
    return list(chains.values())
