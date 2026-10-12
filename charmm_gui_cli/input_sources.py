"""Compatibility import; implementation lives in chem.input_sources."""
import sys
from .chem import input_sources as _implementation
sys.modules[__name__] = _implementation
