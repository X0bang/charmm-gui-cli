"""Compatibility import; implementation lives in chem.pdb_normalization."""
import sys
from .chem import pdb_normalization as _implementation
sys.modules[__name__] = _implementation
