"""Legacy import shim; targets are centralized in _compat.MODULE_ALIASES."""
import sys
from .._compat import install_alias
_implementation = install_alias('charmm_gui_cli.store.background' if __name__ == "__main__" else __name__)

if __name__ == "__main__":
    sys.exit(_implementation.worker(sys.argv[1], int(sys.argv[2])))
