"""Local input checks with explicit structural review findings, never repair.

The preparation adapters remain authoritative for chemistry and coordinates.
Passing this check means the selected local input workflow succeeded, not that
the protein is complete, correctly protonated, parameterized, or simulation-ready.
"""

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path

from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.chem.input_sources import prepare_sources
from charmm_gui_cli.chem.inputs import _pdb, inspect_ligand, prepare_inputs, read_ligand
from charmm_gui_cli.chem.pdb_normalization import normalize_lines
from .residues import BACKBONE as _BACKBONE, SIDECHAINS as _SIDECHAINS, ALIASES as _ALIASES, MODIFIED, validate_input_options
from ..locales.i18n import t


def _file_evidence(value):
    if not isinstance(value, (str, Path)):
        raise ToolError(key="input.path_type", next_step=t("input.path_next"))
    from .input_sources import _path
    try:
        path = _path(value, "Input")
    except ToolError as error:
        # Retain the established machine-readable message while adding context.
        raise ToolError("An input file does not exist or is not a regular file.", key=error.key,
                        params=error.params, next_step=error.next_step) from None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def inspect_protein(path, *, excluded_resnames=(), lines=None):
    """Inspect every explicit model before strict preparation rejects ambiguity.

    Numbering gaps are coordinate-index observations only. No sequence alignment
    or missing-residue reconstruction is attempted. TER boundaries, chains and
    segments are kept separate; insertion codes are retained in residue IDs.
    """
    from .pdb_layout import validate_layout, FIELD_LABELS
    from .structure_inventory import NUCLEIC_NAMES, GLYCAN_NAMES
    non_peptide_names = NUCLEIC_NAMES | GLYCAN_NAMES
    lines = list(lines) if lines is not None else Path(path).read_text(encoding="utf-8").splitlines()
    validate_layout(lines)
    groups, chains, altlocs = {}, defaultdict(list), []
    model, model_count, block, atom_count = 0, 0, 0, 0
    sequence_records = False
    for line_number, line in enumerate(lines, 1):
        record = line[:6].strip()
        if record == "MODEL":
            model_count += 1
            model = model_count
            block = 0
        elif record == "TER":
            block += 1
        elif record == "SEQRES":
            sequence_records = True
        if record not in ("ATOM", "HETATM"):
            continue
        atom_count += 1
        try:
            residue_number = int(line[22:26])
        except ValueError:
            raise ToolError(key="input.pdb_columns", params={"line": line_number, "field": FIELD_LABELS["residue"]},
                            next_step=t("input.pdb_columns_next")) from None
        identity = {"model": model if model_count else 1, "chain": line[21:22].strip(),
                    "residue_number": residue_number, "insertion_code": line[26:27].strip(),
                    "segment": line[72:76].strip(), "resname": line[17:21].strip()}
        if line[16:17].strip():
            altlocs.append({**identity, "atom": line[12:16].strip(), "altloc": line[16:17]})
        if identity["resname"] in non_peptide_names:
            continue  # These are not peptide residues or peptide numbering gaps.
        if record != "ATOM" and identity["resname"] not in MODIFIED:
            continue
        if record == "HETATM" and identity["resname"] in excluded_resnames and identity["resname"] not in MODIFIED:
            continue
        key = (identity["model"], block, identity["chain"], identity["segment"], residue_number,
               identity["insertion_code"], identity["resname"])
        if key not in groups:
            groups[key] = {"identity": identity, "atoms": set()}
            chains[key[:4]].append(key)
        if line[76:78].strip().upper() not in ("H", "D", "T"):
            groups[key]["atoms"].add(line[12:16].strip())
    missing, unknown, gaps = [], [], []
    for value in groups.values():
        identity, present = value["identity"], set(value["atoms"])
        template = MODIFIED.get(identity["resname"], _ALIASES.get(identity["resname"], identity["resname"]))
        if template not in _SIDECHAINS:
            unknown.append(identity)
            continue
        # Common terminal/CHARMM naming aliases are not missing heavy atoms.
        if identity["resname"] == "MSE" and "SE" in present:
            present.add("SD")
        if "OT1" in present:
            present.add("O")
        if template == "ILE" and "CD" in present:
            present.add("CD1")
        backbone, sidechain = sorted(_BACKBONE - present), sorted(_SIDECHAINS[template] - present)
        if backbone or sidechain:
            missing.append({**identity, "template": template, "missing_backbone_atoms": backbone,
                            "missing_sidechain_atoms": sidechain, "review_required": True, "repaired": False})
    for keys in chains.values():
        for left, right in zip(keys, keys[1:]):
            if right[4] - left[4] > 1:
                gaps.append({"before": groups[left]["identity"], "after": groups[right]["identity"],
                             "unused_integer_numbers": right[4] - left[4] - 1,
                             "missing_residues_inferred": False, "review_required": True})
    from .structure_inventory import inspect_chains
    return {"path": str(Path(path).resolve()), "models": model_count or (1 if atom_count else 0),
            "structure_inventory": inspect_chains(lines),
            "explicit_model_records": model_count, "atoms_all_models": atom_count,
            "protein_residues_all_models": len(groups), "alternate_locations": altlocs,
            "missing_heavy_atoms": missing, "unknown_residue_templates": unknown, "numbering_gaps": gaps,
            "missing_residues_inferred": None, "sequence_records_present": sequence_records,
            "sequence_alignment_performed": False,
            "review_required": bool(missing or unknown or gaps or altlocs or model_count > 1),
            "interpretation": "编号缺口不保证存在缺失残基；未与参考序列比对，无法据坐标断言缺失残基数。",
            "limitations": ["Only standard amino-acid heavy-atom names and listed protonation-name aliases are checked.",
                            "Missing backbone/sidechain atoms are reported for review, never rebuilt; terminal OXT is optional.",
                            "No sidechain conformation, disulfide assignment, pH state, sequence completeness or membrane orientation is certified."]}


def _chemistry(path, resname):
    molecule = read_ligand(path)
    checked = inspect_ligand(path)
    graph = {"atoms": [{"name": atom.name, "element": atom.element, "formal_charge": atom.charge} for atom in molecule.atoms],
             "bonds": [{"atoms": [molecule.atoms[a].name, molecule.atoms[b].name], "order": order}
                       for (a, b), order in sorted(molecule.bonds.items())]}
    return {"resname": resname, **checked, "net_formal_charge": sum(atom.charge for atom in molecule.atoms),
            "chemical_graph": graph, "graph_connected": True,
            "graph_sha256": hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest(),
            "prepared_sdf": str(Path(path).resolve()), "force_field_quality_verified": False}


def _atom_label(atom, owner):
    result = {"owner": owner, "atom_name": atom.name, "element": atom.element, "source_serial": atom.serial}
    if atom.line:
        result.update(resname=atom.line[17:21].strip(), chain=atom.line[21:22].strip(),
                      residue_number=int(atom.line[22:26]), insertion_code=atom.line[26:27].strip(),
                      segment=atom.line[72:76].strip())
    return result


def _extent(atoms):
    count = len(atoms)
    return {"heavy_atoms": count, "minimum_A": [min(a.xyz[axis] for a in atoms) for axis in range(3)],
            "maximum_A": [max(a.xyz[axis] for a in atoms) for axis in range(3)],
            "centroid_A": [sum(a.xyz[axis] for a in atoms) / count for axis in range(3)],
            "centroid_method": "Unweighted heavy-atom centroid; not center of mass"}


def _pair_distances(left, right, left_owner, right_owner, *, near_A, severe_A, contact_A, sample_limit):
    """Stream pairs with bounded samples; never allocate an atom-pair matrix."""
    closest, closest_pair = math.inf, None
    near_count, severe_count, contact_count = 0, 0, 0
    near_examples, left_contacts, right_contacts, left_residues = [], set(), set(), set()
    near_sq, severe_sq, contact_sq = near_A ** 2, severe_A ** 2, contact_A ** 2
    for i, a in enumerate(left):
        ax, ay, az = a.xyz
        for j, b in enumerate(right):
            dx, dy, dz = ax - b.xyz[0], ay - b.xyz[1], az - b.xyz[2]
            squared = dx * dx + dy * dy + dz * dz
            if squared < closest:
                closest, closest_pair = squared, (a, b)
            if squared <= contact_sq:
                contact_count += 1
                left_contacts.add(i)
                right_contacts.add(j)
                if a.line:
                    left_residues.add((a.line[17:27], a.line[72:76]))
            if squared < near_sq:
                near_count += 1
                severe_count += squared < severe_sq
                if len(near_examples) < sample_limit:
                    near_examples.append({"distance_A": math.sqrt(squared), "left": _atom_label(a, left_owner),
                                          "right": _atom_label(b, right_owner), "severe_overlap": squared < severe_sq})
    return {"left": left_owner, "right": right_owner, "minimum_distance_A": math.sqrt(closest),
            "closest_pair": {"left": _atom_label(closest_pair[0], left_owner), "right": _atom_label(closest_pair[1], right_owner)},
            "near_pair_count": near_count, "severe_overlap_pair_count": severe_count,
            "near_pair_examples": near_examples, "examples_truncated": near_count > len(near_examples),
            "example_order": "Input atom order, not sorted by severity; closest_pair separately records the minimum",
            "contact_pair_count": contact_count, "left_contact_heavy_atoms": len(left_contacts),
            "right_contact_heavy_atoms": len(right_contacts), "left_contact_residues": len(left_residues),
            "review_required": near_count > 0}


def inspect_spatial_review(protein_atoms, components, *, near_A=2.0, severe_A=0.8, contact_A=4.0,
                           distant_A=6.0, sample_limit=50):
    """Describe inter-component geometry without guessing bonds or repairing it.

    ``components`` lists {id, resname, atoms}; each atom is an inputs.Atom in the
    same coordinate frame. Protein-protein and within-component pairs are never
    evaluated, so peptide/ligand internal bonds cannot become clash findings.
    Distance cutoffs are conservative review heuristics, not atomic-radius or
    energy calculations. Existing preparation failures remain authoritative.
    """
    values = (severe_A, near_A, contact_A, distant_A)
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values) or not 0 < severe_A < near_A <= contact_A <= distant_A:
        raise ToolError("Spatial review needs finite positive ordered severe/near/contact/distant cutoffs.")
    if type(sample_limit) is not int or not 0 <= sample_limit <= 1000:
        raise ToolError("Spatial review sample_limit must be an integer between 0 and 1000.")
    protein = [a for a in protein_atoms if a.element.upper() not in ("H", "D", "T")]
    groups = [{**item, "atoms": [a for a in item["atoms"] if a.element.upper() not in ("H", "D", "T")]} for item in components]
    if not protein or not groups or any(not item["atoms"] for item in groups) or len({item["id"] for item in groups}) != len(groups):
        raise ToolError("Spatial review requires protein heavy atoms and uniquely identified components with heavy atoms.")
    if any(len(a.xyz) != 3 or not all(math.isfinite(v) for v in a.xyz) for group in [protein, *(item["atoms"] for item in groups)] for a in group):
        raise ToolError("Spatial review requires finite three-dimensional coordinates.")
    options = {"near_A": near_A, "severe_A": severe_A, "contact_A": contact_A, "sample_limit": sample_limit}
    details, component_pairs, distant = [], [], []
    for group in groups:
        pairing = _pair_distances(protein, group["atoms"], "protein", group["id"], **options)
        is_distant = pairing["minimum_distance_A"] > distant_A
        if is_distant:
            distant.append(group["id"])
        details.append({"id": group["id"], "resname": group["resname"], "extent": _extent(group["atoms"]),
                        "protein_contacts": pairing, "beyond_distant_threshold": is_distant,
                        "review_required": pairing["review_required"] or is_distant})
    for i, left in enumerate(groups):
        for right in groups[i + 1:]:
            component_pairs.append(_pair_distances(left["atoms"], right["atoms"], left["id"], right["id"], **options))
    all_pairs = [item["protein_contacts"] for item in details] + component_pairs
    near_count = sum(item["near_pair_count"] for item in all_pairs)
    severe_count = sum(item["severe_overlap_pair_count"] for item in all_pairs)
    review = bool(near_count or distant)
    warnings = []
    if near_count:
        warnings.append(f"{near_count} inter-component heavy-atom pairs are closer than {near_A:g} A; inspect these unusually short contacts. This is not a force-field clash calculation.")
    if distant:
        warnings.append(f"{len(distant)} components have no protein heavy atom within {distant_A:g} A; verify their intended bound coordinate frame.")
    return {"status": "review_required" if review else "reported", "blocking": False, "review_required": review,
            "summary": {"component_count": len(groups), "near_pair_count": near_count, "severe_overlap_pair_count": severe_count,
                        "distant_component_ids": distant, "review_required": review},
            "protein_extent": _extent(protein), "components": details, "component_pairs": component_pairs,
            "thresholds_A": {"near_strictly_below": near_A, "severe_strictly_below": severe_A,
                              "contact_at_or_below": contact_A, "distant_strictly_above": distant_A},
            "threshold_basis": "Explicit local-review heuristics; not inferred chemical bonds, van der Waals radii, energies or calibrated physical clash scores.",
            "sample_limit_per_pair": sample_limit, "pair_evaluation": "Streaming nested iteration, no dense distance matrix; counts include all evaluated pairs.",
            "warnings": warnings, "coordinates_modified": False, "bonds_inferred": False,
            "membrane_orientation_verified": False, "equilibration_verified": False, "scientific_correctness_verified": False,
            "limitations": ["Only protein-component and distinct component-component heavy-atom pairs are evaluated; protein internal clashes and intramolecular geometry are not assessed.",
                            "No periodic unwrapping, docking, alignment, membrane-frame inference, hydrogen-contact assessment or coordinate repair is performed.",
                            "A nearby ligand or absence of flagged short contacts does not establish a correct binding pose or simulation readiness."]}


def _complex_spatial_review(path, specifications, *, input_options=None, role="complex"):
    """Use explicit selectors and the bound complex, never the chemical SDF pose."""
    atoms, _ = _pdb(path, input_options=input_options, role=role)
    groups, selected = [], set()
    for index, spec in enumerate(specifications, 1):
        if not {"resname", "chain", "resid"} <= spec.keys():
            raise ToolError("Spatial review requires explicit component resname/chain/resid selectors.")
        chosen = [a for a in atoms if a.line[17:21].strip() == spec["resname"] and a.line[21:22].strip() == spec["chain"]
                  and int(a.line[22:26]) == spec["resid"] and ("icode" not in spec or a.line[26:27].strip() == spec["icode"])
                  and ("segment" not in spec or a.line[72:76].strip() == spec["segment"])]
        blocks = {atom.polymer_block for atom in chosen}
        if len(blocks) > 1:
            raise ToolError(key="input.component_ter_ambiguous", params={"residue": " ".join(
                (spec["resname"], spec["chain"] or "-", str(spec["resid"]))), "count": len(blocks)},
                next_step="charmm-gui-cli check --help")
        if not chosen or len({(a.line[17:27], a.line[72:76]) for a in chosen}) != 1 or any(a.serial in selected for a in chosen):
            raise ToolError("Spatial review requires unambiguous, non-overlapping component residue selectors.")
        selected.update(a.serial for a in chosen)
        groups.append({"id": f"component-{index:03d}", "resname": spec["resname"], "atoms": chosen})
    protein = [a for a in atoms if a.line.startswith("ATOM  ") and a.serial not in selected]
    return inspect_spatial_review(protein, groups)


def _record_spatial_review(report, review):
    report["spatial_review"] = review
    report["review_required"] = report["review_required"] or review["review_required"]
    report["checks"]["spatial_review"] = {"passed": True, "blocking": False, "review_required": review["review_required"],
                                           "meaning": "Inter-component distance inventory completed; not a clash-free or correct-pose certificate."}


def check_inputs(*, out, protein=None, ligand=None, complex_pdb=None, ligand_resname="LIG",
                 ligand_smiles=None, accept_conect_bond_orders=False, hydrogens="add_missing", components=None, input_options=None,
                 fragment_preparation=False):
    """Prepare and inspect in a new directory; persist failures as check.json.

    Existing output directories are rejected without any write. ``components``
    is a strict components YAML filename, using the same preparation and policy
    checks as multi-component builds. No task or login session is created.
    """
    directory = Path(out).expanduser().resolve()
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError:
        import shlex
        raise ToolError("Precheck out must be a new directory; existing outputs are never overwritten.",
                        key="input.check_occupied", params={"path": str(directory)},
                        next_step=t("input.output_next", command=shlex.join(["ls", "-ld", str(directory)]))) from None
    report = {"schema_version": 1, "passed": False, "status": "checking", "directory": str(directory),
              "check_file": str(directory / "check.json"), "review_required": False, "input_evidence": {},
              "checks": {}, "prepared_complex": None, "component_resnames": [], "chemistry": [],
              "server_submitted": False, "task_registered": False, "protein_repaired": False,
              "structure_complete_verified": False, "scientific_correctness_verified": False,
              "requested_hydrogen_policy": hydrogens,
              "limitations": ["本地预检通过不等于结构完善、质子化状态正确、力场质量合格或可直接开展模拟。",
                              "只报告蛋白缺失重原子与编号缺口，不自动修补；不执行序列比对、对接、远端参数化或 MD。",
                              "净形式电荷只针对显式定义的配体/组分，不推断蛋白或整个体系的净电荷。"]}
    stage = "options"
    try:
        options = validate_input_options(input_options)
        from .fragment_inputs import validate_fragment_option
        validate_fragment_option(fragment_preparation)
        fragment_options = {"fragment_preparation": True} if fragment_preparation else {}
        report["input_options"] = options
        if hydrogens not in ("preserve", "add_missing"):
            raise ToolError("hydrogens must be preserve or add_missing.")
        if type(accept_conect_bond_orders) is not bool:
            raise ToolError("accept_conect_bond_orders must be boolean.")
        if not isinstance(ligand_resname, str):
            raise ToolError("ligand_resname must be a residue-name string.")
        stage = "input_evidence"
        for role, value in (("protein", protein), ("ligand", ligand), ("complex", complex_pdb)):
            if value is not None:
                report["input_evidence"][role] = _file_evidence(value)
        if components is not None:
            report["input_evidence"]["components_config"] = _file_evidence(components)
            from charmm_gui_cli.chem.multicomponent import load_components_config
            component_config = load_components_config(components)
            for index, component in enumerate(component_config["components"], 1):
                if component.get("sdf") is not None:
                    report["input_evidence"][f"component_{index}_sdf"] = _file_evidence(component["sdf"])
                topology = component.get("topology")
                if isinstance(topology, dict):
                    for key in ("rtf", "prm"):
                        if topology.get(key) is not None:
                            report["input_evidence"][f"component_{index}_{key}"] = _file_evidence(topology[key])
        stage = "protein_inventory"
        coordinate_source = complex_pdb or protein
        if coordinate_source is not None:
            excluded = [item.get("resname") for item in component_config["components"]] if components is not None else [ligand_resname]
            try:
                inventory = inspect_protein(coordinate_source, excluded_resnames=excluded)
            except ToolError as inventory_error:
                # Explicit model selection can exclude malformed other models.
                # Shared normalization remains the authoritative blocking path.
                report["original_inventory_error"] = inventory_error.as_dict()
            else:
                report["protein_inventory"] = inventory
                report["original_protein_inventory"] = inventory
                report["review_required"] = inventory["review_required"]
            normalization = {"repairs": []}
            report["input_normalization"] = normalization
            report["automatic_repairs"] = normalization["repairs"]
            role = "components" if components is not None and component_config["water_policy"] == "retain" else "complex" if complex_pdb else "protein"
            normalized = normalize_lines(coordinate_source, normalization, options=options, role=role)
            report["terminal_predictions"] = normalization["terminal_predictions"]
            inventory = inspect_protein(coordinate_source, excluded_resnames=excluded, lines=normalized)
            report["protein_inventory"] = inventory
            report["review_required"] = inventory["review_required"]
            report["checks"][stage] = {"passed": True, "review_required": inventory["review_required"],
                                      "meaning": "Inventory completed; not a protein-completeness certificate."}
        stage = "source_preparation"
        if components is None:
            sources = prepare_sources(out=directory / "sources", protein=protein, ligand=ligand,
                                      complex_pdb=complex_pdb, ligand_resname=ligand_resname,
                                      ligand_smiles=ligand_smiles, accept_conect_bond_orders=accept_conect_bond_orders, input_options=options, **fragment_options)
            report["sources"] = sources["report"]
            report["checks"][stage] = {"passed": True}
            protein_atoms, _ = _pdb(sources["protein"])
            molecule = read_ligand(sources["ligand"])
            _record_spatial_review(report, inspect_spatial_review(protein_atoms, [
                {"id": "ligand", "resname": ligand_resname, "atoms": molecule.atoms}]))
            stage = "input_preparation"
            prepared = prepare_inputs(sources["protein"], sources["ligand"], directory / "prepared",
                                      ligand_resname=ligand_resname, hydrogen_policy=hydrogens, **fragment_options)
            from .pdb_normalization import attach_source_normalization
            attach_source_normalization(prepared, sources["report"])
            report["input_normalization"] = prepared["manifest"]["input_normalization"]
            report["automatic_repairs"] = prepared["manifest"]["automatic_repairs"]
            report.update(prepared_complex=prepared["files"]["complex.pdb"], prepared_files=prepared["files"],
                          component_resnames=[ligand_resname])
            report["checks"][stage] = {"passed": True}
            stage = "chemical_graph"
            report["chemistry"] = [_chemistry(prepared["files"]["ligand.sdf"], ligand_resname)]
            report["chemistry"][0]["ligand_source"] = prepared["manifest"].get("ligand_selection", {})
            conversion = report["chemistry"][0]["ligand_source"].get("conversion", {})
            if conversion.get("review_required"):
                report["review_required"] = True
                report["chemistry"][0].setdefault("chemistry_warnings", []).append({"key": "input.mol2_review", "params": {"partial": f"{conversion['partial_charge_sum']:.4f}", "formal": conversion["net_formal_charge"]}})
        else:
            if complex_pdb is None or any(value is not None for value in (protein, ligand, ligand_smiles)) or accept_conect_bond_orders:
                raise ToolError("Component precheck requires complex_pdb and per-component chemical definitions; do not mix single-ligand inputs/options.")
            _record_spatial_review(report, _complex_spatial_review(complex_pdb, component_config["components"], input_options=options, role=role))
            from charmm_gui_cli.chem.multicomponent import prepare_build_components
            prepared = prepare_build_components(complex_pdb=complex_pdb, components_config=components,
                                                out=directory / "prepared", hydrogens=hydrogens, input_options=options, **fragment_options)
            if fragment_preparation:
                report["input_normalization"] = prepared["manifest"]["input_normalization"]
            report.update(prepared_complex=prepared["files"]["complex.pdb"], prepared_files=prepared["files"],
                          component_preparation=prepared["manifest"],
                          component_resnames=sorted({item["resname"] for item in prepared["manifest"]["components"]}))
            report["automatic_repairs"] = prepared["manifest"].get("automatic_repairs", report.get("automatic_repairs", []))
            report["checks"][stage] = {"passed": True}
            stage = "chemical_graph"
            report["chemistry"] = [{**_chemistry(item["sdf"], item["resname"]), "component_id": item["id"]}
                                   for item in prepared["manifest"]["components"]]
        report["checks"][stage] = {"passed": True}
        if report.get("input_normalization", {}).get("protein_state_intent", {}).get("residues"):
            report["review_required"] = True
        if report.get("input_normalization", {}).get("fragment_preparation"):
            report["review_required"] = True
            report["prepared_terminal_predictions"] = report["input_normalization"]["terminal_predictions"]
            report["prepared_structure_inventory"] = report["input_normalization"]["structure_inventory"]
        if any(not item["rdkit_sanitized"] for item in report["chemistry"]):
            report["review_required"] = True
        report.update(passed=True, hydrogen_policy=hydrogens,
                      status="passed_with_review" if report["review_required"] else "passed")
    except (ToolError, OSError, UnicodeError) as exc:
        if isinstance(exc, ToolError) and exc.key and exc.key.startswith(("input.", "state.")):
            from .input_diagnostics import bind_input_error, retention_draft
            bind_input_error(exc, protein=protein, ligand=ligand, complex_pdb=complex_pdb,
                             ligand_resname=ligand_resname, components=components, input_options=report.get("input_options", {}), ligand_smiles=ligand_smiles)
            if exc.key == "input.protein_hetero":
                report["heterogen_inventory"] = [detail for detail in exc.details if detail.get("kind")]
                candidates = [{"resname": detail["resname"], "chain": detail["chain"], "resid": detail["resid"],
                               "icode": detail["icode"], "segment": detail["segment"], **detail["matching_ligand_coordinates"]}
                              for detail in report["heterogen_inventory"] if detail.get("matching_ligand_coordinates")]
                if candidates:
                    report["duplicate_ligand_candidates"] = candidates
                try:
                    draft = retention_draft(directory, source=complex_pdb or protein, ligand=ligand,
                                            ligand_resname=ligand_resname, input_options=report.get("input_options", {}), findings=exc.details)
                    if draft:
                        report["retention_draft"] = draft
                        exc.details.append({"key": "input.draft", "params": {"config": draft["config"], "complex": draft["complex"], "command": draft["next_step"]}})
                except (ToolError, OSError, ValueError):
                    # A draft failure must not hide the authoritative input error.
                    pass
        error = exc.as_dict() if isinstance(exc, ToolError) else {
            "category": "local_io", "message": "Cannot read or prepare local input files; retained outputs can be inspected.",
            "retryable": False, "next_step": "Check local paths, permissions and text encoding; use a new output directory."}
        if isinstance(exc, OSError):
            from ..store.diagnostics import filesystem_error
            diagnostic = filesystem_error(exc)
            error.update(message_key=diagnostic.key, message_params=diagnostic.params, next_step=diagnostic.next_step)
        report.update(status="failed", error={"stage": stage, **error})
        report["checks"][stage] = {"passed": False, "error": error}
    with (directory / "check.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return report
