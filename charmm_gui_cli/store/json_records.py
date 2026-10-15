"""Bounded reads of regular private JSON records, without following links."""

import hashlib
import json
import os
from pathlib import Path
import stat


def read_object(path, *, limit=1024 * 1024):
    path = Path(path)
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
