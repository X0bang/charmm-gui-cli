"""Read-only semantic settings; these views never replace submission evidence."""

import hashlib
import json
import math
from pathlib import Path
import re

from .auth import ToolError
from .paths import decode
from ..chem.pdb_normalization import N_TERMINALS, C_TERMINALS


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    try:
        number = float(value) if not isinstance(value, bool) else float('nan')
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _mixture(value):
    if not isinstance(value, str) or len(value) > 4096 or value.count('=') != 1:
        return []
    names, ratios = (part.split(':') for part in value.split('='))
    if (len(names) != len(ratios) or len(set(names)) != len(names)
            or any(not re.fullmatch(r'[A-Za-z0-9_+-]{1,16}', name) for name in names)):
        return []
    parts = [_number(number) for number in ratios]
    if any(part is None or part <= 0 for part in parts) or not math.isfinite(sum(parts)):
        return []
    total = sum(parts)
    return [{'lipid': name, 'parts': part, 'fraction': part / total} for name, part in zip(names, parts)]


def _settings(config):
    membrane, ions = _dict(config.get('membrane')), _dict(config.get('ions'))
    orientation = _dict(config.get('preparation')).get('orientation')
    return {'membrane': {'upper': _mixture(membrane.get('upper')), 'lower': _mixture(membrane.get('lower')),
                         'margin_A': _number(membrane.get('margin_A')), 'water_padding_A': _number(membrane.get('water_padding_A'))},
            'ions': {'type': ions.get('type') if ions.get('type') in ('KCl', 'NaCl') else None,
                     'concentration_M': _number(ions.get('concentration_M'))},
            'orientation': orientation if orientation in ('ppm', 'prepared') else None}


def _patch(value, end):
    return value if isinstance(value, str) and value in (N_TERMINALS if end == 'n_terminal' else C_TERMINALS) else None


def _requested(preparation, chain, end):
    chains = _dict(preparation.get('terminals'))
    matches = [name for name in {chain, chain[-1:]} if name in chains]
    if len(matches) > 1:
        return None
    override = _dict(chains.get(matches[0])) if matches else {}
    return _patch(override.get(end, preparation.get(end)), end)


def _record(path):
    """Read one bounded regular record, returning its original-byte digest."""
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return {}, 'invalid_record', None
        if not path.exists():
            return {}, 'missing', None
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            return {}, 'invalid_record', None
        data = path.read_bytes()
        if len(data) > 8 * 1024 * 1024:
            return {}, 'invalid_record', None
        value = decode(path, json.loads(data))
        if not isinstance(value, dict):
            return {}, 'invalid_record', None
        return value, 'read', hashlib.sha256(data).hexdigest()
    except (OSError, ValueError, ToolError):
        return {}, 'invalid_record', None


def _terminal_record(directory, manifest):
    if directory is not None:
        record, status, _ = _record(directory / 'preparation/parameterization.intent.json')
        if status != 'missing':
            state = record.get('state')
            fields = record.get('fields', [])
            identifiers = [pair[1] for pair in fields if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == 'jobid'] if isinstance(fields, list) else []
            identifier = manifest.get('source_job_id')
            if state == 'response_received' and (not isinstance(identifier, str) or not re.fullmatch(r'[0-9]{1,32}', identifier)
                    or not identifiers or any(value != identifier for value in identifiers)):
                return fields, 'invalid_record', 'local_intent'
            stored = _dict(manifest.get('applied_terminals'))
            if state == 'response_received' and isinstance(record.get('fields'), list) and any(
                    isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[0], str)
                    and pair[0] in stored and pair[1] != stored[pair[0]] for pair in record['fields']):
                return record['fields'], 'conflicting_record', 'local_intent'
            return record.get('fields', []), ('response_received' if state == 'response_received' else
                'submission_unknown' if state in ('submitting', 'submission_unknown') else 'invalid_record'), 'local_intent'
    # Current children retain their fields independently of a mounted source.
    fields = manifest.get('applied_terminals')
    certificate = _dict(_dict(manifest.get('reused_preparation')).get('certificate'))
    if isinstance(fields, dict) and fields:
        return list(fields.items()), 'recorded', 'manifest_record'
    if certificate:
        source = certificate.get('source_directory')
        expected = _dict(certificate.get('evidence_sha256')).get('preparation/parameterization.intent.json')
        if isinstance(source, str) and isinstance(expected, str):
            record, status, digest = _record(Path(source) / 'preparation/parameterization.intent.json')
            if status == 'read' and digest == expected and record.get('state') == 'response_received':
                fields = record.get('fields', [])
                identifiers = [pair[1] for pair in fields if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == 'jobid'] if isinstance(fields, list) else []
                if identifiers and all(value == certificate.get('source_job_id') for value in identifiers):
                    return fields, 'response_received', 'source_certificate'
                return fields, 'invalid_record', 'source_certificate'
        return [], 'unavailable', 'source_certificate'
    adjustments = manifest.get('terminal_adjustments', [])
    fields = []
    if isinstance(adjustments, list):
        for adjustment in adjustments:
            params = _dict(_dict(adjustment).get('params'))
            chain, patch = params.get('chain'), params.get('patch')
            if isinstance(chain, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,8}', chain) and _patch(patch, 'n_terminal'):
                fields.append(('terminal[' + chain + '][first]', patch))
    return fields, 'adjustment_record' if fields else 'missing', 'adjustment_record' if fields else 'none'


def _terminals(directory, manifest, preparation):
    fields, status, source = _terminal_record(directory, manifest)
    values, conflicts, invalid = {}, set(), False
    if not isinstance(fields, list):
        fields, invalid = [], True
    for entry in fields:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2 or not isinstance(entry[0], str):
            invalid = True
            continue
        match = re.fullmatch(r'terminal\[([A-Za-z0-9_-]{1,8})\]\[(first|last)\]', entry[0])
        if not match:
            if entry[0].startswith('terminal['):
                invalid = True
            continue
        chain, end = match[1], 'n_terminal' if match[2] == 'first' else 'c_terminal'
        value = _patch(entry[1], end)
        key = (chain, end)
        if value is None:
            invalid = True
        if key in values and values[key] != value:
            conflicts.add(key)
        values[key] = value
    fragment_rows = None
    inputs = _dict(manifest.get('inputs'))
    normalization = _dict(_dict(inputs.get('manifest')).get('input_normalization'))
    if 'fragment_preparation' in normalization:
        fragment_rows = {}
        try:
            from ..chem.fragment_inputs import verify_prepared_fragments
            from ..chem.fragment_proof import _binding
            record = verify_prepared_fragments(inputs)
            files = inputs['files']
            source_lines = Path(files[record['source_file']]).read_text(encoding='utf-8').splitlines()
            prepared_lines = Path(files[record['prepared_file']]).read_text(encoding='utf-8').splitlines()
            bindings = _binding(preparation, source_lines, prepared_lines, record['plan'], manifest['fragment_mapping'])
            for binding in bindings:
                chain = binding['server_chain']
                fragment_rows[chain] = binding
                for end, entry in binding['terminals'].items():
                    if values.get((chain, end)) != entry['form_value']:
                        invalid = True
            if set(fragment_rows) != {chain for chain, _ in values}:
                invalid = True
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, ToolError):
            fragment_rows, invalid = {}, True
    rows = []
    for chain in sorted({chain for chain, _ in values}):
        row = {'chain': chain}
        fragment = fragment_rows.get(chain) if fragment_rows is not None else None
        for end in ('n_terminal', 'c_terminal'):
            row[end] = None if (chain, end) in conflicts else values.get((chain, end))
            row['requested_' + end] = (fragment['terminals'][end]['value'] if fragment is not None else
                                       None if fragment_rows is not None else _requested(preparation, chain, end))
        if fragment is not None:
            row.update(fragment_index=fragment['fragment_index'], prepared_chain=fragment['prepared_chain'],
                       source_chain=fragment['source_chain'],
                       requested_origins={end: fragment['terminals'][end]['origin'] for end in ('n_terminal', 'c_terminal')})
        rows.append(row)
    return {'status': 'conflicting_record' if conflicts else 'invalid_record' if invalid else status,
            'source': source, 'chains': rows, 'conflict_count': len(conflicts)}


def _membrane(directory, manifest):
    if directory is None:
        return {'status': 'missing', 'source': 'none', 'settings': None}
    record, status, _ = _record(directory / 'bilayer/run.json')
    if status != 'read':
        return {'status': status, 'source': 'none', 'settings': None}
    request = _dict(_dict(record.get('plan')).get('request'))
    form = _dict(request.get('form'))
    if request.get('method') != 'POST' or request.get('endpoint') != '/quick_bilayer' or not form:
        return {'status': 'unavailable', 'source': 'api_record', 'settings': None}
    settings = _settings({'membrane': {'upper': form.get('upper'), 'lower': form.get('lower'),
        'margin_A': form.get('margin'), 'water_padding_A': form.get('wdist')},
        'ions': {'type': form.get('ion_type'), 'concentration_M': form.get('ion_conc')},
        'preparation': {'orientation': 'ppm' if form.get('ppm') == 'true' else 'prepared' if 'ppm' not in form else None}})
    source = manifest.get('source_job_id')
    identifier = manifest.get('build_job_id')
    if (not isinstance(source, str) or not re.fullmatch(r'[0-9]{1,32}', source) or form.get('jobid') != source
            or _dict(record.get('config')).get('source_job_id') != source):
        state = 'invalid_record'
    elif record.get('state') in ('submitting', 'submission_unknown'):
        state = 'submission_unknown'
    elif isinstance(identifier, str) and re.fullmatch(r'[0-9]{1,32}', identifier) and identifier == record.get('job_id'):
        state = 'response_received'
    else:
        state = 'recorded'
    if all(key in _dict(record.get('config')) for key in ('membrane', 'ions', 'preparation')) and settings != _settings(record['config']):
        state = 'conflicting_record'
    return {'status': state, 'source': 'api_plan', 'settings': settings}


def parameter_summary(manifest, directory=None):
    """Whitelist settings only; JSON keys/enums are locale independent."""
    config = _dict(manifest.get('config'))
    preparation = _dict(config.get('preparation'))
    requested = _settings(config)
    requested['terminals'] = {'defaults': {end: _patch(preparation.get(end), end) for end in ('n_terminal', 'c_terminal')},
        'chains': [{'chain': chain, **{end: _patch(_dict(values).get(end, preparation.get(end)), end) for end in ('n_terminal', 'c_terminal')}}
                   for chain, values in sorted((key, value) for key, value in _dict(preparation.get('terminals')).items()
                                              if isinstance(key, str) and re.fullmatch(r'(?:[A-Za-z0-9_-]{1,8}|fragment:[A-Za-z0-9])', key))]}
    directory = Path(directory) if directory is not None else None
    return {'schema_version': 1, 'requested': requested,
            'submission': {'preparation': _terminals(directory, manifest, preparation), 'membrane': _membrane(directory, manifest)}}
