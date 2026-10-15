"""Actual input serialization for explicitly requested fragment staging.

No general geometry bypass: source/final topology and receipts stay mandatory.
Plans bind the final FULL upload complex, including reserved component chain IDs
and final serial numbers, not an earlier protein-only normalization hash.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .fragment_preparation import prepare_fragmented_lines, verify_fragment_preparation, remap_state_intent
from .pdb_normalization import normalize_lines, finding, terminal_predictions
from .protein_states import validate_intent, identity_key
from ..locales.i18n import t
from ..store.auth import ToolError


def failure():
    return ToolError(key="fragment.evidence_changed", next_step=t("fragment.inspect_next"))


def validate_fragment_option(value):
    if type(value) is not bool:
        raise ToolError(key="fragment.option_invalid", next_step=t("fragment.inspect_next"))


def resolve_fragment_option(value, options):
    """The public policy and older explicit internal keyword must not conflict."""
    validate_fragment_option(value)
    requested = options.get('split_chain_breaks')
    if requested is not None:
        validate_fragment_option(requested)
    if value and requested is False:
        raise ToolError(key='fragment.option_conflict', next_step='charmm-gui-cli check -h')
    return value or requested is True


def validate_prepared_terminals(prepared, preparation):
    local = prepared.get('manifest', {})
    normalization = local.get('input_normalization', {}) if isinstance(local, dict) else None
    if isinstance(normalization, dict) and 'fragment_preparation' not in normalization:
        return False
    record = verify_prepared_fragments(prepared)
    if record is None:
        raise failure()
    from .fragment_terminals import resolve_fragment_terminals
    files = prepared['files']
    resolve_fragment_terminals(preparation,
        Path(files[record['source_file']]).read_text(encoding='utf-8').splitlines(),
        Path(files[record['prepared_file']]).read_text(encoding='utf-8').splitlines(), record['plan'])
    return True


def validate_fragment_source(path, atoms, *, options, role, **context):
    """Validate an actual prospective split, never skip geometric validation."""
    from .inputs import _pdb_lines, _validate_protein
    try:
        _validate_protein(atoms, **context)
    except ToolError as error:
        if error.key != "input.chain_breaks":
            raise
    lines = normalize_lines(path, options=options, role=role)
    prepared, plan = prepare_fragmented_lines(lines, accept_breaks=True)
    if plan:
        selected = {atom.serial for atom in atoms}
        parsed, _ = _pdb_lines(prepared)
        _validate_protein([atom for atom in parsed if atom.serial in selected], **context)


def _identity(atom):
    line = atom.line
    return {"polymer_block": atom.polymer_block, "chain": line[21:22].strip(), "segment": line[72:76].strip(),
            "resid": int(line[22:26]), "icode": line[26:27].strip()}


def address_mapping(original, canonical, serial_map):
    """Bind original normalized identities to actual serializer output atoms."""
    by_serial = {atom.serial: atom for atom in canonical}
    result = {}
    for atom in original:
        try:
            new = by_serial[serial_map[atom.serial]]
            if (atom.name, atom.element, atom.xyz, atom.line[17:21].strip()) != (
                    new.name, new.element, new.xyz, new.line[17:21].strip()):
                raise ValueError
            key, value = identity_key(_identity(atom)), _identity(new)
            if key in result and result[key] != value:
                raise ValueError
            result[key] = value
        except (KeyError, ValueError):
            raise failure() from None
    return result


def rebase_state_intent(intent, mapping):
    intent = validate_intent(intent)
    if not intent:
        return {}
    result = deepcopy(intent)
    def move(entry):
        try:
            entry.update(mapping[identity_key(entry)])
        except KeyError:
            raise failure() from None
    for entry in result["residues"]:
        move(entry)
    for pair in result["disulfides"]:
        move(pair["left"])
        move(pair["right"])
    return validate_intent(result)


def _blocks(mapping):
    result = {}
    for old, new in mapping.items():
        key = old[:3]
        value = {key: new[key] for key in ("polymer_block", "chain", "segment")}
        if key in result and result[key] != value:
            raise failure()
        result[key] = value
    return [{"input": dict(zip(("polymer_block", "chain", "segment"), key)), "canonical": value}
            for key, value in result.items()]


def _fingerprint(record):
    body = {key: value for key, value in record.items() if key != "fingerprint"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _protein_text(lines):
    from .inputs import _pdb_lines, _conect
    atoms, bonds = _pdb_lines(lines)
    ids = {atom.serial for atom in atoms if atom.line.startswith("ATOM  ")}
    protein, preceding_protein = [], False
    for line in lines:
        if line.startswith(("ATOM  ", "SSBOND")):
            protein.append(line)
            if line.startswith("ATOM  "):
                preceding_protein = True
        elif line[:6].strip() == "TER" and preceding_protein:
            protein.append(line)
            preceding_protein = False
        elif line.startswith("HETATM"):
            preceding_protein = False
    return "\n".join(protein) + "\n" + _conect({edge: order for edge, order in bonds.items() if set(edge) <= ids}) + "END\n"


def apply_fragment_preparation(files, manifest, original_atoms, serial_map):
    """Transform final serialized upload bytes and store one pre-split snapshot.

    The caller writes the resulting files exclusively only AFTER this succeeds.
    No atoms, coordinates, pH states or caps are generated by this operation.
    """
    from .inputs import _pdb_lines, _validate_protein
    source = files["complex.pdb"].splitlines()
    prepared, plan = prepare_fragmented_lines(source, accept_breaks=True)
    if not plan:
        return None
    canonical, _ = _pdb_lines(source)
    mapping = address_mapping(original_atoms, canonical, serial_map)
    normalization = manifest.setdefault("input_normalization", {})
    source_state = rebase_state_intent(normalization.get("protein_state_intent"), mapping)
    state = remap_state_intent(source_state, source, prepared, plan)
    output_atoms, _ = _pdb_lines(prepared)
    protein = [atom for atom in output_atoms if atom.line.startswith("ATOM  ")]
    _validate_protein(protein)
    record = {"schema_version": 1, "source_file": "fragment-source.pdb", "prepared_file": "complex.pdb",
              "plan": plan, "source_blocks": _blocks(mapping), "source_state_intent": source_state,
              "remote_verified": False, "caps_generated_locally": False}
    record["fingerprint"] = _fingerprint(record)
    files["fragment-source.pdb"] = "\n".join(source) + "\n"
    files["complex.pdb"] = "\n".join(prepared) + "\n"
    files["protein.pdb"] = _protein_text(prepared)
    normalization["fragment_preparation"] = record
    if state:
        normalization["protein_state_intent"] = state
    normalization["terminal_predictions"] = terminal_predictions(prepared)
    from .structure_inventory import inspect_chains
    normalization["structure_inventory"] = inspect_chains(prepared)
    entry = finding("fragment.prepared", fragments=len(plan["fragments"]), breaks=len(plan["breaks"]))
    normalization.setdefault("repairs", []).append(entry)
    normalization["automatic_repairs"] = normalization["repairs"]
    normalization.setdefault("warnings", []).append(entry["message"])
    manifest["automatic_repairs"] = normalization["repairs"]
    return record


def _record_files(prepared):
    try:
        normalization = prepared["manifest"].get("input_normalization", {})
        record = normalization.get("fragment_preparation")
    except (KeyError, TypeError, AttributeError):
        raise failure() from None
    if record is None:
        return None
    try:
        if (set(record) != {"schema_version", "source_file", "prepared_file", "plan", "source_blocks", "source_state_intent",
                            "remote_verified", "caps_generated_locally", "fingerprint"}
                or type(record["schema_version"]) is not int or record["schema_version"] != 1
                or record["source_file"] != "fragment-source.pdb" or record["prepared_file"] != "complex.pdb"
                or record["remote_verified"] is not False or record["caps_generated_locally"] is not False
                or record["fingerprint"] != _fingerprint(record)):
            raise ValueError
        files = prepared["files"]
        source, actual = Path(files[record["source_file"]]), Path(files[record["prepared_file"]])
        if (source.is_symlink() or actual.is_symlink() or source.parent.is_symlink() or actual.parent.is_symlink()
                or source.parent.resolve() != actual.parent.resolve()):
            raise ValueError
        source_lines = source.read_text(encoding="utf-8").splitlines()
        prepared_lines = actual.read_text(encoding="utf-8").splitlines()
        verify_fragment_preparation(source_lines, prepared_lines, record["plan"])
        blocks, targets = set(), set()
        if not isinstance(record["source_blocks"], list) or not record["source_blocks"]:
            raise ValueError
        for row in record["source_blocks"]:
            if not isinstance(row, dict) or set(row) != {"input", "canonical"}:
                raise ValueError
            values = []
            for side in ("input", "canonical"):
                value = row[side]
                if (not isinstance(value, dict) or set(value) != {"polymer_block", "chain", "segment"}
                        or type(value["polymer_block"]) is not int or value["polymer_block"] < 0
                        or not isinstance(value["chain"], str) or len(value["chain"]) > 1
                        or not isinstance(value["segment"], str) or len(value["segment"]) > 4):
                    raise ValueError
                values.append(tuple(value[k] for k in ("polymer_block", "chain", "segment")))
            if values[0] in blocks or values[1] in targets or values[0][1:] != values[1][1:]:
                raise ValueError
            blocks.add(values[0]); targets.add(values[1])
        expected = {tuple(f["source_chain"][k] for k in ("polymer_block", "chain", "segment")) for f in record["plan"]["fragments"]}
        if targets != expected:
            raise ValueError
        protein = Path(files["protein.pdb"])
        if protein.is_symlink() or protein.parent.resolve() != actual.parent.resolve() or protein.read_text(encoding="utf-8") != _protein_text(prepared_lines):
            raise ValueError
        return record, source_lines, prepared_lines
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        raise failure() from None


def verify_prepared_fragments(prepared):
    found = _record_files(prepared)
    if found is None:
        return None
    record, source, actual = found
    state = remap_state_intent(record["source_state_intent"], source, actual, record["plan"])
    if prepared["manifest"]["input_normalization"].get("protein_state_intent", {}) != state:
        raise failure()
    return record


def verify_input_archive(root, manifest, *, mapping=None, reference=None):
    """File-bound library gate; authenticated source provenance is separate."""
    from ..store.paths import read_value
    manifest_path = Path(manifest) if not isinstance(manifest, dict) else None
    manifest = read_value(manifest_path) if manifest_path is not None else manifest
    normalization = manifest.get("input_normalization", {})
    if isinstance(normalization, dict) and "fragment_preparation" not in normalization:
        return None
    try:
        if reference is None:
            if manifest_path is None:
                raise ValueError
            reference = manifest_path.parent / "complex.pdb"
        folder = Path(reference).parent
        files = {name: str(folder / name) for name in ("fragment-source.pdb", "complex.pdb", "protein.pdb")}
        if Path(reference).absolute() != (folder / "complex.pdb").absolute():
            raise ValueError
        record = verify_prepared_fragments({"files": files, "manifest": manifest})
        if not record or not isinstance(mapping, dict):
            raise ValueError
        from .fragment_proof import verify_fragment_archive
        checks = verify_fragment_archive(root, mapping["requested_preparation"],
            Path(files[record["source_file"]]).read_text(encoding="utf-8").splitlines(),
            Path(files[record["prepared_file"]]).read_text(encoding="utf-8").splitlines(),
            record["plan"], mapping, state_intent=record["source_state_intent"])
        return {"required": True, **checks}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        from .terminal_topology import failure as topology_failure
        raise topology_failure() from None


def attach_fragment_states(prepared, source_intent):
    """Keep an original source recipe when base-residue staging lost its name."""
    found = _record_files(prepared)
    if found is None:
        raise failure()
    record, source, actual = found
    blocks = {}
    try:
        for row in record["source_blocks"]:
            key = tuple(row["input"][key] for key in ("polymer_block", "chain", "segment"))
            if key in blocks:
                raise ValueError
            blocks[key] = row["canonical"]
        intent = validate_intent(source_intent)
        addresses = [entry for entry in intent.get("residues", [])] + [entry[side] for entry in intent.get("disulfides", []) for side in ("left", "right")]
        mapping = {}
        for entry in addresses:
            key = identity_key(entry)
            mapping[key] = {**{k: entry[k] for k in ("resid", "icode")}, **blocks[key[:3]]}
        canonical = rebase_state_intent(intent, mapping)
    except (KeyError, TypeError, ValueError):
        raise failure() from None
    state = remap_state_intent(canonical, source, actual, record["plan"])
    record["source_state_intent"] = canonical
    record["fingerprint"] = _fingerprint(record)
    prepared["manifest"]["input_normalization"]["protein_state_intent"] = state
    verify_prepared_fragments(prepared)
