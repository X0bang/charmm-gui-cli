"""Safe, actionable filesystem diagnostics shared by CLI and saved failures."""

import errno
from pathlib import Path
import shlex

from .auth import ToolError
from ..locales.i18n import MESSAGES, t


def filesystem_error(error):
    """Never render exception text, URLs or response bodies as diagnostics."""
    value = getattr(error, "filename", None)
    if not isinstance(value, (str, Path)) or not str(value) or "://" in str(value):
        return ToolError(key="cli.filesystem", category="filesystem_error",
                         next_step=t("filesystem.inspect", command="pwd"))
    value = str(value)
    # Terminal controls cannot be injected through an operating-system path.
    path = "".join(char if char.isprintable() else "?" for char in value)[:1024]
    reason = {errno.EACCES: "permission", errno.EPERM: "permission", errno.EROFS: "readonly",
              errno.ENOSPC: "space", errno.ENOENT: "missing", errno.ENOTDIR: "not_directory",
              errno.EISDIR: "directory"}.get(error.errno, "operation")
    target = Path(path).expanduser().absolute()
    command = shlex.join(["df", "-h", str(target.parent)]) if reason == "space" else shlex.join(["ls", "-ld", str(target.parent)])
    return ToolError(MESSAGES["cli.filesystem"]["en"], key="filesystem." + reason, params={"path": path}, category="filesystem_error",
                     next_step=t("filesystem.inspect", command=command))
