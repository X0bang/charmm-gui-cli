"""Compatibility import; implementation lives in pipeline.sweep."""
import sys
from .pipeline import sweep as _implementation
sys.modules[__name__] = _implementation
