"""Compatibility import; implementation lives in chem.inputs."""
import sys
from .chem import inputs as _implementation
sys.modules[__name__] = _implementation
