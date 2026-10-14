"""Compatibility import; implementation lives in remote.archive_transport."""
import sys
from ..remote import archive_transport as _implementation
sys.modules[__name__] = _implementation
