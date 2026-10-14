"""Verified public-source updates via the existing isolated user installer.

No CHARMM-GUI session is loaded. Old releases and failed downloads are retained.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlparse

import requests

from .. import __version__
from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.locales.i18n import t


REPOSITORY = "X0bang/charmm-gui-cli"
REFERENCE_URL = f"https://api.github.com/repos/{REPOSITORY}/git/ref/heads/main"
RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RAW_BASE = f"https://raw.githubusercontent.com/{REPOSITORY}"
INSTALLER_MARKER = "charmm-gui-cli-user-install-v1"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def _error(key, **params):
    return ToolError(key="update." + key, params=params, next_step="")


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise _error("metadata")
    return tuple(int(part) for part in value.split("."))


def _project_version(data):
    try:
        text = data.decode("utf-8")
        section = re.search(r"(?ms)^\[project\]\s*\n(.*?)(?=^\[|\Z)", text).group(1)
        matches = re.findall(r'^version\s*=\s*"([0-9.]+)"\s*$', section, re.M)
        if len(matches) != 1:
            raise ValueError
        version = matches[0]
        _version(version)
        return version
    except (UnicodeError, AttributeError, ValueError):
        raise _error("metadata") from None


def _download(session, url, *, limit, destination=None):
    """Bounded HTTPS GET; redirects and raw error bodies are never reflected."""
    response = None
    handle = None
    try:
        for _ in range(4):
            response = session.get(url, timeout=(10, 60), allow_redirects=False, stream=True)
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("Location", "")
            parsed = urlparse(location)
            response.close()
            response = None
            try:
                port = parsed.port
            except ValueError:
                raise _error("metadata") from None
            if (parsed.scheme != "https" or parsed.hostname not in
                    ("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com")
                    or parsed.username or parsed.password or port not in (None, 443)):
                raise _error("metadata")
            url = location
        if response is None:
            raise _error("metadata")
        if response.status_code != 200:
            raise _error("http", code=response.status_code)
        if destination is not None:
            handle = Path(destination).open("xb")
        blocks, size = [], 0
        digest = hashlib.sha256()
        for block in response.iter_content(64 * 1024):
            size += len(block)
            if size > limit:
                raise _error("size")
            digest.update(block)
            if handle is not None:
                handle.write(block)
            else:
                blocks.append(block)
        if handle is not None:
            handle.flush()
            os.fsync(handle.fileno())
            return digest.hexdigest()
        return b"".join(blocks)
    except requests.RequestException:
        raise _error("network") from None
    finally:
        if handle is not None:
            handle.close()
        if response is not None:
            response.close()


def check_release(session, current_version):
    """Use the latest published release, never an unreleased main version."""
    try:
        release = json.loads(_download(session, RELEASE_URL, limit=256 * 1024))
        if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
            raise ValueError
        tag = release["tag_name"]
        version = tag.removeprefix("v")
        _version(version)
        if tag != "v" + version or release.get("draft") or release.get("prerelease"):
            raise ValueError
        ref_url = f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{tag}"
        ref = json.loads(_download(session, ref_url, limit=64 * 1024))
        commit = ref["object"]["sha"]
        if ref["ref"] != "refs/tags/" + tag or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError
        if ref["object"]["type"] == "tag":
            annotated = json.loads(_download(session, f"https://api.github.com/repos/{REPOSITORY}/git/tags/{commit}", limit=64 * 1024))
            commit = annotated["object"]["sha"]
            if annotated["object"]["type"] != "commit" or not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise ValueError
        elif ref["object"]["type"] != "commit":
            raise ValueError
        assets = release["assets"]
        if not isinstance(assets, list) or any(not isinstance(asset, dict) for asset in assets):
            raise ValueError
        filename = "charmm-gui-cli-" + version + ".tar.gz"
        urls = {}
        for name in (filename, "SHA256SUMS"):
            candidates = [asset for asset in assets if asset["name"] == name and asset.get("state", "uploaded") == "uploaded"]
            if len(candidates) != 1:
                raise ValueError
            url = candidates[0]["browser_download_url"]
            if url != f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}":
                raise ValueError
            urls[name] = url
    except (ValueError, KeyError, TypeError, AttributeError):
        raise _error("metadata") from None
    checksums = _download(session, urls["SHA256SUMS"], limit=64 * 1024)
    try:
        matches = [match.group(1).lower() for line in checksums.decode("ascii").splitlines()
                   if (match := re.fullmatch(r"([0-9a-fA-F]{64})  " + re.escape(filename), line))]
        if len(matches) != 1:
            raise ValueError
    except (UnicodeError, ValueError):
        raise _error("not_published") from None
    current, latest = _version(current_version), _version(version)
    return {"state": "update_available" if latest > current else "up_to_date" if latest == current else "local_newer",
            "current_version": current_version, "latest_version": version, "release_commit": commit,
            "sha256": matches[0], "archive_url": urls[filename], "release_tag": tag}


def install_context(prefix=None, bin_dir=None):
    if (prefix is None) != (bin_dir is None):
        raise _error("paths")
    if prefix is not None:
        return Path(prefix).expanduser().resolve(), Path(bin_dir).expanduser().resolve()
    release = Path(sys.prefix).resolve().parent
    try:
        marker = json.loads((release / "install.json").read_text())
        if not isinstance(marker, dict) or marker.get("installer") != INSTALLER_MARKER or marker.get("status") != "ready" or release.parent.name != "releases":
            raise ValueError
        target = release / "venv/bin/charmm-gui-cli"
        candidates = [Path(sys.argv[0]).absolute(), Path(shutil.which("charmm-gui-cli") or "/nonexistent")]
        for candidate in candidates:
            if candidate.is_symlink() and candidate.resolve() == target:
                return release.parent.parent, candidate.parent
        # New installers record the publication directory for custom PATHs.
        saved_bin = marker.get("bin_dir")
        if isinstance(saved_bin, str):
            candidate = Path(saved_bin) / "charmm-gui-cli"
            if candidate.is_symlink() and candidate.resolve() == target:
                return release.parent.parent, candidate.parent
    except (OSError, ValueError, TypeError):
        pass
    raise _error("unmanaged")


@contextmanager
def update_lock(prefix):
    prefix.mkdir(parents=True, exist_ok=True)
    path = prefix / ".update.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _error("locked") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def extract_source(archive, destination, version):
    """Reject traversal, links, duplicates, special files and expansion bombs."""
    root = "charmm-gui-cli-" + version
    destination.mkdir(exist_ok=False)
    seen, total = set(), 0
    try:
        with tarfile.open(archive, "r:gz") as source:
            for member in source:
                path = PurePosixPath(member.name)
                if (len(seen) >= 2000 or member.name in seen or not member.isfile() or path.is_absolute()
                        or "\\" in member.name or ".." in path.parts or not path.parts or path.parts[0] != root
                        or str(path) != member.name or len(path.parts) < 2 or member.size < 0):
                    raise _error("unsafe_archive")
                total += member.size
                if total > MAX_SOURCE_BYTES:
                    raise _error("size")
                seen.add(member.name)
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=64 * 1024)
        result = destination / root
        required = ("pyproject.toml", "install.sh", "scripts/install_user.py", "scripts/build_release.py", "charmm_gui_cli/__init__.py")
        if not all((result / name).is_file() for name in required):
            raise _error("unsafe_archive")
        if _project_version((result / "pyproject.toml").read_bytes()) != version:
            raise _error("metadata")
        package_versions = re.findall(r'^__version__\s*=\s*"([0-9.]+)"\s*$', (result / "charmm_gui_cli/__init__.py").read_text(), re.M)
        if package_versions != [version]:
            raise _error("metadata")
        return result
    except (tarfile.TarError, EOFError, ValueError):
        raise _error("unsafe_archive") from None


def update(*, check=False, prefix=None, bin_dir=None, current_version=None, session=None):
    owned_session = session is None
    session = session or requests.Session()
    try:
        result = check_release(session, current_version or __version__)
        result["checked_only"] = check
        if check or result["state"] != "update_available":
            return result
        prefix, bin_dir = install_context(prefix, bin_dir)
        with update_lock(prefix):
            downloads = prefix / "updates"
            downloads.mkdir(exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix="update-" + result["latest_version"] + "-", dir=downloads))
            archive = staging / ("charmm-gui-cli-" + result["latest_version"] + ".tar.gz")
            digest = _download(session, result["archive_url"], limit=MAX_ARCHIVE_BYTES, destination=archive)
            if digest != result["sha256"]:
                raise _error("checksum")
            source = extract_source(archive, staging / "source", result["latest_version"])
            command = [sys.executable, str(source / "scripts/install_user.py"), "--source", str(source),
                       "--prefix", str(prefix), "--bin-dir", str(bin_dir)]
            print(t("update.installing"), file=sys.stderr, flush=True)
            # Do not reflect pip/index/proxy output that could contain secrets.
            completed = subprocess.run(command, cwd=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if completed.returncode != 0:
                raise _error("install_failed", path=str(staging))
            result.update(state="updated", archive=str(archive), prefix=str(prefix), bin_dir=str(bin_dir),
                          command_path=str(bin_dir / "charmm-gui-cli"), previous_releases_retained=True)
            return result
    finally:
        if owned_session:
            session.close()
