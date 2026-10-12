"""Compatibility import; implementation lives in store.delivery."""
import sys
from .store import delivery as _implementation
sys.modules[__name__] = _implementation
