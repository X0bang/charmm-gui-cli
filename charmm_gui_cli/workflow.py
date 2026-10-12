"""Compatibility import; implementation lives in pipeline.workflow."""
import sys
from .pipeline import workflow as _implementation
sys.modules[__name__] = _implementation
