"""Compatibility import; implementation lives in pipeline.config."""
import sys
from .pipeline import config as _implementation
sys.modules[__name__] = _implementation
