"""Compatibility import; implementation lives in remote.http_retry."""
import sys
from .remote import http_retry as _implementation
sys.modules[__name__] = _implementation
