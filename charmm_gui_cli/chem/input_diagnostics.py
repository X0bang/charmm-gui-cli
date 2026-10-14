"""Actionable local input diagnostics and deliberately incomplete retention drafts."""

from pathlib import Path
import shlex

from ..locales.i18n import t
from .residues import MODIFIED, INPUT_FIELDS


def check_command(*, protein=None, ligand=None, complex_pdb=None, ligand_resname="LIG", input_options=None):
    args = ["charmm-gui-cli", "check"]
    for option, value in (("--protein", protein), ("--ligand", ligand), ("--complex", complex_pdb)):
        if value is not None:
            args.extend((option, str(value)))
    if ligand_resname != "LIG":
        args.extend(("--ligand-resname", ligand_resname))
    for key, value in (input_options or {}).items():
        if key not in INPUT_FIELDS:
            continue
        if key in ("keep_mse", "drop_additives"):
            if value:
                args.append("--" + key.replace("_", "-"))
        elif value:
            args.extend(("--" + key.replace("_", "-"), ",".join(value) if isinstance(value, list) else str(value)))
    return shlex.join(args)


def bind_input_error(error, *, protein=None, ligand=None, complex_pdb=None, ligand_resname="LIG", input_options=None):
    options = dict(input_options) if isinstance(input_options, dict) else {}
    if isinstance(options.get("drop_hetero"), str):
        options["drop_hetero"] = options["drop_hetero"].split(",")
    if error.key in ("input.models", "input.model_range"):
        options["model"] = 1
    elif error.key == "input.altloc_incomplete":
        options["altloc"] = "highest"
    removable = sorted({detail["resname"] for detail in error.details if detail.get("resname") not in MODIFIED and detail.get("kind")})
    if error.key == "input.protein_hetero" and removable:
        options["drop_hetero"] = sorted(set(options.get("drop_hetero", [])) | set(removable))
        error.next_step = check_command(protein=protein, ligand=ligand, complex_pdb=complex_pdb,
                                        ligand_resname=ligand_resname, input_options=options)
    else:
        command = check_command(protein=protein, ligand=ligand, complex_pdb=complex_pdb,
                                ligand_resname="LIG" if error.key == "input.reserved_name" else ligand_resname, input_options=options)
        error.next_step = command if error.key in ("input.models", "input.model_range", "input.reserved_name") else t("input.recheck", command=command)
    if error.key == "input.protein_hetero":
        error.details = [detail for detail in error.details if detail.get("key") != "input.retain_via_check"]
        error.details.append({"key": "input.retain_via_check", "params": {"command": check_command(
            protein=protein, ligand=ligand, complex_pdb=complex_pdb, ligand_resname=ligand_resname, input_options=input_options)}})
    return error


def retention_draft(directory, *, source, ligand=None, ligand_resname="LIG", input_options=None, findings=()):
    """Retain coordinates, never fabricate cofactor charges/parameters or bonds.

    The draft is intentionally not ready to submit. Modified/covalent residues
    remain unsupported; the header explains that the component path cannot model
    coordination merely because a selector and a topology filename exist.
    """
    import yaml
    from .inputs import _pdb, _conect, read_ligand
    from .pdb_normalization import normalize_lines
    out = Path(directory) / "retained-components"
    out.mkdir(mode=0o700)
    options = dict(input_options or {})
    atoms, _ = _pdb(source, input_options=options, role="complex")
    lines = normalize_lines(source, options=options, role="complex")
    # Atom text, LINK/SSBOND, charges and encoded bonds are retained verbatim.
    while lines and lines[-1].strip() in ("END", ""):
        lines.pop()
    components, groups = [], {}
    for atom in atoms:
        if atom.line.startswith("HETATM"):
            line = atom.line
            key = (line[17:21].strip(), line[21:22].strip(), int(line[22:26]), line[26:27].strip(), line[72:76].strip())
            groups.setdefault(key, []).append(atom)
    supplied = read_ligand(ligand) if ligand and Path(ligand).suffix.lower() == ".sdf" else None
    if supplied and not any(key[0] == ligand_resname for key in groups):
        chain = next((value for value in "LABCDEFGHIJKMNOPQRSTUVWXYZ0123456789" if value not in {a.line[21:22] for a in atoms}), None)
        if chain is None or max(a.serial for a in atoms) + len(supplied.atoms) > 99999:
            return None
        serial, new_atoms = max(a.serial for a in atoms), []
        from dataclasses import replace
        lines.append("TER")
        for index, atom in enumerate(supplied.atoms, 1):
            charge = f"{abs(atom.charge)}{'+' if atom.charge > 0 else '-'}" if atom.charge else "  "
            line = (f"HETATM{serial + index:5d} {atom.name:>4} {ligand_resname:>3} {chain}   1    "
                    + "".join(f"{value:8.3f}" for value in atom.xyz) + f"  1.00  0.00          {atom.element:>2}{charge}")
            new_atoms.append(replace(atom, serial=serial + index, line=line))
            lines.append(line)
        lines.extend(_conect({(serial + a + 1, serial + b + 1): order for (a, b), order in supplied.bonds.items()}).splitlines())
        groups[(ligand_resname, chain, 1, "", "")] = new_atoms
    for index, (key, group) in enumerate(groups.items(), 1):
        name, chain, resid, icode, segment = key
        spec = {"resname": name, "chain": chain, "resid": resid, "icode": icode, "segment": segment, "role": "cofactor"}
        spec["sdf"] = f"REQUIRED/component-{index:03d}-{name}.sdf"
        if supplied and name == ligand_resname:
            # Add explicit names without rewriting any atom/bond/stereo records.
            names = [atom.name for atom in supplied.atoms]
            if len(names) == len(group) and {(a.name, a.element) for a in group} == {(a.name, a.element) for a in supplied.atoms}:
                text = supplied.sdf
                if "<PDB_ATOM_NAMES>" not in text:
                    head, _, _ = text.partition("$$$$")
                    text = head.rstrip() + "\n\n> <PDB_ATOM_NAMES>\n" + " ".join(names) + "\n\n$$$$\n"
                (out / "ligand.sdf").write_text(text, encoding="utf-8")
                spec.update(sdf="ligand.sdf", role="ligand")
        if len(group) == 1 and group[0].element not in ("H", "C", "N", "O", "S", "P", "F", "Cl", "Br", "I"):
            spec["topology"] = {"rtf": f"REQUIRED/{name}.rtf", "prm": f"REQUIRED/{name}.prm", "trusted": False}
        components.append(spec)
    config = out / "components.yaml"
    complex_path = out / "complex.pdb"
    complex_path.write_text("\n".join(lines + ["END"]) + "\n", encoding="utf-8")
    raw = {"version": 1, "water_policy": "reject", "ion_policy": "reject", "components": components}
    config.write_text("\n".join("# " + line for line in t("input.draft_header").splitlines()) + "\n" + yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return {"config": str(config), "complex": str(complex_path), "ready_to_submit": False,
            "next_step": shlex.join(["charmm-gui-cli", "check", "--complex", str(complex_path), "--components", str(config)])}
