"""Bind explicit residue-state intent to validated PDB Reader controls.

Only literal JSON catalogues are decoded. No website JavaScript is executed.
Unknown chain/residue mapping or absent controls abort before the POST intent.
"""

import json
from pathlib import Path
import re

from ..chem.protein_states import validate_intent
from ..chem.inputs import _pdb
from .web import soup_for, snapshot_error, form_data


def literal_catalogue(soup, name):
    found = []
    for script in soup.find_all("script", src=False):
        text = script.get_text()
        if len(text) > 4 * 1024 * 1024:
            continue
        for match in re.finditer(r"\bvar\s+" + re.escape(name) + r"\s*=\s*", text):
            try:
                value, end = json.JSONDecoder().raw_decode(text[match.end():])
            except ValueError:
                continue
            if isinstance(value, dict) and text[match.end() + end:].lstrip().startswith(";"):
                found.append(value)
    return found[0] if len(found) == 1 else None


def state_fields(snapshot, reference_pdb, intent):
    plan = validate_intent(intent)
    if not plan:
        return {}, {"residues": [], "disulfides": []}
    soup = soup_for(Path(snapshot).read_text(encoding="utf-8"))
    forms = [form for form in soup.find_all("form") if form.select_one('input[name="jobid"]')]
    chains, catalogue = literal_catalogue(soup, "prot_chains"), literal_catalogue(soup, "charmm_patch")
    if len(forms) != 1 or not isinstance(chains, dict):
        raise snapshot_error(snapshot, "state.form_missing")
    form = forms[0]
    if len({node.get("value") for node in form.find_all("input", {"name": "jobid"})}) != 1:
        raise snapshot_error(snapshot, "state.form_missing")
    patch_table = catalogue.get("prot") if isinstance(catalogue, dict) else None
    def available_patches(resname):
        return patch_table.get(resname, []) if isinstance(patch_table, dict) else []
    atoms, _ = _pdb(reference_pdb)
    def locate(entry, expected):
        selected = [atom for atom in atoms if atom.line.startswith("ATOM  ")
            and atom.line[21:22].strip() == entry["chain"] and atom.line[72:76].strip() == entry["segment"]
            and int(atom.line[22:26]) == entry["resid"] and atom.line[26:27].strip() == entry["icode"]]
        if (not selected or len({atom.polymer_block for atom in selected}) != 1
                or {atom.line[17:21].strip() for atom in selected} != {expected}):
            raise snapshot_error(snapshot, "state.form_missing")
        candidates = [name for name in chains if name == entry["segment"] and entry["segment"]
                      or name in (entry["chain"], "PRO" + entry["chain"], "PRO-" + entry["chain"])]
        rid = str(entry["resid"]) + entry["icode"]
        candidates = [name for name in candidates if isinstance(chains[name], dict) and rid in chains[name]]
        if len(candidates) != 1:
            raise snapshot_error(snapshot, "state.form_missing")
        chain = candidates[0]
        observed = chains[chain][rid]
        allowed = {"HIS", "HSD", "HSE", "HSP"} if expected in {"HSD", "HSE", "HSP"} else {expected}
        if expected == "CYM":
            allowed.add("CYS")
        if not isinstance(observed, str) or observed not in allowed:
            raise snapshot_error(snapshot, "state.form_missing")
        return {"segment": chain, "resid": rid, "resname": observed}
    def controls(names):
        for name in names:
            nodes = form.find_all("input" if name.endswith("_checked") else "select", {"name": name})
            if not any(not node.has_attr("disabled") and (not name.endswith("_checked")
                or node.get("type", "text").lower() == "checkbox") for node in nodes):
                raise snapshot_error(snapshot, "state.form_missing")
    fields, receipt, protonation = {}, {"residues": [], "disulfides": []}, []
    for entry in plan["residues"]:
        server = locate(entry, entry["pdb_resname"])
        request = {"source": {key: entry[key] for key in ("polymer_block", "chain", "segment", "resid", "icode")},
                   "server": server, "state": entry["state"], "method": entry["method"]}
        receipt["residues"].append(request)
        if entry["method"] == "patch" or entry["method"] == "residue" and server["resname"] != entry["state"]:
            supported = available_patches(server["resname"])
            if not isinstance(supported, list) or entry["state"] not in supported:
                raise snapshot_error(snapshot, "state.form_missing")
            protonation.append((server["segment"], server["resname"], server["resid"], entry["state"]))
    if protonation:
        names = ["protonation[" + key + "][]" for key in ("chain", "residue", "rid", "patch")]
        controls(["prot_checked", *names])
        fields["prot_checked"] = "1"
        for index, name in enumerate(names):
            fields[name] = [row[index] for row in protonation]
    for pair in plan["disulfides"]:
        left, right = locate(pair["left"], "CYS"), locate(pair["right"], "CYS")
        receipt["disulfides"].append({"left": left, "right": right,
                                     "source_left": pair["left"], "source_right": pair["right"]})
    if receipt["disulfides"]:
        names = ["ssbonds[" + key + "][]" for key in ("segid1", "resid1", "segid2", "resid2")]
        controls(["ssbonds_checked", *names])
        fields["ssbonds_checked"] = "1"
        rows = [(pair["left"]["segment"], pair["left"]["resid"], pair["right"]["segment"], pair["right"]["resid"])
                for pair in receipt["disulfides"]]
        for index, name in enumerate(names):
            fields[name] = [row[index] for row in rows]
    # Never discard existing successful checked rows, including unrelated
    # native disulfides or protonation choices. Dynamic defaults that cannot
    # be established from the saved controls require explicit reconciliation.
    defaults = form_data(form)
    for prefix, switch, columns in (("protonation", "prot_checked", ("chain", "residue", "rid", "patch")),
                                    ("ssbonds", "ssbonds_checked", ("segid1", "resid1", "segid2", "resid2"))):
        if not any(node.has_attr("checked") for node in form.find_all("input", {"name": switch})):
            continue
        names = [prefix + "[" + column + "][]" for column in columns]
        values = [[value for key, value in defaults if key == name] for name in names]
        if not values[0] or len({len(value) for value in values}) != 1 or any(not value for group in values for value in group):
            raise snapshot_error(snapshot, "state.form_defaults")
        old_rows = list(dict.fromkeys(zip(*values)))
        new_rows = list(zip(*(fields[name] for name in names))) if all(name in fields for name in names) else []
        merged = list(old_rows)
        if prefix == "protonation":
            expected = {(entry["server"]["segment"], entry["server"]["resid"]): entry["state"] for entry in receipt["residues"]}
            observed = {}
            for chain, residue, rid, patch in [*old_rows, *new_rows]:
                patches = available_patches(residue)
                address = (chain, rid)
                if (not isinstance(chains.get(chain), dict) or chains[chain].get(rid) != residue
                        or not isinstance(patches, list) or patch not in patches
                        or address in expected and expected[address] != patch
                        or address in observed and observed[address] != patch):
                    raise snapshot_error(snapshot, "state.form_defaults")
                observed[address] = patch
        else:
            partners = {}
            expected = {(entry["server"]["segment"], entry["server"]["resid"]): entry["state"] for entry in receipt["residues"]}
            for chain1, rid1, chain2, rid2 in [*old_rows, *new_rows]:
                left, right = (chain1, rid1), (chain2, rid2)
                if left == right:
                    raise snapshot_error(snapshot, "state.form_defaults")
                for address, partner in ((left, right), (right, left)):
                    chain, rid = address
                    if (not isinstance(chains.get(chain), dict) or chains[chain].get(rid) not in ("CYS", "CYX")
                            or address in expected and expected[address] != "DISU"
                            or address in partners and partners[address] != partner):
                        raise snapshot_error(snapshot, "state.form_defaults")
                    partners[address] = partner
        for row in new_rows:
            reverse = (row[2], row[3], row[0], row[1]) if prefix == "ssbonds" else None
            if row not in merged and reverse not in merged:
                merged.append(row)
        controls([switch, *names])
        fields[switch] = "1"
        for index, name in enumerate(names):
            fields[name] = [row[index] for row in merged]
    # A missing literal catalogue/chain correspondence is never inferred from
    # residue order or suffix alone. Protein atoms remain tied to exact IDs.
    return fields, receipt
