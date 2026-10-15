"""Resolve original-chain ends separately from explicit prepared-fragment ends.

Request resolution only: no local cap generation or remote topology proof.
Inputs must be normalized preparation settings; rederive the fragment plan from
the actual staged source/prepared lines before resolving any end requests.
"""

from collections import defaultdict
from copy import deepcopy
import re

from .fragment_preparation import verify_fragment_preparation
from .pdb_normalization import N_TERMINALS, C_TERMINALS
from ..locales.i18n import t
from ..store.auth import ToolError


def _error(key, **params):
    return ToolError(key=key, params=params, next_step=t("fragment.inspect_next"))


def resolve_fragment_terminals(preparation, source_lines, prepared_lines, plan):
    """Resolve global < original-chain outer ends < explicit fragment overrides.

    Bare selectors always mean ORIGINAL chains/segids, never a newly allocated
    chain. ``fragment:B`` explicitly refers to the prepared chain B. Source-chain
    N settings land on the first fragment, C settings on the last. All other
    newly exposed ends keep the global default unless explicitly overridden.
    """
    verify_fragment_preparation(source_lines, prepared_lines, plan)
    ends = {"n_terminal": N_TERMINALS, "c_terminal": C_TERMINALS}
    if (not isinstance(preparation, dict) or set(preparation) - {*ends, "terminals", "orientation"}
            or any(preparation.get(end) not in choices for end, choices in ends.items())
            or preparation.get("orientation", "ppm") not in ("ppm", "prepared")):
        raise _error("fragment.terminals_invalid")
    overrides = preparation.get("terminals", {})
    if not isinstance(overrides, dict):
        raise _error("fragment.terminals_invalid")
    for selector, patches in overrides.items():
        if (not isinstance(selector, str) or not re.fullmatch(r"(?:[A-Za-z0-9_]{1,4}|fragment:[A-Za-z0-9])", selector)
                or not isinstance(patches, dict) or not patches or set(patches) - set(ends)
                or any(value not in ends[end] for end, value in patches.items())):
            raise _error("fragment.terminals_invalid")
    fragments = plan["fragments"]
    groups, aliases = defaultdict(list), defaultdict(set)
    for fragment in fragments:
        source = fragment["source_chain"]
        group = tuple(source[key] for key in ("polymer_block", "chain", "segment"))
        groups[group].append(fragment)
        chain, segment = source["chain"], source["segment"]
        for alias in (chain, segment, "PRO" + chain if chain else ""):
            if alias:
                aliases[alias].add(group)
    source_patches, explicit = {}, {}
    by_chain = {fragment["prepared_chain"]: fragment for fragment in fragments}
    for selector, patches in overrides.items():
        if selector.startswith("fragment:"):
            chain = selector.split(":", 1)[1]
            if chain not in by_chain:
                raise _error("fragment.selector_missing", selector=selector)
            explicit[chain] = (selector, patches)
            continue
        candidates = aliases.get(selector, set())
        if not candidates:
            raise _error("fragment.selector_missing", selector=selector)
        if len(candidates) != 1:
            raise _error("fragment.selector_ambiguous", selector=selector)
        group = next(iter(candidates))
        if group in source_patches:
            raise _error("fragment.selector_conflict", first=source_patches[group][0], second=selector)
        source_patches[group] = (selector, patches)
    rows, terminals = [], {}
    for fragment in fragments:
        chain, source = fragment["prepared_chain"], fragment["source_chain"]
        group = tuple(source[key] for key in ("polymer_block", "chain", "segment"))
        row = {"fragment_index": fragment["index"], "source_chain": deepcopy(source),
               "prepared_chain": chain, "n_terminus": deepcopy(fragment["n_terminus"]),
               "c_terminus": deepcopy(fragment["c_terminus"]), "terminals": {}}
        terminals[chain] = {}
        for end in ends:
            layers = [{"value": preparation[end], "origin": "global", "selector": None}]
            outer = groups[group][0 if end == "n_terminal" else -1]["index"] == fragment["index"]
            if outer and group in source_patches:
                selector, patches = source_patches[group]
                if end in patches:
                    layers.append({"value": patches[end], "origin": "source_chain", "selector": selector})
            if chain in explicit:
                selector, patches = explicit[chain]
                if end in patches:
                    layers.append({"value": patches[end], "origin": "fragment", "selector": selector})
            resolved = {**layers[-1], "overridden": layers[:-1]}
            value = resolved["value"]
            if end == "n_terminal" and value in ("GLYP", "PROP"):
                residue = fragment["n_terminus"]["source"]["resname"]
                if residue != {"GLYP": "GLY", "PROP": "PRO"}[value]:
                    raise _error("terminal.residue_mismatch", chain="fragment:" + chain, residue=residue, patch=value)
            row["terminals"][end] = resolved
            terminals[chain][end] = value
        rows.append(row)
    return {"schema_version": 1, "plan_fingerprint": plan["fingerprint"],
            "requested_preparation": deepcopy(preparation),
            "preparation": {"n_terminal": preparation["n_terminal"], "c_terminal": preparation["c_terminal"],
                            "orientation": preparation.get("orientation", "ppm"), "terminals": terminals},
            "fragments": rows, "scope": "fragment_terminal_requests_only", "remote_verified": False}
