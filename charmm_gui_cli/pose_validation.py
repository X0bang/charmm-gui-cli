"""Compatibility import; implementation lives in chem.pose_validation."""
import sys
from .chem import pose_validation as _implementation
sys.modules[__name__] = _implementation
