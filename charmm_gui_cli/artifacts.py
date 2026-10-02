"""Inspect archives in place; never extract untrusted archive paths."""

from pathlib import PurePosixPath
import re
import tarfile

from .auth import ToolError
from .config import pdb_residues

MAX_PDB_BYTES = 512 * 1024 * 1024


def inspect_archive(path, expected_ligands, pdb_member=None):
    try:
        with tarfile.open(path, "r:gz") as archive:
            files = []
            for index, member in enumerate(archive):
                if index >= 50000:
                    raise ToolError("Archive has too many members to inspect.")
                if member.isfile():
                    files.append(member)
            if pdb_member:
                candidates = [m for m in files if m.name == pdb_member]
            else:
                # Do not validate against step1/input PDBs: they could still
                # contain a ligand which was dropped during assembly.
                candidates = [m for m in files if re.fullmatch(
                    r"step\d+_(?:assembly|pbcsetup)(?:\.box)?\.pdb", PurePosixPath(m.name).name
                )]
            if not candidates:
                raise ToolError("No recognizable assembled PDB. Use inspect --pdb-member with the final structure's exact archive path.")
            def rank(member):
                base = PurePosixPath(member.name).name
                step = re.search(r"step(\d+)", base)
                return (int(step.group(1)) if step else 0, "assembly" in base, -len(member.name))
            selected = max(candidates, key=rank)
            if selected.size > MAX_PDB_BYTES:
                raise ToolError("Final PDB exceeds the 512 MiB inspection limit.")
            with archive.extractfile(selected) as handle:
                counts = pdb_residues(line.decode("utf-8", errors="replace") for line in handle)
            missing = [name for name in expected_ligands if name not in counts]
            topology = [m.name for m in files if PurePosixPath(m.name).suffix.lower() in
                        (".psf", ".rtf", ".prm", ".str", ".top", ".itp")]
            return {
                "passed": not missing,
                "scope": "residue-name presence in one assembled PDB; NOT chemical/topology/force-field validation",
                "pdb_member": selected.name, "candidate_pdbs": [m.name for m in candidates],
                "expected_ligands": expected_ligands,
                "ligand_residue_counts": {name: counts.get(name, 0) for name in expected_ligands},
                "missing_ligands": missing, "topology_parameter_files": topology,
            }
    except (tarfile.TarError, EOFError, UnicodeError):
        raise ToolError("Cannot inspect the downloaded archive.") from None
