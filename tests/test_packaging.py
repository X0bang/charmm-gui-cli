"""Release integrity and installer publication tests; no network or deletion."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_release
import install_user


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cgui-packaging-test-"))

    def test_source_allowlist_excludes_runtime_credentials_and_user_data(self):
        names = build_release.selected_files(ROOT)
        for prefix in ("session", "runs/", ".venv/", "build/", "test-dataset/", ".git/", "__pycache__/"):
            self.assertFalse(any(name.startswith(prefix) for name in names))
        self.assertIn("install.sh", names)
        self.assertIn("LICENSE", names)
        self.assertIn("scripts/install_user.py", names)
        self.assertIn("docs/INSTALL.md", names)
        self.assertTrue(all(not name.endswith((".pyc", ".token")) for name in names))

    def test_release_wheel_records_metadata_and_source_are_valid(self):
        result = build_release.build(ROOT, self.directory / "release")
        with zipfile.ZipFile(result["wheel"]) as wheel:
            names = wheel.namelist()
            self.assertTrue(all(name.startswith("charmm_gui_cli/") or ".dist-info/" in name for name in names))
            record_name = next(name for name in names if name.endswith("/RECORD"))
            rows = list(csv.reader(io.StringIO(wheel.read(record_name).decode())))
            self.assertEqual({row[0] for row in rows}, set(names))
            for name, digest, size in rows:
                if name == record_name:
                    self.assertEqual((digest, size), ("", ""))
                    continue
                content = wheel.read(name)
                expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                self.assertEqual(digest, "sha256=" + expected)
                self.assertEqual(int(size), len(content))
            metadata = wheel.read(next(name for name in names if name.endswith("/METADATA"))).decode()
            self.assertIn("Provides-Extra: chem", metadata)
            self.assertIn('extra == "chem"', metadata)
            self.assertIn("Requires-Python: >=3.10", metadata)
            self.assertIn("Metadata-Version: 2.4", metadata)
            self.assertIn("License-Expression: GPL-3.0-only", metadata)
            self.assertIn("License-File: LICENSE", metadata)
            license_name = next(name for name in names if name.endswith(".dist-info/licenses/LICENSE"))
            self.assertEqual(wheel.read(license_name), (ROOT / "LICENSE").read_bytes())
        with tarfile.open(result["source"]) as archive:
            names = [member.name.split("/", 1)[1] for member in archive.getmembers()]
            self.assertEqual(names, result["source_files"])
            self.assertTrue(all(member.isfile() for member in archive.getmembers()))
        for line in Path(result["checksums"]).read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((Path(result["checksums"]).parent / name).read_bytes()).hexdigest())
        with self.assertRaises(FileExistsError):
            build_release.build(ROOT, self.directory / "release")

    def test_wheel_is_accepted_by_pip_without_build_or_network(self):
        result = build_release.build(ROOT, self.directory / "release", wheel_only=True)
        destination = self.directory / "installed"
        process = subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile",
                                  "--disable-pip-version-check", "--target", str(destination), result["wheel"]],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue((destination / "charmm_gui_cli" / "cli.py").is_file())

    def test_symlinked_release_input_is_rejected(self):
        source = self.directory / "source"
        source.mkdir()
        (source / "pyproject.toml").symlink_to(ROOT / "pyproject.toml")
        with self.assertRaisesRegex(ValueError, "unsafe release input"):
            build_release.selected_files(source)

    def test_unknown_user_entrypoint_is_preserved(self):
        bin_dir = self.directory / "bin"
        bin_dir.mkdir()
        entry = bin_dir / "charmm-gui-cli"
        entry.write_text("user-owned command")
        with self.assertRaisesRegex(RuntimeError, "unknown entry point"):
            install_user.install(ROOT, self.directory / "prefix", bin_dir)
        self.assertEqual(entry.read_text(), "user-owned command")
        self.assertFalse((self.directory / "prefix").exists())

    def test_offline_mode_requires_wheelhouse(self):
        with self.assertRaisesRegex(RuntimeError, "requires --wheelhouse"):
            install_user.install(ROOT, self.directory / "prefix", self.directory / "bin", offline=True)

    def test_missing_ensurepip_has_ubuntu_guidance_before_any_install(self):
        with patch("install_user.importlib.util.find_spec", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "python3-venv"):
                install_user.install(ROOT, self.directory / "prefix", self.directory / "bin")
        self.assertFalse((self.directory / "prefix").exists())

    def test_owned_marker_required_before_updating_symlink(self):
        prefix = self.directory / "prefix"
        release = prefix / "releases" / "old"
        target = release / "venv/bin/charmm-gui-cli"
        target.parent.mkdir(parents=True)
        target.write_text("old command")
        link = self.directory / "command"
        link.symlink_to(target)
        self.assertFalse(install_user.owned_link(link, prefix))
        (release / "install.json").write_text(json.dumps({"installer": install_user.MARKER, "status": "ready"}))
        self.assertTrue(install_user.owned_link(link, prefix))
        self.assertEqual(target.read_text(), "old command")


if __name__ == "__main__":
    unittest.main()
