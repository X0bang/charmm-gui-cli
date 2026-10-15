"""Native non-replacing directory moves, with no copy/delete fallback."""

import ctypes
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from charmm_gui_cli.store.exclusive_move import move_exclusive


class ExclusiveMoveTests(unittest.TestCase):
    def setUp(self):
        self.base=Path(tempfile.mkdtemp(prefix='cgui-exclusive-move-'))
        self.source,self.destination=self.base/'source 中文',self.base/'target 中文'
        self.source.mkdir()
        (self.source/'keep').write_bytes(b'coordinate fixture')

    @unittest.skipUnless(sys.platform.startswith('linux') or sys.platform=='darwin','Native move targets Linux/macOS')
    def test_real_directory_inode_and_contents_survive_without_a_copy(self):
        inode=self.source.stat().st_ino
        move_exclusive(self.source,self.destination)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.destination.stat().st_ino,inode)
        self.assertEqual((self.destination/'keep').read_bytes(),b'coordinate fixture')

    @unittest.skipUnless(sys.platform.startswith('linux') or sys.platform=='darwin','Native move targets Linux/macOS')
    def test_even_an_empty_foreign_destination_or_symlink_cannot_be_replaced(self):
        self.destination.mkdir()
        inode=self.destination.stat().st_ino
        with self.assertRaises(OSError) as error: move_exclusive(self.source,self.destination)
        self.assertEqual(error.exception.errno,errno.EEXIST)
        self.assertEqual(self.destination.stat().st_ino,inode)
        self.destination.rename(self.base/'retained-target')
        self.destination.symlink_to(self.base/'missing-target',target_is_directory=True)
        with self.assertRaises(OSError): move_exclusive(self.source,self.destination)
        self.assertTrue(self.destination.is_symlink())
        self.assertTrue((self.source/'keep').is_file())

    def test_linux_and_macos_signatures_use_the_documented_exclusive_flags(self):
        for platform,name,expected in (('linux','renameat2',(-100,os.fsencode(self.source),-100,os.fsencode(self.destination),1)),
                                       ('darwin','renamex_np',(os.fsencode(self.source),os.fsencode(self.destination),4))):
            function=Mock(return_value=0)
            library=type('Library',(),{name:function})()
            with patch('sys.platform',platform),patch('ctypes.CDLL',return_value=library):
                move_exclusive(self.source,self.destination)
            self.assertEqual(function.call_args.args,expected)
            self.assertEqual(function.restype,ctypes.c_int)

    def test_unsupported_system_or_filesystem_never_falls_back_to_replacing_rename(self):
        with patch('sys.platform','unsupported'),patch('pathlib.Path.rename') as rename:
            with self.assertRaises(OSError) as error: move_exclusive(self.source,self.destination)
            self.assertEqual(error.exception.errno,errno.ENOTSUP)
            rename.assert_not_called()
        def rejected(*args):
            ctypes.set_errno(errno.EINVAL)
            return -1
        with patch('sys.platform','linux'),patch('ctypes.CDLL',return_value=type('Library',(),{'renameat2':Mock(side_effect=rejected)})()), \
                patch('pathlib.Path.rename') as rename:
            with self.assertRaises(OSError) as error: move_exclusive(self.source,self.destination)
            self.assertEqual(error.exception.errno,errno.EINVAL)
            rename.assert_not_called()
        self.assertTrue((self.source/'keep').is_file())


if __name__=='__main__':
    unittest.main()
