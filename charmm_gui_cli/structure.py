"""Compatibility import; implementation lives in chem.structure."""
import sys
from .chem import structure as _implementation
sys.modules[__name__] = _implementation
