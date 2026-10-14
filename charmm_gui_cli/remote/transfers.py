"""GET-only archive scheduling and bounded, credential-free byte progress."""

from contextlib import contextmanager
from contextvars import ContextVar
import re
import time

_OBSERVER = ContextVar("archive_transfer_observer", default=None)


@contextmanager
def observe(callback):
    token = _OBSERVER.set(callback)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def archive_delay(attempt):
    if type(attempt) is not int or attempt < 1:
        from ..store.auth import ToolError
        raise ToolError(key="download.retry_invalid", next_step="charmm-gui-cli jobs list")
    return (20, 30, 45, 60, 90, 120)[min(attempt - 1, 5)]


class ByteProgress:
    def __init__(self, response):
        self.callback = _OBSERVER.get()
        self.received = 0
        self.started = self.last = time.monotonic()
        length = response.headers.get("Content-Length", "")
        identity = response.headers.get("Content-Encoding", "identity") in ("", "identity")
        self.total = int(length) if identity and isinstance(length, str) and re.fullmatch(r"[0-9]{1,19}", length) and int(length) > 0 else None
        self.emit()

    def add(self, count):
        self.received += count
        if time.monotonic() - self.last >= 1:
            self.emit()

    def emit(self, *, complete=False):
        self.last = time.monotonic()
        if self.callback is not None:
            self.callback({"downloaded_bytes": self.received, "total_bytes": self.total,
                           "transfer_complete": complete, "download_elapsed_seconds": max(0, self.last - self.started)})
