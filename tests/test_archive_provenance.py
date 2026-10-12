"""Immutable download ownership and interrupted-download recovery."""

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock

from charmm_gui_cli import workflow
from charmm_gui_cli.auth import ToolError


class ArchiveProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp(prefix="cgui-archive-provenance-")).resolve()
        self.archive = self.run / "charmm-gui.tgz"
        self.receipt = self.run / "download-receipt.json"
        self.manifest = {
            "schema_version": 1, "state": "downloading", "remote_status": "done", "job_id": "234",
            "config": {"version": 1, "source_job_id": "123", "expected_ligands": ["LIG"],
                       "preparation": {"ligand_parameters_ready": True, "orientation": "ppm"},
                       "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20}},
        }
        self.write_archive()

    def write_archive(self, root="charmm-gui-234", extra=None, payload=None):
        payload = payload or b"HETATM    1  C1  LIG A   1       1.000   2.000   3.000  1.00  0.00           C\n"
        with tarfile.open(self.archive, "w:gz") as archive:
            for name in [root + "/step5_assembly.pdb"] + ([extra] if extra else []):
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))

    def seal(self):
        return workflow.verify_archive_provenance(self.run, self.manifest, create_receipt=True)

    def test_new_download_creates_immutable_receipt_and_readonly_reuses(self):
        receipt = self.seal()
        self.assertEqual(receipt, {"version": 1, "job_id": "234", "archive_root": "charmm-gui-234",
                                   "archive_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest()})
        before = self.receipt.stat().st_mtime_ns
        self.assertEqual(workflow.verify_archive_provenance(self.run, self.manifest), receipt)
        self.assertEqual(self.seal(), receipt)
        self.assertEqual(self.receipt.stat().st_mtime_ns, before)

    def test_legacy_matching_hash_is_readonly_until_explicit_migration(self):
        self.manifest.update(state="ligands_present", archive_sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())
        expected = workflow.verify_archive_provenance(self.run, self.manifest)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.seal(), expected)
        self.assertTrue(self.receipt.exists())

    def test_changed_archive_does_not_replace_recorded_hash_or_receipt(self):
        receipt = self.seal()
        self.manifest["archive_sha256"] = receipt["archive_sha256"]
        old_receipt = self.receipt.read_bytes()
        self.write_archive(payload=b"changed payload")
        with self.assertRaisesRegex(ToolError, "SHA256 mismatch"):
            self.seal()
        self.assertEqual(self.manifest["archive_sha256"], receipt["archive_sha256"])
        self.assertEqual(self.receipt.read_bytes(), old_receipt)

    def test_changed_archive_without_manifest_hash_rejected_by_receipt(self):
        self.seal()
        self.write_archive(payload=b"changed payload")
        with self.assertRaisesRegex(ToolError, "receipt does not match"):
            self.seal()

    def test_foreign_root_rejected_even_with_preexisting_hash(self):
        self.write_archive(root="charmm-gui-999")
        self.manifest["archive_sha256"] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ToolError, "root does not match"):
            self.seal()
        self.assertFalse(self.receipt.exists())

    def test_nonofficial_and_mixed_roots_rejected(self):
        for root, extra in [("job", None), ("charmm-gui-234", "charmm-gui-999/foreign.pdb"),
                            ("charmm-gui-234", "outside.txt")]:
            with self.subTest(root=root, extra=extra):
                self.write_archive(root, extra)
                with self.assertRaises(ToolError):
                    self.seal()

    def test_safe_leading_dot_directory_is_accepted(self):
        self.write_archive("./charmm-gui-234")
        self.assertEqual(self.seal()["archive_root"], "charmm-gui-234")

    def test_archive_without_receipt_requires_old_hash_or_durable_download_intent(self):
        with self.assertRaisesRegex(ToolError, "ownership cannot be established"):
            workflow.verify_archive_provenance(self.run, self.manifest)
        for state, remote in [("submitted", "done"), ("downloading", "running"), ("ligands_present", "done")]:
            with self.subTest(state=state):
                self.manifest.update(state=state, remote_status=remote)
                with self.assertRaisesRegex(ToolError, "ownership cannot be established"):
                    self.seal()

    def test_receipt_job_tampering_is_not_repaired(self):
        receipt = self.seal()
        receipt["job_id"] = "999"
        self.receipt.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ToolError, "receipt does not match"):
            self.seal()
        self.assertEqual(json.loads(self.receipt.read_text())["job_id"], "999")

    def test_invalid_recorded_hash_is_not_treated_as_absent(self):
        for value in (None, "", "0" * 63, "A" * 64):
            with self.subTest(value=value):
                self.manifest["archive_sha256"] = value
                with self.assertRaisesRegex(ToolError, "SHA256 is invalid"):
                    self.seal()

    def test_traversal_and_link_members_rejected(self):
        self.write_archive(extra="charmm-gui-234/../foreign")
        with self.assertRaisesRegex(ToolError, "unsafe path"):
            self.seal()
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo("charmm-gui-234/link")
            member.type = tarfile.SYMTYPE
            member.linkname = "outside"
            archive.addfile(member)
        with self.assertRaisesRegex(ToolError, "unsupported member type"):
            self.seal()

    def test_bad_crc_is_rejected_even_when_tar_members_are_readable(self):
        packed = bytearray(self.archive.read_bytes())
        packed[-8] ^= 1
        self.archive.write_bytes(packed)
        with self.assertRaisesRegex(ToolError, "integrity"):
            self.seal()

    def test_resume_preserves_hash_and_state_on_mismatch_without_network(self):
        receipt = self.seal()
        self.manifest.update(state="ligands_present", archive_sha256=receipt["archive_sha256"])
        workflow.save(self.run, self.manifest)
        self.write_archive(payload=b"changed payload")
        client = Mock()
        with self.assertRaisesRegex(ToolError, "SHA256 mismatch"):
            workflow.resume(client, self.run)
        saved = workflow.read(self.run)
        self.assertEqual(saved["archive_sha256"], receipt["archive_sha256"])
        self.assertEqual(saved["state"], "ligands_present")
        self.assertEqual(saved["last_error"]["category"], "archive_provenance")
        client.status.assert_not_called()
        client.download.assert_not_called()
        client.submit.assert_not_called()

    def test_interrupted_download_recovers_without_network(self):
        workflow.save(self.run, self.manifest)
        client = Mock()
        self.assertEqual(workflow.resume(client, self.run)["state"], "ligands_present")
        self.assertTrue(self.receipt.exists())
        client.status.assert_not_called()
        client.download.assert_not_called()
        client.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
