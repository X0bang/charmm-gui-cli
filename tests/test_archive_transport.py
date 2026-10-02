import gzip
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli.archive_transport import normalize_download
from charmm_gui_cli.auth import ToolError


def packed_tar(payload=b"test\n"):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo("step5_assembly.pdb")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return stream.getvalue()


class ArchiveTransportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-archive-transport-test-"))
        self.raw = self.root / "response.raw"
        self.destination = self.root / "archive.tgz"

    def normalize(self, payload):
        with self.raw.open("xb") as handle:
            handle.write(payload)
        return normalize_download(self.raw, self.destination)

    def test_plain_gzip_is_preserved_exactly(self):
        data = packed_tar()
        report = self.normalize(data)
        self.assertEqual(self.destination.read_bytes(), data)
        self.assertEqual(self.raw.read_bytes(), data)
        self.assertEqual(report["footer"], "none")
        self.assertEqual(report["compressed_bytes"], len(data))

    def test_exact_decimal_length_footer_removed_without_recompressing(self):
        data = packed_tar()
        report = self.normalize(data + str(len(data)).encode())
        self.assertEqual(self.destination.read_bytes(), data)
        self.assertEqual(report["footer"], "compressed_byte_length")
        self.assertGreater(self.raw.stat().st_size, self.destination.stat().st_size)

    def test_wrong_footer_is_rejected(self):
        with self.assertRaisesRegex(ToolError, "footer"):
            self.normalize(packed_tar() + b"123")
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.raw.exists())

    def test_truncated_gzip_is_rejected(self):
        with self.assertRaisesRegex(ToolError, "truncated"):
            self.normalize(packed_tar()[:-4])
        self.assertFalse(self.destination.exists())

    def test_bad_crc_is_rejected(self):
        data = bytearray(packed_tar())
        data[-8] ^= 1
        with self.assertRaisesRegex(ToolError, "CRC"):
            self.normalize(data)
        self.assertFalse(self.destination.exists())

    def test_multiple_gzip_members_are_rejected(self):
        with self.assertRaisesRegex(ToolError, "multiple gzip"):
            self.normalize(packed_tar() + gzip.compress(b"second member"))

    def test_valid_gzip_with_non_tar_payload_is_rejected(self):
        with self.assertRaisesRegex(ToolError, "tar archive"):
            self.normalize(gzip.compress(b"not a tar file"))

    def test_existing_destination_never_overwritten(self):
        with self.destination.open("xb") as handle:
            handle.write(b"keep")
        with self.assertRaisesRegex(ToolError, "overwrite"):
            self.normalize(packed_tar())
        self.assertEqual(self.destination.read_bytes(), b"keep")

    def test_highly_compressible_payload_uses_bounded_decompression(self):
        data = packed_tar(b"A" * (5 * 1024 * 1024))
        report = self.normalize(data + str(len(data)).encode())
        self.assertEqual(self.destination.read_bytes(), data)
        self.assertGreaterEqual(report["decoded_bytes"], 5 * 1024 * 1024)

    def test_gzip_and_decimal_footer_across_tiny_chunk_boundaries(self):
        data = packed_tar(b"abc123" * 100)
        with patch("charmm_gui_cli.archive_transport.CHUNK_SIZE", 13):
            report = self.normalize(data + str(len(data)).encode())
        self.assertEqual(report["compressed_bytes"], len(data))
        self.assertEqual(self.destination.read_bytes(), data)

    def test_length_footer_with_newline_is_not_accepted(self):
        data = packed_tar()
        with self.assertRaisesRegex(ToolError, "footer"):
            self.normalize(data + str(len(data)).encode() + b"\n")


if __name__ == "__main__":
    unittest.main()
