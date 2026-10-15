"""Coordinate-preserving fragment primitives; not a submission permission gate.

The public CLI does not expose splitting until terminal controls and actual
source/final PSF separation are verified. These functions operate on already
normalized, single-model PDB text and never read/write files or infer new bonds.
"""

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import string

from ..locales.i18n import t
from ..store.auth import ToolError
from .residues import SIDECHAINS, ALIASES, MODIFIED


_CHAINS = string.ascii_uppercase + string.ascii_lowercase + string.digits
_PROTEIN = set(SIDECHAINS) | set(ALIASES) | set(MODIFIED)


@dataclass
class _Record:
    serial: int
    line: str
    xyz: tuple
    polymer_block: int

    @property
    def name(self):
        return self.line[12:16].strip()


def _failure(key="fragment.invalid", **params):
    return ToolError(key=key, params=params, next_step=t("fragment.inspect_next"))


def _address(atom):
    line = atom.line
    return {"polymer_block": atom.polymer_block, "chain": line[21:22].strip(),
            "segment": line[72:76].strip(), "resid": int(line[22:26]),
            "icode": line[26:27].strip(), "resname": line[17:21].strip()}


def _label(atom):
    a = _address(atom)
    return " ".join((a["resname"], a["chain"] or a["segment"] or "-",
                     str(a["resid"]) + a["icode"], atom.name))


def _sequence(atoms):
    """Preserve contiguous residue order; never combine recurring addresses."""
    result, seen = [], set()
    for atom in atoms:
        if not atom.line.startswith("ATOM  "):
            continue
        identity = _address(atom)
        key = tuple(identity[k] for k in ("polymer_block", "chain", "segment", "resid", "icode"))
        if not result or result[-1]["key"] != key:
            if key in seen:
                raise _failure("fragment.recurring_residue", residue=_label(atom).rsplit(" ", 1)[0])
            seen.add(key)
            result.append({"key": key, "address": identity, "atoms": {}, "records": []})
        elif result[-1]["address"]["resname"] != identity["resname"]:
            raise _failure("fragment.recurring_residue", residue=_label(atom).rsplit(" ", 1)[0])
        if atom.name in result[-1]["atoms"]:
            raise _failure("input.atom_identity", atom=_label(atom))
        result[-1]["atoms"][atom.name] = atom
        result[-1]["records"].append(atom)
    return result


def _chain(address):
    return tuple(address[k] for k in ("polymer_block", "chain", "segment"))


def _break(left, right):
    if (_chain(left["address"]) != _chain(right["address"])
            or left["address"]["resname"] not in _PROTEIN or right["address"]["resname"] not in _PROTEIN
            or "C" not in left["atoms"] or "N" not in right["atoms"]):
        return None
    a, b = left["atoms"]["C"], right["atoms"]["N"]
    distance = math.dist(a.xyz, b.xyz)
    if not math.isfinite(distance):
        raise _failure()
    if distance <= 2.0:
        return None
    from .pdb_normalization import finding
    return {**finding("input.chain_break", left=_label(a), right=_label(b), distance=f"{distance:.2f}"),
            "distance_A": distance, "left_source": _address(a), "right_source": _address(b),
            "left_atom_serial": a.serial, "right_atom_serial": b.serial}


def backbone_breaks(atoms):
    """Shared geometric decision, independent of numbering gaps or insertion codes."""
    chains = defaultdict(list)
    for residue in _sequence(atoms):
        chains[_chain(residue["address"])].append(residue)
    return [entry for residues in chains.values() for left, right in zip(residues, residues[1:])
            if (entry := _break(left, right))]


def _records(lines):
    if not isinstance(lines, (list, tuple)) or not all(isinstance(line, str) and "\n" not in line and "\r" not in line for line in lines):
        raise _failure()
    if any(line[:6].strip() in ("MODEL", "ENDMDL") for line in lines):
        raise _failure()
    atoms, line_by_serial, block = [], {}, 0
    for index, line in enumerate(lines):
        if line[:6].strip() == "TER":
            block += 1
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        try:
            serial = int(line[6:11])
            int(line[22:26])
            xyz = tuple(float(line[i:i+8]) for i in (30, 38, 46))
            if (len(line) < 54 or not 1 <= serial <= 99999 or serial in line_by_serial or not line[12:16].strip()
                    or line[16:17].strip() or not all(math.isfinite(v) for v in xyz)):
                raise ValueError
        except (ValueError, OverflowError):
            raise _failure() from None
        atoms.append(_Record(serial, line, xyz, block))
        line_by_serial[serial] = index
    if not atoms:
        raise _failure()
    return atoms, line_by_serial


def _hash(lines):
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def _fingerprint(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _ssbond(line, number, addresses):
    line = line.ljust(36)
    for chain_index, residue_index, icode_index in ((15, 17, 21), (29, 31, 35)):
        try:
            key = (line[chain_index], int(line[residue_index:residue_index+4]), line[icode_index].strip())
            candidates = addresses[key]
            if len(candidates) != 1:
                raise ValueError
        except (ValueError, KeyError):
            raise _failure("fragment.ssbond_ambiguous", line=number) from None
        line = line[:chain_index] + candidates[0] + line[chain_index+1:]
    return line


def prepare_fragmented_lines(lines, *, accept_breaks=False):
    """Explicitly split geometric gaps into unique PDB chains and TER blocks.

    Atom serials/names, residue numbers, coordinate columns, charge/element and
    CONECT records are retained. Segids are cleared in the changed protein text
    so they cannot merge newly separated chains; original identities and end
    mapping are retained in the plan. No missing atoms or caps are generated.
    """
    if type(accept_breaks) is not bool:
        raise _failure()
    atoms, line_by_serial = _records(lines)
    residues = _sequence(atoms)
    gaps = backbone_breaks(atoms)
    original_chains = list(dict.fromkeys(_chain(row["address"]) for row in residues))
    # Explicit TER/segment boundaries that reuse a PDB chain ID also need
    # distinct upload chains when the caller explicitly requests separation.
    repeated_chain_ids = len({chain[1] for chain in original_chains}) < len(original_chains)
    if not gaps and not (accept_breaks and repeated_chain_ids):
        return list(lines), {}
    if not accept_breaks:
        raise ToolError(key="input.chain_breaks", params={"count": len(gaps)}, details=gaps,
                        next_step=t("fragment.inspect_next"))
    if any(line.startswith("LINK  ") for line in lines):
        raise _failure("fragment.linked_input")
    if any(row["address"]["resname"] not in _PROTEIN for row in residues):
        raise _failure("fragment.unsupported_polymer")
    seen_chains, previous_chain = set(), None
    for row in residues:
        chain = _chain(row["address"])
        if chain != previous_chain:
            if chain in seen_chains:
                raise _failure("fragment.interleaved_chains")
            seen_chains.add(chain)
            previous_chain = chain
    fragments, current = [], None
    gaps_by_pair = {(g["left_atom_serial"], g["right_atom_serial"]): g for g in gaps}
    for i, row in enumerate(residues):
        previous = residues[i-1] if i else None
        boundary = previous is None or _chain(row["address"]) != _chain(previous["address"])
        if previous is not None and "C" in previous["atoms"] and "N" in row["atoms"]:
            boundary = boundary or (previous["atoms"]["C"].serial, row["atoms"]["N"].serial) in gaps_by_pair
        if boundary:
            current = []
            fragments.append(current)
        current.append(row)
    occupied = {atom.line[21:22] for atom in atoms if atom.line[21:22].strip()}
    allocated, serial_to_fragment, addresses, entries = set(), {}, defaultdict(list), []
    for index, fragment in enumerate(fragments, 1):
        if "N" not in fragment[0]["atoms"] or "C" not in fragment[-1]["atoms"]:
            raise _failure("fragment.terminal_atoms_missing", fragment=index)
        original_chain = fragment[0]["address"]["chain"]
        chain = original_chain if original_chain in _CHAINS and original_chain not in allocated else next(
            (value for value in _CHAINS if value not in occupied and value not in allocated), None)
        if chain is None:
            raise _failure("fragment.chains_exhausted", count=len(fragments))
        allocated.add(chain)
        ids = []
        for row in fragment:
            source = row["address"]
            if source["resname"] in ("CYS", "CYX") and "SG" in row["atoms"]:
                addresses[(source["chain"], source["resid"], source["icode"])].append(chain)
            for atom in row["records"]:
                serial_to_fragment[atom.serial] = index
                ids.append(atom.serial)
        entries.append({"index": index, "source_chain": {k: fragment[0]["address"][k] for k in ("polymer_block", "chain", "segment")},
            "source_residues": [dict(row["address"]) for row in fragment],
            "prepared_chain": chain, "prepared_segment": "", "atom_serials": ids,
            "n_terminus": {"source": dict(fragment[0]["address"]), "prepared_chain": chain,
                           "atom_serial": fragment[0]["atoms"]["N"].serial},
            "c_terminus": {"source": dict(fragment[-1]["address"]), "prepared_chain": chain,
                           "atom_serial": fragment[-1]["atoms"]["C"].serial}})
    pairs = {tuple(sorted(pair)) for pair in gaps_by_pair}
    cross_bonds = set()
    by_serial = {a.serial: a for a in atoms}
    for line in lines:
        if not line.startswith("CONECT"):
            continue
        try:
            ids = [int(line[i:i+5]) for i in range(6, len(line), 5) if line[i:i+5].strip()]
            if not ids or any(i not in by_serial for i in ids):
                raise ValueError
        except ValueError:
            raise _failure() from None
        for other in ids[1:]:
            if other == ids[0]:
                raise _failure()
            pair = tuple(sorted((ids[0], other)))
            if (pair in pairs or ids[0] in serial_to_fragment and other in serial_to_fragment
                    and serial_to_fragment[ids[0]] != serial_to_fragment[other]
                    and {by_serial[ids[0]].name, by_serial[other].name} == {"C", "N"}):
                raise _failure("fragment.explicit_peptide_bond", left=pair[0], right=pair[1])
            if ids[0] in serial_to_fragment and other in serial_to_fragment and serial_to_fragment[ids[0]] != serial_to_fragment[other]:
                cross_bonds.add(pair)
    # Insert only missing TER boundaries; retain every original TER and metadata
    # record. Determine the next significant atom/TER in a linear reverse pass.
    following, next_record = {}, None
    for index in range(len(lines)-1, -1, -1):
        following[index] = next_record
        if lines[index].startswith(("ATOM  ", "HETATM")) or lines[index][:6].strip() == "TER":
            next_record = lines[index][:6].strip()
    ends = {line_by_serial[entry["atom_serials"][-1]] for entry in entries}
    prepared, prepared_blocks, block = [], {}, 0
    for number, original in enumerate(lines, 1):
        line = original
        if original.startswith("ATOM  "):
            serial = int(original[6:11])
            entry = entries[serial_to_fragment[serial]-1]
            line = original.ljust(76)
            line = line[:21] + entry["prepared_chain"] + line[22:72] + "    " + line[76:]
            prepared_blocks.setdefault(entry["index"], block)
        elif original.startswith("SSBOND"):
            line = _ssbond(original, number, addresses)
        prepared.append(line)
        if original[:6].strip() == "TER":
            block += 1
        if number-1 in ends and following[number-1] != "TER":
            prepared.append("TER")
            block += 1
    for entry in entries:
        entry["prepared_polymer_block"] = prepared_blocks[entry["index"]]
    for gap in gaps:
        # The threshold decision uses the full coordinate-derived distance.
        # Persist sub-PDB precision rather than a platform-dependent last ULP
        # in the identity fingerprint; this is not a relaxed break threshold.
        gap["distance_A"] = round(gap["distance_A"], 6)
        gap["left_fragment"] = serial_to_fragment[gap["left_atom_serial"]]
        gap["right_fragment"] = serial_to_fragment[gap["right_atom_serial"]]
        gap["required_terminal_sides"] = {"left": "c_terminal", "right": "n_terminal"}
    plan = {"schema_version": 1, "distance_threshold_A": 2.0, "accepted": True,
            "source_sha256": _hash(lines), "prepared_sha256": _hash(prepared),
            "coordinates_modified": False, "missing_atoms_generated": False,
            "caps_generated_locally": False, "remote_verified": False,
            "fragments": entries, "breaks": gaps,
            "retained_cross_fragment_bonds": [list(pair) for pair in sorted(cross_bonds)]}
    plan["fingerprint"] = _fingerprint(plan)
    return prepared, plan


def verify_fragment_preparation(source_lines, prepared_lines, plan):
    """Re-derive the plan, rather than trusting an editable hash or label alone."""
    expected_lines, expected = prepare_fragmented_lines(source_lines, accept_breaks=True)
    if not expected or prepared_lines != expected_lines or plan != expected:
        raise _failure("fragment.evidence_changed")
    return {"passed": True, "schema_version": 1, "fragment_count": len(plan["fragments"]),
            "break_count": len(plan["breaks"]), "fingerprint": plan["fingerprint"],
            "scope": "local_fragment_preparation_only", "remote_verified": False}


def remap_state_intent(intent, source_lines, prepared_lines, plan):
    """Bind already parsed explicit states to the actual newly prepared chains.

    This does not select states, infer disulfides, alter charges or certify any
    remote model. Re-derive the fragment plan before accepting address changes.
    """
    from copy import deepcopy
    from .protein_states import validate_intent, identity_key
    intent = validate_intent(intent)
    if not intent:
        return {}
    verify_fragment_preparation(source_lines, prepared_lines, plan)
    mapping, resnames = {}, {}
    for fragment in plan["fragments"]:
        for source in fragment["source_residues"]:
            key = identity_key(source)
            if key in mapping:
                raise _failure("fragment.evidence_changed")
            resnames[key] = source["resname"]
            mapping[key] = {"chain": fragment["prepared_chain"], "segment": fragment["prepared_segment"],
                            "polymer_block": fragment["prepared_polymer_block"]}
    result = deepcopy(intent)
    def move(entry, allowed):
        key = identity_key(entry)
        replacement = mapping.get(key)
        if replacement is None or resnames.get(key) not in allowed:
            raise _failure("fragment.evidence_changed")
        entry.update(replacement)
    for entry in result["residues"]:
        move(entry, {entry["pdb_resname"]})
    for pair in result["disulfides"]:
        move(pair["left"], {"CYS", "CYX"})
        move(pair["right"], {"CYS", "CYX"})
    return validate_intent(result)
