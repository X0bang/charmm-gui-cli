"""Compatibility import; implementation lives in remote.api."""
import sys
from .remote import api as _implementation
sys.modules[__name__] = _implementation
