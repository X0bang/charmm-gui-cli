"""Small bilingual catalog; parse environment strings without OS locale APIs."""

import os


MESSAGES = {
    "cli.error": {"zh": "错误：{message}", "en": "Error: {message}"},
    "cli.next": {"zh": "下一步：{action}", "en": "Next: {action}"},
    "cli.filesystem": {
        "zh": "文件系统操作失败；请检查路径、权限和剩余空间。",
        "en": "Filesystem operation failed; check file paths, permissions and free space.",
    },
    "cli.interrupted": {"zh": "操作中断。", "en": "Operation interrupted. "},
}

_language = None


def _normalize(value):
    value = value.strip().split(".", 1)[0].split("@", 1)[0].replace("-", "_").lower()
    if value in ("", "c", "posix"):
        return None
    return "zh" if value.startswith("zh") else "en"


def detect_lang(explicit=None, *, environ=None):
    """Pure detection, including neutral-locale fallthrough; never changes locale."""
    if explicit is not None:
        if explicit not in ("zh", "en"):
            raise ValueError("Language must be zh or en.")
        return explicit
    env = os.environ if environ is None else environ
    for variable in ("CHARMM_GUI_CLI_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        raw = env.get(variable, "")
        for value in raw.split(":") if variable == "LANGUAGE" else (raw,):
            language = _normalize(value)
            if language is not None:
                return language
    return "en"


def set_language(explicit=None):
    """Set the process cache at CLI startup (also supports embedded invocation)."""
    global _language
    _language = detect_lang(explicit)
    return _language


def get_language():
    global _language
    if _language is None:
        _language = detect_lang()
    return _language


def t(key, **params):
    versions = MESSAGES.get(key, {})
    template = versions.get(get_language()) or versions.get("en") or key
    return template.format(**params)
