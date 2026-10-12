"""Compatibility import; implementation lives in pipeline.archive_transport."""
import sys
from .pipeline import archive_transport as _implementation
sys.modules[__name__] = _implementation
