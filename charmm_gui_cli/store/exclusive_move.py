"""No-overwrite directory moves; refuse unsupported platforms/filesystems.

Linux: https://man7.org/linux/man-pages/man2/rename.2.html
macOS: https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/stdio.h
No copy/delete or ordinary replacing-rename fallback is permitted.
"""

import ctypes
import errno
import os
from pathlib import Path
import sys


def move_exclusive(source, destination):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux') and hasattr(library, 'renameat2'):
        function = library.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == 'darwin' and hasattr(library, 'renamex_np'):
        function = library.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    else:
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP), str(source), None, str(destination))
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(*arguments) != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code), str(source), None, str(destination))
