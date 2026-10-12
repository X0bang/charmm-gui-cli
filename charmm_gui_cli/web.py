"""Compatibility import; implementation lives in remote.web."""
import sys
from .remote import web as _implementation
sys.modules[__name__] = _implementation
