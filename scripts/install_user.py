#!/usr/bin/env python3
"""Non-interactive, user-level installer; retains prior releases and failures."""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
try:
    import venv
except ImportError:
    venv = None

from build_release import build, metadata


MARKER = "charmm-gui-cli-user-install-v1"


def owned_link(link, prefix):
    """Only our symlinks into a marked release under this prefix may change."""
    if not link.is_symlink():
        return False
    target = link.resolve()
    releases = prefix / "releases"
    if not target.is_relative_to(releases):
        return False
    relative = target.relative_to(releases)
    if len(relative.parts) != 4 or relative.parts[1:3] != ("venv", "bin"):
        return False
    try:
        marker = json.loads((releases / relative.parts[0] / "install.json").read_text())
    except (OSError, ValueError):
        return False
    return marker.get("installer") == MARKER and marker.get("status") == "ready"


def install(source, prefix, bin_dir, *, offline=False, wheelhouse=None):
    source, prefix, bin_dir = (Path(path).expanduser().resolve() for path in (source, prefix, bin_dir))
    project = metadata(source)
    if venv is None or importlib.util.find_spec("ensurepip") is None:
        raise RuntimeError("Python venv/ensurepip is unavailable. On Ubuntu 22.04+ ask your administrator to run: sudo apt install python3-venv (or the matching python3.X-venv). The installer does not run apt or sudo.")
    if offline and wheelhouse is None:
        raise RuntimeError("--offline requires --wheelhouse containing all dependencies, including RDKit and its dependencies.")
    if wheelhouse is not None and not Path(wheelhouse).is_dir():
        raise RuntimeError("--wheelhouse is not an existing directory.")
    for name in project["scripts"]:
        link = bin_dir / name
        if os.path.lexists(link) and not owned_link(link, prefix):
            raise RuntimeError(f"Refusing to overwrite an unknown entry point: {link}. Choose --bin-dir or move that file yourself.")
    label = project["version"] + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    release = prefix / "releases" / label
    release.mkdir(parents=True, exist_ok=False)
    print(f"Creating isolated installation: {release}", flush=True)
    print("No previous release will be deleted. Failed installation directories are retained.", flush=True)
    if not offline:
        print("First installation downloads pip dependencies, including RDKit, from your configured pip package index. Network access is required.", flush=True)
    environment = release / "venv"
    try:
        venv.EnvBuilder(with_pip=True).create(environment)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Could not create a venv. On Ubuntu install the matching python3-venv package; also check available disk space. No system packages were changed.") from exc
    artifacts = build(source, release / "artifacts", wheel_only=True)
    python = environment / "bin" / "python"
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    if offline:
        command.append("--no-index")
    if wheelhouse:
        command.extend(["--find-links", str(Path(wheelhouse).resolve())])
    command.append(artifacts["wheel"] + "[chem]")
    subprocess.run(command, check=True)
    subprocess.run([str(python), "-c", "import requests, yaml, bs4; from rdkit import Chem; from charmm_gui_cli.cli import parser; parser()"], check=True, cwd=release)
    for name in project["scripts"]:
        subprocess.run([str(environment / "bin" / name), "--help"], stdout=subprocess.DEVNULL, check=True, cwd=release)
    marker = {"installer": MARKER, "status": "ready", "version": project["version"],
              "release": str(release), "python": sys.version, "source": str(source),
              "entry_points": list(project["scripts"])}
    with (release / "install.json").open("x", encoding="utf-8") as handle:
        json.dump(marker, handle, indent=2)
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in project["scripts"]:
        link = bin_dir / name
        target = environment / "bin" / name
        # Recheck ownership immediately before publication. Unknown existing
        # files are never replaced; fresh links use an exclusive symlink call.
        if os.path.lexists(link):
            if not owned_link(link, prefix):
                raise RuntimeError(f"Entry point changed during installation; refusing to replace {link}.")
            replacement = bin_dir / ("." + name + ".install-" + uuid.uuid4().hex)
            replacement.symlink_to(target)
            os.replace(replacement, link)
        else:
            link.symlink_to(target)
    print(f"Installed version {project['version']} at {release}")
    print(f"Commands: {', '.join(str(bin_dir / name) for name in project['scripts'])}")
    print(f"Add this directory to PATH if needed: {bin_dir}")
    print("No CHARMM-GUI credentials were requested or copied. Next: charmm-gui-cli login")
    return marker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--prefix", type=Path, default=Path(os.environ.get("CHARMM_GUI_INSTALL_ROOT", str(Path.home() / ".local/share/charmm-gui-cli"))))
    parser.add_argument("--bin-dir", type=Path, default=Path(os.environ.get("CHARMM_GUI_BIN_DIR", str(Path.home() / ".local/bin"))))
    parser.add_argument("--wheelhouse", type=Path, help="Local dependency wheels; must match this OS/Python/architecture")
    parser.add_argument("--offline", action="store_true", help="Use only --wheelhouse; never contact package indexes")
    args = parser.parse_args()
    try:
        install(args.source, args.prefix, args.bin_dir, offline=args.offline, wheelhouse=args.wheelhouse)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Installation failed: {exc}\nExisting installations and failed artifacts were retained.\n")


if __name__ == "__main__":
    main()
