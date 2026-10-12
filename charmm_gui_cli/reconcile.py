"""Compatibility import; implementation lives in remote.reconcile."""
import sys
from .remote import reconcile as _implementation
sys.modules[__name__] = _implementation
