"""Token-file compatibility. Decoding a JWT does not verify its signature."""

import base64
import json
import math
import os
from pathlib import Path
import tempfile
import time


class ToolError(Exception):
    """A user-facing error that never includes credentials or response bodies."""


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
            raise ToolError("Token file contains invalid JSON.") from None
    if not isinstance(raw, str):
        raise ToolError("Expected a token string or a JSON object with a token field.")
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) != 3 or not all(parts) or any(c.isspace() for c in raw):
        raise ToolError("Expected an API JWT, not a Cookie or filename. Use --token-file PATH.")
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if not isinstance(claims, dict):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ToolError("Cannot decode API JWT payload; check the token file.") from None
    return raw, claims


def token_info(token):
    _, claims = normalize_token(token)
    expiry = claims.get("exp")
    if expiry is not None and (
        isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)
    ):
        raise ToolError("JWT exp claim is not a finite timestamp.")
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
            raise ToolError("Cannot read the explicitly selected token file.") from None
        source = str(selected)
    elif os.environ.get("CHARMMGUI_TOKEN"):
        raw, source = os.environ["CHARMMGUI_TOKEN"], "CHARMMGUI_TOKEN"
    else:
        # A new `login` must not be shadowed by an expired project-local token.
        # Legacy files remain readable only when no saved global login exists.
        candidates = [default_token_path(), Path("session.token"), Path.home() / ".charmmgui_token"]
        selected = next((p for p in candidates if p.is_file()), None)
        if selected is None:
            raise ToolError("No API token found. Run login or use --token-file PATH.")
        try:
            raw = selected.read_text(encoding="utf-8")
        except OSError:
            raise ToolError("Cannot read the discovered token file; select --token-file explicitly.") from None
        source = str(selected)
    token, _ = normalize_token(raw)
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
