"""Compatibility import; implementation lives in commands.terminal_output."""
import sys
from .commands import terminal_output as _implementation
sys.modules[__name__] = _implementation
