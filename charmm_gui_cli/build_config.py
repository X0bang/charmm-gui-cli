"""Compatibility import; implementation lives in pipeline.build_config."""
import sys
from .pipeline import build_config as _implementation
sys.modules[__name__] = _implementation
