"""Bounded diagnostics from error fields, never whole responses or credentials."""

import re
from urllib.parse import unquote

from bs4 import BeautifulSoup, Comment

_SECRET_KEY = re.compile(r"password|passwd|\bpwd\b|token|cookie|session|csrf|authorization|secret|api[_-]?key", re.I)
_FIELDS = frozenset(("error", "errors", "message", "msg", "detail", "details", "description", "reason", "error_message", "errmsg"))


def secret_values(value):
    """Collect sensitive sibling/nested values before rendering error text."""
    values = []
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                    values.append(str(item))
                elif isinstance(item, (dict, list)):
                    values.extend(_leaves(item))
            else:
                values.extend(secret_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(secret_values(item))
    return values


def _leaves(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaves(item)]
    return []


def _messages(value):
    if isinstance(value, str):
        return [value] if len(value) <= 65536 else []
    if isinstance(value, dict):
        return [message for key, item in value.items() if str(key).lower() in _FIELDS
                for message in _messages(item)]
    if isinstance(value, list):
        return [message for item in value[:100] for message in _messages(item)]
    return []


def diagnostic(value, *, secrets=(), limit=300):
    """Sanitize completely before truncation; skip unknown structured fields."""
    text = " ".join(_messages(value))
    if not text:
        return None
    soup = BeautifulSoup(text, "html.parser")
    protected = list(secrets) + secret_values(value)
    for field in soup.select("input, textarea, select"):
        if _SECRET_KEY.search(field.get("name", "")) or field.get("type", "").lower() == "password":
            protected.extend((field.get("value", ""), field.get_text()))
    for node in soup.select("script, style, input, textarea, select"):
        node.decompose()
    for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
        node.extract()
    text = unquote(soup.get_text(" ", strip=True))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    # Redact complete header lines before whitespace normalization, and bearer
    # values before their label (otherwise only 'Bearer' could be removed).
    text = re.sub(r"(?im)\b(?:set-cookie|cookie|authorization)\s*[:=]\s*[^\r\n]+", "[redacted]", text)
    text = re.sub(r"(?i)\bBearer\s+\S+|\beyJ[\w-]+\.[\w-]+\.[\w-]+", "[redacted]", text)
    for secret in sorted({item for item in protected if isinstance(item, str) and item}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    label = r"(?:password|passwd|pwd|token|cookie|session|csrf|authorization|secret|api[_-]?key)[\w-]*"
    text = re.sub(r"(?i)\b" + label + r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", "[redacted]", text)
    text = re.sub(r"(?i)\b(?:password|token|cookie|secret|csrf|session)[_-][\w.-]+", "[redacted]", text)
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redacted]@", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return " ".join(text.split())[:limit] or None
