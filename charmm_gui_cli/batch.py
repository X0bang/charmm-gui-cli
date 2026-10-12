"""Compatibility import; implementation lives in pipeline.batch."""
import sys
from .pipeline import batch as _implementation
sys.modules[__name__] = _implementation
