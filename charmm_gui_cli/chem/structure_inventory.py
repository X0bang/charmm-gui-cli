"""Read-only chain and LINK inventories, never domain or bond inference.

Names are conservative local classification hints, not a chemical dictionary.
COMPND declarations are depositor-provided text, not sequence assignments.
"""

from collections import Counter

from .residues import SIDECHAINS, ALIASES, MODIFIED, WATERS, ADDITIVES, DETERGENTS

NUCLEIC_NAMES = frozenset(("A", "C", "G", "U", "I", "DA", "DC", "DG", "DT", "DI"))
GLYCAN_NAMES = frozenset(("NAG", "NDG", "MAN", "BMA", "FUC", "FUL", "GAL", "GLC", "BGC", "SIA", "NAN"))


def residue_kind(name, record):
    if name in SIDECHAINS or name in ALIASES:
        return "protein"
    if name in MODIFIED:
        return "modified_protein"
    if name in NUCLEIC_NAMES:
        return "nucleic_acid"
    if name in GLYCAN_NAMES:
        return "glycan"
    if name in WATERS:
        return "water"
    if name in ADDITIVES:
        return "additive"
    if name in DETERGENTS:
        return "detergent"
    return "unknown" if record == "ATOM" else "heterogen"


def _declarations(lines):
    # Header text cannot grow terminal output or create an unbounded token list.
    chunks, length, truncated = [], 0, False
    for line in lines:
        if line[:6] != "COMPND":
            continue
        chunk = line[10:80].strip()
        if length + len(chunk) + 1 > 131072:
            truncated = True
            break
        chunks.append(chunk)
        length += len(chunk) + 1
    entities, current = [], None
    for token in " ".join(chunks).split(";"):
        key, separator, value = token.strip().partition(":")
        if not separator:
            continue
        key, value = key.strip().upper(), " ".join(value.split())
        if key == "MOL_ID":
            current = {"mol_id": value[:64], "molecules": [], "fragments": [], "chains": []}
            entities.append(current)
        elif current is not None and key in ("MOLECULE", "FRAGMENT"):
            field = "molecules" if key == "MOLECULE" else "fragments"
            if value and len(current[field]) < 20:
                current[field].append(value[:300])
        elif current is not None and key == "CHAIN":
            current["chains"].extend(value.strip() if value.strip() != "NULL" else "" for value in value.split(","))
    return entities, truncated


def link_records(lines):
    """Extract fixed-column annotations without validating their chemistry."""
    result = []
    for number, line in enumerate(lines, 1):
        if not line.startswith("LINK  "):
            continue
        endpoints = []
        for offset in (0, 30):
            endpoints.append({"atom": line[12 + offset:16 + offset].strip(),
                              "altloc": line[16 + offset:17 + offset].strip(),
                              "resname": line[17 + offset:20 + offset].strip(),
                              "chain": line[21 + offset:22 + offset].strip(),
                              "residue_number": line[22 + offset:26 + offset].strip(),
                              "insertion_code": line[26 + offset:27 + offset].strip()})
        result.append({"line": number, "endpoints": endpoints,
                       "glycan_name_hint": any(item["resname"] in GLYCAN_NAMES for item in endpoints),
                       "symmetry": [line[59:65].strip(), line[66:72].strip()],
                       "connectivity_verified": False})
    return result


def endpoint_label(endpoint):
    return " ".join((endpoint["resname"] or "?", endpoint["chain"] or "-",
                     endpoint["residue_number"] + endpoint["insertion_code"], endpoint["atom"] or "?"))


def inspect_chains(lines):
    """Keep models, TER blocks, chains and segments distinct; count altlocs once."""
    declarations, truncated = _declarations(lines)
    groups, model, block = {}, 1, 0
    model_count = 0
    for line in lines:
        record = line[:6].strip()
        if record == "MODEL":
            model_count += 1
            model, block = model_count, 0
        elif record == "TER":
            block += 1
        if record not in ("ATOM", "HETATM"):
            continue
        chain, segment = line[21:22].strip(), line[72:76].strip()
        key = (model, block, chain, segment)
        group = groups.setdefault(key, {"model": model, "polymer_block": block, "chain": chain,
                                       "segment": segment, "coordinate_records": 0, "residues": {}})
        group["coordinate_records"] += 1
        identity = (line[22:26].strip(), line[26:27].strip(), line[17:21].strip())
        residue = group["residues"].setdefault(identity, {"residue_number": int(identity[0]), "insertion_code": identity[1],
            "resname": identity[2], "record_types": set(), "atom_names": set()})
        residue["record_types"].add(record)
        residue["atom_names"].add(line[12:16].strip())
    chains = []
    for group in groups.values():
        residues = list(group.pop("residues").values())
        for residue in residues:
            residue["kind"] = residue_kind(residue["resname"], "ATOM" if "ATOM" in residue["record_types"] else "HETATM")
            residue["record_types"] = sorted(residue["record_types"])
            residue["unique_atom_names"] = len(residue.pop("atom_names"))
        group.update(residue_count=len(residues), residues=residues,
                     kind_counts=dict(Counter(residue["kind"] for residue in residues)),
                     residue_name_counts=dict(Counter(residue["resname"] for residue in residues)),
                     declared_molecules=[entity for entity in declarations if group["chain"] in entity["chains"]])
        chains.append(group)
    return {"schema_version": 1, "scope": "provided_coordinate_records", "chains": chains,
            "link_annotations": link_records(lines), "header_truncated": truncated,
            "classification_basis": "local_residue_name_hints", "sequence_assignment_performed": False,
            "domain_identity_verified": False, "coordinates_modified": False}
