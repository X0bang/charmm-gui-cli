"""Compatibility import; implementation lives in commands.reporting."""
import sys
from .commands import reporting as _implementation
sys.modules[__name__] = _implementation
