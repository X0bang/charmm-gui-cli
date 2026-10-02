import base64
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests

from charmm_gui_cli.api import API_BASE, AuthError, Client, SubmissionUnknown
from charmm_gui_cli.artifacts import inspect_archive
from charmm_gui_cli.auth import ToolError, atomic_write, load_token, normalize_token, token_info
from charmm_gui_cli.cli import main
from charmm_gui_cli.config import load_config, plan, payload_for, validate_config
from charmm_gui_cli import workflow
from charmm_gui_cli.structure import analyze_pdb


def token(exp=None):
    payload = json.dumps({"exp": exp if exp is not None else time.time() + 3600}).encode()
    return "eyJhbGciOiJIUzI1NiJ9." + base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".testsignature"


def response(value, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(value).encode()
    result._content_consumed = True
    return result


def config():
    return validate_config({
        "version": 1, "source_job_id": "123", "expected_ligands": ["LIG"],
        "preparation": {"ligand_parameters_ready": True, "orientation": "ppm"},
        "membrane": {"upper": "POPC:CHL1=3:1", "lower": "POPC=1", "margin_A": 20},
    })


def pdb(name):
    return f"HETATM    1  C1  {name:>3} A   1       1.000   2.000   3.000  1.00  0.00           C\n"


def archive_bytes(entries):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, value in entries.items():
            info = tarfile.TarInfo(name)
            data = value.encode()
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return result.getvalue()


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        # Preserve test artifacts under the user's no-deletion constraint.
        self.root = Path(tempfile.mkdtemp(prefix="cgui-test-"))


class AuthTests(TemporaryTest):
    def test_raw_bearer_and_login_json_are_compatible(self):
        value = token()
        for raw in (value, "Bearer " + value, json.dumps({"token": value}), json.dumps(value)):
            self.assertEqual(normalize_token(raw)[0], value)

    def test_filename_and_cookie_are_not_tokens(self):
        for value in ("session.token", "PHPSESSID=secret", '{"token": null}', "a.!.b"):
            with self.assertRaises(ToolError):
                normalize_token(value)

    def test_expiry_is_not_signature_verification(self):
        info = token_info(token(1))
        self.assertTrue(info["expired"])
        self.assertFalse(info["signature_verified_locally"])

    def test_explicit_file_beats_environment(self):
        value = token()
        atomic_write(self.root / "official.token", value)
        with patch.dict(os.environ, {"CHARMMGUI_TOKEN": "wrong"}):
            self.assertEqual(load_token(self.root / "official.token")[0], value)
        self.assertEqual(stat.S_IMODE((self.root / "official.token").stat().st_mode), 0o600)

    def test_official_default_file_fallback(self):
        atomic_write(self.root / ".charmmgui_token", token())
        # Do not read a real session.token from the developer's working tree.
        original_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with patch("pathlib.Path.home", return_value=self.root), patch.dict(os.environ, {}, clear=True):
                value, source = load_token()
        finally:
            os.chdir(original_cwd)
        self.assertEqual(source, str(self.root / ".charmmgui_token"))
        self.assertEqual(token_info(value)["format"], "JWT")

    def test_auth_output_does_not_disclose_token(self):
        value = token()
        path = self.root / "official.token"
        atomic_write(path, value)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--token-file", str(path), "auth"]), 0)
        self.assertNotIn(value, output.getvalue())
        self.assertIn('"server_checked": false', output.getvalue())


class ApiTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.session = Mock()
        self.value = token()
        self.client = Client(self.value, self.session)

    def test_bearer_header_exactly_matches_official_protocol(self):
        self.session.request.return_value = response({"status": "running"})
        self.assertEqual(self.client.status("123")["status"], "running")
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("GET", API_BASE + "/check_status"))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer " + self.value})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["params"], {"jobid": "123"})
        self.assertEqual(kwargs["timeout"], (10, 60))

    def test_live_service_requires_jobid_even_for_auth_check(self):
        with self.assertRaisesRegex(ToolError, "requires a job ID"):
            self.client.status()
        self.session.request.assert_not_called()

    def test_auth_check_without_jobid_reports_actionable_error(self):
        path = self.root / "session.token"
        atomic_write(path, self.value)
        output = io.StringIO()
        with patch("requests.Session.request") as request, contextlib.redirect_stderr(output):
            self.assertEqual(main(["--token-file", str(path), "auth", "--check"]), 2)
        request.assert_not_called()
        self.assertIn("--jobid", output.getvalue())
        self.assertNotIn(self.value, output.getvalue())

    def test_auth_check_accepts_matching_new_job_with_null_status(self):
        path = self.root / "session.token"
        atomic_write(path, self.value)
        output = io.StringIO()
        with patch("requests.Session.request", return_value=response({"jobid": "123", "status": None})), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--token-file", str(path), "auth", "--check", "--jobid", "123"]), 0)
        info = json.loads(output.getvalue())
        self.assertTrue(info["server_checked"])
        self.assertEqual(info["job_access_confirmed"], "123")
        self.assertIsNone(info["remote_status"])

    def test_auth_check_rejects_explicit_mismatched_jobid(self):
        path = self.root / "session.token"
        atomic_write(path, self.value)
        output = io.StringIO()
        with patch("requests.Session.request", return_value=response({"jobid": "456", "status": "running"})), contextlib.redirect_stderr(output):
            self.assertEqual(main(["--token-file", str(path), "auth", "--check", "--jobid", "123"]), 2)
        self.assertIn("different job ID", output.getvalue())

    def test_login_does_not_send_old_token(self):
        self.session.request.return_value = response({"token": self.value})
        self.assertEqual(self.client.login("user@example.org", "secret"), self.value)
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(kwargs["headers"], {})
        self.assertEqual(kwargs["json"]["password"], "secret")

    def test_rejected_auth_and_html_responses_are_redacted(self):
        for code in (401, 403, 302, 500):
            self.session.request.return_value = response({"error": self.value}, code)
            with self.assertRaises(ToolError) as caught:
                self.client.status("123")
            self.assertNotIn(self.value, str(caught.exception))
        self.session.request.return_value = response({"error": self.value})
        with self.assertRaises(ToolError):
            self.client.status("123")
        html = response({})
        html._content = b"<html>login</html>"
        self.session.request.return_value = html
        with self.assertRaisesRegex(ToolError, "JSON"):
            self.client.status("123")

    def test_expired_token_never_sent(self):
        self.client.token = token(1)
        with self.assertRaises(AuthError):
            self.client.status("123")
        self.session.request.assert_not_called()

    def test_submit_timeout_is_ambiguous_and_not_retried(self):
        self.session.request.side_effect = requests.Timeout("sensitive details")
        with self.assertRaises(SubmissionUnknown) as caught:
            self.client.submit(payload_for(config()))
        self.assertNotIn("sensitive details", str(caught.exception))
        self.assertEqual(self.session.request.call_count, 1)

    def test_unacknowledged_submission_is_not_success(self):
        for data in ({"jobid": "234", "submitted": "false"}, {"submitted": "true"},
                     {"submitted": "true", "jobid": "../234"}):
            self.session.request.return_value = response(data)
            with self.assertRaises(SubmissionUnknown):
                self.client.submit(payload_for(config()))

    def test_documented_submission_acknowledgements(self):
        for acknowledgement in (True, "true"):
            self.session.request.return_value = response({"submitted": acknowledgement, "jobid": "234"})
            self.assertEqual(self.client.submit(payload_for(config())), "234")

    def test_json_download_is_not_saved_as_tgz(self):
        self.session.request.return_value = response({"error": "not ready"})
        destination = self.root / "result.tgz"
        with self.assertRaisesRegex(ToolError, "archive"):
            self.client.download("123", destination)
        self.assertFalse(destination.exists())
        self.assertEqual(len(list(self.root.glob(".download-*"))), 1)

    def test_download_valid_archive_without_overwriting(self):
        result = response({})
        result._content = archive_bytes({"step5_assembly.pdb": pdb("LIG")})
        self.session.request.return_value = result
        destination = self.root / "result.tgz"
        self.client.download("123", destination)
        self.assertTrue(tarfile.is_tarfile(destination))
        with self.assertRaisesRegex(ToolError, "overwrite"):
            self.client.download("123", destination)

    def test_download_rejects_truncated_payload_after_valid_first_header(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            payload = os.urandom(100000)
            member = tarfile.TarInfo("large.bin")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        packed = gzip.compress(raw.getvalue())
        result = response({})
        result._content = packed[:len(packed) // 2]
        self.session.request.return_value = result
        destination = self.root / "truncated.tgz"
        with self.assertRaises(ToolError):
            self.client.download("123", destination)
        self.assertFalse(destination.exists())
        self.assertEqual(len(list(self.root.glob(".download-*"))), 1)

    def test_download_rejects_invalid_gzip_crc_after_tar_end(self):
        packed = bytearray(archive_bytes({"step5_assembly.pdb": pdb("LIG")}))
        packed[-8] ^= 1
        result = response({})
        result._content = bytes(packed)
        self.session.request.return_value = result
        destination = self.root / "bad-crc.tgz"
        with self.assertRaises(ToolError):
            self.client.download("123", destination)
        self.assertFalse(destination.exists())


class ConfigTests(TemporaryTest):
    def test_incomplete_example_is_a_plan_not_a_submission(self):
        proposal = plan(load_config(Path(__file__).parents[1] / "examples/membrane-ligand.yaml"))
        self.assertFalse(proposal["ready_to_submit"])
        self.assertEqual(len(proposal["blockers"]), 2)

    def test_only_known_api_fields_and_explicit_ligand_retention(self):
        payload = payload_for(config())
        self.assertEqual(payload["heteroatoms"], "true")
        self.assertEqual(payload["clone_job"], "true")
        self.assertEqual(payload["ppm"], "true")
        self.assertIn("ion_conc", payload)
        self.assertIn("ion_type", payload)
        self.assertNotIn("Ion_conc", payload)
        self.assertNotIn("Ion_type", payload)
        self.assertNotIn("run_ppm", payload)
        self.assertNotIn("expected_ligands", payload)
        self.assertNotIn("verbose", payload)

    def test_nondefault_salt_uses_live_service_lowercase_fields(self):
        value = config()
        value["ions"] = {"type": "KCl", "concentration_M": 0.10}
        payload = payload_for(validate_config(value))
        self.assertEqual(payload["ion_type"], "KCl")
        self.assertEqual(payload["ion_conc"], "0.1")
        self.assertFalse({"Ion_type", "Ion_conc"} & payload.keys())

    def test_strict_config_rejects_typos_and_nonnumeric_settings(self):
        for bad in ({"source_job_id": 123}, {"unexpected": True}, {"expected_ligands": []}):
            value = config()
            value.update(bad)
            with self.assertRaises(ToolError):
                validate_config(value)
        for bad in (float("nan"), -1, True):
            value = config()
            value["membrane"]["margin_A"] = bad
            with self.assertRaises(ToolError):
                validate_config(value)

    def test_duplicate_yaml_fields_are_rejected(self):
        path = self.root / "config.yaml"
        atomic_write(path, "version: 1\nversion: 1\n")
        with self.assertRaisesRegex(ToolError, "unique"):
            load_config(path)

    def test_local_pdb_missing_ligand_blocks_submission(self):
        path = self.root / "input.pdb"
        atomic_write(path, pdb("ALA"))
        value = config()
        value["input_pdb"] = str(path)
        proposal = plan(value)
        self.assertFalse(proposal["ready_to_submit"])
        self.assertEqual(proposal["local_pdb"]["missing_ligands"], ["LIG"])


class ArtifactTests(TemporaryTest):
    def make_archive(self, entries):
        path = self.root / "result.tgz"
        path.write_bytes(archive_bytes(entries))
        return path

    def test_missing_final_ligand_does_not_pass_from_input_pdb(self):
        path = self.make_archive({"step1_pdbreader.pdb": pdb("LIG"), "step5_assembly.pdb": pdb("ALA")})
        report = inspect_archive(path, ["LIG"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["missing_ligands"], ["LIG"])

    def test_input_only_archive_cannot_be_validated(self):
        path = self.make_archive({"step1_pdbreader.pdb": pdb("LIG")})
        with self.assertRaisesRegex(ToolError, "assembled"):
            inspect_archive(path, ["LIG"])

    def test_archive_is_not_extracted_and_counts_ligand(self):
        path = self.make_archive({"../../escape.txt": "bad", "job/step5_assembly.pdb": pdb("LIG"), "job/lig/lig.str": "parameters"})
        report = inspect_archive(path, ["LIG"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["ligand_residue_counts"], {"LIG": 1})
        self.assertEqual(report["topology_parameter_files"], ["job/lig/lig.str"])
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_final_pdb_override(self):
        path = self.make_archive({"output/final.pdb": pdb("LIG")})
        self.assertTrue(inspect_archive(path, ["LIG"], "output/final.pdb")["passed"])


class StructureTests(TemporaryTest):
    def test_prepare_creates_handoff_without_credentials_or_network(self):
        path = self.root / "input.pdb"
        atomic_write(path, pdb("LIG"))
        output = self.root / "preparation"
        with patch("requests.Session.request") as request, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["prepare", str(path), "--ligand", "LIG", "--out", str(output)]), 0)
        request.assert_not_called()
        prepared = load_config(output / "system.yaml")
        self.assertIsNone(prepared["source_job_id"])
        self.assertFalse(prepared["preparation"]["ligand_parameters_ready"])
        self.assertEqual(prepared["input_pdb"], str(path))
        self.assertTrue((output / "preparation.json").exists())

    def test_nan_coordinates_are_rejected(self):
        path = self.root / "invalid.pdb"
        line = pdb("LIG")
        atomic_write(path, line[:30] + "     nan" + line[38:])
        with self.assertRaisesRegex(ToolError, "Malformed"):
            analyze_pdb(path, ["LIG"])

    def test_user_fixture_is_inventoried_without_assuming_parameterization(self):
        fixture = Path(__file__).parents[1] / "test-dataset/CrtW_BetaCarotene_af3_fixed.pdb"
        if not fixture.exists():
            self.skipTest("User's local fixture is not distributed with the code")
        info = analyze_pdb(fixture, ["LIG"])
        self.assertEqual(info["atoms_first_model"], 2584)
        self.assertEqual(info["chains"][0]["residues"], 320)
        ligand = info["ligands"][0]
        self.assertEqual(ligand["elements"], {"C": 40})
        self.assertEqual(ligand["conect_internal_bonds"], 41)
        self.assertEqual(ligand["conect_multiplicity_counts"], {"1": 30, "2": 11})
        self.assertAlmostEqual(ligand["minimum_protein_heavy_atom_distance_A"], 2.753)
        self.assertTrue(any("residue number 0" in warning for warning in info["warnings"]))


class WorkflowTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.client = Mock()
        self.client.submit.return_value = "234"
        self.directory = self.root / "run"

    def test_live_queue_status_strings(self):
        self.assertEqual(workflow.status_summary({"status": "running quick_bilayer"})["status"], "running")
        self.assertEqual(workflow.status_summary({"status": "submitted"})["status"], "pending")
        with self.assertRaises(ToolError):
            workflow.status_summary({"status": None, "rqinfo": "0 queued jobs"})

    def test_intent_persisted_before_post_and_no_duplicate_run(self):
        def submit(payload):
            manifest = workflow.read(self.directory)
            self.assertEqual(manifest["state"], "submitting")
            self.assertIsNone(manifest["job_id"])
            return "234"
        self.client.submit.side_effect = submit
        result = workflow.start(self.client, config(), self.directory)
        self.assertEqual(result["state"], "submitted")
        with self.assertRaisesRegex(ToolError, "already exists"):
            workflow.start(self.client, config(), self.directory)
        self.assertEqual(self.client.submit.call_count, 1)

    def test_timeout_survives_restart_without_resubmitting(self):
        self.client.submit.side_effect = SubmissionUnknown("timeout")
        with self.assertRaises(SubmissionUnknown):
            workflow.start(self.client, config(), self.directory)
        self.assertEqual(workflow.read(self.directory)["state"], "submission_unknown")
        with self.assertRaisesRegex(ToolError, "unknown"):
            workflow.resume(self.client, self.directory)
        self.assertEqual(self.client.submit.call_count, 1)
        self.client.status.assert_not_called()

    def test_attach_requires_ambiguous_run_and_new_accessible_job(self):
        self.client.submit.side_effect = SubmissionUnknown("timeout")
        with self.assertRaises(SubmissionUnknown):
            workflow.start(self.client, config(), self.directory)
        self.client.status.return_value = {"status": "running"}
        with self.assertRaises(ToolError):
            workflow.attach(self.client, self.directory, "123")
        self.assertEqual(workflow.attach(self.client, self.directory, "234")["job_id"], "234")
        with self.assertRaises(ToolError):
            workflow.attach(self.client, self.directory, "345")

    def test_complete_and_resume_reuses_download(self):
        workflow.start(self.client, config(), self.directory)
        self.client.status.return_value = {"status": "done"}
        self.client.download.side_effect = lambda _, path: path.write_bytes(
            archive_bytes({"job/step5_assembly.pdb": pdb("LIG")})
        )
        for _ in range(2):
            result = workflow.resume(self.client, self.directory)
            self.assertEqual(result["state"], "ligands_present")
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertEqual(self.client.download.call_count, 1)
        self.assertEqual(self.client.status.call_count, 1)

    def test_remote_failure_is_recorded(self):
        workflow.start(self.client, config(), self.directory)
        self.client.status.return_value = {"status": "error"}
        with self.assertRaisesRegex(ToolError, "failed"):
            workflow.resume(self.client, self.directory)
        self.assertEqual(workflow.read(self.directory)["state"], "remote_error")
        self.client.download.assert_not_called()

    def test_pending_status_does_not_download(self):
        workflow.start(self.client, config(), self.directory)
        self.client.status.return_value = {"status": "pending"}
        self.assertEqual(workflow.resume(self.client, self.directory)["state"], "pending")
        self.client.download.assert_not_called()

    def test_missing_ligand_is_persisted_as_failure(self):
        workflow.start(self.client, config(), self.directory)
        self.client.status.return_value = {"status": "done"}
        self.client.download.side_effect = lambda _, path: path.write_bytes(
            archive_bytes({"step5_assembly.pdb": pdb("ALA")})
        )
        with self.assertRaisesRegex(ToolError, "missing"):
            workflow.resume(self.client, self.directory)
        self.assertEqual(workflow.read(self.directory)["state"], "validation_failed")
        self.assertTrue((self.directory / "validation.json").exists())

    def test_lock_rejects_concurrent_operation(self):
        self.directory.mkdir()
        with workflow.run_lock(self.directory):
            with self.assertRaisesRegex(ToolError, "Another process"):
                with workflow.run_lock(self.directory):
                    self.fail("must not acquire lock")


if __name__ == "__main__":
    unittest.main()
