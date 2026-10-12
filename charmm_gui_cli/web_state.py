"""Compatibility import; implementation lives in remote.web_state."""
import sys
from .remote import web_state as _implementation
sys.modules[__name__] = _implementation
