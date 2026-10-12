"""Compatibility import; implementation lives in commands.doctor."""
import sys
from .commands import doctor as _implementation
sys.modules[__name__] = _implementation
