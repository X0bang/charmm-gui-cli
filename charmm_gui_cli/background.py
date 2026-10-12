"""Compatibility import; implementation lives in store.background."""
import sys
from .store import background as _implementation
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    sys.exit(_implementation.worker(sys.argv[1], int(sys.argv[2])))
