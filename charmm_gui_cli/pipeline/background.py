"""Detached, locked workers and read-only log following (POSIX platforms)."""

import charmm_gui_cli.store.tasks as _task_store
import charmm_gui_cli.store.workers as _worker_store

from charmm_gui_cli.store.layout import modern, task_root, inputs_path, log_path, full_archive_path

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import math
import shutil
from charmm_gui_cli.store.paths import read_value

from charmm_gui_cli.store.auth import ToolError, atomic_json, load_token, resolve_cookie_path
from charmm_gui_cli.locales.i18n import set_language, t


def launch(args):
    from charmm_gui_cli.pipeline.build_command import execute
    if (getattr(args, "interval", 30) < 10 or getattr(args, "max_wait", 21600) <= 0
            or not math.isfinite(getattr(args, "post_timeout", 900)) or getattr(args, "post_timeout", 900) <= 0):
        raise ToolError(key="background.runtime_invalid", next_step="charmm-gui-cli jobs resume -h")
    if getattr(args, "grompp", False) and not shutil.which(getattr(args, "gmx", "gmx")):
        raise ToolError(key="background.gromacs_missing", next_step="charmm-gui-cli doctor")
    # Local checks are completed synchronously; no modeling POST occurs here.
    if args.command == "build":
        local = argparse.Namespace(**vars(args))
        local.dry_run = True
        result, code = execute(local)
        if code:
            return result, code
        directory = Path(result["directory"])
    else:
        directory = _task_store.resolve_run(args.directory)
        from charmm_gui_cli.pipeline.full_build import _read
        manifest = _read(directory)
        result = {"directory": str(directory), "state": manifest.get("state", "unknown")}
    fd = _worker_store.claim(directory)
    try:
        manifest = _task_store._read_json(directory / "full-run.json")
        from .full_build import require_protein_state_support
        require_protein_state_support(directory, manifest)
        local_archive = (full_archive_path(directory)).is_file()
        offline = local_archive and manifest.get("state") in ("ligands_present", "downloaded_unverified", "validation_failed", "validated", "complete", "completed")
        if not offline:
            token, source = load_token(args.token_file)
            from charmm_gui_cli.remote.api import Client
            from charmm_gui_cli.remote.sessions import bind, warn_expiry, session_cookie_path
            api = Client(token)
            cookies = session_cookie_path(args.cookies, source)
            manager = bind(api, source, cookies)
            api.validate_auth()
            token = api.token if isinstance(api.token, str) else token
            try:
                warn_expiry(token, source)
            except ToolError:
                pass
        if not offline and not (directory / "bilayer" / "run.json").exists() and "reused_preparation" not in manifest:
            from charmm_gui_cli.remote.web import WebClient, soup_for
            web = WebClient(cookies, post_timeout=args.post_timeout)
            bind(web, source, cookies, manager)
            web.validate_auth()
            response = web.request("GET", "?doc=input/membrane.bilayer")
            page = soup_for(response.text)
            if page.select_one('input[type="password"]') or not page.select_one('form[name="pdb"]'):
                raise ToolError(key="background.auth", category="authentication", next_step="charmm-gui-cli login")
        options = dict(vars(args), command="build-resume", directory=str(directory), foreground=True, dry_run=False, _worker=True)
        # Save paths/options, never loaded passwords, cookies or tokens.
        for key, value in list(options.items()):
            if isinstance(value, Path):
                options[key] = str(value.resolve())
        atomic_json(directory / "worker-options.json", options)
        initial = {"started_at": datetime.now(timezone.utc).isoformat()}
        _worker_store.write_lock(fd, initial)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
        # Hold the worker at an inherited pipe until its PID is published.
        # Only the parent writes during this handoff; otherwise a fast child
        # could finish before a late parent write and lose its terminal record.
        gate_read, gate_write = os.pipe()
        try:
            with open(log_path(directory), "a", encoding="utf-8") as log:
                os.chmod(log_path(directory), 0o600)
                process = subprocess.Popen([sys.executable, "-m", "charmm_gui_cli.pipeline.background", str(directory), str(fd), str(gate_read)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                    pass_fds=(fd, gate_read), env=env)
            initial["pid"] = process.pid
            _worker_store.write_lock(fd, initial)
        finally:
            os.close(gate_read)
            os.close(gate_write)  # EOF releases the child, including on parent exit.
        if sys.platform == "darwin":
            try:
                subprocess.Popen(["caffeinate", "-i", "-w", str(process.pid)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                pass
        result.update(_task_store._job_metadata(directory))
        result["background"] = dict(initial, running=True, log=str(log_path(directory)))
        return result, 0
    finally:
        os.close(fd)  # The worker retains the inherited flock until it exits.


def watch(directory, *, interval=1):
    from charmm_gui_cli.presentation.progress import clear_line, terminal_frame
    directory = Path(directory)
    offset = 0
    phase = "charmm-gui-cli"
    job = None
    counter = 0
    try:
        while True:
            path = log_path(directory)
            if path.is_file():
                with path.open(encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    for line in handle:
                        if sys.stdout.isatty():
                            print(clear_line(), end="")
                        print(line, end="", flush=True)
                        if len(line) > 10 and line[3:4] == ":":
                            fields = line.strip().split(" | ")
                            phase = fields[0][11:].removeprefix("[charmm-gui-cli] ")
                            identifiers = [field for field in fields[1:] if "=" in field and field.rsplit("=", 1)[1].isdigit()]
                            job = identifiers[-1] if identifiers else None
                    offset = handle.tell()
            state = _worker_store.metadata(directory)
            if not state["running"]:
                if sys.stdout.isatty():
                    print(clear_line(), end="")
                return state.get("exit_code", 0)
            if sys.stdout.isatty():
                elapsed = state["elapsed_seconds"]
                from charmm_gui_cli.store.transfer_state import saved_transfer
                from charmm_gui_cli.locales.transfers import transfer_text
                transfer = saved_transfer(directory)
                label = phase + " | " + transfer_text(transfer) if transfer else phase
                print(terminal_frame(label, job, elapsed, counter), end="", flush=True)
                counter += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        if sys.stdout.isatty():
            print(clear_line(), end="", flush=True)
        print(t("background.watch_stopped"), file=sys.stderr)
        return 130  # Never send a signal to the worker.


def worker(directory, fd, *, startup_fd=None):
    from charmm_gui_cli.pipeline.build_command import execute
    from charmm_gui_cli.presentation.terminal_output import format_build
    if startup_fd is not None:
        try:
            os.read(startup_fd, 1)
        finally:
            os.close(startup_fd)
    directory = Path(directory)
    values = read_value(directory / "worker-options.json")
    for key in ("directory", "token_file", "cookies", "out", "config"):
        if values.get(key):
            values[key] = Path(values[key])
    set_language(values.get("lang"))
    record = {"pid": os.getpid(), "started_at": _worker_store.metadata(directory).get("started_at", datetime.now(timezone.utc).isoformat())}
    _worker_store.write_lock(fd, record)
    code = 2
    try:
        result, code = execute(argparse.Namespace(**values))
        atomic_json(directory / "worker-result.json", result)
        print(format_build(result), flush=True)
    except ToolError as exc:
        print(t("cli.error", message=exc.localized()), file=sys.stderr)
        print(t("cli.next", action=exc.next_step or "charmm-gui-cli jobs list"), file=sys.stderr)
    except (OSError, ValueError):
        print(t("cli.filesystem"), file=sys.stderr)
    except KeyboardInterrupt:
        code = 130
        print(t("cli.interrupted"), file=sys.stderr)
    except Exception:
        # No traceback or arbitrary exception text containing HTTP data.
        print(t("background.worker_failed"), file=sys.stderr)
        print(t("cli.next", action="charmm-gui-cli jobs status NAME"), file=sys.stderr)
    finally:
        record.update(finished_at=datetime.now(timezone.utc).isoformat(), exit_code=code)
        _worker_store.write_lock(fd, record)
        os.close(fd)
        from charmm_gui_cli.presentation.notifications import notify_completion
        notify_completion(code)
    return code


if __name__ == "__main__":
    sys.exit(worker(sys.argv[1], int(sys.argv[2]), startup_fd=int(sys.argv[3]) if len(sys.argv) > 3 else None))


# Backward-compatible function exports; internal code uses the owner module.
from charmm_gui_cli.store.workers import claim, metadata, write_lock
