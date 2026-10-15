"""Explicit, recoverable local index removal. Never touches task data or HTTP."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
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
    path = Path(path)
    # Inspect the opened inode, not a prior pathname stat. NONBLOCK also keeps
    # an unexpected FIFO/device in an index from hanging ordinary queries.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or path.is_symlink():
            raise ValueError
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError
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


def _entries(directory, identifier, name, *, additional_aliases=()):
    entries = []
    old_root = origin(task_root(directory))
    aliases = {str(directory), str(old_root / '.charmm-gui') if directory.name == '.charmm-gui' else str(directory), *additional_aliases}
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
            matching = (record.get('task_id') == identifier if record.get('task_id') is not None
                        else isinstance(record.get('directory'), str) and record['directory'] in aliases)
            if matching and (identifier is not None or record.get('task_id') is None):
                entries.append({'registry': registry, 'path': path, 'sha256': digest,
                                'relative': path.relative_to(registry), 'bytes': path.stat().st_size})
    return entries


def _missing_context(target):
    """Resolve only an explicit missing index binding, never infer remote state."""
    token = str(target)
    supplied = Path(token).expanduser()
    explicit = supplied.is_absolute() or '/' in token or token.startswith('~')
    candidates = []
    if not explicit:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', token):
            raise ToolError(key='input.task_name', next_step='charmm-gui-cli jobs forget -h')
        for registry in tasks._registries():
            path = Path(registry) / 'names' / (token + '.json')
            if path.exists() or path.is_symlink():
                try:
                    record, digest = _record(path)
                except (OSError, ValueError):
                    raise _error('unsafe_index', path.parent, path=str(path)) from None
                if record.get('name') != token:
                    raise ToolError(key='cleanup.missing_not_indexed', next_step='charmm-gui-cli jobs list')
                candidates = [(record, digest)]
                break
    else:
        roots = {supplied.resolve(), supplied.resolve() / '.charmm-gui'}
        for registry in _registries():
            folders = [registry / 'names', registry / 'run-history']
            if any(folder.is_symlink() for folder in folders):
                raise _error('unsafe_index', supplied, path=str(registry))
            paths = [*folders[0].glob('*.json'), *folders[1].glob('*.json')]
            if (registry / 'latest-run.json').exists():
                paths.append(registry / 'latest-run.json')
            if len(paths) > 10000:
                raise _error('index_limit', supplied, path=str(registry))
            for path in sorted(paths):
                try:
                    record, digest = _record(path)
                    if isinstance(record.get('directory'), str) and Path(record['directory']).is_absolute() and Path(record['directory']).resolve() in roots:
                        candidates.append((record, digest))
                except (OSError, ValueError):
                    continue
    if not candidates:
        raise ToolError(key='cleanup.missing_not_indexed', next_step='charmm-gui-cli jobs list')
    for record, _ in candidates:
        if (not isinstance(record.get('directory'), str)
                or record.get('task_id') is not None and not isinstance(record['task_id'], str)
                or record.get('name') is not None and not isinstance(record['name'], str)):
            raise ToolError(key='cleanup.missing_not_indexed', next_step='charmm-gui-cli jobs list')
    identities = {(record.get('task_id'), record.get('directory')) for record, _ in candidates}
    if len(identities) != 1:
        raise ToolError(key='cleanup.missing_ambiguous', next_step='charmm-gui-cli jobs list')
    record, digest = candidates[0]
    raw = record.get('directory')
    identifier = record.get('task_id')
    if (not isinstance(raw, str) or not Path(raw).is_absolute()
            or identifier is not None and (not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier))):
        raise _error('missing_not_indexed', supplied)
    directory = Path(raw).resolve()
    if directory.exists() or directory.is_symlink():
        raise _error('missing_present', directory, path=str(task_root(directory)))
    return directory, identifier, record.get('name'), raw, digest


def _borrowed_lease(directory, descriptor):
    """Internal callers may retain the same process lease across a task move."""
    try:
        import fcntl
        import stat
        if type(descriptor) is not int or (directory / 'run.lock').is_symlink():
            raise ValueError
        held, target = os.fstat(descriptor), (directory / 'run.lock').stat()
        if not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (target.st_dev, target.st_ino):
            raise ValueError
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ValueError):
        raise _error('lease_invalid', directory) from None


def forget(target, *, yes=False, missing=False, _lease=None, _aliases=()):
    """Default is a read-only plan; --yes moves only matching index records."""
    if missing:
        context = _missing_context(target)
        directory, identifier, embedded_name, raw_directory, _ = context
    else:
        try:
            directory = tasks.resolve_run(target).resolve()
        except ToolError as error:
            if error.key == 'jobs.directory_missing':
                error.next_step = shlex.join(['charmm-gui-cli', 'jobs', 'forget', str(target), '--missing', '--dry-run'])
            raise
        if _lease is not None:
            _borrowed_lease(directory, _lease)
        _safe_to_forget(directory, check_process=_lease is None)
        identifier = task_id(directory)
        embedded_name = tasks._job_metadata(directory).get('name')
        raw_directory = str(directory)
    name = embedded_name or str(task_root(directory))
    entries = _entries(directory, identifier, name, additional_aliases=[raw_directory, *_aliases])
    result = {'state': 'forget_plan', 'name': name, 'directory': str(task_root(directory)), 'task_id': identifier,
              'index_records': len(entries), 'index_bytes': sum(item['bytes'] for item in entries),
              'index_files': [str(item['path']) for item in entries],
              'task_data_preserved': True, 'remote_requests': 0, 'missing_directory': missing,
              'next_step': shlex.join(['charmm-gui-cli', 'jobs', 'forget', str(target), *(['--missing'] if missing else []), '--yes'])}
    if not yes:
        return result
    fd = None if missing else _lease if _lease is not None else claim(directory)
    try:
        with ExitStack() as stack:
            # All cooperating name/history writers use the same names lock.
            for registry in _registries():
                names = registry / 'names'
                if names.is_symlink():
                    raise _error('unsafe_index', directory, path=str(names))
                names.mkdir(mode=0o700, parents=True, exist_ok=True)
                stack.enter_context(run_lock(names))
            if not missing:
                stack.enter_context(run_lock(directory))
            # Own the lease now; inspect submission state without mistaking our
            # own flock for an active model worker.
            if missing:
                if _missing_context(target) != context:
                    raise _error('index_changed', directory)
            else:
                _safe_to_forget(directory, check_process=False)
                if task_id(directory) != identifier:
                    raise _error('not_task', directory, path=str(task_root(directory)))
            entries = _entries(directory, identifier, name, additional_aliases=[raw_directory, *_aliases])
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
                                   'directory': str(directory), 'missing_directory': missing, 'files': [
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
        if fd is not None and _lease is None:
            os.close(fd)
    result.update(state='forgotten', index_records=len(entries), index_bytes=sum(item['bytes'] for item in entries),
                  index_files=[str(item['path']) for item in entries],
                  backup_directories=[str(backup) for backup, _ in receipts.values()],
                  next_step=shlex.join(['charmm-gui-cli', 'jobs', 'relink', name, str(task_root(directory))]
                                      if embedded_name else ['charmm-gui-cli', 'jobs', 'remember', str(task_root(directory))]))
    if missing:
        result['next_step'] = 'charmm-gui-cli jobs list'
    return result


def remember(target, *, _lease=None):
    """Restore local discovery only, never resume a modeling pipeline."""
    path = Path(target).expanduser().resolve()
    directory = path / '.charmm-gui' if (path / '.charmm-gui/full-run.json').is_file() else path
    if _lease is not None:
        _borrowed_lease(directory, _lease)
    elif metadata(directory)['running']:
        raise _error('running', directory)
    name = tasks._job_metadata(directory).get('name')
    if name:
        from .discovery import relink
        binding = any((Path(registry) / 'names' / (name + '.json')).exists() for registry in tasks._registries())
        if binding or forgotten_binding(name, directory) is not None:
            result = relink(name, directory, _lease=_lease)
        else:
            # Explicit import of an unregistered named local plan. Require an
            # existing, consistent UUID; do not invent task identity here.
            if task_id(directory) is None:
                raise _error('not_task', directory, path=str(task_root(directory)))
            tasks.assign_name(directory, name)
            result = {'name': name, 'directory': str(task_root(directory)), 'task_id': task_id(directory),
                      'next_step': shlex.join(['charmm-gui-cli', 'jobs', 'status', name])}
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
