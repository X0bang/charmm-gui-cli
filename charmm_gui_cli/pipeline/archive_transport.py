"""Legacy import shim; targets are centralized in _compat.MODULE_ALIASES."""
from .._compat import install_alias
install_alias(__name__)
