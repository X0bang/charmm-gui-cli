"""Compatibility import; implementation lives in pipeline.progress."""
import sys
from .pipeline import progress as _implementation
sys.modules[__name__] = _implementation
