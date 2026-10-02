"""Non-mutating installation checks for the supported Linux CLI workflow."""

import importlib
import platform
import shutil
import sys

from . import __version__
from .auth import default_cookie_path, default_token_path


def inspect_installation(gmx="gmx"):
    checks = {
        "python": {"passed": sys.version_info >= (3, 10), "version": platform.python_version()},
        "platform": {"passed": sys.platform in ("linux", "darwin"), "system": platform.system()},
    }
    for module in ("requests", "yaml", "bs4", "numpy", "rdkit"):
        try:
            imported = importlib.import_module(module)
            checks[module] = {"passed": True, "version": getattr(imported, "__version__", "installed")}
        except (ImportError, OSError):
            checks[module] = {"passed": False, "fix": "Re-run install.sh to install the full isolated environment."}
    executable = shutil.which(gmx)
    return {"version": __version__, "passed": all(c["passed"] for c in checks.values()),
            "checks": checks,
            "gromacs": {"available": executable is not None, "executable": executable,
                        "required": False, "scope": "Optional input compilation; not required for remote model building"},
            "session": {"token_saved": default_token_path().is_file(),
                        "cookie_saved": default_cookie_path().is_file(),
                        "server_checked": False},
            "next_step": "charmm-gui-cli login; then charmm-gui-cli build -h"}
