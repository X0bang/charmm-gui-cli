"""Opt-in OS credentials and paired-session renewal, never modeling replay."""

from contextlib import contextmanager
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import time
import uuid

from ..store.auth import ToolError, atomic_write, default_token_path, resolve_cookie_path, token_info
from ..locales.i18n import t


def account_path(token_path):
    path = Path(token_path).expanduser().absolute()
    return path.with_name(path.name + ".account.json")


def _error(key):
    return ToolError(key="session." + key, category="authentication", next_step="charmm-gui-cli login")


def _regular(path):
    if path.is_symlink() or path.exists() and not path.is_file():
        raise _error("unsafe_path")


@contextmanager
def session_lock(token_path, cookie_path=None):
    cookies = Path(cookie_path or resolve_cookie_path(token_source=str(token_path))).absolute()
    paths = sorted({account_path(token_path).with_suffix(".lock"), cookies.with_name(cookies.name + ".lock")})
    descriptors = []
    try:
        for path in paths:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            descriptors.append(fd)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        for fd in reversed(descriptors):
            os.close(fd)
        raise _error("lock") from None
    try:
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _session_json(path, value):
    # Credentials are not task-path metadata, even with a custom destination.
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _backend(expected=None):
    """Never permit file-based/third-party plaintext or cryptfile fallback."""
    try:
        library = importlib.import_module("keyring")
        selected = library.get_keyring()
        candidates = list(selected.backends) if type(selected).__module__ == "keyring.backends.chainer" else [selected]
        allowed = {"keyring.backends.macOS", "keyring.backends.SecretService", "keyring.backends.kwallet", "keyring.backends.Windows"}
        selected = next(candidate for candidate in candidates if type(candidate).__module__ in allowed and candidate.priority > 0
                        and (expected is None or _backend_id(candidate) == expected))
        return selected
    except Exception:
        raise _error("keyring") from None


def _service(token_path):
    return "charmm-gui-cli/" + _digest(str(Path(token_path).absolute()).encode())[:24]


def _backend_id(backend):
    return type(backend).__module__ + "." + type(backend).__qualname__


def command_password():
    """Bounded memory-only capture; never echo argv, stdout or stderr."""
    command = os.environ.get("CHARMM_GUI_PASSWORD_COMMAND", "")
    try:
        argv = shlex.split(command)
        if not argv:
            raise ValueError
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except (OSError, ValueError):
        raise _error("command") from None
    output, deadline = bytearray(), time.monotonic() + 30
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError
                for key, _ in selector.select(remaining):
                    block = os.read(key.fd, 4097)
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(block)
                        if len(output) > 4096:
                            raise ValueError
        if process.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
            raise ValueError
        password = output.decode("utf-8").rstrip("\r\n")
        if not password or any(char in password for char in "\r\n\x00"):
            raise ValueError
        return password
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        raise _error("command") from None
    finally:
        process.stdout.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def _read_profile(token_path):
    path = account_path(token_path)
    _regular(path)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or set(value) != {"version", "email", "credential_source", "credential_backend", "cookie_file", "generation", "token_hash", "cookie_hash"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["email"], str) or not value["email"] or any(c in value["email"] for c in "\n\r\x00")
                or value["credential_source"] not in (None, "keyring", "command")
                or (value["credential_source"] == "keyring" and not isinstance(value["credential_backend"], str))
                or (value["credential_source"] != "keyring" and value["credential_backend"] is not None)
                or not isinstance(value["cookie_file"], str) or not Path(value["cookie_file"]).is_absolute()
                or any(not isinstance(value[key], str) or not value[key] for key in ("generation", "token_hash", "cookie_hash"))):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, UnicodeError):
        raise _error("profile") from None


def cookies_value(web):
    return {"version": 1, "cookies": [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
             "secure": c.secure, "expires": c.expires, "rest": c._rest} for c in web.session.cookies]}


def save_pair(token_path, cookie_path, token, web, email, credential_source, credential_backend=None):
    """Caller holds session_lock. Roll back an interrupted publication safely."""
    token_path, cookie_path = Path(token_path).absolute(), Path(cookie_path).absolute()
    profile_path = account_path(token_path)
    if len({token_path, cookie_path, profile_path}) != 3:
        raise _error("unsafe_path")
    for path in (token_path, cookie_path, profile_path):
        _regular(path)
    token_bytes = (token + "\n").encode()
    cookie_bytes = (json.dumps(cookies_value(web), indent=2, ensure_ascii=False) + "\n").encode()
    profile = {"version": 1, "email": email, "credential_source": credential_source,
               "credential_backend": credential_backend or (_backend_id(_backend()) if credential_source == "keyring" else None),
               "cookie_file": str(cookie_path), "generation": uuid.uuid4().hex,
               "token_hash": _digest(token_bytes), "cookie_hash": _digest(cookie_bytes)}
    contents = {cookie_path: cookie_bytes, token_path: token_bytes,
                profile_path: (json.dumps(profile, indent=2, ensure_ascii=False) + "\n").encode()}
    old = {path: path.read_bytes() if path.exists() else None for path in contents}
    try:
        for path, data in contents.items():
            atomic_write(path, data.decode())
    except (OSError, KeyboardInterrupt) as exc:
        incomplete = False
        for path in reversed(contents):
            try:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != contents[path]:
                    continue
                if old[path] is None:
                    path.unlink()  # Only a new file created by this failed publication.
                else:
                    atomic_write(path, old[path].decode())
            except OSError:
                incomplete = True
        if incomplete:
            raise _error("save_rollback") from None
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise _error("save") from None
    return profile


def login_session(token_path, cookie_path, email, password, *, save_password=False, api=None, web=None):
    from ..remote.api import Client
    from ..remote.web import WebClient
    token_path, cookie_path = Path(token_path).expanduser().absolute(), Path(cookie_path).expanduser().absolute()
    api, web = api or Client(), web or WebClient(post_timeout=60)
    backend = _backend() if save_password else None
    with session_lock(token_path, cookie_path):
        previous = None
        if backend is not None:
            try:
                previous = backend.get_password(_service(token_path), email)
            except Exception:
                raise _error("keyring") from None
        token = api.login(email, password)
        result = web.login(email, password)
        if backend is not None:
            try:
                backend.set_password(_service(token_path), email, password)
            except Exception:
                raise _error("keyring") from None
        source = "keyring" if backend is not None else "command" if os.environ.get("CHARMM_GUI_PASSWORD_COMMAND") else None
        try:
            save_pair(token_path, cookie_path, token, web, email, source, _backend_id(backend) if backend else None)
        except ToolError:
            if backend is not None:
                try:
                    if previous is None:
                        backend.delete_password(_service(token_path), email)
                    else:
                        backend.set_password(_service(token_path), email, previous)
                except Exception:
                    raise _error("keyring_rollback") from None
            raise
    return result


def logout(token_path=None, cookie_path=None):
    """Remove only this explicitly selected local session and remembered entry."""
    token_path = Path(token_path or default_token_path()).expanduser().absolute()
    initial = _read_profile(token_path)
    selected = Path(cookie_path).expanduser().absolute() if cookie_path else Path(initial["cookie_file"]) if initial else resolve_cookie_path(token_source=str(token_path)).absolute()
    with session_lock(token_path, selected):
        profile = _read_profile(token_path)
        recorded = Path(profile["cookie_file"]) if profile else resolve_cookie_path(token_source=str(token_path)).absolute()
        cookies = Path(cookie_path).expanduser().absolute() if cookie_path else recorded
        if profile and cookies != recorded:
            raise _error("changed")
        paths = (token_path, cookies, account_path(token_path))
        for path in paths:
            _regular(path)
        if profile:
            if cookies != selected:
                raise _error("changed")
            for path, key in ((token_path, "token_hash"), (cookies, "cookie_hash")):
                if not path.is_file() or _digest(path.read_bytes()) != profile[key]:
                    raise _error("changed")
        try:
            if token_path.exists():
                token_info(token_path.read_text().strip())
            if cookies.exists():
                value = json.loads(cookies.read_text())
                if not isinstance(value, dict) or not isinstance(value.get("cookies"), list):
                    raise ValueError
        except (ValueError, UnicodeError, ToolError):
            raise _error("unsafe_path") from None
        if profile and profile["credential_source"] == "keyring":
            try:
                backend = _backend(profile["credential_backend"])
                if backend.get_password(_service(token_path), profile["email"]) is not None:
                    backend.delete_password(_service(token_path), profile["email"])
            except Exception:
                raise ToolError(key="session.logout_keyring", category="authentication",
                                next_step=t("session.logout_retry", command=shlex.join(["charmm-gui-cli", "--token-file", str(token_path), "logout"]))) from None
        removed = []
        for path in paths:
            if path.exists():
                path.unlink()
                removed.append(str(path))
    return {"logged_out": True, "removed_files": removed, "remote_revoked": False,
            "environment_token_active": bool(os.environ.get("CHARMMGUI_TOKEN")),
            "next_step": "unset CHARMMGUI_TOKEN" if os.environ.get("CHARMMGUI_TOKEN") else "charmm-gui-cli login"}


class SessionManager:
    def __init__(self, source, cookie_path=None, *, allow_renew=True):
        self.path = Path(source).expanduser().absolute() if source != "CHARMMGUI_TOKEN" else None
        self.profile = _read_profile(self.path) if self.path else None
        self.cookies = Path(cookie_path).expanduser().absolute() if cookie_path else Path(self.profile["cookie_file"]) if self.profile else resolve_cookie_path(token_source=source).absolute() if self.path else None
        self.allow_renew = allow_renew
        self.last_login = -float("inf")
        self.generation = self.profile["generation"] if self.profile else None

    def _current(self):
        profile = _read_profile(self.path)
        if (not profile or profile["email"] != self.profile["email"] or profile["cookie_file"] != str(self.cookies)):
            raise _error("changed")
        for path, key in ((self.path, "token_hash"), (self.cookies, "cookie_hash")):
            _regular(path)
            if not path.is_file() or _digest(path.read_bytes()) != profile[key]:
                raise _error("changed")
        return profile

    def token(self, current, *, force=False):
        if not self.profile:
            return current
        with session_lock(self.path, self.cookies):
            profile = self._current()
            token = self.path.read_text(encoding="utf-8").strip()
            if not token_info(token)["expired"] and (not force or token != current):
                self.generation = profile["generation"]
                return token
            if not self.allow_renew:
                raise _error("not_configured")
            failure_path = account_path(self.path).with_suffix(".renew-failure.json")
            if failure_path.exists():
                _regular(failure_path)
                try:
                    failure = json.loads(failure_path.read_text())
                    if failure["generation"] == profile["generation"] and failure["until_unix"] > time.time():
                        raise _error("renew_failed")
                except (ValueError, KeyError, TypeError):
                    raise _error("profile") from None
            if time.monotonic() - self.last_login < 60:
                raise _error("renew_failed")
            source = "command" if os.environ.get("CHARMM_GUI_PASSWORD_COMMAND") else profile["credential_source"]
            if source == "command":
                password = command_password()
            elif source == "keyring":
                try:
                    password = _backend(profile["credential_backend"]).get_password(_service(self.path), profile["email"])
                except Exception:
                    raise _error("keyring") from None
                if not isinstance(password, str) or not password:
                    raise _error("keyring")
            else:
                raise _error("not_configured")
            self.last_login = time.monotonic()
            from ..remote.api import Client
            from ..remote.web import WebClient
            api, web = Client(), WebClient(post_timeout=60)
            print(t("session.renewing"), file=sys.stderr, flush=True)
            try:
                token = api.login(profile["email"], password)
                web.login(profile["email"], password)
            except ToolError:
                try:
                    _session_json(failure_path, {"generation": profile["generation"], "until_unix": time.time() + 60})
                except OSError:
                    pass
                raise _error("renew_failed") from None
            self.profile = save_pair(self.path, self.cookies, token, web, profile["email"], profile["credential_source"], profile["credential_backend"])
            self.generation = self.profile["generation"]
            print(t("session.renewed"), file=sys.stderr, flush=True)
            return token

    def web_ready(self, web, *, force=False):
        if not self.profile:
            return
        old_generation = getattr(web, "_credential_generation", None)
        self.token(web._credential_token, force=force)
        with session_lock(self.path, self.cookies):
            profile = self._current()
            self.generation = profile["generation"]
            if old_generation != self.generation:
                from ..remote.web import load_cookies
                load_cookies(web, self.cookies)
                web._credential_generation = self.generation
            web._credential_token = self.path.read_text(encoding="utf-8").strip()

    def persist_cookies(self, web):
        if not self.profile:
            _session_json(self.cookies, cookies_value(web))
            return
        with session_lock(self.path, self.cookies):
            profile = self._current()
            if profile["generation"] != getattr(web, "_credential_generation", None):
                return  # A newer login must not be replaced by an old request.
            _session_json(self.cookies, cookies_value(web))
            profile["cookie_hash"] = _digest(self.cookies.read_bytes())
            _session_json(account_path(self.path), profile)
            self.profile = profile


def bind(client, source, cookie_path=None, manager=None):
    manager = manager or SessionManager(source, cookie_path)
    client.renewer = manager
    if hasattr(client, "cookie_path"):
        client._credential_generation = None
        client._credential_token = manager.path.read_text(encoding="utf-8").strip() if manager.profile else ""
    return manager


def session_cookie_path(cookie_path, source):
    return SessionManager(source, cookie_path).cookies if source != "CHARMMGUI_TOKEN" else resolve_cookie_path(cookie_path, source)


_WARNED = set()


def warn_expiry(token, source):
    expiry = token_info(token)["expires_at_unix"]
    remaining = expiry - time.time() if expiry is not None else None
    if remaining is not None and 0 < remaining < 7200 and source not in _WARNED:
        _WARNED.add(source)
        print(t("session.expiring", minutes=max(1, int(remaining / 60))), file=sys.stderr, flush=True)
