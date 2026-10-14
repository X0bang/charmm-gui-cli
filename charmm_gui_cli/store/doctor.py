"""Non-mutating installation and dependency diagnostics."""

import importlib
import platform
import shutil
import sys
import os
import importlib.util

from .. import __version__
from .auth import ToolError, default_cookie_path, default_token_path, load_token, token_info


def inspect_installation(gmx="gmx", token_file=None):
    checks = {
        "python": {"passed": sys.version_info >= (3, 10), "version": platform.python_version()},
        "platform": {"passed": sys.platform in ("linux", "darwin"), "system": platform.system()},
    }
    for module in ("requests", "yaml", "bs4", "numpy", "PIL", "rdkit"):
        try:
            imported = importlib.import_module(module)
            checks[module] = {"passed": True, "version": getattr(imported, "__version__", "installed")}
        except (ImportError, OSError):
            checks[module] = {"passed": False, "fix": "Re-run install.sh to install the full isolated environment."}
    executable = shutil.which(gmx)
    try:
        keyring_available = importlib.util.find_spec("keyring") is not None
    except (ImportError, ValueError):
        keyring_available = False
    session = {"token_saved": default_token_path().is_file(), "cookie_saved": default_cookie_path().is_file(), "server_checked": False}
    try:
        token, source = load_token(token_file)
        session.update(source=source, **token_info(token))
    except ToolError as exc:
        session["error_key"] = exc.key
    return {"version": __version__, "passed": all(c["passed"] for c in checks.values()),
            "checks": checks,
            "gromacs": {"available": executable is not None, "executable": executable,
                        "required": False, "scope": "Optional input compilation; not required for remote model building"},
            "session": session,
            "credential_store": {"library_available": keyring_available,
                                 "password_command_configured": bool(os.environ.get("CHARMM_GUI_PASSWORD_COMMAND"))},
            "next_step": "charmm-gui-cli login; then charmm-gui-cli build -h"}
