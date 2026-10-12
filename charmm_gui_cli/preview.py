"""Compatibility import; implementation lives in chem.preview."""
import sys
from .chem import preview as _implementation
sys.modules[__name__] = _implementation
