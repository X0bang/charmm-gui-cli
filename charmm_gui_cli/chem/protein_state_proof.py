"""Narrow CHARMM36 residue-state proof from actual PDB/PSF atoms and bonds.

These signatures concern explicit side-chain states, not total residue charge
(which terminal patches change), full force-field quality or equilibration.
"""

import hashlib
import json
import math
from pathlib import Path
import re

from ..store.auth import ToolError
from ..locales.i18n import t
from .protein_states import validate_intent, identity_key

# CHARMM36 top_all36_prot.rtf state-distinguishing sites (type, charge).
SIGNATURES = {
    "HSD": {"ND1": ("NR1", -.36), "NE2": ("NR2", -.70), "HD1": ("H", .32)},
    "HSE": {"ND1": ("NR2", -.70), "NE2": ("NR1", -.36), "HE2": ("H", .32)},
    "HSP": {"ND1": ("NR3", -.51), "NE2": ("NR3", -.51), "HD1": ("H", .44), "HE2": ("H", .44)},
    "ASPP": {"OD1": ("OB", -.55), "OD2": ("OH1", -.61), "HD2": ("H", .44)},
    "GLUP": {"OE1": ("OB", -.55), "OE2": ("OH1", -.61), "HE2": ("H", .44)},
    "LSN": {"NZ": ("NH2", -.96), "HZ1": ("HC", .34), "HZ2": ("HC", .34)},
    "CYM": {"SG": ("SS", -.80)}, "DISU": {"SG": ("SM", -.08)},
}
PROTONS = {"HSD": {"ND1": ["HD1"], "NE2": []}, "HSE": {"ND1": [], "NE2": ["HE2"]},
           "HSP": {"ND1": ["HD1"], "NE2": ["HE2"]}, "ASPP": {"OD1": [], "OD2": ["HD2"]},
           "GLUP": {"OE1": [], "OE2": ["HE2"]}, "LSN": {"NZ": ["HZ1", "HZ2"]},
           "CYM": {"SG": []}, "DISU": {"SG": []}}
FORBIDDEN = {"HSD": {"HE2"}, "HSE": {"HD1"}, "HSP": set(), "ASPP": {"HD1"},
             "GLUP": {"HE1"}, "LSN": {"HZ3"}, "CYM": {"HG", "HG1", "HSG"},
             "DISU": {"HG", "HG1", "HSG"}}


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def failure():
    return ToolError(key="state.proof_invalid", category="protein_state_unverified", next_step=t("state.proof_next"))


def read_psf(path):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        starts = [i for i, line in enumerate(lines) if re.search(r"!NATOM\b", line)]
        bonds = [i for i, line in enumerate(lines) if re.search(r"!NBOND\b", line)]
        if len(starts) != 1 or len(bonds) != 1:
            raise ValueError
        start, bond_start = starts[0], bonds[0]
        count = int(lines[start].split()[0])
        if not 0 < count <= 2_000_000 or start + count >= bond_start:
            raise ValueError
        atoms = []
        for number, line in enumerate(lines[start + 1:start + 1 + count], 1):
            fields = line.split()
            if len(fields) < 8 or int(fields[0]) != number:
                raise ValueError
            charge, mass = float(fields[6]), float(fields[7])
            if not math.isfinite(charge) or not math.isfinite(mass) or mass <= 0:
                raise ValueError
            atoms.append({"serial": number, "segment": fields[1], "resid": fields[2], "resname": fields[3],
                          "name": fields[4], "type": fields[5], "charge": charge, "mass": mass})
        expected = int(lines[bond_start].split()[0])
        if not 0 <= expected <= 8_000_000:
            raise ValueError
        values = []
        for line in lines[bond_start + 1:]:
            if "!" in line:
                break
            values.extend(map(int, line.split()))
            if len(values) > 2 * expected:
                raise ValueError
        if len(values) != 2 * expected:
            raise ValueError
        edges = {tuple(sorted(pair)) for pair in zip(values[::2], values[1::2])}
        if len(edges) != expected or any(a == b or not 1 <= a <= count or not 1 <= b <= count for a, b in edges):
            raise ValueError
        return atoms, edges
    except (ValueError, IndexError, UnicodeError):
        raise failure() from None


def read_pdb(path):
    try:
        atoms, block = [], 0
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line[:6].strip() == "TER":
                block += 1
            if line.startswith(("ATOM  ", "HETATM")):
                xyz = [float(line[start:start + 8]) for start in (30, 38, 46)]
                if not all(math.isfinite(value) for value in xyz):
                    raise ValueError
                atoms.append({"segment": line[72:76].strip(), "chain": line[21:22].strip(),
                    "resid": line[22:27].strip(), "resname": line[17:21].strip(), "name": line[12:16].strip(),
                    "element": line[76:78].strip().upper(), "polymer_block": block})
        return atoms
    except (ValueError, UnicodeError):
        raise failure() from None


def verify_states(plan, mapping, pdb, psf, *, reference=None):
    """Prove every requested state and unique S–S bond on these actual files."""
    plan = validate_intent(plan)
    if not plan:
        return {"required": False, "passed": True}
    try:
        if not isinstance(mapping, dict) or set(mapping) != {"residues", "disulfides"}:
            raise ValueError
        requested = {identity_key(entry): entry for entry in plan["residues"]}
        mapped = {identity_key(entry["source"]): entry for entry in mapping["residues"]}
        if len(mapped) != len(mapping["residues"]) or set(mapped) != set(requested):
            raise ValueError
        addresses = []
        for key, entry in mapped.items():
            server = entry["server"]
            if (entry["state"] != requested[key]["state"] or entry["method"] != requested[key]["method"]
                    or not isinstance(server["segment"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", server["segment"])
                    or not isinstance(server["resid"], str) or not re.fullmatch(r"-?[0-9]+[A-Za-z0-9]?", server["resid"])):
                raise ValueError
            addresses.append((server["segment"], server["resid"]))
        if len(set(addresses)) != len(addresses):
            raise ValueError
        atoms, edges = read_psf(psf)
        coordinates = read_pdb(pdb)
        if len(coordinates) != len(atoms) or any((a["segment"], a["resid"], a["resname"], a["name"]) !=
                (b["segment"], b["resid"], b["resname"], b["name"]) for a, b in zip(atoms, coordinates)):
            raise ValueError
        groups, neighbors = {}, {a["serial"]: [] for a in atoms}
        for atom in atoms:
            group = groups.setdefault((atom["segment"], atom["resid"]), {})
            if atom["name"] in group:
                raise ValueError
            group[atom["name"]] = atom
        for a, b in edges:
            neighbors[a].append(atoms[b - 1])
            neighbors[b].append(atoms[a - 1])
        checks = []
        for key, entry in mapped.items():
            state, server = entry["state"], entry["server"]
            group = groups[(server["segment"], server["resid"])]
            base = requested[key]["pdb_resname"]
            allowed = {base, state} if state in ("ASPP", "GLUP", "LSN", "DISU", "CYM") else {state}
            if state == "DISU":
                allowed = {"CYS", "CYX"}
            if state == "CYM":
                allowed = {"CYM", "CYS"}
            if {a["resname"] for a in group.values()} - allowed:
                raise ValueError
            if FORBIDDEN[state] & group.keys():
                raise ValueError
            for name, (atom_type, charge) in SIGNATURES[state].items():
                atom = group[name]
                element = name[0]
                mass = {"H": 1.008, "N": 14.007, "O": 15.9994, "S": 32.06}[element]
                if (atom["type"] != atom_type or abs(atom["charge"] - charge) > 1e-4
                        or abs(atom["mass"] - mass) > .03
                        or coordinates[atom["serial"] - 1]["element"] not in ("", element)):
                    raise ValueError
            for donor, names in PROTONS[state].items():
                actual = neighbors[group[donor]["serial"]]
                hydrogens = [atom for atom in actual if atom["mass"] <= 2.5]
                if {atom["serial"] for atom in hydrogens} != {group[name]["serial"] for name in names}:
                    raise ValueError
                if any(not .99 <= atom["mass"] <= 1.02 for atom in hydrogens):
                    raise ValueError
                if any([neighbor["serial"] for neighbor in neighbors[atom["serial"]]] != [group[donor]["serial"]]
                       for atom in hydrogens):
                    raise ValueError
            checks.append({"source": entry["source"], "server": server, "state": state, "passed": True})
        expected_pairs = {frozenset((identity_key(p["left"]), identity_key(p["right"]))) for p in plan["disulfides"]}
        actual_pairs = set()
        for pair in mapping["disulfides"]:
            keys = frozenset((identity_key(pair["source_left"]), identity_key(pair["source_right"])))
            if keys in actual_pairs:
                raise ValueError
            actual_pairs.add(keys)
            sulfur_atoms = []
            for end, source in (("left", "source_left"), ("right", "source_right")):
                address = pair[end]
                source_key = identity_key(pair[source])
                if source_key in mapped and address != mapped[source_key]["server"]:
                    raise ValueError
                group = groups[(address["segment"], address["resid"])]
                sg = group["SG"]
                if (sg["type"] != "SM" or abs(sg["charge"] + .08) > 1e-4 or abs(sg["mass"] - 32.06) > .03
                        or FORBIDDEN["DISU"] & group.keys() or any(a["mass"] <= 2.5 for a in neighbors[sg["serial"]])):
                    raise ValueError
                sulfur_atoms.append(sg)
            a, b = sulfur_atoms
            if tuple(sorted((a["serial"], b["serial"]))) not in edges:
                raise ValueError
            for left, right in ((a, b), (b, a)):
                if [atom["serial"] for atom in neighbors[left["serial"]] if atom["type"] in ("SM", "S", "SS")] != [right["serial"]]:
                    raise ValueError
        if actual_pairs != expected_pairs:
            raise ValueError
        if reference is not None:
            original = read_pdb(reference)
            endpoints = {identity_key(entry["source"]): entry["server"] for entry in mapping["residues"]}
            for pair in mapping["disulfides"]:
                endpoints.update({identity_key(pair["source_left"]): pair["left"], identity_key(pair["source_right"]): pair["right"]})
            for key, address in endpoints.items():
                _, chain, segment, rid, icode = key
                selected = [a for a in original if a["chain"] == chain and a["segment"] == segment and a["resid"] == str(rid) + icode]
                if not selected or len({atom["polymer_block"] for atom in selected}) != 1:
                    raise ValueError
                source_names = {a["name"] for a in selected if a["element"] not in ("H", "D", "T")}
                target = groups[address["segment"], address["resid"]]
                target_names = {"O" if name == "OT1" else "OXT" if name == "OT2" else name for name in target}
                if not source_names <= target_names:
                    raise ValueError
        return {"required": True, "passed": True, "schema_version": 1, "intent_sha256": canonical(plan),
                "mapping_sha256": canonical(mapping), "residues": checks, "disulfide_count": len(actual_pairs),
                "scope": "CHARMM36 sidechain atom types, charges, hydrogen bonds and explicit disulfide connectivity",
                "scientific_correctness_verified": False}
    except (KeyError, TypeError, ValueError, IndexError):
        raise failure() from None


def derive_mapping(plan, reference, pdb):
    """Read-only fallback for standalone artifact inspection, never submission."""
    plan = validate_intent(plan)
    coordinates = read_pdb(pdb)
    def locate(source):
        rid = str(source["resid"]) + source["icode"]
        candidates = {(atom["segment"], atom["resid"], atom["resname"]) for atom in coordinates
            if atom["resid"] == rid and (atom["segment"] == source["segment"] and source["segment"] or
                atom["segment"] in (source["chain"], "PRO" + source["chain"], "PRO-" + source["chain"]))}
        if len(candidates) != 1:
            raise failure()
        segment, resid, resname = candidates.pop()
        return {"segment": segment, "resid": resid, "resname": resname}
    keys = ("polymer_block", "chain", "segment", "resid", "icode")
    return {"residues": [{"source": {key: entry[key] for key in keys}, "server": locate(entry),
                          "state": entry["state"], "method": entry["method"]} for entry in plan["residues"]],
            "disulfides": [{"left": locate(pair["left"]), "right": locate(pair["right"]),
                             "source_left": pair["left"], "source_right": pair["right"]} for pair in plan["disulfides"]]}


def verify_archive_states(root, manifest, *, reference, mapping=None):
    """Require both PDB Reader and final assembly evidence in the actual model."""
    from .protein_states import intent_for
    plan = validate_intent(intent_for(manifest))
    if not plan:
        return {"required": False, "passed": True}
    root = Path(root).resolve()
    def unique(name):
        # Root CHARMM files only; do not choose an arbitrary engine copy.
        paths = [path for path in root.glob("*/" + name)] + [root / name]
        paths = [path for path in paths if path.is_file() and not path.is_symlink()]
        if len(paths) != 1:
            raise failure()
        return paths[0]
    pdb, psf = unique("step1_pdbreader.pdb"), unique("step1_pdbreader.psf")
    mapping = mapping if mapping is not None else derive_mapping(plan, reference, pdb)
    source = verify_states(plan, mapping, pdb, psf, reference=reference)
    final_pdb, final_psf = unique("step5_assembly.pdb"), unique("step5_assembly.psf")
    final = verify_states(plan, mapping, final_pdb, final_psf, reference=reference)
    return {"passed": True, "required": True, "revision": 1, "source": source, "assembly": final,
            "evidence_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in (pdb, psf, final_pdb, final_psf)}}
