"""Immutable archive ownership and receipt verification; no HTTP."""

import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import zlib
from .auth import ToolError
from .identifiers import job_id
from .layout import archive_path as run_archive_path
from .run_manifest import read

def verify_archive_provenance(directory, manifest=None, *, create_receipt=False):
    """Bind an immutable download to its build job; never repair mismatches.

    Legacy downloads need a previously recorded hash and a matching official
    archive root. An interrupted first download can be sealed only after its
    durable downloading intent after done or the observed compressing status.
    Read-only consumers never bootstrap it.
    """
    directory = Path(directory)
    manifest = read(directory) if manifest is None else manifest

    def reject(message):
        raise ToolError(message, category="archive_provenance",
                        next_step="Inspect this run's job ID, archive and download-receipt.json; retain all files and do not replace or resubmit automatically.")

    identifier = job_id(manifest.get("job_id"))
    archive_path = run_archive_path(directory)
    receipt_path = directory / "download-receipt.json"
    if archive_path.is_symlink() or receipt_path.is_symlink():
        reject("Archive and download receipt must be regular local files, not symlinks.")
    recorded_hash = manifest.get("archive_sha256")
    if "archive_sha256" in manifest and not (
            isinstance(recorded_hash, str) and re.fullmatch(r"[0-9a-f]{64}", recorded_hash)):
        reject("The recorded archive SHA256 is invalid; refusing to replace it.")
    try:
        digest = hashlib.sha256()
        with archive_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        actual_hash = digest.hexdigest()
        if recorded_hash is not None and recorded_hash != actual_hash:
            reject("Archive SHA256 mismatch: the downloaded archive has changed since it was recorded.")
        receipt = None
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                reject("Invalid download receipt.")
        elif recorded_hash is None and not (
                create_receipt and manifest.get("state") == "downloading"
                and (manifest.get("remote_status") == "done" or
                     (manifest.get("remote_status") == "archive_preparing" and manifest.get("raw_remote_status") == "compressing"))):
            reject("Existing archive has no download receipt or previously recorded SHA256; ownership cannot be established.")

        expected_root = "charmm-gui-" + identifier
        found_file = False
        # Exhaust gzip as well as tar to verify the CRC/trailer on legacy files.
        with gzip.open(archive_path, "rb") as decoded:
            with tarfile.open(fileobj=decoded, mode="r|") as archive:
                for index, member in enumerate(archive):
                    if index >= 50000:
                        reject("Archive has too many members to verify ownership.")
                    name = member.name
                    parts = PurePosixPath(name).parts
                    if (not name or name.startswith("/") or "\\" in name
                            or ".." in parts or not (member.isfile() or member.isdir())):
                        reject("Archive contains an unsafe path or unsupported member type.")
                    if not parts and member.isdir():
                        continue
                    if not parts or parts[0] != expected_root:
                        reject("Archive root does not match build job " + identifier + ".")
                    if member.isfile():
                        if len(parts) < 2:
                            reject("Archive root must be a directory containing result files.")
                        found_file = True
            for _ in iter(lambda: decoded.read(1024 * 1024), b""):
                pass
        if not found_file:
            reject("Archive contains no result files under its build job root.")
        expected = {"version": 1, "job_id": identifier,
                    "archive_sha256": actual_hash, "archive_root": expected_root}
        if receipt is not None and receipt != expected:
            reject("Download receipt does not match the current job and archive.")
        if receipt is None and create_receipt:
            # Exclusive hard-link publication cannot overwrite an old receipt.
            # Retain the staging file in accordance with no-deletion operation.
            descriptor, staged = tempfile.mkstemp(prefix=".download-receipt-", dir=directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(expected, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(staged, receipt_path)
            except FileExistsError:
                if json.loads(receipt_path.read_text(encoding="utf-8")) != expected:
                    reject("A different download receipt already exists; refusing to overwrite it.")
        return expected
    except (OSError, ValueError, EOFError, tarfile.TarError, zlib.error) as exc:
        reject("Cannot verify archive ownership/integrity: " + type(exc).__name__ + ".")
