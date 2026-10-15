"""Actual PDB/PSF fragment separation, retained links and terminal site checks.

Evidence is file-bound, not a certificate of server origin or scientific quality.
The orchestrator must separately bind successful POSTs/GET origins and require
these checks on BOTH the source artifact and final assembly before submission
support can be opened. No public break bypass is introduced here.
"""

from collections import defaultdict
import hashlib
from pathlib import Path
import re

from .fragment_terminals import resolve_fragment_terminals
from .protein_state_proof import read_psf, read_pdb, canonical
from .protein_states import validate_intent, identity_key
from .residues import SIDECHAINS, ALIASES, MODIFIED
from .terminal_topology import read_terminal_topology, terminal_profile, failure
from ..store.auth import ToolError


_CAP_ATOMS = {"HT1", "HT2", "HT3", "HN1", "HN2", "CAY", "HY1", "HY2", "HY3", "CY", "OY",
              "OT1", "OT2", "HT2B", "CT", "NT", "HNT", "CAT", "OXT"}
_PROTEIN = set(SIDECHAINS) | set(ALIASES) | set(MODIFIED)


def _digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _stream_digest(handle)


def _stream_digest(handle):
    result = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def _binding(preparation, source, prepared, plan, receipt):
    resolved = resolve_fragment_terminals(preparation, source, prepared, plan)
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("plan_fingerprint") != plan["fingerprint"]
            or receipt.get("scope") != "saved_form_terminal_binding_only"
            or receipt.get("submission_confirmed") is not False or receipt.get("remote_verified") is not False
            or receipt.get("requested_preparation") != resolved["requested_preparation"]
            or not isinstance(receipt.get("source_job_id"), str) or not re.fullmatch(r"[0-9]{10}", receipt["source_job_id"])
            or not isinstance(receipt.get("snapshot_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["snapshot_sha256"])
            or not isinstance(receipt.get("fragments"), list) or len(receipt["fragments"]) != len(resolved["fragments"])):
        raise failure()
    used = set()
    for expected, actual in zip(resolved["fragments"], receipt["fragments"]):
        server = actual["server_chain"]
        chain = expected["prepared_chain"]
        if server not in (chain, "PRO" + chain, "PRO-" + chain) or server in used:
            raise failure()
        used.add(server)
        if {k: v for k, v in actual.items() if k not in ("server_chain", "terminals")} != {
                k: v for k, v in expected.items() if k != "terminals"}:
            raise failure()
        if set(actual["terminals"]) != {"n_terminal", "c_terminal"}:
            raise failure()
        for end, suffix in (("n_terminal", "first"), ("c_terminal", "last")):
            request, entry = expected["terminals"][end], actual["terminals"][end]
            if {k: v for k, v in entry.items() if k not in ("field", "form_value")} != request:
                raise failure()
            value, allowed = entry["form_value"], {request["value"]}
            if end == "n_terminal" and request["value"] == "NTER":
                residue = expected["n_terminus"]["source"]["resname"]
                allowed.update({"GLYP"} if residue == "GLY" else {"PROP"} if residue == "PRO" else set())
            if value not in allowed or entry["field"] != f"terminal[{server}][{suffix}]":
                raise failure()
    return receipt["fragments"]


def _source_atoms(source, plan):
    result, addresses, block = {}, {}, 0
    fragments = {serial: entry["index"] for entry in plan["fragments"] for serial in entry["atom_serials"]}
    for line in source:
        if line[:6].strip() == "TER":
            block += 1
        if line.startswith("ATOM  "):
            serial = int(line[6:11])
            address = (block, line[21:22].strip(), line[72:76].strip(), int(line[22:26]), line[26:27].strip())
            entry = {"fragment": fragments[serial], "address": address, "name": line[12:16].strip(),
                     "resname": line[17:21].strip(), "element": line[76:78].strip().upper()}
            result[serial] = entry
            addresses[address, entry["name"]] = serial
    return result, addresses


def _retained_links(source, plan, original, addresses, state_intent):
    links = {tuple(edge) for edge in plan["retained_cross_fragment_bonds"]}
    def connect(left, right):
        a, b = addresses[left, "SG"], addresses[right, "SG"]
        if original[a]["resname"] not in ("CYS", "CYX") or original[b]["resname"] not in ("CYS", "CYX"):
            raise failure()
        if original[a]["fragment"] != original[b]["fragment"]:
            links.add(tuple(sorted((a, b))))
    for line in source:
        if line.startswith("SSBOND"):
            endpoints = []
            for ci, ri, ii in ((15, 17, 21), (29, 31, 35)):
                found = [address for address, name in addresses if name == "SG" and
                    (address[1], address[3], address[4]) == (line[ci:ci+1].strip(), int(line[ri:ri+4]), line[ii:ii+1].strip())]
                if len(found) != 1:
                    raise failure()
                endpoints.append(found[0])
            connect(*endpoints)
    states = validate_intent(state_intent)
    for pair in states.get("disulfides", []):
        connect(identity_key(pair["left"]), identity_key(pair["right"]))
    return links


def verify_fragment_artifact(preparation, source_lines, prepared_lines, plan, receipt, pdb, psf, topology, *, state_intent=None):
    """Prove concrete topology on one actual source OR assembly PDB/PSF pair."""
    try:
        bindings = _binding(preparation, source_lines, prepared_lines, plan, receipt)
        evidence = {name: _digest(path) for name, path in (("pdb", pdb), ("psf", psf), ("topology", topology))}
        templates = read_terminal_topology(topology)
        atoms, edges = read_psf(psf)
        coordinates = read_pdb(pdb)
        if len(atoms) != len(coordinates) or any(tuple(a[k] for k in ("segment", "resid", "resname", "name")) !=
                tuple(b[k] for k in ("segment", "resid", "resname", "name")) for a, b in zip(atoms, coordinates)):
            raise failure()
        segments = {row["server_chain"]: row["fragment_index"] for row in bindings}
        groups, owner = {}, {}
        for atom in atoms:
            if atom["segment"] not in segments:
                if atom["resname"] in _PROTEIN and atom["name"] == "CA":
                    raise failure()
                continue
            group = groups.setdefault((atom["segment"], atom["resid"]), {})
            if atom["name"] in group:
                raise failure()
            group[atom["name"]] = atom
            owner[atom["serial"]] = segments[atom["segment"]]
        neighbors = defaultdict(set)
        actual_cross, peptide = set(), set()
        for left, right in edges:
            if left in owner:
                neighbors[left].add(right)
            if right in owner:
                neighbors[right].add(left)
            if (left in owner) != (right in owner):
                raise failure()
            if left in owner and owner[left] != owner[right]:
                actual_cross.add((left, right))
        original, addresses = _source_atoms(source_lines, plan)
        retained = _retained_links(source_lines, plan, original, addresses, state_intent)
        source_to_actual, checked_ends = {}, []
        by_index = {row["fragment_index"]: row for row in bindings}
        for fragment in plan["fragments"]:
            binding = by_index[fragment["index"]]
            segment = binding["server_chain"]
            residues = fragment["source_residues"]
            ids = [str(row["resid"]) + row["icode"] for row in residues]
            if {rid for seg, rid in groups if seg == segment} != set(ids):
                raise failure()
            for residue, rid in zip(residues, ids):
                names = groups[segment, rid]
                allowed = {"HIS", "HSD", "HSE", "HSP"} if residue["resname"] in {"HIS", "HSD", "HSE", "HSP"} else {residue["resname"]}
                if residue["resname"] == "CYM":
                    allowed.add("CYS")
                if {a["resname"] for a in names.values()} - allowed:
                    raise failure()
            for left, right in zip(ids, ids[1:]):
                pair = tuple(sorted((groups[segment, left]["C"]["serial"], groups[segment, right]["N"]["serial"])))
                if pair not in edges:
                    raise failure()
                peptide.add(pair)
            for serial in fragment["atom_serials"]:
                entry = original[serial]
                rid = str(entry["address"][3]) + entry["address"][4]
                target = groups[segment, rid]
                name = entry["name"]
                if rid == ids[-1] and name not in target:
                    name = {"O": "OT1", "OXT": "OT2"}.get(name, name)
                if name in target:
                    source_to_actual[serial] = target[name]["serial"]
                elif entry["element"] not in ("H", "D", "T"):
                    raise failure()
            # Compose both ends together only when they share the same residue.
            end_groups = defaultdict(list)
            for end, rid, residue in (("n_terminal", ids[0], residues[0]), ("c_terminal", ids[-1], residues[-1])):
                end_groups[rid].append((end, binding["terminals"][end]["form_value"], residue))
            for rid, ends in end_groups.items():
                profile = terminal_profile(templates, ends[0][2]["resname"], [patch for _, patch, _ in ends])
                names = set(_CAP_ATOMS)
                for end, patch, _ in ends:
                    names.update({"N", "CA", "HN", "HA", "HA1", "HA2"} if end == "n_terminal" else {"C", "O"})
                    if patch != "NONE":
                        names.update(templates["patches"][patch]["atoms"])
                target = groups[segment, rid]
                if (set(target) & names) != (set(profile["atoms"]) & names):
                    raise failure()
                expected_neighbors = defaultdict(set)
                for a, b in profile["bonds"]:
                    if a in names:
                        expected_neighbors[a].add(target[b]["serial"])
                    if b in names:
                        expected_neighbors[b].add(target[a]["serial"])
                for a, b in peptide:
                    for serial, other in ((a, b), (b, a)):
                        atom = atoms[serial - 1]
                        if atom["segment"] == segment and atom["resid"] == rid:
                            expected_neighbors[atom["name"]].add(other)
                for name in names & profile["atoms"].keys():
                    actual, expected = target[name], profile["atoms"][name]
                    if (actual["type"] != expected["type"] or abs(actual["charge"] - expected["charge"]) > 1e-4
                            or abs(actual["mass"] - expected["mass"]) > .03
                            or coordinates[actual["serial"]-1]["element"] not in ("", expected["type"][0])
                            or neighbors[actual["serial"]] != expected_neighbors[name]):
                        raise failure()
                checked_ends.extend({"fragment_index": fragment["index"], "segment": segment, "resid": rid,
                                     "end": end, "patch": patch, "passed": True} for end, patch, _ in ends)
        expected_cross = {tuple(sorted((source_to_actual[a], source_to_actual[b]))) for a, b in retained}
        if expected_cross != actual_cross:
            raise failure()
        disulfides = 0
        for a, b in retained:
            if all(original[serial]["name"] == "SG" and original[serial]["resname"] in ("CYS", "CYX") for serial in (a, b)):
                disulfides += 1
                left, right = source_to_actual[a], source_to_actual[b]
                for serial, partner in ((left, right), (right, left)):
                    atom = atoms[serial - 1]
                    if (atom["type"] != "SM" or abs(atom["charge"] + .08) > 1e-4 or abs(atom["mass"] - 32.06) > .03
                            or any(atoms[neighbor-1]["mass"] <= 2.5 for neighbor in neighbors[serial])
                            or {neighbor for neighbor in neighbors[serial] if 31 < atoms[neighbor-1]["mass"] < 33} != {partner}):
                        raise failure()
        for a, b in edges:
            if a in owner and {atoms[a-1]["name"], atoms[b-1]["name"]} == {"C", "N"} and (a, b) not in peptide:
                raise failure()
        if evidence != {name: _digest(path) for name, path in (("pdb", pdb), ("psf", psf), ("topology", topology))}:
            raise failure()
        return {"passed": True, "schema_version": 1, "fragment_count": len(bindings), "terminals": checked_ends,
                "retained_cross_fragment_bonds": len(expected_cross), "disulfide_bonds_checked": disulfides,
                "peptide_bonds_checked": len(peptide),
                "plan_fingerprint": plan["fingerprint"], "mapping_sha256": canonical(receipt),
                "evidence_sha256": evidence,
                "scope": "artifact_fragment_segments_terminal_sites_and_declared_connectivity_only",
                "server_origin_verified": False, "scientific_correctness_verified": False}
    except ToolError as error:
        if error.key and error.key.startswith(("fragment.", "terminal.")):
            raise
        raise failure() from None
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise failure() from None


def verify_fragment_archive(root, preparation, source_lines, prepared_lines, plan, receipt, *, state_intent=None):
    """Require source AND final-assembly root CHARMM files, not engine copies."""
    try:
        return _verify_archive(root, preparation, source_lines, prepared_lines, plan, receipt, state_intent=state_intent)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise failure() from None


def _verify_archive(root, preparation, source_lines, prepared_lines, plan, receipt, *, state_intent=None):
    root = Path(root).resolve()
    def unique(name):
        paths = [root / name, *root.glob("*/" + name)]
        valid = []
        for path in paths:
            if path.relative_to(root).parts[0].lower() in ("amber", "desmond", "genesis", "gromacs", "lammps", "namd", "openmm"):
                continue
            if not path.is_file():
                continue
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root):
                raise failure()
            valid.append(path)
        if len(valid) != 1:
            raise failure()
        return valid[0]
    pdb, psf = unique("step1_pdbreader.pdb"), unique("step1_pdbreader.psf")
    final_pdb, final_psf = unique("step5_assembly.pdb"), unique("step5_assembly.psf")
    topology = unique("toppar/top_all36_prot.rtf")
    if len({pdb.parent, psf.parent, final_pdb.parent, final_psf.parent, topology.parent.parent}) != 1:
        raise failure()
    paths = (pdb, psf, final_pdb, final_psf, topology)
    evidence = {str(path.relative_to(root)): _digest(path) for path in paths}
    source = verify_fragment_artifact(preparation, source_lines, prepared_lines, plan, receipt, pdb, psf, topology, state_intent=state_intent)
    assembly = verify_fragment_artifact(preparation, source_lines, prepared_lines, plan, receipt, final_pdb, final_psf, topology, state_intent=state_intent)
    for result, pair in ((source, (pdb, psf)), (assembly, (final_pdb, final_psf))):
        for name, path in (("pdb", pair[0]), ("psf", pair[1]), ("topology", topology)):
            if result["evidence_sha256"][name] != evidence[str(path.relative_to(root))]:
                raise failure()
    if evidence != {str(path.relative_to(root)): _digest(path) for path in paths}:
        raise failure()
    return {"passed": True, "schema_version": 1, "source": source, "assembly": assembly,
            "evidence_sha256": evidence,
            "server_origin_verified": False, "scientific_correctness_verified": False}
