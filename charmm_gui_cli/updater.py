"""Compatibility import; implementation lives in commands.updater."""
import sys
from .commands import updater as _implementation
sys.modules[__name__] = _implementation
