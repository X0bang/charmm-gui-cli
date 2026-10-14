"""One stable console timeline for a queue, without per-round child spinners."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
import sys
import threading
import time

from charmm_gui_cli.presentation.progress import CONSOLE_LOCK, clear_line, describe_progress, format_progress, terminal_frame
from ..locales.i18n import t
from ..store.layout import log_path
from ..store.auth import ToolError

_OBSERVER = ContextVar("batch_progress", default=None)


def current():
    return _OBSERVER.get()


@contextmanager
def observe(reporter):
    token = _OBSERVER.set(reporter)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def elapsed(value):
    seconds = max(0, int(value or 0))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


class ChildProgress:
    def __init__(self, owner, row):
        self.owner, self.row = owner, row

    def __call__(self, event):
        self.owner.event(self.row, event)

    def close(self):
        pass  # The queue owns the terminal and outlives an individual poll.


class BatchReporter:
    def __init__(self, directory, *, stream=None):
        self.directory = Path(directory).resolve()
        self.stream = sys.stderr if stream is None else stream
        self.started = time.monotonic()
        self.phases, self.ids, self.links, self.errors = {}, {}, set(), {}
        self.transfers = {}
        self.summary, self.last_summary, self.last_record = None, None, self.started
        self.stop = threading.Event()
        self.thread = None
        self.counter = 0

    def child(self, row):
        return ChildProgress(self, row)

    def _line(self, text, row=None):
        line = datetime.now().strftime("[%H:%M:%S] ") + text
        with CONSOLE_LOCK:
            if self.stream.isatty():
                print(clear_line(), end="", file=self.stream)
            print(line, file=self.stream, flush=True)
            paths = [self.directory / "run.log"] if self.directory.is_dir() else []
            if row is not None and Path(row["directory"]).is_dir():
                # The console timeline and each child's authoritative log both
                # get the record; never store terminal refresh frames.
                paths.append(log_path(Path(row["directory"])))
            for path in paths:
                try:
                    if path.is_symlink():
                        raise OSError
                    with path.open("a", encoding="utf-8") as handle:
                        import os
                        os.chmod(path, 0o600)
                        handle.write(line + "\n")
                except OSError:
                    import shlex
                    raise ToolError(key="batch.log_unwritable", params={"path": str(path)}, category="filesystem_error",
                                    next_step="charmm-gui-cli batch-status " + shlex.quote(str(self.directory))) from None

    def event(self, row, event):
        name = row["name"]
        from ..locales.transfers import transfer_text
        if event.get("state") != "downloading" or event.get("transfer_complete"):
            self.transfers.pop(name, None)
        elif transfer_text(event):
            self.transfers[name] = transfer_text(event)
        ids = self.ids.setdefault(name, {})
        detail = describe_progress(event, **ids)
        ids.update(source_job_id=detail["source_job_id"], build_job_id=detail["build_job_id"])
        phase = (event.get("state"), detail.get("internal_stage"), detail.get("last_output_file"), *ids.values())
        if phase != self.phases.get(name) or event.get("transfer_complete"):
            self.phases[name] = phase
            self._line(t("batch.child_progress", name=name, stage=format_progress(event, **ids),
                         elapsed=elapsed(time.monotonic() - self.started)), row)
        for url in detail["official_links"].values():
            if url not in self.links:
                self.links.add(url)
                self._line(t("batch.child_link", name=name, url=url), row)

    def failure(self, row):
        self.transfers.pop(row["name"], None)
        error = row.get("error_detail", {})
        message = ToolError(row.get("error", ""), key=error.get("message_key"), params=error.get("message_params"),
                            server_message=error.get("server_message"), details=error.get("details")).localized()
        key = (row.get("state"), message)
        if key != self.errors.get(row["name"]):
            self.errors[row["name"]] = key
            self._line(t("batch.child_error", name=row["name"], message=message), row)

    def _label(self):
        value = self.summary or {}
        label = t("batch.progress", completed=value.get("completed", 0), total=value.get("total", 0),
                  active=value.get("active", 0), failed=value.get("failed", 0))
        transfers = self.transfers.copy()
        if transfers:
            name, progress = next(iter(transfers.items()))
            label += " | " + name + ": " + progress
        return label

    def update(self, value):
        if self.summary is None:
            self.started = time.monotonic() - value.get("elapsed_seconds", 0)
        self.summary = value
        phase = (value["state"], tuple(sorted(value["counts"].items())))
        if phase != self.last_summary or time.monotonic() - self.last_record >= 300:
            self._line(self._label() + " | " + t("progress.elapsed", elapsed=elapsed(value.get("elapsed_seconds", 0))))
            self.last_summary, self.last_record = phase, time.monotonic()
        if self.stream.isatty() and self.thread is None:
            self.thread = threading.Thread(target=self._animate, daemon=True)
            self.thread.start()

    def _animate(self):
        while not self.stop.wait(.25):
            with CONSOLE_LOCK:
                print(terminal_frame(self._label(), self.directory.name, int(time.monotonic() - self.started), self.counter),
                      end="", file=self.stream, flush=True)
                self.counter += 1

    def close(self):
        if self.stop.is_set():
            return
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
            with CONSOLE_LOCK:
                print(clear_line(), end="", file=self.stream, flush=True)
