"""Compatibility import; implementation lives in chem.precheck."""
import sys
from .chem import precheck as _implementation
sys.modules[__name__] = _implementation
