"""Compatibility import; implementation lives in locales.helptext."""
import sys
from .locales import helptext as _implementation
sys.modules[__name__] = _implementation
