"""Compatibility import; implementation lives in commands.build_command."""
import sys
from .commands import build_command as _implementation
sys.modules[__name__] = _implementation
