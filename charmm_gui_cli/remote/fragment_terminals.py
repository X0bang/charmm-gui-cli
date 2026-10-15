"""Bind verified fragment end requests to one literal saved PDB Reader form.

No HTTP, JavaScript evaluation, residue renumbering or suffix/sequence guessing.
This records form correspondence only; actual source/final PSF proof is separate.
"""

from copy import deepcopy
import hashlib
from pathlib import Path
import re

from ..chem.fragment_terminals import resolve_fragment_terminals
from .protein_states import literal_catalogue
from .web import soup_for, snapshot_error


def bind_fragment_terminals(snapshot, preparation, source_lines, prepared_lines, plan):
    resolved = resolve_fragment_terminals(preparation, source_lines, prepared_lines, plan)
    content = Path(snapshot).read_bytes()
    soup = soup_for(content.decode("utf-8"))
    forms = [form for form in soup.find_all("form") if form.select_one('input[name="jobid"]')]
    chains = literal_catalogue(soup, "prot_chains")
    def fail():
        return snapshot_error(snapshot, "fragment.form_missing")
    if len(forms) != 1 or not isinstance(chains, dict):
        raise fail()
    form = forms[0]
    jobids = form.find_all("input", {"name": "jobid"})
    if len(jobids) != 1 or not re.fullmatch(r"[0-9]{10}", jobids[0].get("value", "")):
        raise fail()
    if jobids[0].has_attr("disabled") or jobids[0].find_parent("fieldset", disabled=True):
        raise fail()
    fields = {}
    for node in form.select("[name]"):
        name = node["name"]
        if not name.startswith("terminal["):
            continue
        if (node.name != "select" or not re.fullmatch(r"terminal\[[^]]+\]\[(first|last)\]", name)
                or name in fields or node.has_attr("disabled") or node.find_parent("fieldset", disabled=True)
                or node.has_attr("multiple")):
            raise fail()
        fields[name] = node
    server_preparation = {key: value for key, value in resolved["preparation"].items() if key != "terminals"}
    server_preparation["terminals"] = {}
    bindings, used, expected_fields = [], set(), set()
    histidines = {"HIS", "HSD", "HSE", "HSP"}
    for fragment, row in zip(plan["fragments"], resolved["fragments"]):
        chain = fragment["prepared_chain"]
        candidates = [name for name in (chain, "PRO" + chain, "PRO-" + chain) if name in chains]
        if len(candidates) != 1 or candidates[0] in used:
            raise fail()
        server = candidates[0]
        observed = chains[server]
        expected = {str(entry["resid"]) + entry["icode"]: entry["resname"] for entry in fragment["source_residues"]}
        if not isinstance(observed, dict) or set(observed) != set(expected):
            raise fail()
        for rid, name in expected.items():
            allowed = histidines if name in histidines else {"CYS", "CYM"} if name == "CYM" else {name}
            if not isinstance(observed[rid], str) or observed[rid] not in allowed:
                raise fail()
        binding = deepcopy(row)
        binding["server_chain"] = server
        for end, suffix in (("n_terminal", "first"), ("c_terminal", "last")):
            name = f"terminal[{server}][{suffix}]"
            if name not in fields:
                raise fail()
            expected_fields.add(name)
            binding["terminals"][end]["field"] = name
        server_preparation["terminals"][server] = dict(resolved["preparation"]["terminals"][chain])
        used.add(server)
        bindings.append(binding)
    if used != set(chains) or expected_fields != set(fields):
        raise fail()
    return server_preparation, {"schema_version": 1, "plan_fingerprint": plan["fingerprint"],
        "snapshot_sha256": hashlib.sha256(content).hexdigest(), "source_job_id": jobids[0]["value"],
        "requested_preparation": resolved["requested_preparation"], "fragments": bindings,
        "scope": "saved_form_terminal_binding_only", "submission_confirmed": False, "remote_verified": False}
