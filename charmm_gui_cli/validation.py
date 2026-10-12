"""Compatibility import; implementation lives in chem.validation."""
import sys
from .chem import validation as _implementation
sys.modules[__name__] = _implementation
