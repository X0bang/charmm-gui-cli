"""Compatibility import; implementation lives in store.layout."""
import sys
from .store import layout as _implementation
sys.modules[__name__] = _implementation
