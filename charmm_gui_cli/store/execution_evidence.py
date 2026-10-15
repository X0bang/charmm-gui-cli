"""Bounded read-only submission observations; never reconcile or retire intents."""

from pathlib import Path
import re

from .json_records import read_object


def _legacy_build_record(directory):
    """Early flat runs retained 'submitting' after saving successful replies.

    Recognize only already-recorded downstream lineage, not filenames alone.
    This is a historical observation, never a receipt rewrite or authorization
    to send a POST. Explicit unknown/unreadable records are never exempted.
    """
    if directory.name == '.charmm-gui':
        return False
    try:
        full, _ = read_object(directory / 'full-run.json')
        remote, _ = read_object(directory / 'bilayer/run.json')
        source, build = full['source_job_id'], full['build_job_id']
        if (any(not isinstance(v,str) or not re.fullmatch(r'[0-9]{1,32}',v) for v in (source,build))
                or source == build or remote.get('job_id') != build
                or remote.get('config', {}).get('source_job_id') != source
                or full.get('preparation_page_state') != 'ready'
                or full.get('state') not in ('bilayer_submitted','running','done','downloaded_unverified','ligands_present','validated','validation_failed','remote_error')
                or remote.get('state') not in ('submitted','pending','running','done','archive_preparing','downloading','downloaded_unverified','ligands_present','validated','validation_failed','remote_error')):
            return False
        snapshot = Path(full['preparation_snapshot'])
        return snapshot.is_file() and not snapshot.is_symlink() and snapshot.resolve().is_relative_to((directory/'preparation').resolve())
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        return False


def submission_evidence(directory):
    directory = Path(directory)
    result = {'pending': False, 'unknown': False, 'unreadable': False, 'records': [], 'historical_receipts': []}
    preparation = directory / 'preparation'
    bilayer = directory / 'bilayer'
    if preparation.is_symlink() or bilayer.is_symlink():
        result.update(unknown=True, unreadable=True)
        result['records'].append({'path': str(preparation if preparation.is_symlink() else bilayer), 'state': 'unreadable'})
        return result
    paths = [preparation / 'web-run.json', bilayer / 'run.json']
    paths.extend(sorted(preparation.glob('*.intent.json')))
    if len(paths) > 258:
        result.update(unknown=True, unreadable=True)
        result['records'].append({'path': str(preparation), 'state': 'unreadable'})
        return result
    legacy = _legacy_build_record(directory)
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            record, _ = read_object(path)
            state = record.get('state')
            if not isinstance(state, str):
                raise ValueError
        except (OSError, ValueError):
            result.update(unknown=True, unreadable=True)
            result['records'].append({'path': str(path), 'state': 'unreadable'})
            continue
        if state in ('submitting', 'upload_submitting', 'submission_unknown'):
            if legacy and state == 'submitting' and path.name in ('selection.intent.json','parameterization.intent.json'):
                result['historical_receipts'].append({'path':str(path),'state':state,'basis':'matched_recorded_build_lineage'})
                continue
            result['pending'] = True
            result['unknown'] |= state == 'submission_unknown'
            result['records'].append({'path': str(path), 'state': state})
    return result
