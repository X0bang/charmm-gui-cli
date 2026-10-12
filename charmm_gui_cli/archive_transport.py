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

    Failed responses/staging files are retained. After successful publication,
    the staging link and an owned .download-* response are removed. Explicit
    non-temporary raw inputs are preserved. Existing destinations are never overwritten.
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
    staging.unlink()
    if raw_path.parent.resolve() == destination.parent.resolve() and raw_path.name.startswith(".download-") and not raw_path.is_symlink():
        raw_path.unlink()
    return {"archive": str(destination.resolve()), "raw_response": str(raw_path.resolve()),
            "normalized_staging": str(staging.resolve()), "compressed_bytes": compressed_bytes,
            "decoded_bytes": decoded_bytes, "members": members,
            "footer": "compressed_byte_length" if footer else "none"}


def cleanup_download_duplicates(destination):
    """Remove only named temporary files matching an already verified archive.

    Older clients retained these files. Unknown/failed responses, symlinks and
    unrelated files are preserved; no recursive deletion or broad directory cleanup.
    """
    import hashlib
    destination = Path(destination)
    if destination.is_symlink() or not destination.is_file():
        return 0
    size = destination.stat().st_size
    digest = hashlib.sha256()
    with destination.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    expected = digest.digest()
    removed = 0
    for prefix in (".download-", ".normalized-"):
        for candidate in destination.parent.glob(prefix + "*"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            before = candidate.stat()
            if before.st_size not in (size, size + len(str(size))):
                continue
            observed = hashlib.sha256()
            remaining = size
            with candidate.open("rb") as source:
                while remaining:
                    chunk = source.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    observed.update(chunk)
                    remaining -= len(chunk)
                footer = source.read(len(str(size)) + 1)
            after = candidate.stat()
            if (remaining == 0 and observed.digest() == expected and footer in (b"", str(size).encode("ascii"))
                    and (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns)
                    == (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns)):
                candidate.unlink()
                removed += 1
    return removed
