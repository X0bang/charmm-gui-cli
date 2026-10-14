"""Bounded coordinate-only duplicate-pose diagnostics, never chemistry repair."""

from collections import Counter, defaultdict
import math
from pathlib import Path

from ..store.auth import ToolError
from .residues import SIDECHAINS, ALIASES, MODIFIED

RMSD_LIMIT_A = 0.5


class _DiagnosticLimit(Exception):
    pass


def _cost(left, right):
    return sum((a - b) * (a - b) for a, b in zip(left.xyz, right.xyz))


def _minimum_assignment(costs, budget):
    """Minimum-cost square assignment with an explicit diagnostic work bound."""
    count = len(costs)
    u, v, p, way = [0.] * (count + 1), [0.] * (count + 1), [0] * (count + 1), [0] * (count + 1)
    for row in range(1, count + 1):
        p[0], column = row, 0
        minimum, used = [math.inf] * (count + 1), [False] * (count + 1)
        while True:
            used[column] = True
            active_row, delta, next_column = p[column], math.inf, 0
            for candidate in range(1, count + 1):
                if used[candidate]:
                    continue
                budget[0] -= 1
                if budget[0] < 0:
                    raise _DiagnosticLimit
                reduced = costs[active_row - 1][candidate - 1] - u[active_row] - v[candidate]
                if reduced < minimum[candidate]:
                    minimum[candidate], way[candidate] = reduced, column
                if minimum[candidate] < delta:
                    delta, next_column = minimum[candidate], candidate
            for candidate in range(count + 1):
                if used[candidate]:
                    u[p[candidate]] += delta
                    v[candidate] -= delta
                else:
                    minimum[candidate] -= delta
            column = next_column
            if p[column] == 0:
                break
        while column:
            previous = way[column]
            p[column] = p[previous]
            column = previous
    return [(p[column] - 1, column - 1) for column in range(1, count + 1)]


def coordinate_match(reference, observed, budget):
    """Find an element-preserving bijection below the RMSD limit, without fit.

    The returned value proves a coordinate match, not molecular identity.
    A sorted bijection is sufficient when already below the threshold; otherwise
    minimum-cost assignment is used. No nearest-atom reuse or fitted alignment.
    """
    if not reference or len(reference) != len(observed) or Counter(a.element for a in reference) != Counter(a.element for a in observed):
        return None
    if any(len(atom.xyz) != 3 or not all(math.isfinite(value) for value in atom.xyz) for atom in reference + observed):
        return None
    count = len(reference)
    groups = []
    for element in sorted({a.element for a in reference}):
        left = sorted((i for i, atom in enumerate(reference) if atom.element == element), key=lambda i: reference[i].xyz)
        right = sorted((i for i, atom in enumerate(observed) if atom.element == element), key=lambda i: observed[i].xyz)
        groups.append((left, right))
    mapping = [(a, b) for left, right in groups for a, b in zip(left, right)]
    total = math.fsum(_cost(reference[a], observed[b]) for a, b in mapping)
    if total >= count * RMSD_LIMIT_A ** 2:
        matrix_size = sum(len(left) ** 2 for left, _ in groups)
        budget[0] -= matrix_size
        if budget[0] < 0:
            raise _DiagnosticLimit
        matrices = [[[_cost(reference[a], observed[b]) for b in right] for a in left] for left, right in groups]
        if math.fsum(min(row) for costs in matrices for row in costs) >= count * RMSD_LIMIT_A ** 2:
            return None
        mapping = [(left[a], right[b]) for (left, right), costs in zip(groups, matrices)
                   for a, b in _minimum_assignment(costs, budget)]
        total = math.fsum(_cost(reference[a], observed[b]) for a, b in mapping)
    rmsd = math.sqrt(total / count)
    if rmsd >= RMSD_LIMIT_A:
        return None
    return {"rmsd_A": rmsd, "max_displacement_A": max(math.sqrt(_cost(reference[a], observed[b])) for a, b in mapping),
            "heavy_atoms": count, "aligned": False, "chemical_identity_verified": False,
            "scope": "unfitted_element_preserving_heavy_coordinate_bijection",
            "atom_mapping": [{"ligand_index": a + 1, "protein_serial": observed[b].serial, "element": reference[a].element}
                             for a, b in sorted(mapping)]}


def enrich_duplicate_diagnostics(error, atoms, ligand, *, input_options=None):
    """Enrich an existing blocking heterogen error; do not change acceptance."""
    if error.key != "input.protein_hetero" or ligand is None or Path(ligand).suffix.lower() not in (".sdf", ".mol", ".mol2"):
        return
    from .inputs import read_ligand
    try:
        molecule = read_ligand(ligand, pose=(input_options or {}).get("ligand_pose"))
    except (ToolError, OSError, ValueError):
        return  # Preserve the authoritative protein error if chemistry is unavailable.
    reference = [atom for atom in molecule.atoms if atom.element not in ("H", "D", "T")]
    groups = defaultdict(list)
    for atom in atoms:
        line = atom.line
        if line.startswith("HETATM") and atom.element not in ("H", "D", "T"):
            key = (atom.polymer_block, line[17:21].strip(), line[21:22].strip(), int(line[22:26]), line[26:27].strip(), line[72:76].strip())
            groups[key].append(atom)
    budget, additions = [4_000_000], []
    for detail in error.details:
        if not detail.get("kind") or detail.get("resname") in set(SIDECHAINS) | set(ALIASES) | set(MODIFIED):
            continue
        key = (detail.get("polymer_block", 0), detail["resname"], detail["chain"], detail["resid"], detail["icode"], detail["segment"])
        try:
            match = coordinate_match(reference, groups[key], budget)
        except _DiagnosticLimit:
            additions.append({"key": "input.duplicate_scan_limited", "params": {}})
            break
        if match is None:
            continue
        # Map back from the heavy-only reference to the original SDF indices.
        full_index = {id(atom): index + 1 for index, atom in enumerate(molecule.atoms)}
        for pair in match["atom_mapping"]:
            pair["ligand_index"] = full_index[id(reference[pair["ligand_index"] - 1])]
        match.update(ligand_source=str(Path(ligand).resolve()), selected_pose=molecule.provenance.get("selected_pose", 1))
        detail["matching_ligand_coordinates"] = match
        additions.append({"key": "input.duplicate_ligand_pose", "params": {"residue": detail["params"]["residue"],
                          "count": match["heavy_atoms"], "rmsd": f"{match['rmsd_A']:.4f}"}})
    error.details.extend(additions)
