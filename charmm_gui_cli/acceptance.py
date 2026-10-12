"""Compatibility import; implementation lives in pipeline.acceptance."""
import sys
from .pipeline import acceptance as _implementation
sys.modules[__name__] = _implementation
