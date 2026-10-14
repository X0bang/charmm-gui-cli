"""Recover declared PDBQT chemistry without docking or distance-based bonds.

Heavy-atom correspondences must be explicit, or a unique isomorphism of an
encoded CONECT graph. Meeko metadata is checked independently of its exporter.
"""

from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import math
from pathlib import Path
import re
import shlex
import sys

from ..store.auth import ToolError
from ..locales.i18n import t
from .ligand_formats import _rdkit, _visible, _signature, _clear_inferred_stereo
from .pdb_normalization import finding

ELEMENTS = {"C": "C", "A": "C", "N": "N", "NA": "N", "NS": "N", "O": "O", "OA": "O", "OS": "O",
            "S": "S", "SA": "S", "P": "P", "F": "F", "Cl": "Cl", "Br": "Br", "I": "I", "H": "H", "HD": "H", "HS": "H"}


def error(key):
    return ToolError(key="input.pdbqt_" + key, next_step=t("input.pdbqt_next"))


def _select(text, pose):
    lines, blocks, shared, current = text.splitlines(keepends=True), [], [], None
    has_models = any(line.startswith('MODEL ') for line in lines)
    if has_models:
        for line in lines:
            if line.startswith('MODEL '):
                if current is not None:
                    raise error('parse')
                current = []
            elif line.startswith('ENDMDL'):
                if current is None:
                    raise error('parse')
                blocks.append(''.join(shared + current))
                current = None
            elif current is not None:
                current.append(line)
            elif not blocks and line.startswith('REMARK '):
                shared.append(line)
            elif line.strip() and not line.startswith(('REMARK ', 'END')):
                raise error('parse')
        if current is not None:
            raise error('parse')
    else:
        blocks = [text]
    inventory = []
    for index, block in enumerate(blocks, 1):
        scores = {}
        for match in re.finditer(r'(?m)^REMARK VINA RESULT:\s*(\S+)', block):
            try:
                score = float(match[1])
                if math.isfinite(score):
                    scores['vina_affinity'] = score
            except ValueError:
                pass
        inventory.append({'index': index, 'name': 'pose-' + str(index), 'scores': scores})
    details = [{'key': 'input.ligand_pose_row', 'params': {'index': row['index'], 'name': row['name'],
                'scores': ', '.join(key + '=' + format(value, 'g') for key, value in row['scores'].items()) or '—'}} for row in inventory[:20]]
    if len(blocks) > 1 and pose is None:
        raise ToolError(key='input.ligand_poses', params={'count': len(blocks)}, details=details,
                        next_step='charmm-gui-cli check --ligand-pose 1 -h')
    if not blocks or pose is not None and (type(pose) is not int or not 1 <= pose <= len(blocks)):
        raise ToolError(key='input.ligand_pose_range', params={'count': len(blocks)}, details=details,
                        next_step='charmm-gui-cli check --ligand-pose 1 -h')
    selected = pose or 1
    block = blocks[selected - 1]
    return block, {'source_format': 'pdbqt', 'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
        'pose_count': len(blocks), 'selected_pose': selected, 'pose_inventory': inventory,
        'selected_record_sha256': hashlib.sha256(block.encode()).hexdigest(), 'explicit_pose_selection': pose is not None}


def _rows(block):
    rows, conect, branches, stack, roots = {}, set(), [], [], 0
    try:
        for line in block.splitlines():
            record = line.split(maxsplit=1)[0] if line.strip() else ''
            if record in ('BEGIN_RES', 'END_RES'):
                raise error('unsupported')
            if record == 'ROOT':
                roots += 1
            elif record in ('BRANCH', 'ENDBRANCH'):
                fields = line.split()
                if len(fields) != 3:
                    raise ValueError
                pair = tuple(map(int, fields[1:]))
                if record == 'BRANCH':
                    branches.append(pair)
                    stack.append(pair)
                elif not stack or stack.pop() != pair:
                    raise ValueError
            elif record in ('ATOM', 'HETATM'):
                serial = int(line[6:11])
                atom_type = line[77:].strip()
                if atom_type not in ELEMENTS:
                    raise error('type')
                xyz = tuple(float(line[start:start + 8]) for start in (30, 38, 46))
                charge = float(line[70:76])
                if serial in rows or not 1 <= serial <= 99999 or not all(math.isfinite(x) for x in (*xyz, charge)) or line[16:17].strip():
                    raise ValueError
                rows[serial] = {'serial': serial, 'name': line[12:16].strip(), 'element': ELEMENTS[atom_type],
                                'type': atom_type, 'partial_charge': charge, 'xyz': xyz}
            elif record == 'CONECT':
                values = [int(line[start:start + 5]) for start in range(6, len(line), 5) if line[start:start + 5].strip()]
                if not values:
                    raise ValueError
                conect.update(tuple(sorted((values[0], other))) for other in values[1:])
        if not rows or len(rows) > 999 or roots > 1 or stack or not math.isfinite(sum(row['partial_charge'] for row in rows.values())):
            raise ValueError
        if any(a == b or a not in rows or b not in rows for a, b in [*conect, *branches]):
            raise ValueError
        return rows, conect, branches
    except (ValueError, IndexError, OverflowError):
        raise error('parse') from None


def _template(smiles, *, keep_h=False):
    Chem, rdBase = _rdkit()
    try:
        if not isinstance(smiles, str) or not 0 < len(smiles) <= 100000:
            raise ValueError
        params = Chem.SmilesParserParams()
        params.removeHs = not keep_h
        params.parseName = False
        params.allowCXSMILES = False
        with rdBase.BlockLogs():
            mol = Chem.MolFromSmiles(smiles, params)
        if mol is None or not 0 < mol.GetNumAtoms() <= 999 or len(Chem.GetMolFrags(mol)) != 1:
            raise ValueError
        allowed = {Chem.BondType.SINGLE, Chem.BondType.DOUBLE, Chem.BondType.TRIPLE, Chem.BondType.AROMATIC}
        if (any(a.GetIsotope() or a.HasQuery() or a.GetNumRadicalElectrons() for a in mol.GetAtoms())
                or any(b.HasQuery() or b.GetBondType() not in allowed for b in mol.GetBonds())
                or len(mol.GetStereoGroups()) or len(Chem.GetMolSubstanceGroups(mol))):
            raise error('semantics')
        return mol
    except (ValueError, RuntimeError, OverflowError):
        raise error('definition') from None


def _chemical_smiles(mol):
    Chem, _ = _rdkit()
    copy = Chem.Mol(mol)
    for atom in copy.GetAtoms():
        atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(Chem.RemoveHs(copy), isomericSmiles=True)


def _proof_signature(mol):
    signature = _signature(mol)
    # A virtual [C@H] H may round-trip as an implicit valence H; this does not
    # add/delete a coordinate atom or change the molecular formula/stereo.
    signature['atoms'] = [(symbol, charge, explicit + implicit, cip)
                          for symbol, charge, explicit, implicit, cip in signature['atoms']]
    return signature


def _pairs(block, name):
    values = []
    try:
        for line in block.splitlines():
            if line.startswith('REMARK ' + name + ' '):
                fields = line[len('REMARK ' + name + ' '):].split()
                if len(fields) % 2:
                    raise ValueError
                values.extend((int(fields[i]), int(fields[i + 1])) for i in range(0, len(fields), 2))
        if any(a < 1 or b < 1 for a, b in values):
            raise ValueError
    except ValueError:
        raise error('mapping') from None
    return values


def _mapped_molecule(template, mapping, rows):
    """Retain coordinate atoms, making only absent ordinary Hs implicit."""
    Chem, rdBase = _rdkit()
    with rdBase.BlockLogs():
        mol = Chem.AddHs(template)
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    conf = Chem.Conformer(mol.GetNumAtoms())
    conf.Set3D(True)
    if set(mapping.values()) != set(rows) or len(mapping) != len(rows):
        raise error('mapping')
    for index, serial in mapping.items():
        if not 0 <= index < mol.GetNumAtoms() or mol.GetAtomWithIdx(index).GetSymbol() != rows[serial]['element']:
            raise error('mapping')
        mol.GetAtomWithIdx(index).SetAtomMapNum(serial)
        conf.SetAtomPosition(index, rows[serial]['xyz'])
    if any(a.GetAtomicNum() != 1 and a.GetIdx() not in mapping for a in mol.GetAtoms()):
        raise error('mapping')
    mol.RemoveAllConformers()
    mol.AddConformer(conf)
    params = Chem.RemoveHsParameters()
    params.removeMapped = False
    with rdBase.BlockLogs():
        mol = Chem.RemoveHs(mol, params)
    correspondence = {a.GetAtomMapNum(): a.GetIdx() for a in mol.GetAtoms()}
    if set(correspondence) != set(rows) or len(correspondence) != len(rows):
        raise error('mapping')
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    return mol, correspondence


def _user_molecule(smiles, rows, conect):
    Chem, rdBase = _rdkit()
    template = _template(smiles, keep_h=True)
    numbers = [a.GetAtomMapNum() for a in template.GetAtoms() if a.GetAtomMapNum()]
    if numbers:
        if len(numbers) != len(set(numbers)):
            raise error('mapping')
        mapping = {a.GetIdx(): a.GetAtomMapNum() for a in template.GetAtoms() if a.GetAtomMapNum()}
        return (*_mapped_molecule(template, mapping, rows), template, 'user_atom_mapped_smiles')
    template = Chem.RemoveHs(template)
    heavy = [serial for serial, row in rows.items() if row['element'] != 'H']
    edges = {(heavy.index(a), heavy.index(b)) for a, b in conect if a in heavy and b in heavy}
    if not edges and len(heavy) > 1:
        raise error('mapping')
    source, graph = Chem.RWMol(), Chem.RWMol()
    for serial in heavy:
        source.AddAtom(Chem.Atom(rows[serial]['element']))
    for atom in template.GetAtoms():
        graph.AddAtom(Chem.Atom(atom.GetAtomicNum()))
    for a, b in edges:
        source.AddBond(a, b, Chem.BondType.SINGLE)
    for bond in template.GetBonds():
        graph.AddBond(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), Chem.BondType.SINGLE)
    if len(heavy) != template.GetNumAtoms() or len(edges) != template.GetNumBonds():
        raise error('mapping')
    matches = source.GetMol().GetSubstructMatches(graph.GetMol(), uniquify=False, maxMatches=2)
    if len(matches) != 1:
        raise error('mapping')
    mapping = {index: heavy[other] for index, other in enumerate(matches[0])}
    expanded = Chem.AddHs(template)
    for serial, row in rows.items():
        if row['element'] != 'H':
            continue
        parents = [b if a == serial else a for a, b in conect if serial in (a, b)]
        if len(parents) != 1 or parents[0] not in mapping.values():
            raise error('mapping')
        parent = next(index for index, value in mapping.items() if value == parents[0])
        choices = [a.GetIdx() for a in expanded.GetAtomWithIdx(parent).GetNeighbors() if a.GetAtomicNum() == 1 and a.GetIdx() not in mapping]
        if not choices:
            raise error('mapping')
        mapping[choices[0]] = serial  # Ordinary equivalent Hs only; heavy mapping is unique.
    return (*_mapped_molecule(template, mapping, rows), template, 'user_smiles_unique_conect')


def _meeko_molecule(block, smiles, rows):
    Chem, rdBase = _rdkit()
    template = _template(smiles)
    pairs, parents = _pairs(block, 'SMILES IDX'), _pairs(block, 'H PARENT')
    heavy = {serial for serial, row in rows.items() if row['element'] != 'H'}
    if (len(pairs) != template.GetNumAtoms() or {a for a, _ in pairs} != set(range(1, template.GetNumAtoms() + 1))
            or {serial for _, serial in pairs} != heavy or len({serial for _, serial in pairs}) != len(pairs)
            or len({serial for _, serial in parents}) != len(parents)
            or {serial for _, serial in parents} != set(rows) - heavy):
        raise error('mapping')
    correspondence = {serial: index - 1 for index, serial in pairs}
    for serial, index in correspondence.items():
        if template.GetAtomWithIdx(index).GetSymbol() != rows[serial]['element']:
            raise error('mapping')
    counts = Counter(parent for parent, _ in parents)
    if any(not 1 <= parent <= template.GetNumAtoms() or count > template.GetAtomWithIdx(parent - 1).GetTotalNumHs()
           for parent, count in counts.items()):
        raise error('mapping')
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), rdBase.BlockLogs():
            import meeko
            if not re.fullmatch(r'0\.8\.[0-9]+', meeko.__version__):
                raise ToolError(key='input.pdbqt_dependency', next_step=t('input.pdbqt_install', python=shlex.quote(sys.executable)))
            from meeko import PDBQTMolecule, RDKitMolCreate
            molecules = RDKitMolCreate.from_pdbqt_mol(PDBQTMolecule(block, skip_typing=True))
        if len(molecules) != 1 or molecules[0] is None:
            raise ValueError
        mol = molecules[0]
        if mol.GetNumConformers() != 1 or _chemical_smiles(mol) != _chemical_smiles(template):
            raise ValueError
        conf = mol.GetConformer()
        for parent, serial in parents:
            choices = [a.GetIdx() for a in mol.GetAtomWithIdx(parent - 1).GetNeighbors() if a.GetAtomicNum() == 1
                and a.GetIdx() not in correspondence.values()
                and max(abs(x - y) for x, y in zip(conf.GetAtomPosition(a.GetIdx()), rows[serial]['xyz'])) < .001]
            if len(choices) != 1:
                raise ValueError
            correspondence[serial] = choices[0]
        for serial, index in correspondence.items():
            if max(abs(x - y) for x, y in zip(conf.GetAtomPosition(index), rows[serial]['xyz'])) >= .001:
                raise ValueError
            conf.SetAtomPosition(index, rows[serial]['xyz'])
        # The exporter may generate absent H coordinates. Rebuild from the
        # declared graph and retain only independently mapped input atoms.
        # Removing Hs by index alone can turn explicit [C@H] into a radical.
        expanded = Chem.AddHs(template)
        mapping = {index - 1: serial for index, serial in pairs}
        for parent, serial in parents:
            choices = [a.GetIdx() for a in expanded.GetAtomWithIdx(parent - 1).GetNeighbors()
                       if a.GetAtomicNum() == 1 and a.GetIdx() not in mapping]
            if not choices:
                raise ValueError
            mapping[choices[0]] = serial
        restored, correspondence = _mapped_molecule(template, mapping, rows)
        return restored, correspondence, template, 'meeko_embedded_smiles', meeko.__version__
    except ImportError:
        raise ToolError(key='input.pdbqt_dependency', next_step=t('input.pdbqt_install', python=shlex.quote(sys.executable))) from None
    except ToolError:
        raise
    except Exception:
        raise error('mapping') from None


def ligand_text(path, *, pose=None, smiles=None, report=None):
    Chem, rdBase = _rdkit()
    try:
        text = Path(path).read_bytes().decode('utf-8')
        if len(text) > 32 * 1024 * 1024:
            raise error('size')
        block, evidence = _select(text, pose)
        definitions = re.findall(r'(?m)^REMARK SMILES (?!IDX\b)(\S+)\s*$', block)
        if len(definitions) > 1:
            raise error('definition')
        if not definitions and not smiles:
            raise error('definition')
        rows, conect, branches = _rows(block)
        if definitions:
            mol, correspondence, template, source, version = _meeko_molecule(block, definitions[0], rows)
            if smiles and _chemical_smiles(_template(smiles)) != _chemical_smiles(template):
                raise error('conflict')
        else:
            mol, correspondence, template, source = _user_molecule(smiles, rows, conect)
            version = rdBase.rdkitVersion
        if _chemical_smiles(mol) != _chemical_smiles(template):
            raise error('roundtrip')
        for atom in mol.GetAtoms():
            if atom.GetAtomicNum() == 1:
                neighbors = list(atom.GetNeighbors())
                if len(neighbors) != 1:
                    raise error('mapping')
                conf = mol.GetConformer()
                if not .5 <= math.dist(conf.GetAtomPosition(atom.GetIdx()), conf.GetAtomPosition(neighbors[0].GetIdx())) <= 1.5:
                    raise error('mapping')
        for a, b in [*conect, *branches]:
            bond = mol.GetBondBetweenAtoms(correspondence[a], correspondence[b])
            if bond is None or (a, b) in branches and bond.GetBondType() != Chem.BondType.SINGLE:
                raise error('mapping')
        for serial, index in correspondence.items():
            if rows[serial]['type'] in ('C', 'A') and (rows[serial]['type'] == 'A') != mol.GetAtomWithIdx(index).GetIsAromatic():
                raise error('mapping')
        counts, names = Counter(), [''] * mol.GetNumAtoms()
        explicit = len({row['name'] for row in rows.values()}) == len(rows) and all(re.fullmatch(r'[A-Za-z0-9]{1,4}', row['name']) for row in rows.values())
        for serial, index in sorted(correspondence.items(), key=lambda pair: pair[1]):
            row = rows[serial]
            counts[row['element']] += 1
            names[index] = row['name'] if explicit else row['element'].upper() + str(counts[row['element']])
        if mol.GetNumAtoms() > 999 or mol.GetNumBonds() > 999 or any(len(name) > 4 for name in names):
            raise error('size')
        declared_atoms = {a.GetIdx() for a in mol.GetAtoms() if a.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED}
        declared_bonds = {b.GetIdx() for b in mol.GetBonds() if b.GetStereo() != Chem.BondStereo.STEREONONE}
        geometry = Chem.Mol(mol)
        Chem.RemoveStereochemistry(geometry)
        Chem.AssignStereochemistryFrom3D(geometry, replaceExistingTags=True)
        for index in declared_atoms:
            expected, observed = mol.GetAtomWithIdx(index), geometry.GetAtomWithIdx(index)
            if not expected.HasProp('_CIPCode') or not observed.HasProp('_CIPCode') or expected.GetProp('_CIPCode') != observed.GetProp('_CIPCode'):
                serial = next(serial for serial, target in correspondence.items() if target == index)
                raise ToolError(key='input.pdbqt_stereo_geometry', params={'serial': serial}, next_step=t('input.pdbqt_stereo_next'))
        if any(mol.GetBondWithIdx(index).GetStereo() != geometry.GetBondWithIdx(index).GetStereo() for index in declared_bonds):
            raise error('roundtrip')
        before = _proof_signature(mol)
        output = Chem.MolToMolBlock(mol, includeStereo=True, kekulize=False, forceV3000=False)
        with rdBase.BlockLogs():
            checked = Chem.MolFromMolBlock(output, removeHs=False, strictParsing=True)
        if checked is None:
            raise error('roundtrip')
        _clear_inferred_stereo(checked, declared_atoms, declared_bonds)
        after = _proof_signature(checked)
        for index, (original, restored) in enumerate(zip(before['atoms'], after['atoms'])):
            if original[-1] and original[-1] != restored[-1]:
                serial = next(serial for serial, target in correspondence.items() if target == index)
                raise ToolError(key='input.pdbqt_stereo_geometry', params={'serial': serial}, next_step=t('input.pdbqt_stereo_next'))
        if after != before:
            raise error('roundtrip')
        delta = max(abs(x - y) for serial, index in correspondence.items()
                    for x, y in zip(checked.GetConformer().GetAtomPosition(index), rows[serial]['xyz']))
        if delta > .000051:
            raise error('roundtrip')
        from .ligand_stereo import annotation, declared
        output += '\n> <PDB_ATOM_NAMES>\n' + ' '.join(names) + '\n' + annotation(mol) + '$$$$\n'
        declared(output, checked)
        conversion = {'input_format': 'pdbqt', 'output_format': 'sdf_v2000', 'backend': 'Meeko' if definitions else 'RDKit',
            'backend_version': version, 'chemical_definition': source, 'explicit_atom_names': explicit,
            'coordinates_transformed': False, 'maximum_coordinate_roundoff_A': delta,
            'graph_and_stereo_roundtrip_verified': True, 'input_atoms_retained': True, 'added_coordinate_atoms': 0,
            'input_partial_charges': [rows[serial]['partial_charge'] for serial in rows],
            'partial_charge_sum': sum(row['partial_charge'] for row in rows.values()),
            'net_formal_charge': sum(a.GetFormalCharge() for a in mol.GetAtoms()), 'review_required': True,
            'implicit_valence_hydrogens': sum(a.GetNumImplicitHs() + a.GetNumExplicitHs() for a in mol.GetAtoms()),
            'atom_name_mapping': [{'index': index + 1, 'source_serial': serial, 'source_name': _visible(rows[serial]['name']),
                'pdb_atom_name': names[index], 'autodock_type': rows[serial]['type']} for serial, index in correspondence.items()]}
        evidence.update(conversion=conversion, explicit_atom_names=explicit)
        repairs = []
        if evidence['pose_count'] > 1 or pose is not None:
            repairs.append(finding('input.ligand_pose_selected', index=evidence['selected_pose'], count=evidence['pose_count'], name='pose-' + str(evidence['selected_pose'])))
        repairs.append(finding('input.pdbqt_restored', count=len(rows)))
        evidence['repairs'] = repairs
        if report is not None:
            report.update(evidence)
        return output, evidence
    except ToolError:
        raise
    except (ValueError, RuntimeError, IndexError, KeyError, OverflowError, UnicodeError):
        raise error('parse') from None
