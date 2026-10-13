"""Portable task metadata; resolve paths without rewriting immutable evidence."""

import json
from copy import deepcopy
from pathlib import Path

MARKER = "_task_paths"


def root_for(path):
    for ancestor in Path(path).absolute().parents:
        if ancestor.name == ".charmm-gui":
            return ancestor.parent.resolve()
        if (ancestor / ".charmm-gui/full-run.json").is_file() or (ancestor / ".charmm-gui/path-mode.json").is_file():
            return ancestor.resolve()
    return None


def portable(root):
    if root is None:
        return False
    path = Path(root) / ".charmm-gui/path-mode.json"
    if not path.exists():
        return False
    from .auth import ToolError
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if path.is_symlink() or not isinstance(value, dict) or type(value.get("version")) is not int or value != {"version": 1}:
            raise ValueError
    except (OSError, ValueError):
        raise ToolError(key="paths.invalid", params={"path": str(path)}, next_step="charmm-gui-cli jobs status " + str(root)) from None
    return True


def origin(root):
    """The old embedded directory is an identity anchor, not an IO location."""
    if root is None or portable(root):
        return root
    path = Path(root) / ".charmm-gui/job.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        old = Path(record["directory"])
        if old.is_absolute() and old.name == ".charmm-gui":
            return old.parent
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return root


def _replace(value, old, new):
    if isinstance(value, dict):
        return {key: _replace(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, old, new) for item in value]
    if isinstance(value, str) and value.startswith("/"):
        path = Path(value)
        if path.is_relative_to(old):
            return str(new / path.relative_to(old))
    return value


def _relative(value, root, fields, address=()):
    if isinstance(value, dict):
        return {key: _relative(item, root, fields, address + (key,)) for key, item in value.items()}
    if isinstance(value, list):
        return [_relative(item, root, fields, address + (index,)) for index, item in enumerate(value)]
    if isinstance(value, str) and value.startswith("/"):
        path = Path(value)
        if path.is_relative_to(root):
            fields.append(list(address))
            return path.relative_to(root).as_posix()
    return value


def encode(path, value):
    root = root_for(path)
    if root is None or not isinstance(value, dict):
        return value
    if not portable(root):
        # Do not migrate old records or invalidate their path-bound digests.
        return _replace(value, root, origin(root))
    fields = []
    result = _relative(value, root, fields)
    if fields:
        result[MARKER] = {"version": 1, "fields": fields}
    return result


def decode(path, value):
    root = root_for(path)
    if not isinstance(value, dict) or root is None:
        return value
    if MARKER not in value:
        return _replace(value, origin(root), root)
    from .auth import ToolError
    result = deepcopy(value)
    marker = result.pop(MARKER)
    if not isinstance(marker, dict) or type(marker.get("version")) is not int or marker["version"] != 1 or not isinstance(marker.get("fields"), list):
        raise ToolError(key="paths.invalid", params={"path": str(path)}, next_step="charmm-gui-cli jobs status " + str(root))
    for address in marker["fields"]:
        try:
            if not isinstance(address, list) or not address:
                raise ValueError
            node = result
            for key in address[:-1]:
                if not (isinstance(node, dict) and isinstance(key, str) or isinstance(node, list) and type(key) is int and key >= 0):
                    raise ValueError
                node = node[key]
            key = address[-1]
            if not (isinstance(node, dict) and isinstance(key, str) or isinstance(node, list) and type(key) is int and key >= 0):
                raise ValueError
            item = node[key]
            if not isinstance(item, str) or not item or Path(item).is_absolute() or ".." in Path(item).parts:
                raise ValueError
            resolved = root / item
            if not resolved.resolve().is_relative_to(root):
                raise ValueError
            node[key] = str(resolved)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ToolError(key="paths.invalid", params={"path": str(path)}, next_step="charmm-gui-cli jobs status " + str(root)) from None
    return result


def read_value(path):
    return decode(path, json.loads(Path(path).read_text(encoding="utf-8")))


def identity(value, directory):
    """Stable digest representation: relative for new tasks, original for old."""
    root = root_for(Path(directory) / "identity.json")
    if root is None:
        return value
    if portable(root):
        return _relative(value, root, [])
    return _replace(value, root, origin(root))
