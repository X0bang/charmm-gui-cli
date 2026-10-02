"""Normalize CHARMM-GUI download transport without changing archive contents."""

import os
from pathlib import Path
import tarfile
import tempfile
import zlib

from .auth import ToolError

CHUNK_SIZE = 1024 * 1024


def normalize_download(raw_path, destination):
    """Validate and atomically publish one gzip/tar, preserving its raw response.

    The server sometimes appends the ASCII byte length of its gzip file. Only
    that exact decimal footer (or no footer) is accepted. CRC/ISIZE checking is
    performed by zlib before any publication. Concatenated gzip members and all
    other trailers are rejected. Memory use is bounded by chunk size; decoded
    molecular data are neither altered nor extracted.

    Both raw and staging files are retained, including on failure. Destination
    is created by a hard link, so existing files cannot be overwritten.
    """
    raw_path, destination = Path(raw_path), Path(destination)
    if destination.exists():
        raise ToolError("Archive destination already exists; refusing to overwrite it.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".normalized-", dir=destination.parent)
    staging = Path(name)
    compressed_bytes = decoded_bytes = 0
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    footer = b""
    try:
        with os.fdopen(fd, "wb") as output, raw_path.open("rb") as source:
            while not decoder.eof:
                data = source.read(CHUNK_SIZE)
                if not data:
                    raise ToolError("Downloaded gzip archive is truncated; complete CRC/trailer was not found.")
                pending = data
                while pending and not decoder.eof:
                    decoded = decoder.decompress(pending, CHUNK_SIZE)
                    decoded_bytes += len(decoded)
                    remainder = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
                    consumed = len(pending) - len(remainder)
                    output.write(pending[:consumed])
                    compressed_bytes += consumed
                    pending = remainder
                if decoder.eof:
                    # Bound the read: an accepted decimal footer cannot exceed
                    # the number of digits in the actual compressed byte count.
                    expected = str(compressed_bytes).encode("ascii")
                    footer = decoder.unused_data + source.read(len(expected) + 1)
                    if footer not in (b"", expected):
                        raise ToolError("Downloaded gzip archive has an unsupported footer or multiple gzip members; only the exact compressed byte length is accepted.")
            output.flush()
            os.fsync(output.fileno())
        members = 0
        with tarfile.open(staging, mode="r|gz") as archive:
            for _ in archive:
                members += 1
        if not members:
            raise ToolError("Downloaded archive is empty.")
    except zlib.error:
        raise ToolError("Downloaded archive is not a valid gzip stream or its CRC/length check failed.") from None
    except (tarfile.TarError, EOFError):
        raise ToolError("Downloaded gzip stream does not contain a valid tar archive.") from None
    try:
        os.link(staging, destination)
    except FileExistsError:
        raise ToolError("Archive destination already exists; refusing to overwrite it.") from None
    return {"archive": str(destination.resolve()), "raw_response": str(raw_path.resolve()),
            "normalized_staging": str(staging.resolve()), "compressed_bytes": compressed_bytes,
            "decoded_bytes": decoded_bytes, "members": members,
            "footer": "compressed_byte_length" if footer else "none"}
