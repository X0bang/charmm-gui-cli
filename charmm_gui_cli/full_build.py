"""Compatibility import; implementation lives in pipeline.full_build."""
import sys
from .pipeline import full_build as _implementation
sys.modules[__name__] = _implementation
