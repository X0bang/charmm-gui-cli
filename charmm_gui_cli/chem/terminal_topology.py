"""Bounded, data-only terminal atom/bond projection from a CHARMM protein RTF.

This is not a CHARMM interpreter, force-field converter or energy validation.
Only RESI/PRES atom declarations, local bonds and atom deletions are used.
Other directives cannot supply atom/bond evidence. Never execute RTF commands.
"""

import math
from pathlib import Path

from ..locales.i18n import t
from ..store.auth import ToolError


def failure():
    return ToolError(key="fragment.topology_invalid", category="fragment_topology_unverified",
                     next_step=t("fragment.topology_next"))


def read_terminal_topology(path):
    try:
        with Path(path).open("rb") as handle:
            content = handle.read(4 * 1024 * 1024 + 1)
        if not 0 < len(content) <= 4 * 1024 * 1024:
            raise ValueError
        result = {"masses": {}, "residues": {}, "patches": {}}
        current = None
        for line in content.decode("utf-8").splitlines():
            fields = line.split("!", 1)[0].split()
            if not fields or fields[0].startswith("*"):
                continue
            command = fields[0].upper()
            if command == "MASS":
                name, mass = fields[2], float(fields[3])
                if name in result["masses"] or not math.isfinite(mass) or mass <= 0:
                    raise ValueError
                result["masses"][name] = mass
            elif command in ("RESI", "PRES"):
                table = result["residues" if command == "RESI" else "patches"]
                name = fields[1]
                if name in table or not math.isfinite(float(fields[2])):
                    raise ValueError
                current = {"atoms": {}, "bonds": set(), "deleted": set(), "unsupported": False}
                table[name] = current
            elif command == "END":
                current = None
            elif current is not None and command == "ATOM":
                if len(fields) != 4 or fields[1] in current["atoms"]:
                    raise ValueError
                name, kind, charge = fields[1], fields[2], float(fields[3])
                if not math.isfinite(charge):
                    raise ValueError
                current["atoms"][name] = {"type": kind, "charge": charge}
            elif current is not None and command in ("BOND", "DOUBLE", "TRIPLE"):
                if len(fields) < 3 or len(fields) % 2 != 1:
                    raise ValueError
                for left, right in zip(fields[1::2], fields[2::2]):
                    if left == right:
                        raise ValueError
                    current["bonds"].add(tuple(sorted((left, right))))
            elif current is not None and command in ("DELETE", "DELE"):
                if len(fields) == 3 and fields[1].upper() == "ATOM":
                    current["deleted"].add(fields[2])
                else:
                    current["unsupported"] = True
            elif current is not None and command not in (
                    "GROUP", "IC", "IMPR", "IMPH", "CMAP", "DONOR", "ACCEPTOR", "PATCHING", "ANGLE", "DIHE"):
                # Unknown executable/abbreviated directives cannot be ignored
                # in a template that will later be used as terminal evidence.
                current["unsupported"] = True
        if not result["masses"] or not result["residues"] or not result["patches"]:
            raise ValueError
        return result
    except (OSError, UnicodeError, IndexError, ValueError, TypeError):
        raise failure() from None


def terminal_profile(topology, resname, patches):
    """Compose only nonconflicting local terminal atom/bond declarations.

    Opposite-end patches on one residue must agree on any shared atom edit;
    their CHARMM application order is deliberately not guessed.
    """
    try:
        base = topology["residues"][resname]
        if base["unsupported"] or base["deleted"]:
            raise ValueError
        atoms = {name: dict(value) for name, value in base["atoms"].items()}
        bonds = set(base["bonds"])
        edits = {}
        for patch in patches:
            if patch == "NONE":
                continue
            spec = topology["patches"][patch]
            if spec["unsupported"] or not spec["atoms"]:
                raise ValueError
            for name, value in [*((name, None) for name in spec["deleted"]), *spec["atoms"].items()]:
                if name in edits and edits[name] != value:
                    raise ValueError
                edits[name] = value
            for name in spec["deleted"]:
                atoms.pop(name, None)
                bonds = {edge for edge in bonds if name not in edge}
            atoms.update({name: dict(value) for name, value in spec["atoms"].items()})
            bonds.update(spec["bonds"])
        # External +/- residue references are checked through the actual
        # verified fragment's peptide order, not guessed by this projection.
        bonds = {edge for edge in bonds if not any(name.startswith(("+", "-")) for name in edge)}
        if any(name not in atoms for edge in bonds for name in edge):
            raise ValueError
        for value in atoms.values():
            value["mass"] = topology["masses"][value["type"]]
        return {"atoms": atoms, "bonds": bonds}
    except (KeyError, TypeError, ValueError):
        raise failure() from None
