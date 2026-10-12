"""Compatibility import; implementation lives in store.run_logging."""
import sys
from .store import run_logging as _implementation
sys.modules[__name__] = _implementation
