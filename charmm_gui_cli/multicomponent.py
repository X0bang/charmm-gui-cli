"""Explicit, coordinate-preserving preparation of noncovalent components.

Each ligand instance is selected by PDB residue identity. Chemistry is provided
by a uniquely atom-named SDF or explicitly acknowledged CONECT bond orders.
This module neither guesses connectivity nor parameterizes retained components.
"""

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

from .auth import ToolError
from .input_sources import _chemical_sdf_in_complex, _internal
from .inputs import Ligand, _apply_hydrogen_policy, _conect, _pdb, _sdf, _validate_ligand, _validate_protein, read_ligand


_PROTEIN = set("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL HSD HSE HSP HID HIE HIP ASH GLH LYN CYM CYX".split())
_WATER = {"HOH", "WAT", "TIP3"}
_IONS = {"NA": ("Na", 1), "SOD": ("Na", 1), "K": ("K", 1), "POT": ("K", 1), "CL": ("Cl", -1), "CLA": ("Cl", -1)}
_ORGANIC = set("H B C N O F Si P S Cl Br I".split())


def _file(path, label):
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise ToolError(f"{label} does not exist: {resolved}.")
    return resolved


def _evidence(path):
    path = Path(path)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def _identity(atom):
    line = atom.line
    return (line[17:21].strip(), line[21:22].strip(), int(line[22:26]), line[26:27].strip(), line[72:76].strip())


def _identity_dict(key):
    return dict(zip(("resname", "chain", "resid", "icode", "segment"), key))


def _select(spec, residues):
    if not isinstance(spec, dict) or not {"resname", "chain", "resid"} <= spec.keys():
        raise ToolError("Each component needs explicit resname, chain and resid selectors.")
    unknown = set(spec) - {"resname", "chain", "resid", "icode", "segment", "sdf", "accept_conect_bond_orders", "topology", "role"}
    if unknown:
        raise ToolError(f"Unknown component options: {', '.join(sorted(unknown))}.")
    if not isinstance(spec["resname"], str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,2}", spec["resname"]):
        raise ToolError("Component resname must be 1-3 uppercase alphanumeric PDB characters, starting with a letter.")
    if not isinstance(spec["chain"], str) or len(spec["chain"]) > 1 or isinstance(spec["resid"], bool) or not isinstance(spec["resid"], int):
        raise ToolError("Component chain must be zero/one character and resid an integer.")
    if spec.get("role", "ligand") not in ("ligand", "cofactor"):
        raise ToolError("Component role must be ligand or cofactor.")
    matches = [key for key in residues if key[:3] == (spec["resname"], spec["chain"], spec["resid"]) and
               all(key[index] == spec[field] for field, index in (("icode", 3), ("segment", 4)) if field in spec)]
    if len(matches) != 1:
        raise ToolError("Component selector must identify exactly one residue; specify icode/segment when ambiguous.")
    return matches[0]


def _signature(molecule):
    payload = {"atoms": sorted((a.name, a.element, a.charge) for a in molecule.atoms),
               "bonds": sorted((*sorted((molecule.atoms[a].name, molecule.atoms[b].name)), order)
                               for (a, b), order in molecule.bonds.items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), payload


def _topology(spec, molecule, resname):
    """Check a trusted, standalone CHARMM residue against its chemical sidecar.

    Parameter completeness and physical accuracy are not certified here. RTF
    bonds do not reliably encode aromatic chemical orders, hence the SDF sidecar.
    """
    if not isinstance(spec, dict) or set(spec) != {"rtf", "prm", "trusted"} or spec["trusted"] is not True:
        raise ToolError("Existing topology requires exactly rtf, prm and trusted=true, plus the component's named SDF chemical sidecar.")
    rtf, prm = _file(spec["rtf"], "RTF"), _file(spec["prm"], "PRM")
    if not prm.read_text().strip():
        raise ToolError("Existing parameter file is empty.")
    atom_rows, edges, residue_charge = {}, set(), None
    active, occurrences = False, 0
    for line in rtf.read_text().splitlines():
        fields = line.split("!", 1)[0].split()
        if not fields or fields[0].startswith("*"):
            continue
        command = fields[0].upper()
        if command in ("RESI", "RESIDUE", "PRES", "PRESIDUE"):
            active = command in ("RESI", "RESIDUE") and len(fields) > 1 and fields[1] == resname
            if active:
                occurrences += 1
                try:
                    residue_charge = float(fields[2])
                except (ValueError, IndexError):
                    raise ToolError("Malformed RTF residue charge.") from None
            continue
        if not active:
            continue
        if command in ("PATCH", "DELETE", "LONEPAIR", "DRUDE") or any(token.startswith(("+", "-")) and not re.fullmatch(r"[+-]?\d+(?:\.\d*)?(?:[Ee][+-]?\d+)?", token) for token in fields[1:]):
            raise ToolError("Patched, external-bond, polarizable or lone-pair topology requires a specialized workflow.")
        if command == "PATCHING" and fields[1:] != ["FIRST", "NONE", "LAST", "NONE"]:
            raise ToolError("Only standalone unpatched topology with PATCHING FIRST NONE LAST NONE is supported.")
        if command == "ATOM":
            if len(fields) < 4 or fields[1] in atom_rows:
                raise ToolError("Malformed or duplicate RTF atom identity.")
            try:
                atom_rows[fields[1]] = {"type": fields[2], "charge": float(fields[3])}
            except ValueError:
                raise ToolError("Malformed RTF atom charge.") from None
        if command in ("BOND", "DOUBLE", "TRIPLE"):
            if len(fields[1:]) % 2:
                raise ToolError("Malformed RTF bond list.")
            edges.update(tuple(sorted(pair)) for pair in zip(fields[1::2], fields[2::2]))
    names = {a.name for a in molecule.atoms}
    chemical_edges = {tuple(sorted((molecule.atoms[a].name, molecule.atoms[b].name))) for a, b in molecule.bonds}
    if occurrences != 1 or set(atom_rows) != names or edges != chemical_edges:
        raise ToolError("Trusted RTF must contain one matching RESI with exactly the named SDF atoms and connectivity.")
    charges = [residue_charge, *(row["charge"] for row in atom_rows.values())]
    formal_charge = sum(a.charge for a in molecule.atoms)
    if any(not math.isfinite(v) for v in charges) or abs(residue_charge - formal_charge) > 1e-4 or abs(sum(row["charge"] for row in atom_rows.values()) - formal_charge) > 1e-4:
        raise ToolError("RTF residue/partial-charge sum conflicts with the SDF formal charge.")
    return {"mode": "trusted_charmm", "rtf": _evidence(rtf), "prm": _evidence(prm),
            "atom_types": atom_rows, "parameter_coverage_verified": False,
            "limitation": "User-trusted standalone CHARMM files; atom identity, connectivity and charge sum checked, not force-field completeness or physical accuracy."}


def prepare_multicomponent(*, complex_pdb, components, out, water_policy, ion_policy):
    """Prepare explicit noncovalent component instances, retaining all input atoms.

    ``components`` is a nonempty list of selector dictionaries. Each specifies
    named ``sdf`` chemistry, or ``accept_conect_bond_orders=True``. Optional
    ``topology={rtf, prm, trusted: True}`` requires the named SDF sidecar.
    ``water_policy`` is ``reject`` or ``retain``; ``ion_policy`` is ``reject`` or
    ``retain_noncoordinating`` (only explicitly acknowledged Na/K/Cl spectators).
    No missing hydrogens or solvent atoms are added, and no input atoms removed.
    """
    destination = Path(out).resolve()
    if destination.exists():
        raise ToolError("Multicomponent out must be a new directory; existing outputs are never overwritten.")
    if water_policy not in ("reject", "retain") or ion_policy not in ("reject", "retain_noncoordinating"):
        raise ToolError("Explicit water_policy reject/retain and ion_policy reject/retain_noncoordinating are required.")
    if not isinstance(components, list) or not components:
        raise ToolError("Provide a nonempty list of explicit component instances.")
    source = _file(complex_pdb, "Complex PDB")
    atoms, bonds = _pdb(source)
    residues = {}
    for atom in atoms:
        residues.setdefault(_identity(atom), []).append(atom)
    selected, ownership = {}, {}
    for index, spec in enumerate(components, 1):
        key = _select(spec, residues)
        if key in selected:
            raise ToolError("A component residue was selected more than once.")
        if key[0] in _PROTEIN | _WATER | set(_IONS):
            raise ToolError("Component residue names must not collide with protein, water or supported ion names.")
        selected[key] = (index, spec)
        ownership.update((a.serial, f"component-{index}") for a in residues[key])
    protein, retained_waters, retained_ions = [], [], []
    for key, residue in residues.items():
        if key in selected:
            continue
        elements = Counter(a.element for a in residue)
        if key[0] in _WATER:
            if water_policy != "retain":
                raise ToolError("Input water is present but water_policy=reject; explicitly retain it or supply an intentionally edited input.")
            if elements not in (Counter({"O": 1}), Counter({"O": 1, "H": 2})) or any(a.charge for a in residue):
                raise ToolError("Unrecognized water chemistry; explicit specialized treatment is required.")
            retained_waters.append({**_identity_dict(key), "atom_serials": [a.serial for a in residue], "model_assigned": False})
            owner = "water:" + repr(key)
        elif key[0] in _IONS:
            element, charge = _IONS[key[0]]
            if ion_policy != "retain_noncoordinating":
                raise ToolError("Input ions require explicit ion_policy=retain_noncoordinating; no ions are silently discarded.")
            if len(residue) != 1 or residue[0].element != element or (residue[0].line[78:80].strip() and residue[0].charge != charge):
                raise ToolError("Ion identity/charge does not match the supported monatomic spectator ion.")
            retained_ions.append({**_identity_dict(key), "atom_serials": [residue[0].serial], "element": element,
                                  "expected_formal_charge": charge, "coordination": "user-declared noncoordinating; not inferred from distance"})
            owner = "ion:" + repr(key)
        elif all(a.line.startswith("ATOM  ") for a in residue) and key[0] in _PROTEIN:
            protein.extend(residue)
            owner = "protein"
        else:
            raise ToolError(f"Unexplained nonprotein residue {key}: declare every ligand/cofactor instance; unknown metals and nonstandard residues need a specialized workflow.")
        ownership.update((a.serial, owner) for a in residue)
    _validate_protein(protein)
    for a, b in bonds:
        if ownership[a] != ownership[b]:
            raise ToolError("CONECT crosses components/protein/solvent: covalent attachments or metal coordination require a specialized workflow.")
    # LINK records can encode bonds absent from CONECT, including coordination.
    for line in source.read_text().splitlines():
        if line.startswith("LINK  "):
            raise ToolError("PDB LINK records require explicit specialized linkage/coordination handling; they cannot be ignored in a noncovalent workflow.")
        if line.startswith("SSBOND"):
            try:
                linked = [(line[11:14].strip(), line[15:16].strip(), int(line[17:21]), line[21:22].strip()),
                          (line[25:28].strip(), line[29:30].strip(), int(line[31:35]), line[35:36].strip())]
            except ValueError:
                raise ToolError("Malformed SSBOND identity; covalent linkage information cannot be ignored.") from None
            for identity in linked:
                matches = [key for key in residues if key[:4] == identity]
                if len(matches) != 1 or any(ownership[a.serial] != "protein" for a in residues[matches[0]]):
                    raise ToolError("SSBOND requires uniquely mapped protein residues; covalent cross-component or ambiguous linkages are unsupported.")
    files, reports, groups = {}, [], {}
    for key, (index, spec) in selected.items():
        bound = residues[key]
        if any(a.element not in _ORGANIC for a in bound):
            raise ToolError("Metal-containing or unsupported-element components need explicit coordination/force-field treatment; ordinary CGenFF chemistry cannot be assumed.")
        internal = _internal(bound, bonds)
        chemistry = spec.get("sdf")
        accepted = spec.get("accept_conect_bond_orders", False)
        if not isinstance(accepted, bool):
            raise ToolError("accept_conect_bond_orders must be a boolean.")
        if chemistry:
            path = _file(chemistry, "Component SDF")
            sdf, mapping = _chemical_sdf_in_complex(path, bound, internal)
            molecule = read_ligand(path)
            chemical_evidence = _evidence(path)
            if accepted:
                expected = {tuple(sorted((bound[a].name, bound[b].name))): order for (a, b), order in internal.items()}
                actual = {tuple(sorted((molecule.atoms[a].name, molecule.atoms[b].name))): order for (a, b), order in molecule.bonds.items()}
                if expected != actual:
                    raise ToolError("Acknowledged CONECT bond orders disagree with SDF chemistry.")
        else:
            if not accepted:
                raise ToolError("Each component needs a named SDF or explicit accept_conect_bond_orders=true; chemical orders are not guessed.")
            sdf = _sdf(bound, internal, key[0])
            molecule = Ligand(bound, internal, sdf, key[0])
            _validate_ligand(molecule)
            mapping = {"chemical_source": "explicitly acknowledged input CONECT bond orders", "mapping": "PDB atom serial/name identities"}
            chemical_evidence = _evidence(source)
        topology = None
        if spec.get("topology") is not None:
            if not chemistry:
                raise ToolError("Trusted topology requires a named SDF chemical sidecar; RTF alone does not reliably specify chemical bond orders.")
            topology = _topology(spec["topology"], molecule, key[0])
        signature, graph = _signature(molecule)
        mode = "trusted_charmm" if topology else "cgenff_required"
        group = groups.setdefault(key[0], {"resname": key[0], "chemical_signature": signature, "mode": mode, "instances": []})
        topology_signature = {name: topology[name]["sha256"] for name in ("rtf", "prm")} if topology else None
        if group["chemical_signature"] != signature or group["mode"] != mode or group.get("topology_signature", topology_signature) != topology_signature:
            raise ToolError("Same-resname copies must have identical named chemistry and topology source; use distinct residue names for different states.")
        group["topology_signature"] = topology_signature
        prefix = f"component-{index:03d}"
        sdf_path = destination / f"{prefix}.sdf"
        by_name = {a.name: a for a in bound}
        report = {"id": prefix, **_identity_dict(key), "role": spec.get("role", "ligand"), "mode": mode,
                  "sdf": str(sdf_path), "chemical_signature": signature, "chemical_graph": graph,
                  "chemical_source": chemical_evidence, "mapping": mapping, "coordinate_source": str(source),
                  "coordinate_transform": "none", "hydrogens_added": 0,
                  "atoms": [{"sdf_index": i + 1, "name": a.name, "element": a.element, "formal_charge": a.charge,
                             "source_serial": by_name[a.name].serial, "xyz_A": list(by_name[a.name].xyz)} for i, a in enumerate(molecule.atoms)],
                  "heavy_atoms": sum(a.element != "H" for a in molecule.atoms), "topology": topology,
                  "parameterized": topology is not None, "parameter_coverage_verified": False}
        report["expected_ligand"] = {
            "resname": key[0], "heavy_atoms": report["heavy_atoms"], "bonds": len(molecule.bonds),
            "atom_mapping": [{"pdb_atom_name": a.name, "element": a.element} for a in molecule.atoms],
            "heavy_atom_bonds": [{"atoms": sorted((molecule.atoms[a].name, molecule.atoms[b].name)), "order": order}
                                 for (a, b), order in molecule.bonds.items() if molecule.atoms[a].element != "H" and molecule.atoms[b].element != "H"]}
        files[f"{prefix}.sdf"] = sdf
        files[f"{prefix}.json"] = json.dumps(report, indent=2) + "\n"
        reports.append(report)
        group["instances"].append(prefix)
        group.setdefault("representative_sdf", str(sdf_path))
        group.setdefault("files", {"sdf": str(sdf_path), **({"rtf": topology["rtf"]["path"], "prm": topology["prm"]["path"]} if topology else {})})
    protein_serials = {a.serial for a in protein}
    files["protein.pdb"] = "\n".join(a.line for a in protein) + "\n" + _conect({edge: order for edge, order in bonds.items() if set(edge) <= protein_serials}) + "END\n"
    report = {"schema_version": 1, "source": _evidence(source), "complex": str(destination / "complex.pdb"),
              "protein": str(destination / "protein.pdb"), "components": reports, "chemistry_groups": list(groups.values()),
              "policies": {"water": water_policy, "ions": ion_policy}, "retained_waters": retained_waters, "retained_ions": retained_ions,
              "source_atoms": len(atoms), "output_complex_atoms": len(atoms), "discarded_atoms": 0,
              "coordinate_transform": "none", "scientific_correctness_verified": False,
              "remote_support_verified": False,
              "remote_validation_supported": all(len(group["instances"]) == 1 for group in groups.values()) and not retained_waters and not retained_ions,
              "limitations": ["No docking, hydrogen addition, pH-state selection or coordinate-based bond guessing.",
                              "Retained waters/ions still require explicit downstream server options and final identity checks.",
                              "Metal coordination, cross-component covalent linkages and stereo-labelled coordinate transfer are unsupported.",
                              "Local preparation does not prove multi-component HTTP parameterization or force-field compatibility."]}
    files["multicomponent-manifest.json"] = json.dumps(report, indent=2) + "\n"
    destination.mkdir(parents=True, exist_ok=False)
    # Preserve the complete original structure (including TER/SSBOND metadata).
    with (destination / "complex.pdb").open("xb") as handle:
        handle.write(source.read_bytes())
    for name, text in files.items():
        with (destination / name).open("x") as handle:
            handle.write(text)
    return {"complex": report["complex"], "protein": report["protein"],
            "manifest": str(destination / "multicomponent-manifest.json"), "report": report}


def validate_components(system_dir, input_manifest, reference_pdb, run_grompp=False, gmx=None, output_parent=None):
    """Aggregate exact-copy topology and common-protein-frame pose checks.

    Distinct residue names with one copy each are supported. Repeated copies and
    retained input solvent/ions fail explicitly until residue-level lineage can
    be proved; merely finding the same number of bulk waters is not such proof.
    """
    from .pose_validation import validate_bound_pose
    from .validation import validate_system

    report = {"passed": False, "scientific_correctness_verified": False, "checks": {}, "components": {}, "errors": [],
              "scope": "Exact component counts, heavy-atom chemistry retention and protein-frame relative poses; optional single grompp"}
    try:
        manifest = input_manifest if isinstance(input_manifest, dict) else json.loads(Path(input_manifest).read_text())
        groups, instances = manifest["chemistry_groups"], manifest["components"]
        by_id = {entry["id"]: entry for entry in instances}
        if not groups or len(by_id) != len(instances):
            raise ToolError("Multicomponent manifest needs unique component IDs and explicit chemistry groups.")
        if manifest.get("retained_waters") or manifest.get("retained_ions"):
            raise ToolError("Retained input water/ion identity tracking is not yet implemented for final validation; bulk solvent counts cannot prove those molecules were retained.")
        assigned = []
        for group in groups:
            if len(group["instances"]) != 1:
                raise ToolError("Repeated same-resname copies require explicit final residue mapping or unique molecular assignment; final pose validation does not yet support this case.")
            for identifier in group["instances"]:
                if identifier not in by_id or by_id[identifier]["resname"] != group["resname"]:
                    raise ToolError("Manifest chemistry groups and component identities disagree.")
                assigned.append(identifier)
        if sorted(assigned) != sorted(by_id) or len({g["resname"] for g in groups}) != len(groups):
            raise ToolError("Manifest groups must cover each component once with unique residue names.")
        common_transform = None
        for index, group in enumerate(groups):
            instance = by_id[group["instances"][0]]
            expected = {"ligand": instance["expected_ligand"]}
            if expected["ligand"]["resname"] != group["resname"]:
                raise ToolError("Expected ligand residue name differs from its chemistry group.")
            checked = validate_system(system_dir, expected, run_grompp=run_grompp and index == 0,
                                      gmx=gmx, output_parent=output_parent)
            counts = checked.get("checks", {})
            topo_copies = counts.get("ligand_topology_presence", {}).get("copies")
            coord_copies = counts.get("ligand_coordinate_retention", {}).get("copies")
            exact = {"passed": topo_copies == coord_copies == len(group["instances"]), "expected": len(group["instances"]),
                     "topology": topo_copies, "coordinates": coord_copies}
            pose = validate_bound_pose(system_dir, expected, reference_pdb)
            same_frame = False
            if "rotation_row_vector" in pose and "translation_A" in pose:
                transform = [*sum(pose["rotation_row_vector"], []), *pose["translation_A"]]
                if common_transform is None:
                    common_transform = transform
                same_frame = len(transform) == len(common_transform) and all(abs(a - b) <= 1e-10 for a, b in zip(transform, common_transform))
            component_passed = checked["passed"] and exact["passed"] and pose["passed"] and same_frame
            report["components"][group["resname"]] = {"passed": component_passed, "topology_validation": checked,
                                                       "exact_copy_count": exact, "bound_pose": pose, "same_protein_transform": same_frame}
        report["checks"]["all_components"] = {"passed": bool(report["components"]) and all(c["passed"] for c in report["components"].values())}
        report["checks"]["same_protein_transform"] = {"passed": common_transform is not None and all(c["same_protein_transform"] for c in report["components"].values()),
                                                      "scope": "All components use the identical protein CA fit; no component is independently fitted"}
        report["passed"] = all(check["passed"] for check in report["checks"].values())
    except (ToolError, OSError, ValueError, KeyError, TypeError) as exc:
        report["errors"].append(str(exc))
    report["status"] = "passed" if report["passed"] else "failed"
    return report


def load_components_config(path):
    """Read strict version-1 YAML; resolve chemical files relative to the YAML."""
    import yaml
    from .config import UniqueLoader

    path = _file(path, "Components configuration")
    try:
        raw = yaml.load(path.read_text(), Loader=UniqueLoader)
    except (yaml.YAMLError, UnicodeError):
        raise ToolError("Invalid components YAML.") from None
    if not isinstance(raw, dict) or set(raw) != {"version", "components", "water_policy", "ion_policy"} or type(raw["version"]) is not int or raw["version"] != 1:
        raise ToolError("Components YAML requires version: 1, components, water_policy and ion_policy, without unknown fields.")
    if not isinstance(raw["components"], list) or not raw["components"]:
        raise ToolError("Components YAML needs a nonempty components list.")
    for component in raw["components"]:
        if not isinstance(component, dict):
            raise ToolError("Each component must be a selector mapping.")
        if "sdf" in component:
            if not isinstance(component["sdf"], str) or not component["sdf"]:
                raise ToolError("Component sdf must be a nonempty file path.")
            component["sdf"] = str((path.parent / component["sdf"]).resolve())
        if "topology" in component:
            topology = component["topology"]
            if not isinstance(topology, dict):
                raise ToolError("Component topology must be a mapping of rtf, prm and trusted.")
            for field in ("rtf", "prm"):
                if not isinstance(topology.get(field), str) or not topology[field]:
                    raise ToolError("Component topology requires explicit RTF and PRM paths.")
                topology[field] = str((path.parent / topology[field]).resolve())
    return raw


def prepare_build_components(*, components_config, complex_pdb, out, hydrogens="add_missing", allow_distant_pose=False):
    """Prepare the complete upload without losing any declared component.

    Returns the same ``files``/``manifest`` envelope as ``prepare_inputs``.
    Only distinct, single-copy organic components are currently accepted for
    remote builds. Local ``prepare_multicomponent`` is less restrictive.
    """
    if hydrogens not in ("preserve", "add_missing"):
        raise ToolError("hydrogens must be preserve or add_missing; no pH state is selected.")
    destination = Path(out).resolve()
    if destination.exists():
        raise ToolError("Build preparation out must be a new directory.")
    config = load_components_config(components_config)
    prepared = prepare_multicomponent(complex_pdb=complex_pdb, components=config["components"], out=destination / "sources",
                                     water_policy=config["water_policy"], ion_policy=config["ion_policy"])
    manifest = prepared["report"]
    if not manifest["remote_validation_supported"]:
        raise ToolError("Remote builds currently require distinct component residue names with one copy each and no retained source waters/ions; local preparation is preserved under sources/.")
    if hydrogens != "preserve" and any(component["topology"] for component in manifest["components"]):
        raise ToolError("Trusted existing topology requires hydrogens=preserve so its validated atom set is not modified.")
    source = _file(complex_pdb, "Complex PDB")
    protein, protein_bonds = _pdb(prepared["protein"])
    protein_map = {a.serial: i + 1 for i, a in enumerate(protein)}
    protein_lines = [a.line[:6] + f"{protein_map[a.serial]:5d}" + a.line[11:] for a in protein]
    mapped_bonds = {tuple(protein_map[i] for i in edge): order for edge, order in protein_bonds.items()}
    all_lines, serial = list(protein_lines), len(protein)
    file_text, groups = {}, {group["resname"]: group for group in manifest["chemistry_groups"]}
    previous_heavy = []
    for component in manifest["components"]:
        molecule = _apply_hydrogen_policy(read_ligand(component["sdf"]), hydrogens)
        validation = _validate_ligand(molecule)
        heavy = [a for a in molecule.atoms if a.element != "H"]
        distance = min(math.dist(a.xyz, b.xyz) for a in heavy for b in protein if b.element != "H")
        if distance < 0.8:
            raise ToolError("A component overlaps protein heavy atoms (<0.8 A); verify the bound input coordinates.")
        if distance > 6 and not allow_distant_pose:
            raise ToolError("A component is >6 A from protein; provide a bound pose or explicitly allow_distant_pose.")
        if any(math.dist(a.xyz, b.xyz) < 0.8 for a in heavy for b in previous_heavy):
            raise ToolError("Distinct components overlap in heavy-atom coordinates (<0.8 A); no noncovalent binding pose can be assumed.")
        previous_heavy.extend(heavy)
        start = serial
        atom_mapping, instance_lines = [], []
        originals = {a["name"]: a for a in component["atoms"]}
        for i, atom in enumerate(molecule.atoms):
            serial += 1
            if serial > 99999 or any(len(f"{value:8.3f}") != 8 for value in atom.xyz):
                raise ToolError("Prepared coordinates exceed PDB atom/coordinate field widths.")
            name = f" {atom.name:<3}" if len(atom.element) == 1 and len(atom.name) < 4 else f"{atom.name:<4}"
            charge = f"{abs(atom.charge)}{'+' if atom.charge > 0 else '-'}" if atom.charge else "  "
            if len(charge) != 2:
                raise ToolError("Component charge exceeds the PDB field width.")
            line = (f"HETATM{serial:5d} {name} {component['resname']:>3} {component['chain']:1}{component['resid']:4d}{component['icode']:1}   "
                    + "".join(f"{v:8.3f}" for v in atom.xyz) + f"  1.00  0.00      {component['segment']:<4}{atom.element:>2}{charge}")
            instance_lines.append(line)
            atom_mapping.append({"sdf_index": i + 1, "pdb_serial": serial, "pdb_atom_name": atom.name,
                                 "element": atom.element, "input_coordinates_A": list(atom.xyz)})
        instance_bonds = {tuple(start + i + 1 for i in edge): order for edge, order in molecule.bonds.items()}
        mapped_bonds.update(instance_bonds)
        all_lines.extend(instance_lines)
        sdf_name, pdb_name = component["id"] + ".sdf", component["id"] + ".pdb"
        file_text[sdf_name] = molecule.sdf
        file_text[pdb_name] = "\n".join(instance_lines) + "\n" + _conect(instance_bonds) + "END\n"
        component["source_bound_sdf"] = component["sdf"]
        component["sdf"] = str(destination / sdf_name)
        component["pdb"] = str(destination / pdb_name)
        component["hydrogens_added"] = len(molecule.atoms) - len(originals)
        component["hydrogen_policy"] = hydrogens
        component["minimum_protein_ligand_heavy_distance_A"] = distance
        component["atoms"] = [{"sdf_index": i + 1, "name": atom.name, "element": atom.element, "formal_charge": atom.charge,
                               "source_serial": originals.get(atom.name, {}).get("source_serial"), "pdb_serial": start + i + 1,
                               "xyz_A": list(atom.xyz)} for i, atom in enumerate(molecule.atoms)]
        signature, graph = _signature(molecule)
        component["chemical_signature"], component["chemical_graph"] = signature, graph
        component["expected_ligand"].update(validation)
        component["expected_ligand"].update({"resname": component["resname"], "atom_mapping": atom_mapping,
                                             "hydrogen_policy": hydrogens, "chain": component["chain"], "residue_number": component["resid"]})
        groups[component["resname"]].update({"chemical_signature": signature, "representative_sdf": component["sdf"]})
        groups[component["resname"]]["files"]["sdf"] = component["sdf"]
        file_text[component["id"] + ".json"] = json.dumps(component, indent=2) + "\n"
    metadata = [line for line in source.read_text().splitlines() if line.startswith("SSBOND")]
    file_text["protein.pdb"] = "\n".join(metadata + protein_lines) + "\n" + _conect({tuple(protein_map[i] for i in edge): order for edge, order in protein_bonds.items()}) + "END\n"
    file_text["complex.pdb"] = "\n".join(metadata + all_lines) + "\n" + _conect(mapped_bonds) + "END\n"
    manifest.update({"complex": str(destination / "complex.pdb"), "protein": str(destination / "protein.pdb"),
                     "output_complex_atoms": serial, "hydrogens_added": serial - manifest["source_atoms"],
                     "hydrogen_policy": hydrogens, "components_config": _evidence(components_config),
                     "force_field_parameters_generated": False,
                     "preparation_warnings": ["Added valence hydrogens, not pH-dependent protonation or optimized hydrogen positions."] if hydrogens == "add_missing" else []})
    file_text["input-manifest.json"] = json.dumps(manifest, indent=2) + "\n"
    paths = {}
    for name, text in file_text.items():
        with (destination / name).open("x") as handle:
            handle.write(text)
        paths[name] = str(destination / name)
    return {"files": paths, "manifest": manifest}
