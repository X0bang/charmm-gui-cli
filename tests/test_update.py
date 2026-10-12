"""Updater tests: no private sessions, network access or deletion operations."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli import cli, i18n, updater
from charmm_gui_cli.auth import ToolError


COMMIT = "a" * 40
VERSION = "0.5.2"


def archive_bytes(extra=(), version=VERSION, omit=()):
    files = {
        "pyproject.toml": f'[project]\nversion = "{version}"\n'.encode(),
        "charmm_gui_cli/__init__.py": f'__version__ = "{version}"\n'.encode(),
        "install.sh": b"#!/bin/sh\n",
        "scripts/install_user.py": b"# fixture installer\n",
        "scripts/build_release.py": b"# fixture builder\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            if name in omit:
                continue
            member = tarfile.TarInfo(f"charmm-gui-cli-{VERSION}/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        for name, kind, content in extra:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            member.linkname = "../../outside"
            archive.addfile(member, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
    return buffer.getvalue()


def response(content, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = content
    result._content_consumed = True
    return result


def transport(archive=None, version=VERSION, digest=None):
    archive = archive if archive is not None else archive_bytes()
    digest = digest if digest is not None else hashlib.sha256(archive).hexdigest()
    base = updater.RAW_BASE + "/" + COMMIT
    payloads = {
        updater.REFERENCE_URL: json.dumps({"ref": "refs/heads/main", "object": {"type": "commit", "sha": COMMIT}}).encode(),
        base + "/pyproject.toml": f'[project]\nversion = "{version}"\n'.encode(),
        base + "/download/SHA256SUMS": f"{digest}  charmm-gui-cli-{version}.tar.gz\n".encode(),
        base + f"/download/charmm-gui-cli-{version}.tar.gz": archive,
    }
    session = Mock()
    session.get.side_effect = lambda url, **kwargs: response(payloads[url])
    return session, payloads


class UpdateTests(unittest.TestCase):
    def setUp(self):
        # Retain test artifacts instead of deleting directories.
        self.directory = Path(tempfile.mkdtemp(prefix="cgui-update-test-"))
        self.prefix = self.directory / "install"
        self.bin_dir = self.directory / "bin"
        i18n.set_language("en")

    def tearDown(self):
        i18n.set_language()

    def run_update(self, **kwargs):
        session, _ = transport()
        with contextlib.redirect_stderr(io.StringIO()):
            return updater.update(prefix=self.prefix, bin_dir=self.bin_dir,
                                  current_version="0.5.1", session=session, **kwargs)

    def extract(self, content):
        archive = self.directory / "source.tar.gz"
        archive.write_bytes(content)
        return updater.extract_source(archive, self.directory / "source", VERSION)

    def test_numeric_versions_and_no_downgrade(self):
        for latest, current, state in (("0.5.10", "0.5.9", "update_available"),
                                       (VERSION, VERSION, "up_to_date"),
                                       ("0.5.1", VERSION, "local_newer")):
            session, _ = transport(version=latest)
            self.assertEqual(updater.check_release(session, current)["state"], state)

    def test_metadata_checksum_and_download_pinned_to_same_commit(self):
        session, _ = transport()
        result = updater.check_release(session, "0.5.1")
        self.assertEqual(result["release_commit"], COMMIT)
        self.assertIn(COMMIT, result["archive_url"])
        self.assertEqual(len(session.get.call_args_list), 3)
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            if call.args[0] != updater.REFERENCE_URL:
                self.assertIn(COMMIT, call.args[0])

    def test_check_only_does_not_touch_files_or_invoke_installer(self):
        session, _ = transport()
        with patch.object(updater, "install_context") as context, patch.object(updater.subprocess, "run") as run:
            result = updater.update(check=True, current_version="0.5.1", session=session)
        self.assertTrue(result["checked_only"])
        context.assert_not_called()
        run.assert_not_called()
        session.close.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_current_or_newer_local_version_does_not_install(self):
        for current in (VERSION, "0.6.0"):
            session, _ = transport()
            with patch.object(updater, "install_context") as context:
                updater.update(current_version=current, session=session)
            context.assert_not_called()

    def test_owned_http_session_closed_on_failure(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("TOKEN_SECRET")
        with patch.object(updater.requests, "Session", return_value=session):
            with self.assertRaises(ToolError) as caught:
                updater.update(check=True)
        self.assertEqual(caught.exception.key, "update.network")
        self.assertNotIn("TOKEN_SECRET", caught.exception.localized())
        session.close.assert_called_once()

    def test_http_body_never_reflected_and_redirect_rejected(self):
        for status in (302, 404, 500):
            session = Mock()
            session.get.return_value = response(b"TOKEN_COOKIE_PASSWORD_SECRET", status)
            with self.assertRaises(ToolError) as caught:
                updater.check_release(session, VERSION)
            self.assertEqual(caught.exception.key, "update.http")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_invalid_reference_metadata_rejected_without_body(self):
        for body in (b'[]', b'{"object":{"sha":"SECRET"}}', b'BODY_SECRET'):
            session = Mock()
            session.get.return_value = response(body)
            with self.assertRaises(ToolError) as caught:
                updater.check_release(session, VERSION)
            self.assertEqual(caught.exception.key, "update.metadata")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_unpublished_or_duplicate_checksum_refused(self):
        for checksum in (b"", b"BODY_SECRET", (b"a" * 64 + b"  charmm-gui-cli-0.5.2.tar.gz\n") * 2):
            session, payloads = transport()
            payloads[updater.RAW_BASE + "/" + COMMIT + "/download/SHA256SUMS"] = checksum
            with self.assertRaises(ToolError) as caught:
                updater.check_release(session, "0.5.1")
            self.assertEqual(caught.exception.key, "update.not_published")

    def test_download_size_limit_and_exclusive_destination(self):
        session = Mock()
        session.get.side_effect = lambda *args, **kwargs: response(b"12345")
        destination = self.directory / "archive"
        with self.assertRaises(ToolError) as caught:
            updater._download(session, "https://example.invalid", limit=4, destination=destination)
        self.assertEqual(caught.exception.key, "update.size")
        self.assertTrue(destination.exists())
        with self.assertRaises(FileExistsError):
            updater._download(session, "https://example.invalid", limit=10, destination=destination)

    def test_checksum_failure_keeps_original_entrypoint_and_download(self):
        self.bin_dir.mkdir()
        link = self.bin_dir / "charmm-gui-cli"
        link.symlink_to(self.directory / "old-version")
        session, _ = transport(digest="0" * 64)
        with patch.object(updater.subprocess, "run") as run:
            with self.assertRaises(ToolError) as caught:
                updater.update(prefix=self.prefix, bin_dir=self.bin_dir,
                               current_version="0.5.1", session=session)
        self.assertEqual(caught.exception.key, "update.checksum")
        self.assertEqual(link.readlink(), self.directory / "old-version")
        self.assertEqual(len(list((self.prefix / "updates").glob("*/*.tar.gz"))), 1)
        run.assert_not_called()

    def test_success_uses_installer_without_shell_or_secret_logs(self):
        with patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            result = self.run_update()
        self.assertEqual(result["state"], "updated")
        self.assertTrue(result["previous_releases_retained"])
        command = run.call_args.args[0]
        self.assertIn("scripts/install_user.py", command[1])
        self.assertEqual(command[-4:], ["--prefix", str(self.prefix), "--bin-dir", str(self.bin_dir)])
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_failed_installer_keeps_old_command_and_staging(self):
        self.bin_dir.mkdir()
        link = self.bin_dir / "charmm-gui-cli"
        link.symlink_to(self.directory / "old-version")
        with patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(ToolError) as caught:
                self.run_update()
        self.assertEqual(caught.exception.key, "update.install_failed")
        self.assertEqual(link.readlink(), self.directory / "old-version")
        self.assertTrue(Path(caught.exception.params["path"]).is_dir())

    def test_valid_archive_extraction(self):
        result = self.extract(archive_bytes())
        self.assertEqual(result.name, "charmm-gui-cli-" + VERSION)
        self.assertTrue((result / "scripts/install_user.py").is_file())

    def test_unsafe_archive_members_refused(self):
        cases = [("../outside", tarfile.REGTYPE, b"secret"),
                 (f"charmm-gui-cli-{VERSION}/../outside", tarfile.REGTYPE, b"x"),
                 ("/absolute", tarfile.REGTYPE, b"x"),
                 ("other-root/file", tarfile.REGTYPE, b"x"),
                 (f"charmm-gui-cli-{VERSION}/link", tarfile.SYMTYPE, b""),
                 (f"charmm-gui-cli-{VERSION}/hard", tarfile.LNKTYPE, b""),
                 (f"charmm-gui-cli-{VERSION}/dir", tarfile.DIRTYPE, b""),
                 (f"charmm-gui-cli-{VERSION}/install.sh", tarfile.REGTYPE, b"duplicate")]
        for index, member in enumerate(cases):
            archive = self.directory / f"case-{index}.tar.gz"
            archive.write_bytes(archive_bytes(extra=(member,)))
            with self.assertRaises(ToolError) as caught:
                updater.extract_source(archive, self.directory / f"case-{index}", VERSION)
            self.assertEqual(caught.exception.key, "update.unsafe_archive")
        self.assertFalse((self.directory / "outside").exists())

    def test_archive_expansion_limit(self):
        with patch.object(updater, "MAX_SOURCE_BYTES", 10):
            with self.assertRaises(ToolError) as caught:
                self.extract(archive_bytes())
        self.assertEqual(caught.exception.key, "update.size")

    def test_archive_missing_required_files(self):
        with self.assertRaises(ToolError) as caught:
            self.extract(archive_bytes(omit=("scripts/install_user.py",)))
        self.assertEqual(caught.exception.key, "update.unsafe_archive")

    def test_archive_version_mismatch(self):
        with self.assertRaises(ToolError) as caught:
            self.extract(archive_bytes(version="0.5.1"))
        self.assertEqual(caught.exception.key, "update.metadata")

    def test_unknown_install_and_incomplete_paths_refused(self):
        with patch.object(updater.sys, "prefix", str(self.directory / "unmanaged/venv")):
            with self.assertRaises(ToolError) as caught:
                updater.install_context()
        self.assertEqual(caught.exception.key, "update.unmanaged")
        with self.assertRaises(ToolError) as caught:
            updater.install_context(self.prefix)
        self.assertEqual(caught.exception.key, "update.paths")

    def test_managed_custom_install_autodetected_from_marker(self):
        release = self.prefix / "releases/old"
        release.mkdir(parents=True)
        self.bin_dir.mkdir()
        target = release / "venv/bin/charmm-gui-cli"
        (self.bin_dir / "charmm-gui-cli").symlink_to(target)
        (release / "install.json").write_text(json.dumps({"installer": updater.INSTALLER_MARKER,
                                                         "status": "ready", "bin_dir": str(self.bin_dir)}))
        with patch.object(updater.sys, "prefix", str(release / "venv")), patch.object(updater.sys, "argv", ["/unused"]), patch.object(updater.shutil, "which", return_value=None):
            self.assertEqual(updater.install_context(), (self.prefix, self.bin_dir))
        # Old installers did not record bin_dir; the active symlink still works.
        (release / "install.json").write_text(json.dumps({"installer": updater.INSTALLER_MARKER, "status": "ready"}))
        with patch.object(updater.sys, "prefix", str(release / "venv")), patch.object(updater.sys, "argv", [str(self.bin_dir / "charmm-gui-cli")]), patch.object(updater.shutil, "which", return_value=None):
            self.assertEqual(updater.install_context(), (self.prefix, self.bin_dir))

    def test_invalid_marker_does_not_crash(self):
        release = self.prefix / "releases/old"
        release.mkdir(parents=True)
        (release / "install.json").write_text("[]")
        with patch.object(updater.sys, "prefix", str(release / "venv")):
            with self.assertRaises(ToolError) as caught:
                updater.install_context()
        self.assertEqual(caught.exception.key, "update.unmanaged")

    def test_update_lock_rejects_concurrent_attempt_and_remains_reusable(self):
        with updater.update_lock(self.prefix):
            with self.assertRaises(ToolError) as caught:
                with updater.update_lock(self.prefix):
                    self.fail("concurrent lock acquired")
        self.assertEqual(caught.exception.key, "update.locked")
        with updater.update_lock(self.prefix):
            self.assertEqual((self.prefix / ".update.lock").stat().st_mode & 0o777, 0o600)

    def test_update_lock_does_not_follow_symlink(self):
        self.prefix.mkdir()
        unrelated = self.directory / "unrelated"
        unrelated.write_text("unchanged")
        (self.prefix / ".update.lock").symlink_to(unrelated)
        with self.assertRaises(OSError):
            with updater.update_lock(self.prefix):
                self.fail("followed lock symlink")
        self.assertEqual(unrelated.read_text(), "unchanged")

    def test_cli_check_dispatch_and_json_schema(self):
        result = {"state": "up_to_date", "current_version": VERSION, "latest_version": VERSION, "checked_only": True}
        output = io.StringIO()
        with patch.object(updater, "update", return_value=result) as update, contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--lang", "en", "--json", "update", "--check"]), 0)
        self.assertEqual(output.getvalue(), json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        update.assert_called_once_with(check=True, prefix=None, bin_dir=None)

    def test_update_tty_summary_is_bilingual_and_allowlisted(self):
        value = {"state": "update_available", "current_version": "0.5.1", "latest_version": VERSION,
                 "password": "PASSWORD_SECRET", "body": "BODY_SECRET"}
        for language, expected in (("en", "update"), ("zh", "新版本")):
            i18n.set_language(language)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(output, "isatty", return_value=True), patch.object(cli, "_JSON_OUTPUT", False):
                cli.emit(value, command="update")
            self.assertIn(expected, output.getvalue().lower())
            self.assertNotIn("SECRET", output.getvalue())

    def test_interrupted_update_does_not_offer_modeling_resubmission(self):
        output = io.StringIO()
        with patch.object(updater, "update", side_effect=KeyboardInterrupt), contextlib.redirect_stderr(output):
            self.assertEqual(cli.main(["--lang", "en", "update"]), 130)
        self.assertNotIn("jobs resume", output.getvalue())
        self.assertNotIn("submission_unknown", output.getvalue())
