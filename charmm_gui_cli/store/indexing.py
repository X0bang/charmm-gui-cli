"""Keep local plans out of the global task index until explicitly launched."""

from contextlib import contextmanager
from contextvars import ContextVar

_WRITES = ContextVar("task_index_writes", default=True)


def writes_allowed():
    return _WRITES.get()


@contextmanager
def local_plan():
    token = _WRITES.set(False)
    try:
        yield
    finally:
        _WRITES.reset(token)
