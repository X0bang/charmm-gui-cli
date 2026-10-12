"""Compatibility import; implementation lives in pipeline.artifacts."""
import sys
from .pipeline import artifacts as _implementation
sys.modules[__name__] = _implementation
