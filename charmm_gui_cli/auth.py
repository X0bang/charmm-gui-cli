"""Compatibility import; implementation lives in store.auth."""
import sys
from .store import auth as _implementation
sys.modules[__name__] = _implementation
