"""Conservative ligand retention/topology checks, not scientific certification.

No simulation is run. Optional GROMACS grompp compiles the supplied minimization
input with zero tolerated warnings and retains every generated artifact.
"""

from collections import Counter
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from .auth import ToolError


def validate_charmm_source(pdb, psf, rtf, prm, manifest):
    """Check a prepared CHARMM source job and report its CGenFF penalties.

    This validates the selected ligand's retention and naming before membrane
    assembly, not completeness/suitability of the final force-field parameters.
    """
    if not isinstance(manifest, dict):
        manifest = json.loads(Path(manifest).read_text())
    expected = manifest["ligand"]
    name = expected["resname"]
    coordinates = inspect_coordinates(pdb)
    psf_lines = Path(psf).read_text().splitlines()
    try:
        start = next(i for i, line in enumerate(psf_lines) if "!NATOM" in line)
        count = int(psf_lines[start].split()[0])
        atoms = []
        for line in psf_lines[start + 1:start + 1 + count]:
            fields = line.split()
            atoms.append({"serial": int(fields[0]), "segment": fields[1], "resid": fields[2],
                          "resname": fields[3], "name": fields[4], "type": fields[5],
                          "charge": float(fields[6]), "mass": float(fields[7])})
        if len(atoms) != count:
            raise ValueError
    except (ValueError, IndexError, StopIteration):
        raise ToolError("Malformed CHARMM PSF atom section.") from None
    ligand = [a for a in atoms if a["resname"] == name]
    coord_ligand = [a for a in coordinates["atoms"] if a["resname"] == name]
    topology_atoms, topology_bonds, active = [], [], False
    rtf_text, prm_text = Path(rtf).read_text(), Path(prm).read_text()
    for line in rtf_text.splitlines():
        fields = line.split("!", 1)[0].split()
        if not fields:
            continue
        if fields[0] in ("RESI", "PRES"):
            active = len(fields) >= 2 and fields[1] == name and fields[0] == "RESI"
        elif active and fields[0] == "ATOM":
            topology_atoms.append({"name": fields[1], "type": fields[2], "charge": float(fields[3])})
        elif active and fields[0] in ("BOND", "DOUBLE", "DOUB", "TRIPLE", "TRIP"):
            topology_bonds.extend(tuple(sorted(pair)) for pair in zip(fields[1::2], fields[2::2]))
    heavy = [a for a in ligand if a["mass"] > 2.5]
    copies = len({(a["segment"], a["resid"]) for a in ligand})
    expected_names = Counter(a["pdb_atom_name"] for a in expected["atom_mapping"] if a["element"] not in ("H", "D", "T"))
    actual_names = Counter(a["name"] for a in heavy)
    matching_names = actual_names == Counter({key: value * copies for key, value in expected_names.items()})
    checks = {
        "pdb_psf_total_atoms": {"passed": count == coordinates["total_atoms"], "psf": count, "pdb": coordinates["total_atoms"]},
        "ligand_heavy_atoms": {"passed": copies > 0 and len(heavy) == copies * expected["heavy_atoms"],
                               "copies": copies, "observed": len(heavy), "expected_per_copy": expected["heavy_atoms"]},
        "ligand_heavy_names": {"passed": matching_names and copies > 0},
        "ligand_coordinate_psf_order": {"passed": bool(ligand) and [a["name"] for a in ligand] == [a["name"] for a in coord_ligand]},
        "ligand_rtf_atoms": {"passed": bool(topology_atoms) and
                              Counter(a["name"] for a in ligand) == Counter({a["name"]: copies for a in topology_atoms}),
                              "atoms_per_residue": len(topology_atoms)},
        "ligand_rtf_atom_types": {"passed": bool(ligand) and all(
            any(t["name"] == atom["name"] and t["type"] == atom["type"] for t in topology_atoms) for atom in ligand)},
    }
    if "heavy_atom_bonds" in expected:
        expected_edges = {tuple(sorted(b["atoms"])) for b in expected["heavy_atom_bonds"]}
        heavy_names = set(expected_names)
        observed_edges = {edge for edge in topology_bonds if set(edge) <= heavy_names}
        checks["ligand_heavy_bond_connectivity"] = {"passed": expected_edges == observed_edges,
                                                   "missing": sorted(expected_edges - observed_edges),
                                                   "extra": sorted(observed_edges - expected_edges)}
    summary = re.search(r"param penalty\s*=\s*([0-9.eE+-]+)\s*;\s*charge penalty\s*=\s*([0-9.eE+-]+)", rtf_text)
    individual = [float(x) for x in re.findall(r"penalty\s*=\s*([0-9.eE+-]+)", prm_text)]
    penalties = {"parameter_max": float(summary[1]) if summary else None,
                 "charge_max": float(summary[2]) if summary else None,
                 "individual_parameter_max": max(individual) if individual else None,
                 "individual_parameter_terms": len(individual),
                 "interpretation": "CGenFF header: <10 fair analogy, 10-50 basic validation advised, >50 extensive validation/optimization required."}
    return {"passed": all(c["passed"] for c in checks.values()), "scientific_correctness_verified": False,
            "scope": "Prepared CHARMM source PDB/PSF/RTF consistency and reported CGenFF penalties; no membrane validation yet",
            "checks": checks, "cgenff_penalties": penalties,
            "ligand": {"resname": name, "residues": sorted({(a["segment"], a["resid"]) for a in ligand}),
                       "atoms": len(ligand), "heavy_atoms": len(heavy), "hydrogen_atoms": sum(0 < a["mass"] <= 2.5 for a in ligand),
                       "net_partial_charge": round(sum(a["charge"] for a in ligand), 6),
                       "atom_types": dict(Counter(a["type"] for a in ligand))},
            "files": {key: {"path": str(Path(path).resolve()), "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                      for key, path in (("pdb", pdb), ("psf", psf), ("rtf", rtf), ("prm", prm))},
            "limitations": ["Low CGenFF penalties do not prove a physically accurate ligand model.",
                            "Base CHARMM/CGenFF parameter dependencies are not checked by this source-stage inspection."]}


def extract_for_validation(archive_path, destination, *, max_bytes=4 * 1024 ** 3):
    """Extract regular files into a NEW directory, rejecting links and traversal."""
    destination = Path(destination)
    if destination.exists():
        raise ToolError("Validation extraction requires a new destination directory.")
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        if len(members) > 50000 or sum(m.size for m in members) > max_bytes:
            raise ToolError("Archive exceeds validation extraction size/member limits.")
        targets = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ToolError("Unsafe archive path; validation extraction refused.")
            if not member.isdir() and not member.isfile():
                raise ToolError("Archive links/devices are not allowed for validation extraction.")
            if member.isfile():
                if str(path) in targets:
                    raise ToolError("Archive has duplicate file paths.")
                targets.add(str(path))
        destination.mkdir(parents=True)
        for member in members:
            path = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, path.open("xb") as output:
                    shutil.copyfileobj(source, output)
    return destination.resolve()


def _preprocess(path, root, defines=None):
    defines = set(defines or ())
    files, expanded, stack = [], [], []

    def visit(source):
        source = source.resolve()
        if not source.is_relative_to(root):
            raise ToolError(f"Topology include leaves the system directory: {source.name}.")
        if source in stack or len(stack) > 30:
            raise ToolError("Topology includes contain a cycle or excessive nesting.")
        if not source.is_file():
            raise ToolError(f"Missing topology include: {source.name}.")
        stack.append(source)
        files.append(str(source))
        conditions, active = [], True
        for raw in source.read_text().splitlines():
            line = raw.split(";", 1)[0].strip()
            if line.startswith("#"):
                fields = line[1:].strip().split(maxsplit=1)
                directive, argument = fields[0], fields[1] if len(fields) > 1 else ""
                if directive in ("ifdef", "ifndef"):
                    value = argument in defines
                    if directive == "ifndef":
                        value = not value
                    conditions.append((active, value, False))
                    active = active and value
                elif directive == "else":
                    if not conditions or conditions[-1][2]:
                        raise ToolError("Malformed topology preprocessor #else.")
                    parent, value, _ = conditions[-1]
                    conditions[-1] = (parent, value, True)
                    active = parent and not value
                elif directive == "endif":
                    if not conditions:
                        raise ToolError("Unmatched topology #endif.")
                    active = conditions.pop()[0]
                elif directive in ("if", "elif"):
                    raise ToolError("Static topology checks do not support #if/#elif expressions; use a GROMACS-preprocessed topology.")
                elif active and directive == "include":
                    match = re.fullmatch(r'["<]([^">]+)[">]', argument)
                    if not match:
                        raise ToolError("Malformed topology #include.")
                    visit(source.parent / match[1])
                elif active and directive == "define":
                    defines.add(argument.split()[0])
                elif active and directive == "undef":
                    defines.discard(argument)
                elif active and directive not in ("define", "undef"):
                    raise ToolError(f"Unsupported topology directive #{directive}.")
            elif active and line:
                expanded.append((line, str(source)))
        if conditions:
            raise ToolError("Unclosed topology preprocessor conditional.")
        stack.pop()

    visit(path)
    return expanded, sorted(set(files))


def inspect_topology(topology, root=None, defines=None):
    """Inspect a bundled GROMACS topology; handles #ifdef/#ifndef includes."""
    path = Path(topology).resolve()
    root = Path(root).resolve() if root else path.parent
    lines, files = _preprocess(path, root, defines)
    molecules, counts, atomtypes, section, current = {}, [], {}, None, None
    for line, source in lines:
        match = re.fullmatch(r"\[\s*([^]]+?)\s*\]", line)
        if match:
            section = match[1].strip().lower()
            continue
        fields = line.split()
        try:
            if section == "moleculetype":
                current = fields[0]
                if current in molecules:
                    raise ToolError(f"Duplicate topology moleculetype {current}.")
                molecules[current] = {"atoms": [], "bonds": [], "source": source}
            elif section == "atomtypes":
                ptype = next(i for i, value in enumerate(fields) if i >= 3 and value in ("A", "S", "V", "D"))
                # GROMACS accepts name [bond_type] [atomic_number] mass charge
                # ptype sigma epsilon. Distinguish the 6/7/8-field formats.
                prefix = fields[:ptype - 2]
                atomic_number = None
                if len(prefix) == 3:
                    atomic_number = int(prefix[2])
                elif len(prefix) == 2 and re.fullmatch(r"\d+", prefix[1]):
                    atomic_number = int(prefix[1])
                elif len(prefix) not in (1, 2):
                    raise ValueError
                if atomic_number is not None and not 0 <= atomic_number <= 118:
                    raise ValueError
                natural_mass = float(fields[ptype - 2])
                if not math.isfinite(natural_mass) or natural_mass < 0:
                    raise ValueError
                atomtypes[fields[0]] = {"mass": natural_mass, "atomic_number": atomic_number, "source": source}
            elif section == "atoms":
                if current is None or len(fields) < 7:
                    raise ValueError
                mass = float(fields[7]) if len(fields) >= 8 else atomtypes.get(fields[1], {}).get("mass")
                if mass is None or not math.isfinite(mass) or mass < 0:
                    raise ToolError(f"Missing/invalid mass for topology atom type {fields[1]}.")
                molecules[current]["atoms"].append({"nr": int(fields[0]), "type": fields[1], "resid": int(fields[2]),
                                                     "resname": fields[3], "name": fields[4], "charge": float(fields[6]), "mass": mass})
            elif section in ("bonds", "constraints") and current:
                molecules[current]["bonds"].append((int(fields[0]), int(fields[1])))
            elif section == "molecules":
                count = int(fields[1])
                if count < 0:
                    raise ValueError
                counts.append((fields[0], count))
        except (ValueError, IndexError, StopIteration):
            raise ToolError(f"Malformed topology [{section}] record in {Path(source).name}.") from None
    if not counts:
        raise ToolError("Topology has no [ molecules ] composition.")
    for name, count in counts:
        if name not in molecules:
            raise ToolError(f"Undefined molecule type {name} in [ molecules ].")
        atoms = molecules[name]["atoms"]
        if not atoms or [a["nr"] for a in atoms] != list(range(1, len(atoms) + 1)):
            raise ToolError(f"Non-sequential or absent atoms in molecule type {name}.")
        for atom in atoms:
            info = atomtypes.get(atom["type"], {})
            atom["atomic_number"] = info.get("atomic_number")
            atom["natural_mass"] = info.get("mass")
            atom["heavy"] = (atom["atomic_number"] > 1 if atom["atomic_number"] is not None
                             else atom["natural_mass"] > 2.5 if atom["natural_mass"] is not None else None)
    total = sum(len(molecules[name]["atoms"]) * count for name, count in counts)
    if total > 5000000:
        raise ToolError("Topology exceeds the 5 million atom validation limit.")
    return {"molecules": molecules, "composition": counts, "atomtypes": atomtypes, "includes": files,
            "total_atoms": total}


def inspect_coordinates(path):
    """Return per-atom records for a single PDB or GRO, preserving file order."""
    path = Path(path)
    lines = path.read_text().splitlines()
    atoms = []
    try:
        if path.suffix.lower() == ".gro":
            expected = int(lines[1])
            if expected < 1 or len(lines) != expected + 3:
                raise ValueError
            for line in lines[2:2 + expected]:
                xyz = [float(line[i:i + 8]) for i in (20, 28, 36)]
                if not all(math.isfinite(v) for v in xyz):
                    raise ValueError
                atoms.append({"resid": int(line[:5]), "resname": line[5:10].strip(),
                              "name": line[10:15].strip(), "element": None, "chain": "", "segment": "", "icode": ""})
            box = [float(x) for x in lines[-1].split()]
            if len(box) not in (3, 9) or not all(math.isfinite(v) for v in box) or any(v <= 0 for v in box[:3]):
                raise ValueError
        elif path.suffix.lower() == ".pdb":
            if sum(line.startswith("MODEL ") for line in lines) > 1:
                raise ToolError("Coordinate validation requires a single PDB model.")
            for line in lines:
                if line.startswith(("ATOM  ", "HETATM")):
                    if line[16:17].strip():
                        raise ToolError("Coordinate validation requires resolved alternate locations.")
                    xyz = [float(line[i:i + 8]) for i in (30, 38, 46)]
                    if not all(math.isfinite(v) for v in xyz):
                        raise ValueError
                    atoms.append({"resid": int(line[22:26]), "resname": line[17:21].strip(),
                                  "name": line[12:16].strip(), "element": line[76:78].strip().title() or None,
                                  "chain": line[21:22], "segment": line[72:76], "icode": line[26:27]})
        else:
            raise ToolError("Coordinate validation accepts PDB or GRO files.")
    except (ValueError, IndexError):
        raise ToolError("Malformed coordinate file or non-finite coordinates.") from None
    if not atoms:
        raise ToolError("Coordinate file contains no atoms.")
    residues = Counter()
    last = None
    for atom in atoms:
        # Standard PDB insertion codes distinguish residues too. CHARMM-GUI
        # also uses that column for the fifth digit of large residue IDs;
        # ignoring it merges ten adjacent waters into one after residue 9999.
        key = tuple(atom[k] for k in ("resid", "icode", "resname", "chain", "segment"))
        if key != last:
            residues[atom["resname"]] += 1
        last = key
    return {"atoms": atoms, "total_atoms": len(atoms), "residue_counts": dict(residues)}


def _choose(root, explicit, patterns, kind):
    if explicit:
        path = Path(explicit)
        path = path if path.is_absolute() else root / path
        if not path.is_file():
            raise ToolError(f"Missing {kind}: {path}.")
        return path.resolve()
    for pattern in patterns:
        candidates = sorted(root.rglob(pattern))
        if len(candidates) == 1:
            return candidates[0].resolve()
        if len(candidates) > 1:
            raise ToolError(f"Multiple {kind} candidates; supply an explicit path.")
    raise ToolError(f"No {kind} found; supply an explicit path.")


def _mdp_defines(path):
    defines = set()
    for line in path.read_text().splitlines():
        line = line.split(";", 1)[0]
        if "=" in line and line.split("=", 1)[0].strip().lower() == "define":
            defines.update(re.findall(r"-D([A-Za-z_][A-Za-z0-9_]*)", line))
    return defines


def run_grompp_check(topology, coordinates, mdp, output_parent, *, index=None,
                     gmx=None, timeout=180):
    """Compile minimization input; writes only to a fresh retained directory."""
    topology, coordinates, mdp = (Path(p).resolve() for p in (topology, coordinates, mdp))
    settings = {}
    for line in mdp.read_text().splitlines():
        line = line.split(";", 1)[0]
        if "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip().lower()] = value.strip().lower()
    if settings.get("integrator") not in ("steep", "cg", "l-bfgs"):
        raise ToolError("grompp check requires an explicitly supplied energy-minimization MDP.")
    executable = gmx or shutil.which("gmx")
    if not executable:
        raise ToolError("GROMACS executable not found; provide gmx explicitly.")
    parent = Path(output_parent)
    parent.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="grompp-", dir=parent)).resolve()
    command = [str(executable), "grompp", "-f", str(mdp), "-c", str(coordinates), "-r", str(coordinates),
               "-p", str(topology), "-o", str(out / "system.tpr"), "-po", str(out / "processed.mdp"),
               "-pp", str(out / "processed.top"), "-maxwarn", "0"]
    if index:
        command.extend(["-n", str(Path(index).resolve())])
    with (out / "command.json").open("x") as handle:
        json.dump(command, handle, indent=2)
    try:
        with (out / "grompp.log").open("x") as log:
            process = subprocess.run(command, cwd=topology.parent, stdout=log, stderr=subprocess.STDOUT,
                                     timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"passed": False, "reason": f"grompp exceeded {timeout} seconds.", "output_dir": str(out)}
    except OSError as exc:
        return {"passed": False, "reason": f"Could not start grompp: {exc}.", "output_dir": str(out)}
    return {"passed": process.returncode == 0 and (out / "system.tpr").is_file(),
            "returncode": process.returncode, "output_dir": str(out), "log": str(out / "grompp.log"),
            "scope": "GROMACS preprocessing with maxwarn=0; no minimization or dynamics executed"}


def validate_system(system_dir, manifest, *, topology=None, coordinates=None, mdp=None,
                    ligand_resname=None, run_grompp=False, gmx=None, index=None,
                    output_parent=None):
    """Return readable checks; a passing result is never scientific certification.

    manifest is the prepare_inputs return's manifest, or its JSON file path.
    ligand_resname explicitly maps a server-renamed residue if needed.
    """
    root = Path(system_dir).resolve()
    if not isinstance(manifest, dict):
        manifest = json.loads(Path(manifest).read_text())
    expected = manifest["ligand"]
    resname = ligand_resname or expected["resname"]
    checks, errors = {}, []
    report = {"passed": False, "scientific_correctness_verified": False,
              "scope": "Ligand retention and coordinate/topology consistency; optional GROMACS preprocessing",
              "limitations": ["Does not validate binding pose, membrane orientation, protonation, force-field suitability or equilibration.",
                              "CGenFF parameter penalties and missing chemistry require separate review."],
              "checks": checks, "errors": errors, "expected_ligand_resname": resname}
    try:
        top = _choose(root, topology, ["topol.top", "*.top"], "GROMACS topology")
        coord = _choose(root if coordinates else top.parent, coordinates, ["step5_input.gro", "step5_input.pdb", "*.gro", "*.pdb"], "GROMACS coordinates")
        mdp_path = _choose(root if mdp else top.parent, mdp, ["*minimization*.mdp", "*min*.mdp"], "minimization MDP") if mdp or run_grompp else None
        topo = inspect_topology(top, root, _mdp_defines(mdp_path) if mdp_path else None)
        structure = inspect_coordinates(coord)
        report["files"] = {"topology": str(top), "coordinates": str(coord), "includes": topo["includes"]}
        report["coordinate_residue_counts"] = structure["residue_counts"]
        checks["total_atom_count"] = {"passed": topo["total_atoms"] == structure["total_atoms"],
                                      "topology": topo["total_atoms"], "coordinates": structure["total_atoms"]}
        expanded, ligand_groups = [], []
        for molecule_name, count in topo["composition"]:
            molecule = topo["molecules"][molecule_name]
            groups = {}
            for atom in molecule["atoms"]:
                if atom["resname"] == resname:
                    groups.setdefault(atom["resid"], []).append(atom)
            for _ in range(count):
                expanded.extend(molecule["atoms"])
                for residue, atoms in groups.items():
                    ligand_groups.append({"moleculetype": molecule_name, "residue": residue, "atoms": atoms,
                                          "bonds": molecule["bonds"], "source": molecule["source"]})
        checks["ligand_topology_presence"] = {"passed": bool(ligand_groups), "copies": len(ligand_groups),
                                               "moleculetypes": sorted({g["moleculetype"] for g in ligand_groups})}
        heavy = [sum(a["heavy"] is True for a in group["atoms"]) for group in ligand_groups]
        identified = all(a["heavy"] is not None for group in ligand_groups for a in group["atoms"])
        checks["ligand_topology_heavy_atoms"] = {"passed": bool(heavy) and identified and all(n == expected["heavy_atoms"] for n in heavy),
                                                  "expected_per_copy": expected["heavy_atoms"], "observed_per_copy": heavy}
        report["atom_identity_method"] = "Atomtype atomic number, falling back to atomtype natural mass; per-atom repartitioned mass is not used for element identity"
        repartitioned = [{"moleculetype": group["moleculetype"], "atom": atom["name"],
                          "atomic_number": atom["atomic_number"], "natural_mass_Da": atom["natural_mass"], "simulation_mass_Da": atom["mass"]}
                         for group in ligand_groups for atom in group["atoms"]
                         if atom["natural_mass"] is not None and abs(atom["mass"] - atom["natural_mass"]) > 0.001]
        report["ligand_mass_repartitioning"] = {
            "detected": bool(repartitioned), "changed_atoms": len(repartitioned),
            "examples": repartitioned[:6],
            "scope": "Mass differences are reported without modifying the server topology or inferring an appropriate MD time step"}
        undefined = sorted({a["type"] for group in ligand_groups for a in group["atoms"] if a["type"] not in topo["atomtypes"]})
        checks["ligand_atomtypes_defined"] = {"passed": bool(ligand_groups) and not undefined, "undefined": undefined}
        ligand_bond_counts = []
        for group in ligand_groups:
            ids = {a["nr"] for a in group["atoms"]}
            ligand_bond_counts.append(len({tuple(sorted(edge)) for edge in group["bonds"] if set(edge) <= ids}))
        checks["ligand_bond_count"] = {"passed": bool(ligand_groups) and all(n >= expected["bonds"] for n in ligand_bond_counts),
                                       "input_bonds": expected["bonds"], "topology_bonds_including_hydrogen": ligand_bond_counts}
        if "heavy_atom_bonds" in expected:
            expected_edges = {tuple(sorted(bond["atoms"])) for bond in expected["heavy_atom_bonds"]}
            edge_checks = []
            for group in ligand_groups:
                names = {a["nr"]: a["name"] for a in group["atoms"] if a["heavy"] is True}
                observed_edges = {tuple(sorted((names[a], names[b]))) for a, b in group["bonds"] if a in names and b in names}
                edge_checks.append({"missing": sorted(expected_edges - observed_edges), "extra": sorted(observed_edges - expected_edges)})
            checks["ligand_heavy_bond_connectivity"] = {
                "passed": bool(edge_checks) and all(not e["missing"] and not e["extra"] for e in edge_checks),
                "copies": edge_checks, "scope": "Named heavy-atom bond edges; force-field bond parameters/order not assessed"}
        aligned = len(expanded) == len(structure["atoms"]) and all(
            a["name"] == b["name"] and a["resname"] == b["resname"]
            for a, b in zip(expanded, structure["atoms"]))
        checks["coordinate_topology_atom_order"] = {"passed": aligned,
                                                    "scope": "Atom/residue names agree position-by-position"}
        coord_ligand = [i for i, a in enumerate(structure["atoms"]) if a["resname"] == resname]
        if coord.suffix.lower() == ".pdb" and all(structure["atoms"][i]["element"] for i in coord_ligand):
            heavy_count = sum(structure["atoms"][i]["element"] not in ("H", "D", "T") for i in coord_ligand)
            method = "PDB element columns"
        elif aligned and all(expanded[i]["heavy"] is not None for i in coord_ligand):
            heavy_count = sum(expanded[i]["heavy"] is True for i in coord_ligand)
            method = "Position-matched topology atomic number, or atomtype natural mass when atomic number is absent"
        else:
            heavy_count, method = None, "Cannot establish elements without matching topology atom order"
        copies = structure["residue_counts"].get(resname, 0)
        checks["ligand_coordinate_retention"] = {"passed": copies == len(ligand_groups) and copies > 0
                                                and heavy_count == expected["heavy_atoms"] * copies,
                                                "copies": copies, "heavy_atoms": heavy_count, "method": method}
        expected_names = Counter(a["pdb_atom_name"] for a in expected.get("atom_mapping", []) if a["element"] not in ("H", "D", "T"))
        if expected_names:
            checks["ligand_heavy_atom_names"] = {"passed": bool(ligand_groups) and all(
                Counter(a["name"] for a in g["atoms"] if a["heavy"] is True) == expected_names for g in ligand_groups),
                "scope": "Input heavy atom names must be preserved; explicit remapping is needed if the server renames them"}
        if run_grompp:
            index_path = (root / index).resolve() if index else next(iter(sorted(top.parent.glob("*.ndx"))), None)
            if index is None and len(list(top.parent.glob("*.ndx"))) > 1:
                raise ToolError("Multiple index files; supply index explicitly for grompp.")
            checks["grompp"] = run_grompp_check(top, coord, mdp_path, output_parent or root / "validation-results",
                                                 index=index_path, gmx=gmx)
        report["passed"] = bool(checks) and all(check["passed"] for check in checks.values())
    except (ToolError, OSError, ValueError) as exc:
        errors.append(str(exc))
    return report


def find_ion_settings(system_dir, *, stream_paths=None):
    """Find concentration settings in server-generated ion CHARMM input files.

    Evidence is returned with line numbers; request manifests are never used as
    proof of what the server applied. Files without an explicit setting provide
    no direct concentration evidence.
    """
    records, seen = [], set()
    root = Path(system_dir).resolve()
    stream_paths = stream_paths or {}
    for path in sorted(root.rglob("*.inp")):
        if "ion" not in path.name.lower() or path.stat().st_size > 4 * 1024 ** 2:
            continue
        variables = {}
        for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
            line = raw.split("!", 1)[0].strip()
            assignment = re.fullmatch(r"set\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*)?(\d+)", line, re.I)
            if assignment:
                variables[assignment[1].lower()] = int(assignment[2])
            match = re.fullmatch(r"(?:set\s+)?(?:ion_conc|conc|concentration)\s*(?:=\s*)?([0-9.eE+-]+)", line, re.I)
            if match:
                try:
                    value = float(match[1])
                    if math.isfinite(value) and value >= 0:
                        records.append({"file": str(path.resolve()), "line": lineno,
                                        "statement": line, "concentration_M": value})
                except ValueError:
                    pass
            stream = re.fullmatch(r"stream\s+(\S*ions?\S*\.str)\s+(\S+)", line, re.I)
            if not stream:
                continue
            source = Path(stream_paths.get(stream[1], path.parent / stream[1])).resolve()
            if not source.is_relative_to(root) or not source.is_file() or source.stat().st_size > 4 * 1024 ** 2:
                continue
            argument = stream[2]
            selected = variables.get(argument[1:].lower()) if argument.startswith("@") else int(argument) if argument.isdigit() else None
            stream_lines = [(n, raw.split("!", 1)[0].strip()) for n, raw in enumerate(source.read_text().splitlines(), 1)]
            totals = [int(m[1]) for _, statement in stream_lines
                      for m in [re.fullmatch(r"set\s+niontypes\s*(?:=\s*)?(\d+)", statement, re.I)] if m]
            identity = (str(source), selected)
            if identity in seen:
                continue
            seen.add(identity)
            if selected != 1 or totals != [1]:
                records.append({"file": str(source), "concentration_M": None, "status": "unresolved_conditional_stream",
                                "interpretation_limit": "Only a referenced single-salt stream with niontypes=1 and selected index 1 is resolved.",
                                "reference": {"file": str(path), "line": lineno, "statement": line}})
                continue
            settings, evidence = {}, []
            for number, statement in stream_lines:
                conditional = re.fullmatch(r"if\s+@IN1\s+\.eq\.\s+(\d+)\s+set\s+(conc|pos|neg)\s*(?:=\s*)?(\S+)", statement, re.I)
                if conditional and int(conditional[1]) == selected:
                    settings[conditional[2].lower()] = conditional[3]
                    evidence.append({"line": number, "statement": statement})
            try:
                concentration = float(settings["conc"])
                if not math.isfinite(concentration) or concentration < 0:
                    raise ValueError
            except (KeyError, ValueError):
                continue
            records.append({"file": str(source), "concentration_M": concentration,
                            "cation": settings.get("pos"), "anion": settings.get("neg"),
                            "selected_index": 1, "niontypes": 1, "statements": evidence,
                            "reference": {"file": str(path), "line": lineno, "statement": line},
                            "interpretation_limit": "Resolves only the explicit index-1 assignments of this referenced single-salt stream; not a general CHARMM interpreter."})
    return records


def inspect_environment(coordinates, *, expected_lipids, ion_type, concentration_M, system_dir=None):
    """Check requested membrane/ions; water-ratio concentration is only an estimate."""
    structure = inspect_coordinates(coordinates)
    counts = structure["residue_counts"]
    water_names = ("TIP3", "TIP3P", "SOL", "WAT", "HOH")
    water = sum(counts.get(name, 0) for name in water_names)
    cations = {"KCl": ("POT", "K", "K+", "KP"), "NaCl": ("SOD", "NA", "Na", "NA+")}
    if ion_type not in cations:
        raise ToolError("Initial salt validation supports KCl and NaCl only.")
    positive = sum(counts.get(name, 0) for name in cations[ion_type])
    negative = sum(counts.get(name, 0) for name in ("CLA", "CL", "Cl", "CL-"))
    opposite = "NaCl" if ion_type == "KCl" else "KCl"
    unexpected = {name: counts[name] for name in cations[opposite] if counts.get(name)}
    estimate = min(positive, negative) * 55.5 / water if water else None
    evidence = find_ion_settings(system_dir) if system_dir else []
    tolerance = max(0.01, float(concentration_M) * 0.15)
    approximate_match = estimate is not None and abs(estimate - concentration_M) <= tolerance
    resolved_evidence = [item for item in evidence if item.get("concentration_M") is not None]
    direct_match = bool(resolved_evidence) and len(resolved_evidence) == len(evidence) and all(
        abs(item["concentration_M"] - concentration_M) < 0.00001 for item in resolved_evidence)
    checks = {
        "requested_lipids_present": {"passed": bool(expected_lipids) and all(counts.get(name, 0) > 0 for name in expected_lipids),
                                     "counts": {name: counts.get(name, 0) for name in expected_lipids}},
        "water_present": {"passed": water > 0, "molecules": water},
        "ion_species": {"passed": (positive > 0 and negative > 0 if concentration_M > 0 else True) and not unexpected,
                        "requested": ion_type, "cations": positive, "chloride": negative, "unexpected_monovalent_cations": unexpected},
        "concentration_direct_evidence": {"passed": direct_match, "requested_M": concentration_M,
                                         "status": "matched" if direct_match else "unresolved" if len(resolved_evidence) != len(evidence)
                                                   else "mismatch" if evidence else "not_found",
                                         "records": evidence},
    }
    declared_species = [item for item in evidence if item.get("cation") or item.get("anion")]
    if declared_species:
        checks["ion_species_direct_evidence"] = {
            "passed": all(item.get("cation") in cations[ion_type] and item.get("anion") in ("CLA", "CL", "Cl", "CL-")
                          for item in declared_species),
            "records": [{"file": item["file"], "cation": item.get("cation"), "anion": item.get("anion"),
                         "selected_index": item.get("selected_index")} for item in declared_species]}
    return {"passed": all(item["passed"] for item in checks.values()), "checks": checks,
            "water_ratio_salt_estimate": {"concentration_M": estimate, "approximately_matches_request": approximate_match,
                                          "tolerance_M": tolerance, "paired_ions": min(positive, negative),
                                          "excess_counterions": abs(positive - negative),
                                          "formula": "min(monovalent cations, chloride) / water molecules * 55.5 M",
                                          "limitations": "Approximate water-density estimate after excluding excess counterions; not direct evidence of server concentration settings."},
            "residue_counts": counts, "scientific_correctness_verified": False}


def compare_bound_pose(reference_pdb, final_pdb, *, ligand_resname="LIG", ligand_atom_names=None,
                       reference_protein_chain=None, final_protein_segment=None,
                       protein_rmsd_limit_A=1.0, ligand_rmsd_limit_A=2.0):
    """Fit protein CA atoms, then test ligand pose under that same rigid transform.

    Small minimization changes are allowed by explicit thresholds. Coordinates
    must be whole/unwrapped: this function deliberately does not silently shift
    individual atoms across periodic boundaries or optimize the ligand pose.
    """
    try:
        import numpy as np
    except ImportError:
        raise ToolError("Protein/ligand rigid-transform checks require NumPy.") from None

    def records(path):
        result = []
        lines = Path(path).read_text().splitlines()
        if sum(line.startswith("MODEL ") for line in lines) > 1:
            raise ToolError("Pose comparison requires a single coordinate model.")
        for line in lines:
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            if line[16:17].strip():
                raise ToolError("Pose comparison requires resolved alternate locations.")
            try:
                xyz = tuple(float(line[i:i + 8]) for i in (30, 38, 46))
                resid = int(line[22:26])
                if not all(math.isfinite(v) for v in xyz):
                    raise ValueError
            except ValueError:
                raise ToolError("Malformed PDB coordinates in pose comparison.") from None
            result.append({"xyz": xyz, "resid": resid, "name": line[12:16].strip(),
                           "resname": line[17:21].strip(), "chain": line[21:22], "segment": line[72:76].strip(),
                           "element": line[76:78].strip(), "record": line[:6].strip()})
        return result

    reference, final = records(reference_pdb), records(final_pdb)
    source_ca = [a for a in reference if a["record"] == "ATOM" and a["name"] == "CA"
                 and a["resname"] != ligand_resname and (reference_protein_chain is None or a["chain"] == reference_protein_chain)]
    final_ca = [a for a in final if a["record"] == "ATOM" and a["name"] == "CA"
                and a["resname"] != ligand_resname and (final_protein_segment is None or a["segment"] == final_protein_segment)]
    source_map, final_map = {a["resid"]: a for a in source_ca}, {a["resid"]: a for a in final_ca}
    if len(source_map) != len(source_ca) or len(final_map) != len(final_ca):
        raise ToolError("Protein residue numbers are ambiguous across chains; select reference chain/final segment explicitly.")
    if len(source_map) < 3 or set(source_map) != set(final_map):
        raise ToolError("Protein CA residue correspondence is incomplete; explicit residue mapping is required.")
    keys = sorted(source_map)
    x = np.array([source_map[key]["xyz"] for key in keys])
    y = np.array([final_map[key]["xyz"] for key in keys])
    x_center, y_center = x.mean(axis=0), y.mean(axis=0)
    u, singular, vt = np.linalg.svd((x - x_center).T @ (y - y_center))
    if singular[1] < 1e-8:
        raise ToolError("Protein CA coordinates cannot define a unique rigid-body orientation.")
    correction = np.diag([1.0, 1.0, float(np.linalg.det(u @ vt))])
    rotation = u @ correction @ vt
    translation = y_center - x_center @ rotation
    protein_rmsd = float(np.sqrt(np.mean(np.sum((x @ rotation + translation - y) ** 2, axis=1))))
    source_ligand = [a for a in reference if a["resname"] == ligand_resname]
    if ligand_atom_names is None:
        if not source_ligand or any(not a["element"] for a in source_ligand):
            raise ToolError("Supply ligand_atom_names when reference ligand PDB element columns are absent.")
        ligand_atom_names = [a["name"] for a in source_ligand if a["element"] not in ("H", "D", "T")]
    selected = set(ligand_atom_names)
    source_ligand = [a for a in source_ligand if a["name"] in selected]
    target_ligand = [a for a in final if a["resname"] == ligand_resname and a["name"] in selected]
    source_names, target_names = {a["name"]: a for a in source_ligand}, {a["name"]: a for a in target_ligand}
    if (not selected or len(source_names) != len(source_ligand) or len(target_names) != len(target_ligand)
            or set(source_names) != selected or set(target_names) != selected):
        raise ToolError("Ligand atom correspondence is incomplete/ambiguous; select a single ligand copy.")
    names = sorted(selected)
    a = np.array([source_names[name]["xyz"] for name in names])
    b = np.array([target_names[name]["xyz"] for name in names])
    displacement = np.linalg.norm(a @ rotation + translation - b, axis=1)
    ligand_rmsd = float(np.sqrt(np.mean(displacement ** 2)))
    return {"passed": protein_rmsd <= protein_rmsd_limit_A and ligand_rmsd <= ligand_rmsd_limit_A,
            "protein_ca_count": len(keys), "ligand_heavy_atom_count": len(names),
            "protein_ca_rmsd_A": protein_rmsd, "ligand_rmsd_after_protein_fit_A": ligand_rmsd,
            "ligand_max_displacement_A": float(displacement.max()),
            "thresholds_A": {"protein_ca_rmsd": protein_rmsd_limit_A, "ligand_rmsd": ligand_rmsd_limit_A},
            "rotation_row_vector": rotation.tolist(), "translation_A": translation.tolist(),
            "scope": "Protein-fit relative ligand pose; global PPM rotation/translation permitted, no independent ligand fit",
            "limitations": ["Requires whole molecules in one periodic image; does not unwrap coordinates.",
                            "Within-threshold RMSD does not validate the binding pose or guarantee ligand chemistry."],
            "scientific_correctness_verified": False}
