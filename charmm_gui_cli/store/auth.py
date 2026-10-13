"""Token-file compatibility. Decoding a JWT does not verify its signature."""

import base64
import json
import math
import os
import re
import shlex
from pathlib import Path
import tempfile
import time

from ..i18n import MESSAGES, t


def recovery_guidance(category, *, cause_category=None):
    """Credential-free actions; unknown submissions always take priority."""
    if category == "submission_unknown":
        login = t("recovery.login_first") if cause_category in ("authentication", "auth_required", "access_denied") else ""
        return login + t("recovery.submission_unknown")
    keys = {"authentication": "authentication", "auth_required": "authentication",
            "access_denied": "access_denied", "transport_error": "transport_error",
            "archive_preparing": "archive_preparing", "transient_network": "transient",
            "transient_http": "transient", "rate_limited": "transient",
            "interrupted": "interrupted", "wait_timeout": "wait_timeout"}
    key = keys.get(category)
    if key is None:
        return None
    message = t("recovery." + key)
    return message.rstrip() + " " + t("recovery.resume").lstrip() if key in ("transport_error", "transient") else message


class ToolError(Exception):
    """Structured errors; remote diagnostics are sanitized at the HTTP boundary."""

    def __init__(self, message=None, *, key=None, params=None, category="operation_error", next_step=None, retryable=False,
                 retry_after_seconds=None, cause_category=None, server_message=None, details=None):
        self.key = key
        self.params = dict(params or {})
        # Keep str(error) and stored error messages stable for old consumers.
        if message is None:
            message = MESSAGES.get(key, {}).get("en", key or "operation_error").format(**self.params)
        super().__init__(message)
        self.category = category
        self.next_step = next_step or ("charmm-gui-cli login" if key == "login.bad_creds" else
                                      recovery_guidance(category, cause_category=cause_category)) or "charmm-gui-cli -h"
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.cause_category = cause_category
        self.server_message = server_message
        self.details = list(details or [])

    def localized(self):
        message = t(self.key, **self.params) if self.key else str(self)
        if self.server_message and self.server_message not in message:
            message += " " + t("error.server", message=self.server_message)
        for detail in self.details:
            if isinstance(detail, dict) and detail.get("key") in MESSAGES:
                message += "\n" + t(detail["key"], **detail.get("params", {}))
        return message

    def as_dict(self):
        result = {"message": str(self), "category": self.category,
                  "next_step": self.next_step, "retryable": self.retryable}
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = self.retry_after_seconds
        if self.cause_category is not None:
            result["cause_category"] = self.cause_category
        if self.server_message is not None:
            result["server_message"] = self.server_message
        if self.details:
            result["details"] = self.details
        if self.key:
            result["message_key"] = self.key
            result["message_params"] = self.params
        return result

    def for_task(self, target):
        """Bind existing guidance without changing submission/retry semantics."""
        self.next_step = re.sub(r"\bNAME\b", lambda match: shlex.quote(str(target)), self.next_step)
        return self


def default_token_path():
    return Path.home() / ".config" / "charmm-gui-cli" / "session.token"


def default_cookie_path():
    return default_token_path().with_name("session.cookies.json")


def resolve_cookie_path(path=None, token_source=None):
    """Keep website and API sessions together; never mix directory fallbacks."""
    if path is not None:
        return Path(path).expanduser()
    if token_source is not None and token_source != "CHARMMGUI_TOKEN":
        return Path(token_source).expanduser().with_name("session.cookies.json")
    return default_cookie_path()


def normalize_token(raw):
    raw = raw.strip()
    if raw.startswith("{") or raw.startswith('"'):
        try:
            value = json.loads(raw)
            raw = value.get("token") if isinstance(value, dict) else value
        except (ValueError, TypeError):
            raise ToolError(key="auth.invalid_json") from None
    if not isinstance(raw, str):
        raise ToolError(key="auth.expected_string")
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) != 3 or not all(parts) or any(c.isspace() for c in raw):
        raise ToolError(key="auth.expected_jwt")
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if not isinstance(claims, dict):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ToolError(key="auth.decode_failed") from None
    return raw, claims


def token_info(token):
    _, claims = normalize_token(token)
    expiry = claims.get("exp")
    if expiry is not None and (
        isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)
    ):
        raise ToolError(key="auth.expiry_invalid")
    return {
        "format": "JWT",
        "signature_verified_locally": False,
        "expires_at_unix": expiry,
        "expired": expiry is not None and expiry <= time.time(),
    }


def load_token(path=None):
    if path is not None:
        selected = Path(path).expanduser()
        try:
            raw = selected.read_text(encoding="utf-8")
        except OSError:
            raise ToolError(key="auth.read_explicit", category="authentication") from None
        source = str(selected)
    elif os.environ.get("CHARMMGUI_TOKEN"):
        raw, source = os.environ["CHARMMGUI_TOKEN"], "CHARMMGUI_TOKEN"
    else:
        # A new `login` must not be shadowed by an expired project-local token.
        # Legacy files remain readable only when no saved global login exists.
        candidates = [default_token_path(), Path("session.token"), Path.home() / ".charmmgui_token"]
        selected = next((p for p in candidates if p.is_file()), None)
        if selected is None:
            raise ToolError(key="auth.missing", category="authentication")
        try:
            raw = selected.read_text(encoding="utf-8")
        except OSError:
            raise ToolError(key="auth.read_discovered", category="authentication") from None
        source = str(selected)
    try:
        token, _ = normalize_token(raw)
    except ToolError as error:
        raise ToolError(str(error), key=error.key, params=error.params, category="authentication") from None
    return token, source


def atomic_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".cgui-", dir=path.parent)
    temporary = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    # Failed writes are retained for recovery; never delete user/run artifacts.
    os.replace(temporary, path)
