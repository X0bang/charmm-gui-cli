"""Owned, recoverable task locations. No deletion, copying or remote calls."""

import os
from pathlib import Path
import re
import stat
import sys

from . import tasks
from .auth import ToolError, atomic_json
from .discovery import task_id
from .layout import task_root
from .lifecycle import _record
from .paths import portable


def error(key, *, operation=None, **params):
    import shlex
    if key == 'occupied' or key == 'name_conflict':
        action = ['charmm-gui-cli', 'jobs', 'restore', operation, '--to',
                  str(Path.cwd() / ('restored-' + operation[:8])), *(['--no-register'] if key == 'name_conflict' else []), '--dry-run']
    elif key == 'location' and operation:
        action = ['ls', '-l', str(ledger() / (operation + '.json'))]
    elif key in ('unsafe', 'receipt', 'index'):
        action = ['ls', '-ld', params['path']]
    elif key in ('dependent', 'identity'):
        action = ['charmm-gui-cli', 'jobs', 'status', params['path']]
    elif key == 'batch_active':
        action = ['charmm-gui-cli', 'batch-status', params['path']]
    else:
        action = ['charmm-gui-cli', 'jobs', 'restore', operation, '--dry-run'] if operation else ['charmm-gui-cli', 'jobs', 'clean', '-h']
    return ToolError(key='quarantine.' + key, params=params,
                     next_step=shlex.join(action))


def ledger():
    return tasks._registry() / 'clean-history'


def safe_path(path):
    """Do not redirect task or operation paths through a symbolic link."""
    path = Path(path).expanduser().absolute()
    def redirected(p):
        # Darwin's system-owned aliases are normal tempfile locations.
        return p.is_symlink() and not (sys.platform == 'darwin' and p in (Path('/tmp'), Path('/var'), Path('/etc')))
    if any(redirected(p) for p in (path, *path.parents)):
        raise error('unsafe', path=str(path))
    return path.resolve()


def owned(directory):
    directory = safe_path(directory)
    root = task_root(directory)
    identifier = task_id(directory)
    if directory.name != '.charmm-gui' or identifier is None:
        raise error('identity', path=str(root))
    if not portable(root):
        # Historical single-directory tasks need their original absolute anchor.
        job, _ = _record(directory / 'job.json')
        old = job.get('directory')
        if not isinstance(old, str) or not Path(old).is_absolute() or Path(old).name != '.charmm-gui':
            raise error('identity', path=str(root))
    return root, identifier


def protect(root):
    root = safe_path(root)
    protected = [Path.home().resolve(), Path.cwd().resolve(), Path(sys.prefix).resolve(),
                 Path(__file__).resolve().parents[2], *[Path(p).resolve() for p in tasks._registries()]]
    if any(p == root or p.is_relative_to(root) for p in protected):
        raise error('unsafe', path=str(root))
    if any((p / '.charmm-gui/full-run.json').exists() for p in root.parents):
        raise error('unsafe', path=str(root))


def inventory(root):
    """Bounded regular tree, no submounts or symlink targets; logical bytes only."""
    root = safe_path(root)
    device = root.stat().st_dev
    count = size = 0
    def failed(exc):
        raise error('unsafe', path=str(Path(exc.filename or root)))
    for base, folders, files in os.walk(root, followlinks=False, onerror=failed):
        for name in [*folders, *files]:
            path = Path(base) / name
            info = path.lstat()
            count += 1
            if (count > 200000 or info.st_dev != device
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise error('unsafe', path=str(path))
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
    return count, size


def records(*, strict=True):
    parent = safe_path(ledger())
    paths = list(parent.glob('*.json'))
    if len(paths) > 10000:
        raise error('receipt', path=str(parent))
    result = []
    for path in sorted(paths):
        try:
            result.append(read(path))
        except ToolError:
            if strict:
                raise
    return result


def read(target):
    """Accept an operation ID or its owned restore.json mirror."""
    token = str(target)
    path = ledger() / (token + '.json') if re.fullmatch(r'[0-9a-f]{32}', token) else Path(target).expanduser().absolute()
    try:
        safe_path(path)
        record, _ = _record(path, limit=1024 * 1024)
        operation = record['operation']
        allowed = {'version', 'kind', 'operation', 'state', 'task_id', 'name', 'original_root', 'container',
                   'quarantine_root', 'root_dev', 'root_ino', 'created_at', 'updated_at', 'file_count',
                   'logical_bytes', 'remote_requests', 'move_response_uncertain', 'index_backups',
                   'restore_root', 'registered'}
        if (record.get('version') != 1 or record.get('kind') != 'task_quarantine'
                or set(record) - allowed
                or record.get('name') is not None and (not isinstance(record['name'], str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', record['name']))
                or not isinstance(operation, str) or not re.fullmatch(r'[0-9a-f]{32}', operation)
                or not isinstance(record.get('task_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', record['task_id'])
                or record.get('state') not in ('moving', 'quarantined', 'index_pending', 'restoring', 'restored')
                or any(type(record.get(key)) is not int or record[key] < 0 for key in ('root_dev', 'root_ino'))):
            raise ValueError
        for key in ('original_root', 'quarantine_root', 'container'):
            if not isinstance(record.get(key), str) or not Path(record[key]).is_absolute():
                raise ValueError
            safe_path(record[key])
        container = Path(record['container'])
        if container.name != operation or Path(record['quarantine_root']) != container / 'task':
            raise ValueError
        if path not in (ledger() / (operation + '.json'), container / 'restore.json'):
            raise ValueError
        if 'restore_root' in record:
            if not isinstance(record['restore_root'], str) or not Path(record['restore_root']).is_absolute():
                raise ValueError
            safe_path(record['restore_root'])
        return record
    except (OSError, ValueError, KeyError, TypeError, ToolError):
        raise error('receipt', path=str(path)) from None


def save(record):
    """The global intent is authoritative; retain a human-findable mirror too."""
    atomic_json(ledger() / (record['operation'] + '.json'), record)
    atomic_json(Path(record['container']) / 'restore.json', record)


def same_root(root, record):
    root = Path(root)
    if not root.is_dir():
        return False
    info = root.stat()
    if (info.st_dev, info.st_ino) != (record['root_dev'], record['root_ino']):
        return False
    try:
        safe_path(root)
        return task_id(root / '.charmm-gui') == record['task_id']
    except (OSError, ValueError, ToolError):
        return False


def location(record):
    choices = {Path(record[key]) for key in ('original_root', 'quarantine_root', 'restore_root') if key in record}
    found = [p for p in choices if same_root(p, record)]
    if len(found) != 1:
        raise error('location', operation=record['operation'])
    return found[0]


def resolve_directory(directory, expected=None):
    """Resolve only a recorded UUID and physical directory identity, never a name."""
    directory = Path(directory).expanduser().resolve()
    entries = records(strict=False)
    aliases = [r for r in entries if expected is not None and r['task_id'] == expected
               or directory in {Path(r[k]) / '.charmm-gui'
               for k in ('original_root', 'quarantine_root', 'restore_root') if k in r}]
    identifiers = {r['task_id'] for r in aliases if expected is None or r['task_id'] == expected}
    if not identifiers:
        return None
    if len(identifiers) != 1:
        raise error('location')
    found = set()
    for record in entries:
        if record['task_id'] in identifiers:
            for key in ('original_root', 'quarantine_root', 'restore_root'):
                if key in record and same_root(record[key], record):
                    found.add(Path(record[key]) / '.charmm-gui')
    if not found:
        return None  # A later manual move can still use normal UUID discovery.
    if len(found) != 1:
        raise error('location', operation=aliases[-1]['operation'])
    return found.pop()
