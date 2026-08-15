from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
REFERENCES = SKILL_ROOT / "references"
sys.path.insert(0, str(SCRIPTS))

import attestation_verify  # noqa: E402
import event_journal  # noqa: E402
import memory_eval  # noqa: E402
import policy_gate  # noqa: E402
import privacy_gate  # noqa: E402
import telemetry_projection  # noqa: E402


def hash_ref(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_data(run_id: str, **overrides):
    value = {
        "run_id": run_id,
        "task_id": None,
        "dispatch_id": None,
        "entity_id": None,
        "state": "ready",
        "outcome_code": "ACCEPTED",
        "artifact_sha256": None,
        "policy_decision_id": None,
        "reason_codes": [],
    }
    value.update(overrides)
    return value


def proposal(suffix: str, run_id: str, event_type: str, **data_overrides):
    return {
        "id": f"evt_{suffix}_0000000000000000",
        "type": event_type,
        "time": "2026-08-11T00:00:00Z",
        "data": safe_data(run_id, **data_overrides),
    }


class EventJournalTests(unittest.TestCase):
    def test_append_interleaved_runs_and_verify_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal-root"
            root.mkdir(mode=0o700)
            journal = root / "events.jsonl"
            first = event_journal.append_event(
                journal,
                proposal("alpha", "run_alpha", "orca.run.created"),
            )
            event_journal.append_event(
                journal,
                proposal("bravo", "run_bravo", "orca.run.created"),
            )
            third = event_journal.append_event(
                journal,
                proposal(
                    "charlie",
                    "run_alpha",
                    "orca.task.ready",
                    task_id="task_alpha",
                ),
            )
            verified = event_journal.verify_journal(journal)
            self.assertEqual(verified["event_count"], 3)
            self.assertEqual(verified["run_count"], 2)
            self.assertEqual(first["sequence"], 0)
            self.assertEqual(third["sequence"], 1)
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)

    def test_tamper_and_hardlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal-root"
            root.mkdir(mode=0o700)
            journal = root / "events.jsonl"
            event_journal.append_event(journal, proposal("delta", "run_delta", "orca.run.created"))
            row = json.loads(journal.read_text(encoding="utf-8"))
            row["data"]["state"] = "settled"
            journal.write_bytes(event_journal.canonical_bytes(row) + b"\n")
            with self.assertRaises(event_journal.JournalError) as caught:
                event_journal.verify_journal(journal)
            self.assertEqual(caught.exception.code, "EVENT_DIGEST_MISMATCH")

            journal.unlink()
            event_journal.append_event(journal, proposal("echo", "run_echo", "orca.run.created"))
            os.link(journal, root / "events-hardlink.jsonl")
            with self.assertRaises(event_journal.JournalError) as caught:
                event_journal.verify_journal(journal)
            self.assertEqual(caught.exception.code, "JOURNAL_FILE_REJECTED")

    def test_verify_missing_is_read_only_and_extra_body_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "journal-root"
            root.mkdir(mode=0o700)
            journal = root / "missing.jsonl"
            with self.assertRaises(FileNotFoundError):
                event_journal.verify_journal(journal)
            self.assertFalse(journal.exists())
            unsafe = proposal("foxtrot", "run_foxtrot", "orca.run.created")
            unsafe["data"]["prompt"] = "forbidden"
            with self.assertRaises(event_journal.JournalError) as caught:
                event_journal.append_event(journal, unsafe)
            self.assertEqual(caught.exception.code, "EVENT_SCHEMA_REJECTED")
            self.assertFalse(journal.exists())


def all_true_facts():
    return {
        "cwd_on_expected_ssd": True,
        "git_common_dir_on_expected_ssd": True,
        "volume_uuid_matches": True,
        "apfs": True,
        "filevault_enabled": True,
        "volume_unlocked": True,
        "owners_enabled": True,
        "runtime_ready": True,
        "graph_ready": True,
        "context_ack_verified": True,
        "same_bundle_verified": True,
        "authority_fresh": True,
        "write_set_disjoint": True,
        "user_authorized": True,
        "r2_fresh_verified": True,
        "old_paths_lsof_zero": True,
        "final_setup_authorized": True,
        "capacity_gate": "green",
    }


class PolicyGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = json.loads((REFERENCES / "orca-safety-policy.v1.json").read_text(encoding="utf-8"))

    def request(self, operation: str):
        return {
            "decision_id": "decision_fixture",
            "operation": operation,
            "observed_at": "2026-08-11T01:00:00Z",
            "policy_expected_sha256": policy_gate.policy_digest(self.policy),
            "minimum_policy_revision": 1,
            "facts": all_true_facts(),
        }

    def test_digest_bound_policy_allows_complete_offline_review(self):
        receipt = policy_gate.evaluate(self.policy, self.request("offline_review"))
        self.assertEqual(receipt["decision"], "allow")
        self.assertRegex(receipt["receipt_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("facts", receipt)

    def test_red_capacity_and_missing_delete_gate_fail_closed(self):
        request = self.request("spawn_workers")
        request["facts"]["capacity_gate"] = "red"
        receipt = policy_gate.evaluate(self.policy, request)
        self.assertEqual(receipt["decision"], "deny")
        self.assertIn("CAPACITY_RED", receipt["reason_codes"])

        request = self.request("delete_internal_project")
        request["facts"]["r2_fresh_verified"] = False
        request["facts"]["old_paths_lsof_zero"] = False
        receipt = policy_gate.evaluate(self.policy, request)
        self.assertEqual(receipt["decision"], "deny")
        self.assertTrue(any("R2_FRESH_VERIFIED" in item for item in receipt["reason_codes"]))

    def test_digest_rollback_expiry_and_unknown_fields_deny(self):
        request = self.request("offline_review")
        request["policy_expected_sha256"] = hash_ref("wrong")
        request["minimum_policy_revision"] = 2
        request["observed_at"] = "2031-01-01T00:00:00Z"
        receipt = policy_gate.evaluate(self.policy, request)
        self.assertEqual(receipt["decision"], "deny")
        self.assertIn("POLICY_DIGEST_MISMATCH", receipt["reason_codes"])
        self.assertIn("POLICY_ROLLBACK_REJECTED", receipt["reason_codes"])
        self.assertIn("POLICY_EXPIRED", receipt["reason_codes"])
        request = self.request("offline_review")
        request["facts"]["prompt"] = False
        with self.assertRaises(policy_gate.PolicyError):
            policy_gate.evaluate(self.policy, request)


def passing_suite():
    categories = sorted(memory_eval.CATEGORIES)
    cases = []
    for index, category in enumerate(categories):
        memory_id = f"memory_item_{index:04d}"
        abstain = category == "premise_awareness"
        cases.append(
            {
                "case_id": f"case_fixture_{index:04d}",
                "category": category,
                "query_sha256": hash_ref(f"query-{index}"),
                "scope_sha256": hash_ref("scope"),
                "expected_active_ids": [] if abstain else [memory_id],
                "expected_revoked_ids": [f"memory_revoked_{index:04d}"] if category == "revocation" else [],
                "retrieved_ids": [] if abstain else [memory_id],
                "expected_abstain": abstain,
                "abstained": abstain,
                "latency_ms": 10 + index,
                "token_count": 20,
            }
        )
    return {
        "schema_version": 1,
        "suite_id": "suite_fixture_complete",
        "cases": cases,
        "thresholds": {
            "minimum_precision": 1.0,
            "minimum_recall": 1.0,
            "minimum_mrr": 1.0,
            "minimum_abstention_accuracy": 1.0,
            "maximum_revoked_hits": 0,
            "maximum_p95_latency_ms": 100,
            "maximum_total_tokens": 1_000,
        },
    }


class MemoryEvalTests(unittest.TestCase):
    def test_complete_suite_passes_without_text(self):
        receipt = memory_eval.score_suite(passing_suite())
        self.assertEqual(receipt["result"], "pass")
        self.assertEqual(receipt["case_count"], len(memory_eval.CATEGORIES))
        self.assertEqual(receipt["metrics"]["revoked_hits"], 0)
        self.assertNotIn("query", json.dumps(receipt))

    def test_revoked_hit_and_bad_abstention_fail(self):
        suite = passing_suite()
        revoked_case = next(item for item in suite["cases"] if item["category"] == "revocation")
        revoked_case["retrieved_ids"].append(revoked_case["expected_revoked_ids"][0])
        abstain_case = next(item for item in suite["cases"] if item["expected_abstain"])
        abstain_case["abstained"] = False
        receipt = memory_eval.score_suite(suite)
        self.assertEqual(receipt["result"], "fail")
        self.assertIn("REVOKED_MEMORY_RETURNED", receipt["reason_codes"])
        self.assertIn("ABSTENTION_BELOW_THRESHOLD", receipt["reason_codes"])

    def test_missing_category_is_rejected(self):
        suite = passing_suite()
        suite["cases"].pop()
        with self.assertRaises(memory_eval.EvalError) as caught:
            memory_eval.score_suite(suite)
        self.assertEqual(caught.exception.code, "EVAL_CATEGORY_COVERAGE_REQUIRED")


class PrivacyGateTests(unittest.TestCase):
    def test_safe_receipt_passes_without_echoing_values(self):
        value = {
            "format": "orca-safe-receipt-v1",
            "status": "verified",
            "artifact_sha256": hash_ref("artifact"),
            "reason_codes": ["SSD_GATE_PASS", "CONTENT_CAPTURE_DISABLED"],
        }
        receipt = privacy_gate.scan(value)
        self.assertEqual(receipt["status"], "pass")
        self.assertRegex(receipt["value_sha256"], r"^sha256:[0-9a-f]{64}$")
        serialized = json.dumps(receipt, sort_keys=True)
        self.assertNotIn("artifact", serialized)
        self.assertNotIn("SSD_GATE_PASS", serialized)

    def test_forbidden_fields_and_sensitive_shapes_fail_with_stable_codes(self):
        cases = [
            ({"prompt_body": hash_ref("body")}, "FORBIDDEN_FIELD_REJECTED"),
            ({"value": "Bearer synthetic-not-a-real-secret"}, "SECRET_SHAPE_REJECTED"),
            ({"value": "-----BEGIN PRIVATE KEY-----"}, "SECRET_SHAPE_REJECTED"),
            ({"value": "/Users/example/private.txt"}, "ABSOLUTE_PATH_REJECTED"),
            ({"value": "fixture@example.invalid"}, "PII_SHAPE_REJECTED"),
        ]
        for value, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(privacy_gate.PrivacyError) as caught:
                    privacy_gate.scan(value)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertNotIn(next(iter(value.values())), str(caught.exception))

    def test_paperclip_credential_key_parts_reject_opaque_values_without_echo(self):
        cases = (
            ("api_key", "opaque-paperclip-api-key-fixture"),
            ("apikey", "opaque-paperclip-apikey-fixture"),
            ("jwt", "opaque-paperclip-jwt-fixture"),
            ("run_jwt", "opaque-paperclip-run-jwt-fixture"),
            ("bridge_token", "opaque-paperclip-bridge-token-fixture"),
            ("API-Key", "opaque-paperclip-normalized-api-key-fixture"),
            ("runJwt", "opaque-paperclip-normalized-run-jwt-fixture"),
            ("bridgeToken", "opaque-paperclip-normalized-bridge-token-fixture"),
        )
        for key, opaque_value in cases:
            with self.subTest(key=key):
                value = {"envelope": {"metadata": {key: opaque_value}}}
                with self.assertRaises(privacy_gate.PrivacyError) as caught:
                    privacy_gate.scan(value)
                self.assertEqual(caught.exception.code, "FORBIDDEN_FIELD_REJECTED")
                self.assertNotIn(opaque_value, str(caught.exception))

    def test_paperclip_opaque_value_is_not_echoed_by_cli_rejection(self):
        opaque_value = "opaque-paperclip-cli-bridge-fixture"
        with tempfile.TemporaryDirectory() as temporary:
            input_path = Path(temporary) / "privacy-input.json"
            input_path.write_text(
                json.dumps({"wrapper": {"bridge_token": opaque_value}}),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "privacy_gate.py"), "--input", str(input_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            json.loads(completed.stdout),
            {"status": "blocked", "error_code": "FORBIDDEN_FIELD_REJECTED"},
        )
        self.assertNotIn(opaque_value, completed.stdout)
        self.assertNotIn(opaque_value, completed.stderr)


class TelemetryProjectionTests(unittest.TestCase):
    def _journal(self, root: Path) -> Path:
        journal = root / "events.jsonl"
        event_journal.append_event(
            journal,
            proposal("telemetrya", "run_telemetry", "orca.run.created"),
        )
        event_journal.append_event(
            journal,
            proposal(
                "telemetryb",
                "run_telemetry",
                "orca.worker.done",
                task_id="task_telemetry",
                dispatch_id="ctx_telemetry",
                state="settled",
                outcome_code="TASK_FAILED",
                artifact_sha256=hash_ref("artifact"),
                reason_codes=["ERROR_SYNTHETIC_FIXTURE"],
            ),
        )
        return journal

    def test_redacted_local_projection_has_no_content_or_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry-root"
            root.mkdir(mode=0o700)
            journal = self._journal(root)
            records = event_journal.parse_journal(journal.read_bytes())
            projection = telemetry_projection.project_records(records)
            self.assertEqual(projection["format"], telemetry_projection.FORMAT)
            self.assertFalse(projection["export_enabled"])
            spans = projection["resourceSpans"][0]["scopeSpans"][0]["spans"]
            self.assertEqual(len(spans), 2)
            self.assertEqual(len(spans[0]["traceId"]), 32)
            self.assertEqual(len(spans[0]["spanId"]), 16)
            self.assertEqual(spans[1]["status"]["code"], 2)
            serialized = json.dumps(projection, sort_keys=True)
            for forbidden in ("prompt", "message_body", "tool_output", "/Users/", "/Volumes/"):
                self.assertNotIn(forbidden, serialized)
            self.assertEqual(privacy_gate.scan(projection)["status"], "pass")

    def test_output_is_exclusive_mode_0600_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry-root"
            root.mkdir(mode=0o700)
            projection = telemetry_projection.project_records(
                event_journal.parse_journal(self._journal(root).read_bytes())
            )
            output = root / "projection.json"
            payload = privacy_gate.canonical_bytes(projection) + b"\n"
            telemetry_projection._write_exclusive(output, payload)
            self.assertEqual(output.read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                telemetry_projection._write_exclusive(output, payload)

            output.unlink()
            outside = root / "outside.json"
            outside.write_bytes(b"sentinel\n")
            link = root / "projection.json"
            link.symlink_to(outside)
            with self.assertRaises(FileExistsError):
                telemetry_projection._write_exclusive(link, payload)
            self.assertEqual(outside.read_bytes(), b"sentinel\n")


class AttestationTests(unittest.TestCase):
    def _make_envelope(self, root: Path):
        private_key = root / "synthetic-private.pem"
        public_key = root / "synthetic-public.pem"
        subprocess.run(
            ["/usr/bin/openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", private_key],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.chmod(private_key, 0o600)
        subprocess.run(
            ["/usr/bin/openssl", "pkey", "-in", private_key, "-pubout", "-out", public_key],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        keyid = attestation_verify.sha256_ref(public_key.read_bytes())
        candidate = hash_ref("candidate")
        statement = {
            "_type": attestation_verify.STATEMENT_TYPE,
            "subject": [{"name": "review-orca-workflow-learning", "digest": {"sha256": candidate.removeprefix("sha256:")}}],
            "predicateType": attestation_verify.PREDICATE_TYPE,
            "predicate": {
                "schema_version": 1,
                "candidate_core_sha256": candidate,
                "source_event_chain_sha256": hash_ref("events"),
                "policy_sha256": hash_ref("policy"),
                "rollback_preimage_sha256": hash_ref("rollback"),
                "review_threshold": 1,
                "reviewer_keyids": [keyid],
                "created_at": "2026-08-11T00:00:00Z",
                "expires_at": "2026-09-11T00:00:00Z",
            },
        }
        payload = attestation_verify.canonical_bytes(statement)
        pae = attestation_verify.dsse_pae(attestation_verify.PAYLOAD_TYPE, payload)
        pae_path = root / "pae.bin"
        signature_path = root / "signature.bin"
        pae_path.write_bytes(pae)
        subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-sign", private_key, "-out", signature_path, pae_path],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        envelope = {
            "payloadType": attestation_verify.PAYLOAD_TYPE,
            "payload": base64.b64encode(payload).decode("ascii"),
            "signatures": [{"keyid": keyid, "sig": base64.b64encode(signature_path.read_bytes()).decode("ascii")}],
        }
        return envelope, {keyid: os.fspath(public_key)}, private_key

    def test_valid_threshold_attestation_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "attestation-root"
            root.mkdir(mode=0o700)
            scratch = root / "scratch"
            scratch.mkdir(mode=0o700)
            envelope, keys, _ = self._make_envelope(root)
            verified = attestation_verify.verify_envelope(
                envelope,
                keys,
                observed_at="2026-08-11T01:00:00Z",
                scratch_root=scratch,
            )
            self.assertEqual(verified["status"], "verified")
            self.assertEqual(verified["valid_signature_count"], 1)
            tampered = copy.deepcopy(envelope)
            signature = base64.b64decode(tampered["signatures"][0]["sig"])
            tampered["signatures"][0]["sig"] = base64.b64encode(signature[:-1] + bytes([signature[-1] ^ 1])).decode("ascii")
            with self.assertRaises(attestation_verify.AttestationError) as caught:
                attestation_verify.verify_envelope(
                    tampered,
                    keys,
                    observed_at="2026-08-11T01:00:00Z",
                    scratch_root=scratch,
                )
            self.assertEqual(caught.exception.code, "ATTESTATION_THRESHOLD_UNMET")

    def test_private_key_is_never_accepted_as_trust_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "attestation-root"
            root.mkdir(mode=0o700)
            scratch = root / "scratch"
            scratch.mkdir(mode=0o700)
            envelope, keys, private_key = self._make_envelope(root)
            keyid = next(iter(keys))
            with self.assertRaises(attestation_verify.AttestationError) as caught:
                attestation_verify.verify_envelope(
                    envelope,
                    {keyid: os.fspath(private_key)},
                    observed_at="2026-08-11T01:00:00Z",
                    scratch_root=scratch,
                )
            self.assertEqual(caught.exception.code, "PUBLIC_KEY_REJECTED")


if __name__ == "__main__":
    unittest.main()
