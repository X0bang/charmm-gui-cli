"""Explicit, recoverable local index removal. Never touches task data or HTTP."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shlex
import uuid

from . import tasks
from .auth import ToolError, atomic_json
from .discovery import matches, task_id
from .layout import task_root
from .locking import now, run_lock
from .paths import origin
from .workers import claim, metadata


def _error(key, directory, **params):
    return ToolError(key='cleanup.' + key, params=params,
                     next_step=shlex.join(['charmm-gui-cli', 'jobs', 'status', str(task_root(directory))]))


def _registries():
    return sorted({Path(path).expanduser().resolve() for path in tasks._registries()})


def _record(path, *, limit=1024 * 1024):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError
    return value, hashlib.sha256(raw).hexdigest()


def _safe_to_forget(directory, *, check_process=True):
    if check_process and metadata(directory)['running']:
        raise _error('running', directory)
    full = tasks._read_json(directory / 'full-run.json')
    if full.get('schema_version') != 2:
        raise _error('not_task', directory, path=str(task_root(directory)))
    intents = [directory / 'preparation/web-run.json', *directory.rglob('*.intent.json')]
    try:
        if any((directory / folder).is_symlink() for folder in ('preparation', 'bilayer')):
            raise ValueError
        unknown = (full.get('state') in ('submitting', 'submission_unknown', 'upload_submitting')
                   or full.get('execution_state') == 'submission_unknown')
        for path in intents:
            if path.exists() or path.is_symlink():
                value, _ = _record(path)
                unknown |= value.get('state') in ('submitting', 'submission_unknown', 'upload_submitting')
        remote = tasks._read_json(directory / 'bilayer/run.json') if (directory / 'bilayer/run.json').exists() else {}
        unknown |= remote.get('state') in ('submitting', 'submission_unknown')
        if full.get('build_job_id') and remote.get('job_id') and full['build_job_id'] != remote['job_id']:
            unknown = True
        recovery = directory / 'recovery.json'
        if recovery.exists() or recovery.is_symlink():
            saved, _ = _record(recovery)
            if saved.get('active') is True:
                error = saved.get('error', {})
                if not isinstance(error, dict):
                    raise ValueError
                unknown |= error.get('category') == 'submission_unknown'
    except (OSError, ValueError, ToolError):
        unknown = True
        remote = {}
    if unknown:
        raise _error('uncertain', directory)
    finished = {'done', 'downloaded_unverified', 'ligands_present', 'validation_failed', 'validated',
                'complete', 'completed', 'remote_error', 'preparation_failed'}
    if (remote.get('state') in ('submitted', 'pending', 'running', 'archive_preparing', 'downloading')
            or (full.get('build_job_id') or remote.get('job_id'))
            and full.get('state') not in finished and remote.get('state') not in finished):
        raise _error('remote_active', directory)
    return full


def _entries(directory, identifier, name):
    entries = []
    old_root = origin(task_root(directory))
    aliases = {str(directory), str(old_root / '.charmm-gui') if directory.name == '.charmm-gui' else str(directory)}
    for registry in _registries():
        folders = [registry / 'names', registry / 'run-history']
        if any(path.is_symlink() for path in folders):
            raise _error('unsafe_index', directory, path=str(registry))
        candidates = [*folders[0].glob('*.json'), *folders[1].glob('*.json')]
        if (registry / 'latest-run.json').exists():
            candidates.append(registry / 'latest-run.json')
        if len(candidates) > 10000:
            raise _error('index_limit', directory, path=str(registry))
        for path in sorted(candidates):
            try:
                record, digest = _record(path)
            except (OSError, ValueError):
                if path == registry / 'names' / (name + '.json'):
                    raise _error('unsafe_index', directory, path=str(path)) from None
                continue
            matching = record.get('task_id') == identifier if record.get('task_id') is not None else record.get('directory') in aliases
            if matching and (identifier is not None or record.get('task_id') is None):
                entries.append({'registry': registry, 'path': path, 'sha256': digest,
                                'relative': path.relative_to(registry), 'bytes': path.stat().st_size})
    return entries


def forget(target, *, yes=False):
    """Default is a read-only plan; --yes moves only matching index records."""
    directory = tasks.resolve_run(target).resolve()
    _safe_to_forget(directory)
    identifier = task_id(directory)
    embedded = tasks._job_metadata(directory)
    embedded_name = embedded.get('name')
    name = embedded_name or str(task_root(directory))
    entries = _entries(directory, identifier, name)
    result = {'state': 'forget_plan', 'name': name, 'directory': str(task_root(directory)), 'task_id': identifier,
              'index_records': len(entries), 'index_bytes': sum(item['bytes'] for item in entries),
              'index_files': [str(item['path']) for item in entries],
              'task_data_preserved': True, 'remote_requests': 0,
              'next_step': shlex.join(['charmm-gui-cli', 'jobs', 'forget', str(target), '--yes'])}
    if not yes:
        return result
    fd = claim(directory)
    try:
        with ExitStack() as stack:
            # All cooperating name/history writers use the same names lock.
            for registry in _registries():
                names = registry / 'names'
                if names.is_symlink():
                    raise _error('unsafe_index', directory, path=str(names))
                names.mkdir(mode=0o700, parents=True, exist_ok=True)
                stack.enter_context(run_lock(names))
            stack.enter_context(run_lock(directory))
            # Own the lease now; inspect submission state without mistaking our
            # own flock for an active model worker.
            _safe_to_forget(directory, check_process=False)
            if task_id(directory) != identifier:
                raise _error('not_task', directory, path=str(task_root(directory)))
            entries = _entries(directory, identifier, name)
            receipts, moved = {}, []
            event = uuid.uuid4().hex
            current_path = directory
            try:
                for item in entries:
                    registry = item['registry']
                    if registry not in receipts:
                        parent = registry / 'forgotten'
                        current_path = parent
                        if parent.is_symlink():
                            raise _error('unsafe_index', directory, path=str(parent))
                        backup = parent / event
                        current_path = backup
                        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
                        receipt = {'version': 1, 'state': 'moving', 'at': now(), 'name': name, 'task_id': identifier,
                                   'directory': str(directory), 'files': [
                                       {'path': str(row['relative']), 'sha256': row['sha256']}
                                       for row in entries if row['registry'] == registry], 'moved_files': []}
                        receipts[registry] = (backup, receipt)
                        current_path = backup / 'forget.json'
                        atomic_json(backup / 'forget.json', receipt)
                    backup, receipt = receipts[registry]
                    current_path = item['path']
                    try:
                        unchanged = _record(item['path'])[1] == item['sha256']
                    except (OSError, ValueError):
                        unchanged = False
                    if not unchanged:
                        raise _error('index_changed', directory)
                    destination = backup / item['relative']
                    current_path = destination.parent
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    current_path = destination
                    item['path'].rename(destination)
                    moved.append((destination, item['path']))
                    receipt['moved_files'].append(str(item['relative']))
                    current_path = backup / 'forget.json'
                    atomic_json(backup / 'forget.json', receipt)
                for backup, receipt in receipts.values():
                    receipt['state'] = 'forgotten'
                    current_path = backup / 'forget.json'
                    atomic_json(backup / 'forget.json', receipt)
            except (OSError, ToolError, KeyboardInterrupt) as failure:
                partial = False
                for backup_file, original in reversed(moved):
                    try:
                        if original.exists() or original.is_symlink():
                            raise OSError
                        # Exclusive publication cannot overwrite a record that
                        # appeared meanwhile. Retain the backup for diagnostics.
                        os.link(backup_file, original, follow_symlinks=False)
                    except OSError:
                        partial = True
                for backup, receipt in receipts.values():
                    receipt['state'] = 'partial' if partial else 'rolled_back'
                    try:
                        atomic_json(backup / 'forget.json', receipt)
                    except OSError:
                        pass
                if isinstance(failure, KeyboardInterrupt) and not partial:
                    raise
                diagnostic = _error('partial' if partial else 'rolled_back', directory)
                diagnostic.details.append({'key': 'cleanup.failed_at', 'params': {'path': str(current_path)}})
                for backup, _ in receipts.values():
                    diagnostic.details.append({'key': 'cleanup.backup', 'params': {'path': str(backup)}})
                if isinstance(failure, ToolError) and failure.key and failure.key.startswith('cleanup.'):
                    diagnostic.details.append({'key': failure.key, 'params': failure.params})
                diagnostic.next_step = shlex.join(['ls', '-ld', str(current_path.parent)])
                raise diagnostic from None
    finally:
        os.close(fd)
    result.update(state='forgotten', index_records=len(entries), index_bytes=sum(item['bytes'] for item in entries),
                  index_files=[str(item['path']) for item in entries],
                  backup_directories=[str(backup) for backup, _ in receipts.values()],
                  next_step=shlex.join(['charmm-gui-cli', 'jobs', 'relink', name, str(task_root(directory))]
                                      if embedded_name else ['charmm-gui-cli', 'jobs', 'remember', str(task_root(directory))]))
    return result


def remember(target):
    """Restore local discovery only, never resume a modeling pipeline."""
    path = Path(target).expanduser().resolve()
    directory = path / '.charmm-gui' if (path / '.charmm-gui/full-run.json').is_file() else path
    if metadata(directory)['running']:
        raise _error('running', directory)
    name = tasks._job_metadata(directory).get('name')
    if name:
        from .discovery import relink
        result = relink(name, directory)
    else:
        record = tasks.remember_run(directory)
        result = {'name': None, 'directory': str(task_root(directory)), 'task_id': record.get('task_id'),
                  'next_step': shlex.join(['charmm-gui-cli', 'jobs', 'status', str(task_root(directory))])}
    return {**result, 'state': 'remembered', 'remote_requests': 0, 'task_data_preserved': True}


def forgotten_binding(name, directory):
    """Recover a forgotten name only from a receipt and its unchanged backup."""
    found, damaged = [], []
    for registry in _registries():
        parent = registry / 'forgotten'
        if parent.is_symlink():
            continue
        receipts = list(parent.glob('*/forget.json'))
        if len(receipts) > 10000:
            raise _error('index_limit', directory, path=str(parent))
        for path in receipts:
            try:
                if path.parent.is_symlink() or not path.resolve().is_relative_to(parent):
                    continue
                receipt, _ = _record(path, limit=8 * 1024 * 1024)
                if receipt.get('version') != 1 or receipt.get('state') not in ('moving', 'forgotten', 'partial'):
                    continue
                relative = 'names/' + name + '.json'
                expected = [item['sha256'] for item in receipt['files'] if item.get('path') == relative]
                backup = path.parent / relative
                if backup.parent.is_symlink() or not backup.resolve().is_relative_to(path.parent):
                    continue
                record, digest = _record(backup)
                if len(expected) == 1 and record.get('name') == name and matches(record, directory):
                    if digest == expected[0]:
                        found.append(record)
                    else:
                        damaged.append(backup)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
    if not found and damaged:
        raise _error('backup_changed', directory, path=str(damaged[0]))
    return found[-1] if found else None
