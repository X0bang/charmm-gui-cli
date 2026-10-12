"""Compatibility import; implementation lives in locales.i18n."""
import sys
from .locales import i18n as _implementation
sys.modules[__name__] = _implementation
