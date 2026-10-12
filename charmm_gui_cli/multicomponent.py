"""Compatibility import; implementation lives in chem.multicomponent."""
import sys
from .chem import multicomponent as _implementation
sys.modules[__name__] = _implementation
