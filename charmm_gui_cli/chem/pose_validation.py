"""Conservative file selection and manifest mapping for bound-pose checks."""

import json
import hashlib
import math
from pathlib import Path, PurePosixPath
import re
import tarfile

from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.chem.validation import compare_bound_pose, element_from_natural_mass, validate_atom_name_map


def derive_server_atom_mapping(system_dir, reference_pdb, expected, *, archive_path):
    """Prove names from unchanged step1 coordinates, never from the final pose.

    Only job-bound canonical archive members qualify. CHARMM PDBs commonly lack
    elements, so their matching unmodified source PSF supplies natural masses.
    The caller must additionally revalidate mapped final elements and bonds.
    """
    from charmm_gui_cli.store.provenance import verify_archive_provenance
    if archive_path is None:
        raise ToolError("Automatic atom-name mapping requires a job-bound source archive.")
    archive_path = Path(archive_path).resolve()
    if archive_path.name != "charmm-gui.tgz":
        raise ToolError("Atom-name mapping requires the recorded charmm-gui.tgz archive.")
    provenance = verify_archive_provenance(archive_path.parent)
    root = Path(system_dir).resolve()
    canonical = root if root.name == provenance["archive_root"] else root / provenance["archive_root"]
    paths = {name: canonical / name for name in ("step1_pdbreader.pdb", "step1_pdbreader.psf")}
    bodies, hashes = {}, {}
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ToolError("Missing canonical archive step1 PDB/PSF for atom-name mapping.")
        if list(root.rglob(name)) != [path]:
            raise ToolError("Ambiguous step1 PDB/PSF candidates for atom-name mapping.")
        bodies[name] = path.read_bytes()
        hashes[name] = hashlib.sha256(bodies[name]).hexdigest()
    found = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if len(parts) == 2 and parts[0] == provenance["archive_root"] and parts[1] in paths:
                name = parts[1]
                if name in found or not member.isfile():
                    raise ToolError("Ambiguous canonical step1 member in the source archive.")
                with archive.extractfile(member) as handle:
                    digest = hashlib.sha256()
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != hashes[name]:
                    raise ToolError("Extracted step1 file differs from the job-bound archive member.")
                found.add(name)
    if found != set(paths):
        raise ToolError("Source archive lacks the canonical step1 mapping evidence.")

    def records(text):
        lines = text.splitlines()
        if sum(line.startswith("MODEL ") for line in lines) > 1:
            raise ToolError("Atom-name mapping requires a single PDB model.")
        atoms = []
        for line in lines:
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            if line[16:17].strip():
                raise ToolError("Atom-name mapping requires resolved alternate locations.")
            xyz = tuple(float(line[i:i + 8]) for i in (30, 38, 46))
            if not all(math.isfinite(value) for value in xyz):
                raise ToolError("Atom-name mapping requires finite original coordinates.")
            atoms.append({"name": line[12:16].strip(), "resname": line[17:21].strip(),
                          "resid": line[22:27].strip(), "chain": line[21:22], "segment": line[72:76].strip(),
                          "element": line[76:78].strip().title(), "xyz": xyz})
        return atoms

    reference_bytes = Path(reference_pdb).read_bytes()
    source = records(reference_bytes.decode("utf-8"))
    server = records(bodies["step1_pdbreader.pdb"].decode("utf-8"))
    psf_lines = bodies["step1_pdbreader.psf"].decode("utf-8").splitlines()
    starts = [i for i, line in enumerate(psf_lines) if "!NATOM" in line]
    if len(starts) != 1:
        raise ToolError("Step1 PSF must contain one unambiguous atom section.")
    start = starts[0]
    count = int(psf_lines[start].split()[0])
    psf = [line.split() for line in psf_lines[start + 1:start + 1 + count]]
    if count != len(server) or len(psf) != count:
        raise ToolError("Step1 PDB/PSF atom counts differ.")
    resname = expected["resname"]
    for index, (atom, fields) in enumerate(zip(server, psf), 1):
        if (len(fields) < 8 or int(fields[0]) != index or
                (fields[1], fields[2], fields[3], fields[4]) !=
                (atom["segment"], atom["resid"], atom["resname"], atom["name"])):
            raise ToolError("Step1 PDB/PSF atom identities or ordering disagree.")
        if atom["resname"] == resname:
            element = element_from_natural_mass(float(fields[7]))
            if atom["element"] and atom["element"] != element:
                raise ToolError("Step1 PDB element and source PSF natural mass disagree.")
            atom["element"] = element

    def one_copy(atoms):
        chosen = [a for a in atoms if a["resname"] == resname]
        identities = {(a["segment"], a["chain"], a["resid"]) for a in chosen}
        if len(identities) != 1 or any(not atom["element"] for atom in chosen):
            raise ToolError("Mapping requires exactly one residue copy with proven elements on each side.")
        heavy = [a for a in chosen if a["element"].upper() not in ("H", "D", "T")]
        if len({a["name"] for a in heavy}) != len(heavy):
            raise ToolError("Mapping requires unique heavy-atom names within each residue.")
        return heavy

    original, renamed = one_copy(source), one_copy(server)
    declared = {a["pdb_atom_name"]: a["element"].title() for a in expected["atom_mapping"]
                if a["element"].upper() not in ("H", "D", "T")}
    if ({a["name"]: a["element"] for a in original} != declared
            or len(original) != len(renamed) or len(original) != expected["heavy_atoms"]):
        raise ToolError("Step1/reference/manifest do not cover the identical complete heavy-atom set.")
    targets = {}
    for atom in renamed:
        targets.setdefault((atom["element"], atom["xyz"]), []).append(atom)
    mapping, evidence, used = {}, [], set()
    for atom in original:
        matches = targets.get((atom["element"], atom["xyz"]), [])
        if len(matches) != 1 or matches[0]["name"] in used:
            raise ToolError("Original coordinate/element mapping is incomplete or ambiguous; no fuzzy matching is allowed.")
        match = matches[0]
        mapping[atom["name"]] = match["name"]
        used.add(match["name"])
        evidence.append({"reference_name": atom["name"], "server_name": match["name"],
                         "element": atom["element"], "unchanged_coordinates_A": list(atom["xyz"])})
    validate_atom_name_map(expected, mapping)
    return {"mapping": mapping, "coordinate_mapping_passed": True, "resname": resname,
            "reference_pdb": str(Path(reference_pdb).resolve()), "reference_sha256": hashlib.sha256(reference_bytes).hexdigest(),
            "step1_pdb": str(paths["step1_pdbreader.pdb"]), "step1_pdb_sha256": hashes["step1_pdbreader.pdb"],
            "step1_psf": str(paths["step1_pdbreader.psf"]), "step1_psf_sha256": hashes["step1_pdbreader.psf"],
            "archive_provenance": provenance, "atoms": evidence,
            "scope": "Unique exact original-coordinate and element bijection before orientation/minimization; no final-pose fit used to infer names",
            "element_evidence": "Source step1 PSF natural masses, PDB/PSF complete atom-order identity; explicit PDB elements cross-checked when present"}


def validate_bound_pose(system_dir, input_manifest, reference_pdb, *, atom_name_map=None):
    """Compare one final CHARMM assembly to a supplied bound-pose reference.

    Missing or ambiguous assembly files fail explicitly. No coordinate format
    fallback, atom-name guessing, periodic unwrapping, or ligand fitting occurs.
    """
    report = {"passed": False, "scientific_correctness_verified": False,
              "reference_pdb": str(Path(reference_pdb).resolve())}
    try:
        if not Path(reference_pdb).is_file():
            raise ToolError("Reference PDB file does not exist.")
        root = Path(system_dir).resolve()
        candidates = sorted(path for path in root.rglob("step5_assembly.pdb") if path.is_file())
        if not candidates:
            raise ToolError("No step5_assembly.pdb was found for bound-pose validation.")
        if len(candidates) != 1:
            raise ToolError("Multiple step5_assembly.pdb files were found; choose a system directory containing exactly one assembly.")
        final = candidates[0]
        report["assembly_pdb"] = str(final)
        from ..store.paths import read_value
        manifest = input_manifest if isinstance(input_manifest, dict) else read_value(input_manifest)
        ligand = manifest["ligand"]
        resname = ligand["resname"]
        if not isinstance(resname, str) or not re.fullmatch(r"[A-Za-z0-9]{1,4}", resname):
            raise ToolError("Input manifest has an invalid ligand residue name.")
        mapping = ligand["atom_mapping"]
        if not isinstance(mapping, list) or not mapping:
            raise ToolError("Input manifest must include ligand atom_mapping for bound-pose validation.")
        names = []
        for atom in mapping:
            element = atom["element"]
            name = atom["pdb_atom_name"]
            if not isinstance(element, str) or not element.strip() or not isinstance(name, str) or not name.strip():
                raise ToolError("Input manifest ligand atom names and elements must be explicit.")
            if element.upper() not in ("H", "D", "T"):
                names.append(name)
        if not names or len(set(names)) != len(names):
            raise ToolError("Input manifest must provide unique ligand heavy-atom names.")
        if atom_name_map is not None:
            name_map = validate_atom_name_map(ligand, atom_name_map)
            report.update(compare_bound_pose(reference_pdb, final, ligand_resname=resname, ligand_atom_names=names,
                                             final_ligand_atom_names=[name_map[name] for name in names]))
            report["atom_name_correspondence"] = name_map
        else:
            report.update(compare_bound_pose(reference_pdb, final, ligand_resname=resname, ligand_atom_names=names))
        return report
    except ToolError as exc:
        report["error"] = str(exc)
    except (KeyError, TypeError, ValueError):
        report["error"] = "Input manifest or coordinate data are malformed for bound-pose validation."
    except OSError:
        report["error"] = "Could not read the reference, assembly, or input manifest for bound-pose validation."
    return report
