"""Archive lineage tests use real tiny tar archives and unmocked provenance."""

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from charmm_gui_cli import managed
from charmm_gui_cli.auth import ToolError


class ManagedProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cgui-managed-provenance-")).resolve()
        self.run = self.root / "run"
        self.bilayer = self.run / "bilayer"
        self.bilayer.mkdir(parents=True)
        self.archive = self.bilayer / "charmm-gui.tgz"
        self.write_archive("200")
        inputs = self.run / "inputs"
        inputs.mkdir()
        files = {}
        for name in ("input-manifest.json", "complex.pdb", "ligand.sdf", "protein.pdb"):
            path = inputs / name
            path.write_text("{}" if name.endswith("json") else "fixture\n")
            files[name] = str(path)
        self.manifest = {"schema_version": 2, "state": "ligands_present", "source_job_id": "100", "build_job_id": "200",
                         "inputs": {"files": files}, "config": {"membrane": {}, "ions": {}}}
        self.bilayer_manifest = {"schema_version": 1, "state": "ligands_present", "job_id": "200", "remote_status": "done",
                                 "config": {"source_job_id": "100"}, "archive_sha256": managed._digest(self.archive)}
        self.save()

    def write_archive(self, identifier, payload=b"archive fixture"):
        with tarfile.open(self.archive, "w:gz") as tar:
            entry = tarfile.TarInfo(f"charmm-gui-{identifier}/step5_assembly.pdb")
            entry.size = len(payload)
            tar.addfile(entry, io.BytesIO(payload))

    def save(self):
        (self.run / "full-run.json").write_text(json.dumps(self.manifest))
        (self.bilayer / "run.json").write_text(json.dumps(self.bilayer_manifest))

    def publish_report(self):
        evidence = managed._evidence(self.manifest, self.archive, False, "gmx")
        report = {"passed": True, "status": "validated", "validation_complete": True,
                  "fingerprint": managed._canonical_hash(evidence), "evidence": evidence}
        (self.run / "results").mkdir(exist_ok=True)
        (self.run / "results/validation.json").write_text(json.dumps(report))
        return report

    def test_legacy_archive_requires_existing_hash_and_matching_root_without_read_side_effect(self):
        evidence = managed._evidence(self.manifest, self.archive, False, "gmx")
        binding = evidence["job_binding"]
        self.assertEqual(binding["source_job_id"], "100")
        self.assertEqual(binding["build_job_id"], "200")
        self.assertEqual(binding["archive_provenance"]["archive_root"], "charmm-gui-200")
        self.assertFalse((self.bilayer / "download-receipt.json").exists())

    def test_missing_legacy_hash_is_not_trusted_from_current_bytes(self):
        self.bilayer_manifest.pop("archive_sha256")
        self.save()
        with self.assertRaises(ToolError):
            managed._evidence(self.manifest, self.archive, False, "gmx")

    def test_explicit_finalize_migration_creates_receipt_and_readonly_evidence_matches(self):
        first = managed._evidence(self.manifest, self.archive, False, "gmx", create_receipt=True)
        second = managed._evidence(self.manifest, self.archive, False, "gmx")
        self.assertEqual(first, second)
        self.assertTrue(first["job_binding"]["download_receipt_sha256"])

    def test_full_build_id_change_invalidates_previously_successful_report(self):
        self.publish_report()
        self.assertTrue(managed._final_summary(self.run)["passed"])
        self.manifest["build_job_id"] = "300"
        self.save()
        summary = managed._final_summary(self.run)
        self.assertEqual(summary["validation_status"], "stale_or_unreadable")
        self.assertNotIn("passed", summary)

    def test_source_or_bilayer_identity_mismatch_is_rejected(self):
        for target, key in ((self.manifest, "source_job_id"), (self.bilayer_manifest, "job_id"),
                            (self.bilayer_manifest["config"], "source_job_id")):
            before = target[key]
            target[key] = "999"
            self.save()
            with self.assertRaises(ToolError):
                managed._evidence(self.manifest, self.archive, False, "gmx")
            target[key] = before
            self.save()

    def test_coherent_source_metadata_change_still_invalidates_cached_report(self):
        self.publish_report()
        self.manifest["source_job_id"] = "101"
        self.bilayer_manifest["config"]["source_job_id"] = "101"
        self.save()
        self.assertEqual(managed._final_summary(self.run)["validation_status"], "stale_or_unreadable")

    def test_swapped_archive_is_refused_even_with_same_root_job_name(self):
        self.publish_report()
        self.write_archive("200", payload=b"different prepared structure")
        with self.assertRaises(ToolError):
            managed._evidence(self.manifest, self.archive, False, "gmx")
        self.assertNotIn("passed", managed._final_summary(self.run))

    def test_archive_root_must_match_job_even_if_new_hash_was_recorded(self):
        self.write_archive("300")
        self.bilayer_manifest["archive_sha256"] = managed._digest(self.archive)
        self.save()
        with self.assertRaises(ToolError):
            managed._evidence(self.manifest, self.archive, False, "gmx")

    def test_immutable_receipt_defeats_replacement_of_mutable_manifest_hash(self):
        managed._evidence(self.manifest, self.archive, False, "gmx", create_receipt=True)
        receipt = (self.bilayer / "download-receipt.json").read_bytes()
        self.write_archive("200", payload=b"replacement archive")
        self.bilayer_manifest["archive_sha256"] = managed._digest(self.archive)
        self.save()
        with self.assertRaises(ToolError):
            managed._evidence(self.manifest, self.archive, False, "gmx", create_receipt=True)
        self.assertEqual((self.bilayer / "download-receipt.json").read_bytes(), receipt)

    def test_finalize_rejects_foreign_job_before_extraction_or_scientific_checks(self):
        self.manifest["build_job_id"] = "300"
        self.save()
        with patch.object(managed, "extract_for_validation") as extract, patch.object(managed, "validate_system") as validate:
            with self.assertRaises(ToolError):
                managed.finalize_managed_build(self.run)
        extract.assert_not_called()
        validate.assert_not_called()

    def test_receipt_identity_change_invalidates_success_report(self):
        managed._evidence(self.manifest, self.archive, False, "gmx", create_receipt=True)
        self.publish_report()
        receipt_path = self.bilayer / "download-receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["job_id"] = "300"
        receipt_path.write_text(json.dumps(receipt))
        self.assertNotIn("passed", managed._final_summary(self.run))


if __name__ == "__main__":
    unittest.main()
