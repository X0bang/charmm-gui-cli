"""Compatibility import; implementation lives in store.doctor."""
import sys
from .store import doctor as _implementation
sys.modules[__name__] = _implementation
