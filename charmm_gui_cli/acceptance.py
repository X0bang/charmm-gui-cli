"""Traceable final leaflet acceptance and scientific-review diagnostics.

These checks never certify equilibration or physical correctness. Lipid/leaflet
evidence and CHARMM normal termination are blocking; parameter penalties and
short minimization warnings remain explicit scientific-review items.
"""

from collections import Counter
import hashlib
import math
from pathlib import Path
import re

from .auth import ToolError
from .config import composition


def _evidence(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _lines(path):
    with Path(path).open(errors="replace") as handle:
        yield from handle


def _root(system_dir):
    root = Path(system_dir).resolve()
    if (root / "step3_size.str").is_file():
        return root
    found = list(root.rglob("step3_size.str"))
    if len(found) != 1:
        raise ToolError("Exactly one server step3_size.str is required to identify the final system root.")
    return found[0].parent


def _ratios(value):
    composition(value)
    names, values = value.split("=")
    values = [float(x) for x in values.split(":")]
    return {name: value / sum(values) for name, value in zip(names.split(":"), values)}


def _pdb_residues(path, segment):
    residues = {}
    atom_count = 0
    for number, line in enumerate(_lines(path), 1):
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        atom_count += 1
        if line[72:76].strip() != segment:
            continue
        if line[16:17].strip():
            raise ToolError("Alternate coordinates make leaflet identity ambiguous.")
        try:
            residue = int(line[22:27])
            z = float(line[46:54])
            if not math.isfinite(z):
                raise ValueError
        except ValueError:
            raise ToolError("Leaflet evidence requires finite coordinates and numeric CHARMM residue identities.") from None
        entry = residues.setdefault(residue, {"resname": line[17:21].strip(), "atoms": {}})
        name = line[12:16].strip()
        if entry["resname"] != line[17:21].strip() or name in entry["atoms"]:
            raise ToolError("Duplicate/ambiguous residue or atom identity in leaflet coordinates.")
        entry["atoms"][name] = {"z_A": z, "element": line[76:78].strip().upper(), "line": number}
    return residues, atom_count


def _psf_head_atoms(path):
    heads = {}
    with Path(path).open() as handle:
        for line in handle:
            if "!NATOM" in line:
                count = int(line.split()[0])
                break
        else:
            raise ToolError("Final PSF has no atom table for headgroup element verification.")
        for _ in range(count):
            fields = next(handle).split()
            if len(fields) < 8:
                raise ToolError("Malformed final PSF atom table.")
            if fields[1] == "MEMB" and fields[4] in ("P", "O3"):
                key = (int(fields[2]), fields[4])
                if key in heads:
                    raise ToolError("Duplicate headgroup atom identities in final PSF.")
                heads[key] = {"resname": fields[3], "mass_Da": float(fields[7]), "type": fields[5]}
    return heads, count


def check_leaflets(system_dir, membrane):
    """Verify packing/declared counts against final identities and headgroup Z.

    The supported CHARMM convention is HEAD 1..NLIPTOP at positive Z, followed
    by the lower leaflet; final MEMB identities and the same Z-side must agree.
    The box-center ZCEN is never treated as the membrane midplane.
    """
    if not isinstance(membrane, dict) or any(side not in membrane for side in ("upper", "lower")):
        raise ToolError("Leaflet acceptance requires explicit upper and lower composition strings.")
    root = _root(system_dir)
    paths = {"size": root / "step3_size.str", "packing_heads": root / "step3_packing_head.pdb",
             "coordinates": root / "step5_assembly.pdb"}
    for path in paths.values():
        if not path.is_file():
            raise ToolError(f"Missing required leaflet evidence: {path.name}.")
    counts, count_lines = {}, {}
    for number, line in enumerate(paths["size"].read_text().splitlines(), 1):
        match = re.fullmatch(r"\s*SET\s+(NLIPTOP|NLIPBOT)\s*=\s*(\d+)\s*", line, re.I)
        if match:
            key = match[1].upper()
            if key in counts:
                raise ToolError("Multiple leaflet-count assignments cannot be resolved safely.")
            counts[key], count_lines[key] = int(match[2]), number
    if set(counts) != {"NLIPTOP", "NLIPBOT"} or min(counts.values()) < 1:
        raise ToolError("Positive explicit NLIPTOP/NLIPBOT values are required.")
    upper, lower = counts["NLIPTOP"], counts["NLIPBOT"]
    expected_ids = set(range(1, upper + lower + 1))
    heads, _ = _pdb_residues(paths["packing_heads"], "HEAD")
    final, coordinate_atoms = _pdb_residues(paths["coordinates"], "MEMB")
    checks = {"packing_residue_identity": {"passed": set(heads) == expected_ids, "observed": len(heads)},
              "final_residue_identity": {"passed": set(final) == expected_ids, "observed": len(final)}}
    packing_sides = all(len(entry["atoms"]) == 1 and
                        (next(iter(entry["atoms"].values()))["z_A"] > 0 if resid <= upper else
                         next(iter(entry["atoms"].values()))["z_A"] < 0) for resid, entry in heads.items())
    checks["packing_leaflet_side"] = {"passed": bool(heads) and packing_sides,
                                      "scope": "HEAD IDs 1..NLIPTOP positive Z; remaining IDs negative Z"}
    psf_heads, psf_atoms = {}, None
    if (root / "step5_assembly.psf").is_file():
        psf_heads, psf_atoms = _psf_head_atoms(root / "step5_assembly.psf")
        paths["psf"] = root / "step5_assembly.psf"
        checks["psf_coordinate_atom_count"] = {"passed": psf_atoms == coordinate_atoms,
                                               "psf": psf_atoms, "coordinates": coordinate_atoms}
    missing_headgroups, wrong_elements, wrong_sides = [], [], []
    final_z = {"upper": [], "lower": []}
    for resid, entry in final.items():
        # Phospholipids with one explicitly named P; CHARMM cholesterol O3.
        name, element, mass = ("P", "P", 30.974) if "P" in entry["atoms"] else ("O3", "O", 15.999) if entry["resname"] == "CHL1" else (None, None, None)
        if name is None or name not in entry["atoms"]:
            missing_headgroups.append({"resid": resid, "resname": entry["resname"]})
            continue
        atom = entry["atoms"][name]
        psf = psf_heads.get((resid, name))
        psf_confirmed = bool(psf) and psf["resname"] == entry["resname"] and abs(psf["mass_Da"] - mass) < 0.02
        confirmed_element = (psf_confirmed and (not atom["element"] or atom["element"] == element)
                             if psf_atoms is not None else atom["element"] == element)
        if not confirmed_element:
            wrong_elements.append({"resid": resid, "atom": name, "pdb_element": atom["element"], "psf": psf})
        side = "upper" if resid <= upper else "lower"
        final_z[side].append(atom["z_A"])
        if (side == "upper" and atom["z_A"] <= 0) or (side == "lower" and atom["z_A"] >= 0):
            wrong_sides.append({"resid": resid, "resname": entry["resname"], "headgroup_z_A": atom["z_A"]})
    checks["final_headgroup_identity"] = {"passed": bool(final) and not missing_headgroups and not wrong_elements,
                                          "missing_or_unsupported": missing_headgroups, "element_mismatches": wrong_elements,
                                          "method": "Named P headgroups or CHL1 O3, confirmed by PDB elements or matching final PSF identity/natural mass"}
    checks["final_leaflet_side"] = {"passed": bool(final) and not missing_headgroups and not wrong_sides,
                                     "mismatches": wrong_sides,
                                     "headgroup_z_bounds_A": {side: [min(values), max(values)] if values else None for side, values in final_z.items()},
                                     "membrane_midplane_A": 0.0, "box_ZCEN_used_as_midplane": False}
    leaflets = {}
    for side, n in (("upper", upper), ("lower", lower)):
        belongs = (lambda i: i <= upper) if side == "upper" else (lambda i: i > upper)
        initial = Counter(v["resname"] for i, v in heads.items() if belongs(i))
        actual = Counter(v["resname"] for i, v in final.items() if belongs(i))
        ratios = _ratios(membrane[side])
        desired = {name: fraction * n for name, fraction in ratios.items()}
        deviations = {name: actual.get(name, 0) - amount for name, amount in desired.items()}
        composition_match = sum(actual.values()) == n and not (set(actual) - set(ratios)) and all(abs(x) <= 1.000001 for x in deviations.values())
        checks[f"{side}_packing_final_composition"] = {"passed": initial == actual and sum(actual.values()) == n,
                                                        "packing": dict(initial), "final": dict(actual)}
        checks[f"{side}_requested_ratio"] = {"passed": composition_match, "requested": membrane[side],
                                              "expected_fractional_counts": desired, "observed_counts": dict(actual),
                                              "count_deviations": deviations, "rounding_tolerance_molecules": 1.0}
        leaflets[side] = {"declared_total": n, "observed_total": sum(actual.values()), "lipid_counts": dict(actual),
                           "observed_fractions": {name: count / n for name, count in actual.items()}}
    passed = all(c["passed"] for c in checks.values())
    return {"passed": passed, "status": "passed" if passed else "failed", "blocking": True,
            "checks": checks, "leaflets": leaflets, "evidence": {key: _evidence(path) for key, path in paths.items()},
            "declared_count_lines": count_lines,
            "limitations": ["Supports the explicit CHARMM HEAD/MEMB numbering and membrane-at-Z=0 convention; rotated/recentered or unsupported headgroups cannot silently pass.",
                            "A composition-ratio tolerance of one molecule per species accounts only for discrete rounding."]}


def extract_cgenff_penalties(system_dir, ligand_resname="LIG", *, review_threshold=50.0):
    root = _root(system_dir)
    rtf, prm = root / "lig/lig.rtf", root / "lig/lig.prm"
    if not math.isfinite(review_threshold) or review_threshold < 0:
        raise ToolError("CGenFF review threshold must be a finite nonnegative value.")
    if not rtf.is_file() or not prm.is_file():
        return {"passed": None, "status": "incomplete", "blocking": False, "review_required": True,
                "warnings": ["Ligand CGenFF topology/parameter evidence is absent; penalty quality has not been assessed."], "evidence": {}}
    summaries = []
    for line_number, line in enumerate(rtf.read_text().splitlines(), 1):
        if re.match(r"\s*RESI\s+" + re.escape(ligand_resname) + r"\s", line, re.I):
            match = re.search(r"param penalty\s*=\s*([0-9.eE+-]+)\s*;\s*charge penalty\s*=\s*([0-9.eE+-]+)", line)
            if match:
                summaries.append({"parameter_max": float(match[1]), "charge_max": float(match[2]), "rtf_line": line_number})
    evidence = {"rtf": _evidence(rtf), "prm": _evidence(prm)}
    if len(summaries) != 1 or any(not math.isfinite(v) or v < 0 for k, v in summaries[0].items() if k.endswith("max")):
        return {"passed": None, "status": "incomplete", "blocking": False, "review_required": True,
                "warnings": ["No unique valid ligand-specific CGenFF penalty summary was found."], "evidence": evidence}
    summary = summaries[0]
    parameter_terms = [float(x) for x in re.findall(r"penalty\s*=\s*([0-9.eE+-]+)", prm.read_text())]
    maximum = max(summary["parameter_max"], summary["charge_max"], *parameter_terms)
    review = maximum > review_threshold
    return {"passed": True, "status": "review_required" if review else "reported", "blocking": False,
            **summary, "individual_parameter_max": max(parameter_terms) if parameter_terms else None,
            "review_required": review, "basic_validation_recommended": maximum >= 10,
            "review_threshold": review_threshold, "threshold_comparison": "strictly greater than",
            "threshold_source": "Generated CGenFF RTF/PRM header: penalties <10 indicate fair analogy, 10-50 recommend basic validation, >50 require extensive validation/optimization.",
            "warnings": ["CGenFF analogy penalties exceed the configured scientific review threshold."] if review else
                        ["CGenFF analogy penalties recommend basic validation."] if maximum >= 10 else [],
            "evidence": evidence, "scientific_correctness_verified": False,
            "limitation": "Low penalties do not establish physically accurate parameters, binding poses or equilibration."}


def summarize_charmm_minimization(system_dir):
    root = _root(system_dir)
    path = root / "step5_input.out"
    if not path.is_file():
        raise ToolError("Missing final step5_input.out; CHARMM completion cannot be verified.")
    counts, stages, events = Counter(), [], []
    previous_cycle = None
    normal, abnormal = False, False
    severity = None
    limit = re.compile(r"number of steps limit\s*\(\s*(\d+)\s*\)", re.I)
    number = r"[+-]?(?:\d+\.\d*|\.\d+)(?:[Ee][+-]?\d+)?"
    separator = r"(?:\s+|(?=[+-]))"  # CHARMM fixed-width negative columns may touch.
    mini = re.compile(r"^MINI>\s*(\d+)\s*(" + number + r")" + separator + r"(" + number + r")" + separator + r"(" + number + r")")
    for lineno, line in enumerate(_lines(path), 1):
        if "EPHI: WARNING" in line:
            counts["bent_improper_torsion"] += 1
        if "WARNING" in line:
            counts["all_warning_lines"] += 1
        if "ATOMS:********************" in line:
            counts["overflowed_warning_atom_fields"] += 1
        if "NORMAL TERMINATION BY NORMAL STOP" in line:
            normal = True
            events.append({"line": lineno, "text": line.strip()})
        if re.match(r"^[* ]*ABNORMAL TERMINATION", line):
            abnormal = True
            events.append({"line": lineno, "text": line.strip()})
        severe = re.search(r"MOST SEVERE WARNING WAS AT LEVEL\s+(-?\d+)", line)
        if severe:
            severity = int(severe[1])
        step_limit = limit.search(line) if "ABNER>" in line else None
        if step_limit:
            counts["minimization_step_limit_exits"] += 1
            events.append({"line": lineno, "text": line.strip(), "limit_steps": int(step_limit[1])})
        parsed = mini.match(line)
        if line.startswith("MINI>") and not parsed:
            counts["unparsed_minimization_rows"] += 1
        if parsed:
            cycle = int(parsed[1])
            row = {"line": lineno, "cycle": cycle, "energy": float(parsed[2]), "delta_energy": float(parsed[3]), "grms": float(parsed[4])}
            if not all(math.isfinite(row[key]) for key in ("energy", "delta_energy", "grms")):
                counts["nonfinite_minimization_rows"] += 1
            if previous_cycle is None or cycle <= previous_cycle:
                stages.append({"first": row, "last": row})
            else:
                stages[-1]["last"] = row
            previous_cycle = cycle
    warnings = []
    if counts["bent_improper_torsion"]:
        warnings.append(f"CHARMM reported {counts['bent_improper_torsion']} bent-improper warnings; inspect final lipid/ligand geometry before production simulation.")
    if counts["minimization_step_limit_exits"]:
        warnings.append("Setup minimization reached its step limit; this does not establish convergence or equilibration.")
    if severity is not None and severity > 0:
        warnings.append(f"CHARMM's most severe warning level was {severity}; normal termination is not a scientific-quality certificate.")
    passed = normal and not abnormal and bool(stages) and not counts["unparsed_minimization_rows"] and not counts["nonfinite_minimization_rows"]
    return {"passed": passed, "status": "passed_with_warnings" if passed and warnings else "passed" if passed else "failed",
            "blocking": True, "normal_termination": normal, "abnormal_termination": abnormal,
            "minimization_stages": stages, "warning_counts": dict(counts), "most_severe_warning_level": severity,
            "events": events, "warnings": warnings, "review_required": bool(warnings),
            "minimization_convergence_verified": False, "equilibration_verified": False,
            "evidence": {"final_log": _evidence(path)}}


def assess_acceptance(system_dir, *, membrane, ligand_resname="LIG", penalty_review_threshold=50.0):
    """Combine blocking evidence checks and clearly separated review warnings."""
    checks, warnings = {}, []
    tasks = {
        "leaflets": lambda: check_leaflets(system_dir, membrane),
        "cgenff": lambda: extract_cgenff_penalties(system_dir, ligand_resname, review_threshold=penalty_review_threshold),
        "charmm_minimization": lambda: summarize_charmm_minimization(system_dir),
    }
    for name, task in tasks.items():
        try:
            check = task()
        except (ToolError, OSError, ValueError, StopIteration) as exc:
            check = {"passed": False, "status": "incomplete", "blocking": name != "cgenff", "error": str(exc), "evidence": {}}
            if name == "cgenff":
                check["warnings"] = ["CGenFF review is incomplete: " + str(exc)]
                check["review_required"] = True
        checks[name] = check
        warnings.extend(check.get("warnings", []))
    blocking = [check for check in checks.values() if check["blocking"]]
    passed = bool(blocking) and all(check["passed"] is True for check in blocking)
    incomplete = any(check["status"] == "incomplete" for check in blocking)
    return {"passed": passed, "status": "passed_with_warnings" if passed and warnings else "passed" if passed else
            "incomplete" if incomplete else "failed", "checks": checks, "warnings": warnings,
            "review_required": bool(warnings), "scientific_correctness_verified": False,
            "scope": "Final leaflet composition plus server completion evidence; CGenFF and minimization warnings remain scientific review items",
            "evidence": {name: check["evidence"] for name, check in checks.items()}}
