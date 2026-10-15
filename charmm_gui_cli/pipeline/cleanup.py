"""Recoverable whole-task cleanup, coordinated with indexes and dependents."""

from contextlib import ExitStack
import os
from pathlib import Path
import shlex
import uuid

from ..store import tasks, quarantine as q
from ..store.auth import ToolError
from ..store.discovery import locate
from ..store.exclusive_move import move_exclusive
from ..store.layout import task_root
from ..store.lifecycle import _record, _registries, _safe_to_forget, forget, remember
from ..store.locking import now, run_lock
from ..store.paths import read_value
from ..store.workers import claim


def _batch_parent(root):
    if root.parent.name != 'jobs':
        return None
    parent = root.parent.parent
    if not (parent / 'batch.json').exists():
        return None
    from . import batch
    manifest = batch._read(parent, verify=False)
    if manifest['state'] != 'complete' or any(row['state'] not in batch.TERMINAL for row in manifest['jobs']):
        raise q.error('batch_active', path=str(parent))
    rows = [row for row in manifest['jobs'] if Path(row['directory']).resolve() == root / '.charmm-gui']
    if len(rows) != 1:
        raise q.error('identity', path=str(root))
    return parent


def _dependents(directory, identifier):
    """Include forgotten retained children; missing registered runs fail closed."""
    candidates = []
    for registry in _registries():
        paths = [*(registry / 'names').glob('*.json'), *(registry / 'run-history').glob('*.json')]
        if (registry / 'latest-run.json').exists():
            paths.append(registry / 'latest-run.json')
        if len(paths) > 10000:
            raise q.error('index', path=str(registry))
        for path in paths:
            try:
                record, _ = _record(path)
                other = locate(record)
            except (OSError, ValueError, ToolError):
                raise q.error('index', path=str(path)) from None
            candidates.append(other)
        # An explicit forget did not delete data or release a source dependency.
        archives = list((registry / 'forgotten').glob('*/forget.json'))
        if len(archives) > 10000:
            raise q.error('index', path=str(registry))
        for receipt in archives:
            try:
                value, _ = _record(receipt, limit=8 * 1024 * 1024)
                path = Path(value['directory'])
                if path.is_dir():
                    candidates.append(path)
            except (OSError, ValueError, KeyError, TypeError):
                raise q.error('index', path=str(receipt)) from None
    isolated = set()
    for record in q.records():
        try:
            current = q.location(record)
            candidates.append(current / '.charmm-gui')
            if current == Path(record['quarantine_root']):
                isolated.add(current / '.charmm-gui')
        except ToolError:
            if record['state'] != 'restored':
                raise
    dependents = []
    for other in set(candidates):
        if other == directory:
            continue
        try:
            full = read_value(other / 'full-run.json')
            certificate = full.get('reused_preparation', {}).get('certificate', {})
            if not isinstance(certificate, dict):
                raise ValueError
        except (OSError, ValueError, AttributeError, ToolError):
            raise q.error('index', path=str(other / 'full-run.json')) from None
        if (certificate.get('source_task_id') == identifier
                or certificate.get('source_directory') == str(directory)):
            try:
                _safe_to_forget(other)
                if full.get('state') not in ('validated', 'validation_failed', 'remote_error', 'preparation_failed') and other not in isolated:
                    raise q.error('dependent', path=str(task_root(other)))
            except ToolError:
                raise q.error('dependent', path=str(task_root(other))) from None
            dependents.append(other)
    return sorted(dependents)


def _locks(stack, directory):
    for registry in _registries():
        names = q.safe_path(registry / 'names')
        names.mkdir(mode=0o700, parents=True, exist_ok=True)
        stack.enter_context(run_lock(names))
    stack.enter_context(run_lock(directory))


def _move(record, source, destination, state):
    """Record before moving; OS errors can occur after an NFS rename succeeds."""
    q.save(record)
    try:
        move_exclusive(source, destination)
    except KeyboardInterrupt as interrupted:
        interrupted.cleanup_operation = record['operation']
        raise
    except OSError:
        if not source.exists() and q.same_root(destination, record):
            # Positive physical identity evidence, not a guessed retry.
            record['move_response_uncertain'] = True
        else:
            raise q.error('move', operation=record['operation'], source=str(source), destination=str(destination)) from None
    if source.exists() or not q.same_root(destination, record):
        raise q.error('location', operation=record['operation'])
    record.update(state=state, updated_at=now())
    q.save(record)


def clean(target, *, yes=False, trash_dir=None):
    supplied = Path(target).expanduser()
    if supplied.is_symlink():
        raise q.error('unsafe', path=str(supplied))
    directory = tasks.resolve_run(target)
    root, identifier = q.owned(directory)
    for retained in q.records():
        if root == Path(retained['quarantine_root']) and q.same_root(root, retained):
            raise q.error('already', operation=retained['operation'])
    q.protect(root)
    _safe_to_forget(directory)
    parent = _batch_parent(root)
    _dependents(directory, identifier)
    count, size = q.inventory(root)
    trash = q.safe_path(trash_dir or root.parent / '.charmm-gui-trash')
    q.protect(trash)
    if trash == root or trash.is_relative_to(root):
        raise q.error('unsafe', path=str(trash))
    operation = uuid.uuid4().hex
    container = trash / operation
    destination = container / 'task'
    info = root.stat()
    record = {'version': 1, 'kind': 'task_quarantine', 'operation': operation, 'state': 'moving',
              'task_id': identifier, 'name': tasks._job_metadata(directory).get('name'),
              'original_root': str(root), 'container': str(container), 'quarantine_root': str(destination),
              'root_dev': info.st_dev, 'root_ino': info.st_ino, 'created_at': now(),
              'file_count': count, 'logical_bytes': size, 'remote_requests': 0}
    result = {**record, 'state': 'clean_plan', 'directory': str(root), 'destination': str(destination),
              'next_step': shlex.join(['charmm-gui-cli', 'jobs', 'clean', str(target),
                                      *(['--trash-dir', str(trash)] if trash_dir else []), '--yes'])}
    if not yes:
        return result
    q.safe_path(q.ledger()).mkdir(mode=0o700, parents=True, exist_ok=True)
    with run_lock(q.ledger()), ExitStack() as outer:
        if parent:
            outer.enter_context(run_lock(parent))
        fd = claim(directory)
        outer.callback(os.close, fd)
        with ExitStack() as stack:
            _locks(stack, directory)
            q.protect(root)
            _safe_to_forget(directory, check_process=False)
            if not q.same_root(root, record):
                raise q.error('identity', path=str(root))
            _batch_parent(root)
            dependents = _dependents(directory, identifier)
            for child in dependents:
                # Keep a stopped dependent from starting between validation and
                # the physical move. No remote POST can race this lease.
                try:
                    child_fd = claim(child)
                except ToolError:
                    raise q.error('dependent', path=str(task_root(child))) from None
                outer.callback(os.close, child_fd)
            record['file_count'], record['logical_bytes'] = q.inventory(root)
            trash.mkdir(mode=0o700, parents=True, exist_ok=True)
            if trash.stat().st_dev != root.stat().st_dev:
                raise q.error('cross_device', path=str(trash))
            container.mkdir(mode=0o700, exist_ok=False)
            if parent:
                from . import batch
                manifest = batch._read(parent, verify=False)
                for row in manifest['jobs']:
                    if Path(row['directory']).resolve() == directory:
                        row['task_id'] = identifier
                batch._save(parent, manifest)
            _move(record, root, destination, 'index_pending')
        try:
            archived = forget(str(destination), yes=True, _lease=fd, _aliases=[str(directory)])
            record.update(state='quarantined', index_backups=archived.get('backup_directories', []))
            q.save(record)
        except (OSError, ToolError):
            raise q.error('index_pending', operation=operation) from None
        except KeyboardInterrupt as interrupted:
            interrupted.cleanup_operation = operation
            raise
    return {**record, 'directory': str(destination), 'destination': str(destination),
            'next_step': shlex.join(['charmm-gui-cli', 'jobs', 'restore', operation, '--dry-run'])}


def restore(operation, *, yes=False, to=None, register=True):
    record = q.read(operation)
    source = q.location(record)
    _safe_to_forget(source / '.charmm-gui')
    q.inventory(source)
    destination = q.safe_path(to or record.get('restore_root', record['original_root']))
    q.protect(source)
    q.protect(destination)
    if destination != source and (destination.exists() or destination.is_symlink()):
        raise q.error('occupied', operation=record['operation'], path=str(destination))
    name = record.get('name')
    if register and name:
        # Check friendly-name conflicts before touching the task directory.
        for registry in _registries():
            path = registry / 'names' / (name + '.json')
            if path.exists():
                binding, _ = _record(path)
                if binding.get('task_id') != record['task_id']:
                    raise q.error('name_conflict', operation=record['operation'], name=name)
    result = {**record, 'state': 'restore_plan', 'directory': str(source), 'destination': str(destination),
              'register': register, 'next_step': shlex.join(['charmm-gui-cli', 'jobs', 'restore', record['operation'],
                  *(['--to', str(destination)] if to else []), *([] if register else ['--no-register']), '--yes'])}
    if not yes:
        return result
    q.safe_path(q.ledger()).mkdir(mode=0o700, parents=True, exist_ok=True)
    with run_lock(q.ledger()), ExitStack() as outer:
        current_path = q.ledger() / (record['operation'] + '.json')
        current = q.read(record['operation']) if current_path.exists() else q.read(operation)
        if current != record or q.location(record) != source:
            raise q.error('location', operation=record['operation'])
        parent = _batch_parent(Path(record['original_root']))
        if parent:
            outer.enter_context(run_lock(parent))
        fd = claim(source / '.charmm-gui')
        outer.callback(os.close, fd)
        with ExitStack() as stack:
            _locks(stack, source / '.charmm-gui')
            _safe_to_forget(source / '.charmm-gui', check_process=False)
            if not q.same_root(source, record):
                raise q.error('identity', path=str(source))
            q.inventory(source)
            # Recheck the name under the same locks used by all name writers.
            if register and name:
                for registry in _registries():
                    path = registry / 'names' / (name + '.json')
                    if path.exists() and _record(path)[0].get('task_id') != record['task_id']:
                        raise q.error('name_conflict', operation=record['operation'], name=name)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if source != destination:
                record.update(state='restoring', restore_root=str(destination))
                _move(record, source, destination, 'restoring')
        if register:
            try:
                remember(str(destination), _lease=fd)
            except (OSError, ToolError):
                raise q.error('index_pending', operation=record['operation']) from None
        record.update(state='restored', restore_root=str(destination), registered=register, updated_at=now())
        q.save(record)
    return {**record, 'directory': str(destination), 'destination': str(destination),
            'next_step': shlex.join(['charmm-gui-cli', 'jobs', 'status', name if register and name else str(destination)])}
