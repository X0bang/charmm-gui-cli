"""Compatibility import; implementation lives in store.managed."""
import sys
from .store import managed as _implementation
sys.modules[__name__] = _implementation
