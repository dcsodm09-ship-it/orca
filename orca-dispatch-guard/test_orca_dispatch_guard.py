#!/usr/bin/env python3
"""Unit + isolation tests for orca_dispatch_guard.py.

Covers: the two pure detection functions (unaffected by the envelope-nesting
fix, since they scan raw text) and the diagnostics/id/message extraction
helpers against the REAL nested `orca ... --json` envelope shape (confirmed
live against the actual `orca` binary in this worktree on 2026-08-26 -- see
the commands and raw output recorded in this round's review notes, and the
CONFIRMED REAL ENVELOPE SHAPE note in orca_dispatch_guard.py's own
docstring); the full `start` happy path; the false-positive recovery flow
end to end (task-list spec lookup -> tui-idle wait -> terminal read ->
task-create -> worker-start --retry-of), including that the harvest prompt
actually carries the original task's own spec text and still degrades to a
bare task id when that lookup fails or the task is absent; idempotent
journal bookkeeping across a second recovery attempt
on the SAME original dispatch id (remount_count 1 -> 2, captured tail never
overwritten); the remount cap (4th attempt refuses and marks "gave_up"); a
persistently-failing task-create still counting toward that cap instead of
retrying forever uncapped; a remount that exits 0 but whose own dispatch id
cannot be extracted being treated as a failure, not a silent success; the
"still busy, try again later" branch (no remount, journal untouched); a
genuine (non-false-positive) failure passing through byte-for-byte
unchanged; real-thread concurrency proving the per-terminal fcntl.flock lock
actually serializes two `start` invocations against the same terminal
handle end to end, with no lost journal update; a second, independent
real-thread test proving `wait`'s own "mark journal recovered" write is
genuinely serialized against a concurrently-held recovery lock rather than
racing it; path-traversal safety of the on-disk journal filename against a
hostile dispatch id; and a real-path negative control proving no test in
this file ever touches the actual `~/.orca/dispatch-guard/` directory.

Every test that exercises the CLI mocks `subprocess.run` -- the real `orca`
binary is never invoked -- and every test rebinds the module-level
`STATE_ROOT` constant to a location under `tempfile.mkdtemp()`, mirroring
this codebase's own established convention (see e.g.
test_promote_capability.py's `PROMOTION_ROOT` monkeypatch).

Run with:
    python3 -m unittest test_orca_dispatch_guard -v
(from this directory), or plain `python3 test_orca_dispatch_guard.py`.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import orca_dispatch_guard as dg  # noqa: E402


# ---------------------------------------------------------------------------
# Small helpers for building REAL-shaped `orca ... --json` envelope bodies,
# matching what was actually observed live (see module docstring).
# ---------------------------------------------------------------------------


def ok_envelope(result: dict) -> str:
    return json.dumps({"id": "rpc-tracking-id", "ok": True, "result": result, "_meta": {"runtimeId": "rt-1"}})


def err_envelope(code: str, message: str) -> str:
    return json.dumps(
        {"id": "local", "ok": False, "error": {"code": code, "message": message}, "_meta": {"runtimeId": None}}
    )


# ---------------------------------------------------------------------------
# Module-wide safety net: prove the REAL ~/.orca/dispatch-guard/ directory is
# never touched by this entire test run, no matter which tests execute.
# ---------------------------------------------------------------------------

REAL_STATE_ROOT = Path.home() / ".orca" / "dispatch-guard"
_REAL_STATE_ROOT_EXISTED_BEFORE: bool | None = None


def setUpModule() -> None:
    global _REAL_STATE_ROOT_EXISTED_BEFORE
    _REAL_STATE_ROOT_EXISTED_BEFORE = REAL_STATE_ROOT.exists()


def tearDownModule() -> None:
    exists_after = REAL_STATE_ROOT.exists()
    if not _REAL_STATE_ROOT_EXISTED_BEFORE and exists_after:
        raise AssertionError(
            f"REAL {REAL_STATE_ROOT} was created during this test run -- this must never happen. "
            "Every test must rebind orca_dispatch_guard.STATE_ROOT before touching disk state."
        )


# ---------------------------------------------------------------------------
# Fake `orca` CLI router -- patched in for subprocess.run. Thread-safe (the
# concurrency tests drive it from real threads) and tracks the maximum
# number of overlapping calls actually observed, which is the concurrency
# test's central assertion.
# ---------------------------------------------------------------------------


def _key_for(args: list[str]) -> str:
    a = args[1:]  # args[0] is the "orca" binary name
    if a[:2] == ["orchestration", "worker-start"]:
        return "worker-start-retry" if "--retry-of" in a else "worker-start"
    if a[:2] == ["orchestration", "task-create"]:
        return "task-create"
    if a[:2] == ["orchestration", "task-list"]:
        return "task-list"
    if a[:2] == ["terminal", "wait"]:
        return "terminal-wait"
    if a[:2] == ["terminal", "read"]:
        return "terminal-read"
    if a[:2] == ["orchestration", "check"]:
        return "check" if "--ack" not in a else "check-ack"
    return "unknown:" + " ".join(a)


def cp(args: list[str], returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class OrcaCallRouter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[list[str]] = []
        self.handlers: dict[str, list[subprocess.CompletedProcess]] = {}
        self.active = 0
        self.max_active = 0

    def queue(self, key: str, *results: subprocess.CompletedProcess) -> None:
        self.handlers.setdefault(key, []).extend(results)

    def __call__(self, args: list[str], **_kwargs) -> subprocess.CompletedProcess:
        key = _key_for(list(args))
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(list(args))
            queue = self.handlers.get(key)
            if not queue:
                self.active -= 1
                raise AssertionError(f"unexpected orca call for key {key!r}: {args}")
            result = queue.pop(0)
        # A brief window outside the router's own lock -- if the real
        # per-terminal TerminalLock did NOT serialize callers, this is where
        # two threads' calls would genuinely overlap and max_active would
        # exceed 1.
        time.sleep(0.01)
        with self._lock:
            self.active -= 1
        return result


# ---------------------------------------------------------------------------
# Base test case: isolated tmp tree + isolated STATE_ROOT + mocked
# subprocess.run.
# ---------------------------------------------------------------------------


class DispatchGuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-guard-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

        self._orig_state_root = dg.STATE_ROOT
        dg.STATE_ROOT = self.tmp / "state-root"
        self.addCleanup(self._restore_state_root)

        self.router = OrcaCallRouter()
        self._patcher = mock.patch.object(subprocess, "run", side_effect=self.router)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _restore_state_root(self) -> None:
        dg.STATE_ROOT = self._orig_state_root

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dg.main(argv)
        return code, out.getvalue(), err.getvalue()

    def journal_path_for(self, original_dispatch_id: str) -> Path:
        return dg._journal_path(original_dispatch_id)

    def read_journal_for(self, original_dispatch_id: str) -> dict:
        return json.loads(self.journal_path_for(original_dispatch_id).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Detection: pure functions, zero subprocess calls, unaffected by the
# envelope-nesting fix (they scan raw combined stdout+stderr text).
# ---------------------------------------------------------------------------


class DetectionFunctionTests(unittest.TestCase):
    def test_is_stalled_false_positive_true_on_substring_with_nonzero_exit(self) -> None:
        stdout = json.dumps({"ok": False, "stage": "submit", "failedStage": "submit", "reason": "agent_prompt_stalled"})
        self.assertTrue(dg.is_stalled_false_positive(stdout, "", 1))

    def test_is_stalled_false_positive_in_stderr_too(self) -> None:
        self.assertTrue(dg.is_stalled_false_positive("", "boom: agent_prompt_stalled detected", 1))

    def test_is_stalled_false_positive_false_when_exit_code_zero(self) -> None:
        # Even if the substring somehow appears in a successful call's own
        # output (e.g. echoed spec text), a 0 exit is never treated as this
        # failure signature.
        self.assertFalse(dg.is_stalled_false_positive("mentions agent_prompt_stalled in passing", "", 0))

    def test_is_stalled_false_positive_false_when_absent(self) -> None:
        self.assertFalse(dg.is_stalled_false_positive('{"ok": false, "reason": "something_else"}', "", 1))

    def test_is_stalled_false_positive_false_on_empty_strings(self) -> None:
        self.assertFalse(dg.is_stalled_false_positive("", "", 1))

    def test_is_capability_revoked_detects_dispatch_capability_invalid(self) -> None:
        self.assertTrue(dg.is_capability_revoked_failure('{"error": "dispatch_capability_invalid"}', ""))

    def test_is_capability_revoked_detects_capability_is_revoked_phrase(self) -> None:
        self.assertTrue(dg.is_capability_revoked_failure("", "refused: capability is revoked for this dispatch"))

    def test_is_capability_revoked_false_when_absent(self) -> None:
        self.assertFalse(dg.is_capability_revoked_failure('{"ok": true}', ""))


# ---------------------------------------------------------------------------
# Detection must never match on CALLER-ECHOED text. Regression tests for the
# confirmed P2: dispatch specs in this project routinely quote these exact
# phrases (this very audit's tasks did), so an unrelated failure whose error
# body echoes the submitted spec back used to be misread as the stall/
# revocation signature -- which for `is_stalled_false_positive()` triggers a
# real recovery (new task + worker-start), i.e. a duplicate real dispatch.
# ---------------------------------------------------------------------------


class DetectionCallerEchoTests(unittest.TestCase):
    def test_stalled_false_positive_ignores_task_spec_echo_when_failed_stage_differs(self) -> None:
        # The exact confirmed repro: an unrelated failure (`terminal_busy`),
        # with a structured failedStage that is something else entirely, whose
        # body merely echoes back a task spec that NAMES the phrase.
        body = json.dumps(
            {
                "id": "rpc-tracking-id",
                "ok": False,
                "error": {
                    "code": "terminal_busy",
                    "failedStage": "submit",
                    "message": "terminal is busy",
                    "taskSpec": "Investigate the agent_prompt_stalled false positive and report",
                },
                "_meta": {"runtimeId": "rt-1"},
            }
        )
        self.assertFalse(
            dg.is_stalled_false_positive(body, "", 1),
            "a caller's own spec text naming the phrase must never trigger recovery",
        )

    def test_capability_revoked_ignores_spec_echo_when_failed_stage_differs(self) -> None:
        body = json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "some_unrelated_problem",
                    "failedStage": "submit",
                    "spec": "docs mention dispatch_capability_invalid",
                },
            }
        )
        self.assertFalse(dg.is_capability_revoked_failure(body, ""))

    def test_echoed_field_variants_and_nested_json_encoded_payload_are_all_excluded(self) -> None:
        # Name normalization (camelCase / snake_case / SCREAMING) and the
        # JSON-encoded-string `payload` shape a real message uses.
        for field in ("spec", "taskSpec", "task_spec", "TASK-SPEC", "prompt", "task_title", "displayName", "objective"):
            with self.subTest(field=field):
                body = json.dumps({"ok": False, "error": {"code": "boom", field: "about agent_prompt_stalled"}})
                self.assertFalse(dg.is_stalled_false_positive(body, "", 1))
        nested = json.dumps(
            {"ok": False, "error": {"code": "boom", "payload": json.dumps({"spec": "about agent_prompt_stalled"})}}
        )
        self.assertFalse(dg.is_stalled_false_positive(nested, "", 1))

    def test_cli_authored_fields_alongside_an_echoed_spec_still_detect(self) -> None:
        # The exclusion must not blind detection: the same body that echoes a
        # spec ALSO carries the signature in a CLI-authored field, so this is
        # a genuine occurrence and must still be caught.
        body = json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "agent_prompt_stalled",
                    "message": "agent_prompt_stalled: worker prompt did not settle",
                    "taskSpec": "some unrelated task text",
                },
            }
        )
        self.assertTrue(dg.is_stalled_false_positive(body, "", 1))

    def test_non_json_free_text_is_still_searched_whole(self) -> None:
        # Documented residual limitation: free text offers no structure to
        # strip, so it is searched as-is (unchanged from before this fix).
        self.assertTrue(dg.is_stalled_false_positive("", "worker-start failed: agent_prompt_stalled", 1))

    def test_capability_revoked_hit_ignores_a_message_whose_spec_merely_quotes_the_phrase(self) -> None:
        # End-to-end through the read-side helper: a real worker_done for a
        # watched id whose echoed spec names the phrase is NOT a revocation.
        message = {
            "id": "msg-echo",
            "type": "worker_done",
            "payload": json.dumps(
                {"dispatchId": "d-echo", "spec": "fix the dispatch_capability_invalid handling"}
            ),
        }
        parsed = json.loads(ok_envelope({"messages": [message]}))
        self.assertIsNone(dg._find_capability_revoked_hit(parsed, {"d-echo"}))


# ---------------------------------------------------------------------------
# Extraction against the REAL nested envelope shape -- this is the class of
# bug the previous round shipped: every helper below reads keys off the bare
# top level while every real `orca ... --json` response wraps its payload
# one level down in "result"/"error". These tests are built from envelope
# shapes actually observed live against the real `orca` binary (see module
# docstring's CONFIRMED REAL ENVELOPE SHAPE note and this file's own
# docstring for the exact commands run).
# ---------------------------------------------------------------------------


class RealEnvelopeExtractionTests(unittest.TestCase):
    # -- extract_dispatch_id ------------------------------------------------

    def test_extract_dispatch_id_real_nested_shape(self) -> None:
        # Matches live `orchestration worker-list --json` output:
        # result.dispatchId, camelCase, directly on `result`.
        parsed = json.loads(ok_envelope({"dispatchId": "ctx_918690fad6d5", "taskId": "task_ce4bd791751e"}))
        self.assertEqual(dg.extract_dispatch_id(parsed), "ctx_918690fad6d5")

    def test_extract_dispatch_id_nested_dispatch_object_fallback(self) -> None:
        parsed = json.loads(ok_envelope({"dispatch": {"id": "ctx_nested"}}))
        self.assertEqual(dg.extract_dispatch_id(parsed), "ctx_nested")

    def test_extract_dispatch_id_none_when_result_lacks_it(self) -> None:
        parsed = json.loads(ok_envelope({"someOtherField": "x"}))
        self.assertIsNone(dg.extract_dispatch_id(parsed))

    def test_extract_dispatch_id_does_not_fall_back_to_outer_rpc_tracking_id(self) -> None:
        # THE regression this round exists to close: a wrapped envelope
        # (has "error") with no dispatch id anywhere inside it must return
        # None -- it must NEVER fall back to scanning the bare top level,
        # whose own "id" is the outer RPC tracking id (a real, confirmed
        # `worker-start --task task_doesnotexist_zzz` response shape), not a
        # dispatch id.
        parsed = json.loads(err_envelope("task_not_found", "Task task_doesnotexist_zzz was not found in Run run_x."))
        parsed["id"] = "8ddd8a60-991b-4410-9211-782debfafb4c"  # a real observed RPC tracking id
        self.assertIsNone(dg.extract_dispatch_id(parsed))

    def test_extract_dispatch_id_legacy_unwrapped_shape_still_supported(self) -> None:
        # A body with neither "result" nor "error" is not the confirmed
        # wrapped shape at all -- falls back to the older flat reading.
        self.assertEqual(dg.extract_dispatch_id({"dispatch_id": "d-1"}), "d-1")
        self.assertEqual(dg.extract_dispatch_id({"dispatchId": "d-2"}), "d-2")
        self.assertEqual(dg.extract_dispatch_id({"dispatch": {"id": "d-3"}}), "d-3")
        self.assertIsNone(dg.extract_dispatch_id({"ok": False, "reason": "agent_prompt_stalled"}))
        self.assertIsNone(dg.extract_dispatch_id(None))
        self.assertIsNone(dg.extract_dispatch_id({"dispatch_id": ""}))
        self.assertIsNone(dg.extract_dispatch_id({"dispatch_id": 123}))

    # -- extract_task_id ------------------------------------------------

    def test_extract_task_id_real_nested_shape(self) -> None:
        # Matches live `orchestration task-create --json` output exactly:
        # result.task.id.
        parsed = json.loads(
            ok_envelope(
                {
                    "task": {"id": "task_3448bfe19cd2", "run_id": "run_b5f289593797", "status": "ready"},
                    "mutation": {"requestId": "d456717a", "replayed": False},
                }
            )
        )
        self.assertEqual(dg.extract_task_id(parsed), "task_3448bfe19cd2")

    def test_extract_task_id_flat_result_fallback(self) -> None:
        parsed = json.loads(ok_envelope({"taskId": "task_flat"}))
        self.assertEqual(dg.extract_task_id(parsed), "task_flat")

    def test_extract_task_id_does_not_return_outer_rpc_tracking_id(self) -> None:
        # This is the EXACT headline failure mode from the previous round:
        # extract_task_id() returning the outer envelope's own tracking id
        # (e.g. "6299c716-...") instead of the real nested task id, which
        # then gets handed to `worker-start --task <wrong-id>` and rejected
        # with task_not_found. A wrapped envelope with no resolvable task id
        # inside `result` must return None, never the top-level "id".
        parsed = json.loads(ok_envelope({"task": {}}))
        parsed["id"] = "6299c716-outer-rpc-tracking-id"
        self.assertIsNone(dg.extract_task_id(parsed))

    def test_extract_task_id_legacy_unwrapped_shape_still_supported(self) -> None:
        self.assertEqual(dg.extract_task_id({"task_id": "t-1"}), "t-1")
        self.assertEqual(dg.extract_task_id({"id": "t-2"}), "t-2")
        self.assertEqual(dg.extract_task_id({"task": {"id": "t-3"}}), "t-3")
        self.assertIsNone(dg.extract_task_id({}))

    # -- extract_task_spec_text ------------------------------------------------

    def test_extract_task_spec_text_real_nested_shape(self) -> None:
        # Matches live `orchestration task-list --json` output: result.tasks[]
        # entries carrying `id` and `spec`.
        parsed = json.loads(
            ok_envelope(
                {
                    "runId": "run_b5f289593797",
                    "tasks": [
                        {"id": "task_aaaa", "spec": "first task spec", "status": "completed"},
                        {"id": "task_bbbb", "spec": "second task spec", "status": "ready"},
                    ],
                }
            )
        )
        self.assertEqual(dg.extract_task_spec_text(parsed, "task_bbbb"), "second task spec")

    def test_extract_task_spec_text_matches_ids_exactly_not_by_prefix(self) -> None:
        parsed = json.loads(ok_envelope({"tasks": [{"id": "task_12", "spec": "the longer id's spec"}]}))
        self.assertIsNone(dg.extract_task_spec_text(parsed, "task_1"))

    def test_extract_task_spec_text_none_when_absent_or_unusable(self) -> None:
        parsed = json.loads(ok_envelope({"tasks": [{"id": "task_a", "spec": "s"}]}))
        self.assertIsNone(dg.extract_task_spec_text(parsed, "task_missing"))
        self.assertIsNone(dg.extract_task_spec_text(json.loads(ok_envelope({"tasks": [{"id": "t", "spec": ""}]})), "t"))
        self.assertIsNone(dg.extract_task_spec_text(json.loads(ok_envelope({"tasks": [{"id": "t"}]})), "t"))
        self.assertIsNone(dg.extract_task_spec_text(json.loads(err_envelope("run_required", "No Run is bound.")), "t"))
        self.assertIsNone(dg.extract_task_spec_text(None, "t"))
        self.assertIsNone(dg.extract_task_spec_text(parsed, ""))

    def test_extract_task_spec_text_legacy_unwrapped_shape_still_supported(self) -> None:
        self.assertEqual(dg.extract_task_spec_text({"tasks": [{"id": "t-1", "spec": "flat"}]}, "t-1"), "flat")
        self.assertIsNone(dg.extract_task_spec_text({"tasks": "not a list"}, "t-1"))

    # -- extract_stage_diagnostics ------------------------------------------------

    def test_extract_stage_diagnostics_from_real_result_container(self) -> None:
        parsed = json.loads(ok_envelope({"dispatchId": "d-1", "stage": "submit", "failedStage": "settle"}))
        self.assertEqual(dg.extract_stage_diagnostics(parsed), {"stage": "submit", "failedStage": "settle"})

    def test_extract_stage_diagnostics_from_error_container(self) -> None:
        parsed = json.loads(err_envelope("invalid_argument", "Missing required --task"))
        parsed["error"]["stage"] = "submit"
        self.assertEqual(dg.extract_stage_diagnostics(parsed), {"stage": "submit"})

    def test_extract_stage_diagnostics_empty_when_absent_in_wrapped_envelope(self) -> None:
        parsed = json.loads(ok_envelope({"dispatchId": "d-1"}))
        self.assertEqual(dg.extract_stage_diagnostics(parsed), {})

    def test_extract_stage_diagnostics_handles_non_dict(self) -> None:
        self.assertEqual(dg.extract_stage_diagnostics(None), {})
        self.assertEqual(dg.extract_stage_diagnostics("not a dict"), {})

    def test_extract_stage_diagnostics_legacy_unwrapped_shape_still_supported(self) -> None:
        parsed = {"stage": "submit", "failedStage": "settle", "other": "ignored"}
        self.assertEqual(dg.extract_stage_diagnostics(parsed), {"stage": "submit", "failedStage": "settle"})

    # -- extract_delivery_id ------------------------------------------------

    def test_extract_delivery_id_real_nested_shape(self) -> None:
        # Matches live `orchestration check --wait --json` output: the
        # batch-level result.deliveryId, distinct from any message's own id.
        parsed = json.loads(
            ok_envelope(
                {
                    "runId": "run_b5f289593797",
                    "deliveryId": "delivery_333098ad9b73",
                    "messages": [{"id": "msg_8a39754c62c2", "type": "worker_done"}],
                    "count": 1,
                }
            )
        )
        self.assertEqual(dg.extract_delivery_id(parsed), "delivery_333098ad9b73")

    def test_extract_delivery_id_none_when_absent(self) -> None:
        parsed = json.loads(ok_envelope({"messages": []}))
        self.assertIsNone(dg.extract_delivery_id(parsed))

    # -- _message_list ------------------------------------------------

    def test_message_list_real_nested_shape(self) -> None:
        msgs = [{"id": "msg_1", "type": "worker_done"}]
        parsed = json.loads(ok_envelope({"runId": "run_x", "deliveryId": "delivery_1", "messages": msgs, "count": 1}))
        self.assertEqual(dg._message_list(parsed), msgs)

    def test_message_list_empty_when_wrapped_but_absent(self) -> None:
        parsed = json.loads(ok_envelope({"runId": "run_x"}))
        self.assertEqual(dg._message_list(parsed), [])

    def test_message_list_legacy_unwrapped_shape_still_supported(self) -> None:
        self.assertEqual(dg._message_list({"messages": [{"a": 1}]}), [{"a": 1}])
        self.assertEqual(dg._message_list({"not_messages": []}), [])
        self.assertEqual(dg._message_list(None), [])


# ---------------------------------------------------------------------------
# `_find_matching_worker_done`: structured-field matching, not substring.
# ---------------------------------------------------------------------------


class MatchingHelperTests(unittest.TestCase):
    def test_does_not_false_positive_on_watched_id_that_is_a_prefix_of_another(self) -> None:
        # P2 regression: the previous implementation matched via
        # `did in json.dumps(msg)` -- a raw substring search -- so watching
        # "dispatch-1" would incorrectly match a message that only actually
        # carries "dispatch-12" (a proper prefix relationship). Structured
        # field extraction must compare the exact resolved id, not scan the
        # serialized text.
        message = {"id": "msg-p", "type": "worker_done", "payload": json.dumps({"dispatchId": "dispatch-12"})}
        parsed = json.loads(ok_envelope({"messages": [message]}))
        self.assertIsNone(
            dg._find_matching_worker_done(parsed, {"dispatch-1"}),
            "a watched id that is a strict prefix of the message's real id must never match",
        )
        # Sanity: the exact id still matches correctly.
        match = dg._find_matching_worker_done(parsed, {"dispatch-12"})
        self.assertIsNotNone(match)
        self.assertEqual(match["_matched_dispatch_id"], "dispatch-12")

    def test_matches_dispatch_id_nested_in_json_encoded_payload_string(self) -> None:
        message = {"id": "msg-1", "type": "worker_done", "payload": json.dumps({"dispatchId": "d-1"})}
        parsed = json.loads(ok_envelope({"messages": [message]}))
        match = dg._find_matching_worker_done(parsed, {"d-1"})
        self.assertIsNotNone(match)
        self.assertEqual(match["_matched_dispatch_id"], "d-1")

    def test_no_match_when_watched_id_absent_entirely(self) -> None:
        message = {"id": "msg-1", "type": "worker_done", "payload": json.dumps({"dispatchId": "d-1"})}
        parsed = json.loads(ok_envelope({"messages": [message]}))
        self.assertIsNone(dg._find_matching_worker_done(parsed, {"d-completely-different"}))


# ---------------------------------------------------------------------------
# `_find_capability_revoked_hit`: structured-field matching, not substring
# (same P2 bug class as `_find_matching_worker_done` above, fixed the same
# way -- see that function's own docstring for the general rationale).
# ---------------------------------------------------------------------------


class CapabilityRevokedMatchingHelperTests(unittest.TestCase):
    def test_does_not_false_positive_on_watched_id_that_is_a_prefix_of_another(self) -> None:
        # Mirrors MatchingHelperTests' own prefix regression above: watching
        # "dispatch-1" must not match a message whose real id is
        # "dispatch-12" just because "dispatch-1" is a substring of it.
        message = {
            "id": "msg-p",
            "type": "worker_done",
            "error": "dispatch_capability_invalid",
            "payload": json.dumps({"dispatchId": "dispatch-12"}),
        }
        parsed = json.loads(ok_envelope({"messages": [message]}))
        self.assertIsNone(
            dg._find_capability_revoked_hit(parsed, {"dispatch-1"}),
            "a watched id that is a strict prefix of the message's real id must never match",
        )
        # Sanity: the exact id still matches correctly.
        self.assertEqual(dg._find_capability_revoked_hit(parsed, {"dispatch-12"}), "dispatch-12")

    def test_matches_dispatch_id_nested_in_json_encoded_payload_string(self) -> None:
        message = {
            "id": "msg-1",
            "type": "worker_done",
            "error": "capability is revoked",
            "payload": json.dumps({"dispatchId": "d-1"}),
        }
        parsed = json.loads(ok_envelope({"messages": [message]}))
        self.assertEqual(dg._find_capability_revoked_hit(parsed, {"d-1"}), "d-1")

    def test_no_match_when_watched_id_absent_entirely(self) -> None:
        message = {
            "id": "msg-1",
            "type": "worker_done",
            "error": "dispatch_capability_invalid",
            "payload": json.dumps({"dispatchId": "d-1"}),
        }
        parsed = json.loads(ok_envelope({"messages": [message]}))
        self.assertIsNone(dg._find_capability_revoked_hit(parsed, {"d-completely-different"}))

    def test_no_match_when_signature_absent_even_if_id_present(self) -> None:
        # A message that merely names the watched id, with no
        # capability-revoked signature, must not be treated as a hit.
        message = {"id": "msg-1", "type": "worker_done", "payload": json.dumps({"dispatchId": "d-1"})}
        parsed = json.loads(ok_envelope({"messages": [message]}))
        self.assertIsNone(dg._find_capability_revoked_hit(parsed, {"d-1"}))


# ---------------------------------------------------------------------------
# TerminalLock: real fcntl.flock semantics, no mocking.
# ---------------------------------------------------------------------------


class TerminalLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-guard-lock-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self._orig_state_root = dg.STATE_ROOT
        dg.STATE_ROOT = self.tmp / "state-root"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        dg.STATE_ROOT = self._orig_state_root

    def test_acquire_and_release_sequentially(self) -> None:
        with dg.TerminalLock("term-x", timeout_seconds=2.0):
            pass
        with dg.TerminalLock("term-x", timeout_seconds=2.0):
            pass  # must not hang: the first lock was genuinely released

    def test_second_holder_times_out_while_first_holds(self) -> None:
        first = dg.TerminalLock("term-y", timeout_seconds=5.0)
        first.__enter__()
        try:
            second = dg.TerminalLock("term-y", timeout_seconds=0.3)
            with self.assertRaises(dg.TerminalLockTimeout):
                with second:
                    pass
        finally:
            first.__exit__(None, None, None)

    def test_different_terminal_handles_do_not_contend(self) -> None:
        with dg.TerminalLock("term-a", timeout_seconds=1.0):
            with dg.TerminalLock("term-b", timeout_seconds=1.0):
                pass  # distinct lock files -- must not block each other


# ---------------------------------------------------------------------------
# Path traversal: `_journal_path()` must hash the untrusted dispatch id
# rather than using it verbatim as a filename component (mirroring
# `_lock_path_for_terminal()`'s already-correct treatment of terminal
# handles).
# ---------------------------------------------------------------------------


class PathTraversalTests(DispatchGuardTestCase):
    def test_journal_path_is_a_hash_not_the_raw_id(self) -> None:
        path = dg._journal_path("orig-d-1")
        self.assertNotIn("orig-d-1", path.name)
        self.assertRegex(path.name, r"^[0-9a-f]{32}\.json$")

    def test_journal_path_contains_hostile_traversal_id_within_state_root(self) -> None:
        hostile = "../../../../../../tmp/orca-dispatch-guard-pwned"
        path = dg._journal_path(hostile)
        journal_dir = (dg.STATE_ROOT / dg.JOURNAL_DIRNAME).resolve()
        # The resolved path must sit strictly inside journal_dir -- no
        # traversal component may survive into the actual filesystem path.
        self.assertEqual(path.resolve().parent, journal_dir)
        self.assertRegex(path.name, r"^[0-9a-f]{32}\.json$")

    def test_write_journal_atomic_with_hostile_id_never_escapes_state_root(self) -> None:
        hostile = "../../../../../../tmp/orca-dispatch-guard-pwned-real-write"
        dg.write_journal_atomic(
            {
                "original_dispatch_id": hostile,
                "terminal_handle": "term-hostile",
                "run_id": None,
                "remount_count": 0,
                "remount_dispatch_ids": [],
                "created_at": "2026-08-01T00:00:00Z",
                "last_attempt_at": "2026-08-01T00:00:00Z",
                "captured_terminal_tail": None,
                "status": "recovering",
            }
        )
        journal_dir = (dg.STATE_ROOT / dg.JOURNAL_DIRNAME).resolve()
        written_files = list(journal_dir.glob("*.json"))
        self.assertEqual(len(written_files), 1)
        self.assertEqual(written_files[0].parent, journal_dir)
        # No file must have been created outside STATE_ROOT.
        outside_pwned = Path("/tmp/orca-dispatch-guard-pwned-real-write")
        self.assertFalse(outside_pwned.exists())
        # The raw, un-hashed original_dispatch_id must still be recoverable
        # from the entry's own JSON content, AND from read_journal() using
        # the same hostile string (round-trips through the same hash).
        entry = self.read_journal_for(hostile)
        self.assertEqual(entry["original_dispatch_id"], hostile)
        self.assertEqual(dg.read_journal(hostile)["original_dispatch_id"], hostile)


# ---------------------------------------------------------------------------
# `start`: happy path
# ---------------------------------------------------------------------------


class StartHappyPathTests(DispatchGuardTestCase):
    def test_worker_start_success_no_recovery_no_journal(self) -> None:
        self.router.queue("worker-start", cp([], 0, stdout=ok_envelope({"dispatchId": "d-happy"})))
        code, out, err = self.run_cli(["start", "--task", "t-1", "--terminal", "term-happy"])
        self.assertEqual(code, dg.EXIT_OK)
        self.assertIn("d-happy", out)
        self.assertEqual(err, "")
        self.assertEqual(len(self.router.calls), 1, "only the single worker-start call should have happened")
        journal_dir = dg.STATE_ROOT / dg.JOURNAL_DIRNAME
        self.assertFalse(journal_dir.exists(), "no journal entry should be created on a clean success")


# ---------------------------------------------------------------------------
# `start`: false-positive recovery, end to end
# ---------------------------------------------------------------------------


def _stalled_worker_start_body(dispatch_id: str) -> str:
    # No captured ground truth exists for the SPECIFIC agent_prompt_stalled
    # occurrence (see module docstring) -- this is a plausible body built
    # from the confirmed general envelope shape plus the documented
    # stage/failedStage fields from `worker-start --help`, with the literal
    # detection substring present in raw text (detection is text-substring
    # only, per module docstring -- it does not care which field it's in).
    return json.dumps(
        {
            "id": "rpc-tracking-id",
            "ok": False,
            "result": {
                "dispatchId": dispatch_id,
                "stage": "submit",
                "failedStage": "settle",
                "message": "agent_prompt_stalled: worker prompt did not settle before terminal reuse",
            },
            "_meta": {"runtimeId": "rt-1"},
        }
    )


ORIGINAL_SPEC_TEXT = "Audit the widget pipeline and report every duplicate write you find."


def _task_list_body(*, task_id: str = "orig-t", spec: str = ORIGINAL_SPEC_TEXT) -> str:
    # Confirmed live shape (2026-08-27, real `orca orchestration task-list
    # --json`): result.tasks[], each entry with `id`/`spec`/`task_title`.
    return ok_envelope(
        {
            "runId": "run-x",
            "legacyReadOnly": False,
            "tasks": [
                {"id": "some-other-task", "spec": "an unrelated task that must not be picked up", "status": "ready"},
                {"id": task_id, "spec": spec, "task_title": "orig", "status": "ready"},
            ],
        }
    )


class RecoveryFlowTests(DispatchGuardTestCase):
    def _queue_full_recovery(self, *, dispatch_id: str, tail: str, new_task_id: str, new_dispatch_id: str) -> None:
        self.router.queue("worker-start", cp([], 1, stdout=_stalled_worker_start_body(dispatch_id)))
        self.router.queue("task-list", cp([], 0, stdout=_task_list_body()))
        self.router.queue("terminal-wait", cp([], 0, stdout=ok_envelope({})))
        self.router.queue("terminal-read", cp([], 0, stdout=tail))
        self.router.queue("task-create", cp([], 0, stdout=ok_envelope({"task": {"id": new_task_id}})))
        self.router.queue("worker-start-retry", cp([], 0, stdout=ok_envelope({"dispatchId": new_dispatch_id})))

    def test_first_recovery_creates_journal_with_tail_and_remount_count_one(self) -> None:
        self._queue_full_recovery(
            dispatch_id="orig-d-1", tail="captured tail line 1", new_task_id="harvest-t-1", new_dispatch_id="remount-d-1"
        )
        code, out, _err = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-r1"])
        self.assertEqual(code, dg.EXIT_OK)
        self.assertIn("remount-d-1", out)

        journal = self.read_journal_for("orig-d-1")
        self.assertEqual(journal["original_dispatch_id"], "orig-d-1")
        self.assertEqual(journal["remount_count"], 1)
        self.assertEqual(journal["remount_dispatch_ids"], ["remount-d-1"])
        self.assertEqual(journal["captured_terminal_tail"], "captured tail line 1")
        self.assertNotEqual(journal["status"], "gave_up")

    def test_second_recovery_on_same_original_increments_and_keeps_first_tail(self) -> None:
        # Seed a journal as if a first recovery attempt already ran.
        dg.write_journal_atomic(
            {
                "original_dispatch_id": "orig-d-1",
                "terminal_handle": "term-r2",
                "run_id": None,
                "remount_count": 1,
                "remount_dispatch_ids": ["remount-d-1"],
                "created_at": "2026-08-01T00:00:00Z",
                "last_attempt_at": "2026-08-01T00:00:00Z",
                "captured_terminal_tail": "ORIGINAL FIRST-CAPTURE TAIL",
                "status": "recovering",
            }
        )
        # The SAME original dispatch id fails again (simulating a caller
        # retrying `start` after the terminal went busy a second time), and
        # this recovery attempt's own terminal-read returns DIFFERENT text
        # -- it must never overwrite the already-captured tail.
        self._queue_full_recovery(
            dispatch_id="orig-d-1",
            tail="A DIFFERENT LATER TAIL, MUST BE IGNORED",
            new_task_id="harvest-t-2",
            new_dispatch_id="remount-d-2",
        )

        code, _out, _err = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-r2"])
        self.assertEqual(code, dg.EXIT_OK)

        journal = self.read_journal_for("orig-d-1")
        self.assertEqual(journal["remount_count"], 2)
        self.assertEqual(journal["remount_dispatch_ids"], ["remount-d-1", "remount-d-2"])
        self.assertEqual(journal["captured_terminal_tail"], "ORIGINAL FIRST-CAPTURE TAIL")

    def test_fourth_attempt_refuses_to_remount_and_gives_up(self) -> None:
        dg.write_journal_atomic(
            {
                "original_dispatch_id": "orig-d-1",
                "terminal_handle": "term-r4",
                "run_id": None,
                "remount_count": 3,
                "remount_dispatch_ids": ["r-1", "r-2", "r-3"],
                "created_at": "2026-08-01T00:00:00Z",
                "last_attempt_at": "2026-08-01T00:00:00Z",
                "captured_terminal_tail": "ORIGINAL TAIL",
                "status": "recovering",
            }
        )
        # Only worker-start (fails) + terminal-wait (idle) + terminal-read
        # are queued -- if the code under test tried to task-create or
        # worker-start-retry again, the router would raise on the
        # unqueued call, failing this test.
        self.router.queue("worker-start", cp([], 1, stdout=_stalled_worker_start_body("orig-d-1")))
        self.router.queue("task-list", cp([], 0, stdout=_task_list_body()))
        self.router.queue("terminal-wait", cp([], 0, stdout=ok_envelope({})))
        self.router.queue("terminal-read", cp([], 0, stdout="tail on 4th attempt"))

        code, out, _err = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-r4"])
        self.assertEqual(code, dg.EXIT_GAVE_UP)
        self.assertIn("gave_up", out)

        journal = self.read_journal_for("orig-d-1")
        self.assertEqual(journal["status"], "gave_up")
        self.assertEqual(journal["remount_count"], 3, "must not increment past the cap")
        self.assertEqual(journal["remount_dispatch_ids"], ["r-1", "r-2", "r-3"])

    def test_tui_idle_timeout_does_not_remount_and_leaves_no_journal(self) -> None:
        self.router.queue("worker-start", cp([], 1, stdout=_stalled_worker_start_body("orig-d-busy")))
        self.router.queue("task-list", cp([], 0, stdout=_task_list_body()))
        # Non-zero return = "did not report idle in time" per this module's
        # conservative reading of `orca terminal wait`'s own exit code.
        self.router.queue("terminal-wait", cp([], 1, stdout=err_envelope("timeout", "tui-idle wait timed out")))
        # No terminal-read, no task-create, no worker-start-retry queued --
        # any such call would raise in the router.

        code, out, _err = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-busy"])
        self.assertEqual(code, dg.EXIT_RETRY_LATER)
        self.assertIn("retry_later", out)
        self.assertFalse(self.journal_path_for("orig-d-busy").exists(), "no journal should be written when we never got past the idle wait")

    def test_persistently_failing_task_create_still_trips_the_remount_cap(self) -> None:
        # P2 fix: a task-create failure must still count as an attempt, so
        # a caller retrying `start` against a terminal whose recovery
        # task-create keeps failing eventually hits "gave_up" instead of
        # retrying forever uncapped.
        dg.write_journal_atomic(
            {
                "original_dispatch_id": "orig-d-tc",
                "terminal_handle": "term-tc",
                "run_id": None,
                "remount_count": 2,
                "remount_dispatch_ids": ["r-1", "r-2"],
                "created_at": "2026-08-01T00:00:00Z",
                "last_attempt_at": "2026-08-01T00:00:00Z",
                "captured_terminal_tail": "TAIL",
                "status": "recovering",
            }
        )
        self.router.queue("worker-start", cp([], 1, stdout=_stalled_worker_start_body("orig-d-tc")))
        self.router.queue("task-list", cp([], 0, stdout=_task_list_body()))
        self.router.queue("terminal-wait", cp([], 0, stdout=ok_envelope({})))
        self.router.queue("terminal-read", cp([], 0, stdout="tail"))
        self.router.queue("task-create", cp([], 1, stdout="", stderr="task-create exploded"))

        code, _out, _err = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-tc"])
        self.assertEqual(code, dg.EXIT_FAILURE)

        journal = self.read_journal_for("orig-d-tc")
        self.assertEqual(journal["remount_count"], 3, "a failed task-create attempt must still be counted")

        # A subsequent attempt must now refuse outright (cap reached) and
        # never call task-create again.
        self.router.queue("worker-start", cp([], 1, stdout=_stalled_worker_start_body("orig-d-tc")))
        self.router.queue("task-list", cp([], 0, stdout=_task_list_body()))
        self.router.queue("terminal-wait", cp([], 0, stdout=ok_envelope({})))
        self.router.queue("terminal-read", cp([], 0, stdout="tail"))
        code2, out2, _err2 = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-tc"])
        self.assertEqual(code2, dg.EXIT_GAVE_UP)
        self.assertIn("gave_up", out2)

    def test_remount_succeeds_but_dispatch_id_unresolvable_is_a_failure(self) -> None:
        # P2 fix: symmetric with how the INITIAL dispatch id is handled --
        # a remount that exits 0 but whose own JSON body has no extractable
        # dispatch id must be reported as a failure, not masqueraded as
        # EXIT_OK (which would silently lose the ability to `wait` on it).
        self.router.queue("worker-start", cp([], 1, stdout=_stalled_worker_start_body("orig-d-unresolvable")))
        self.router.queue("task-list", cp([], 0, stdout=_task_list_body()))
        self.router.queue("terminal-wait", cp([], 0, stdout=ok_envelope({})))
        self.router.queue("terminal-read", cp([], 0, stdout="tail"))
        self.router.queue("task-create", cp([], 0, stdout=ok_envelope({"task": {"id": "harvest-t-x"}})))
        # Exits 0 but the body has no dispatchId anywhere resolvable.
        self.router.queue("worker-start-retry", cp([], 0, stdout=ok_envelope({"someOtherField": "x"})))

        code, out, _err = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-unresolvable"])
        self.assertEqual(code, dg.EXIT_FAILURE)
        self.assertIn('"ok": false', out.lower())

        journal = self.read_journal_for("orig-d-unresolvable")
        self.assertEqual(journal["remount_count"], 1)
        self.assertEqual(journal["remount_dispatch_ids"], [], "no dispatch id was resolvable, so none should be recorded")

    def _harvest_spec_submitted(self) -> str:
        create_call = next(c for c in self.router.calls if c[1:3] == ["orchestration", "task-create"])
        return create_call[create_call.index("--spec") + 1]

    def test_recovery_prompt_carries_the_original_task_spec_text(self) -> None:
        # Previously `_do_start()` hardcoded original_spec_text=None, so the
        # harvest worker was handed a bare task id and no description of what
        # it had originally been asked to do -- `_build_harvest_spec()`'s
        # non-None branch had zero production callers and zero coverage.
        self._queue_full_recovery(
            dispatch_id="orig-d-spec", tail="tail", new_task_id="harvest-t-s", new_dispatch_id="remount-d-s"
        )
        code, _out, _err = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-spec"])
        self.assertEqual(code, dg.EXIT_OK)

        submitted = self._harvest_spec_submitted()
        self.assertIn(ORIGINAL_SPEC_TEXT, submitted, "the real spec text must reach the harvest worker verbatim")
        self.assertIn("Original task id", submitted)
        self.assertNotIn("an unrelated task that must not be picked up", submitted, "only the requested task's spec")

    def test_recovery_passes_the_run_id_through_to_the_spec_lookup(self) -> None:
        self._queue_full_recovery(
            dispatch_id="orig-d-run", tail="tail", new_task_id="harvest-t-r", new_dispatch_id="remount-d-r"
        )
        code, _out, _err = self.run_cli(
            ["start", "--task", "orig-t", "--terminal", "term-run", "--run", "run-abc"]
        )
        self.assertEqual(code, dg.EXIT_OK)
        list_call = next(c for c in self.router.calls if c[1:3] == ["orchestration", "task-list"])
        self.assertEqual(list_call[list_call.index("--run") + 1], "run-abc")
        self.assertNotIn("--brief", list_call, "--brief would truncate the spec at 160 chars")

    def test_recovery_still_proceeds_when_the_spec_lookup_fails(self) -> None:
        # Best-effort only: a failed task-list must degrade to the previous
        # bare-task-id prompt, never abort the recovery (aborting would lose
        # the real worker result this whole module exists to rescue).
        self.router.queue("worker-start", cp([], 1, stdout=_stalled_worker_start_body("orig-d-nospec")))
        self.router.queue("task-list", cp([], 1, stdout="", stderr="task-list exploded"))
        self.router.queue("terminal-wait", cp([], 0, stdout=ok_envelope({})))
        self.router.queue("terminal-read", cp([], 0, stdout="tail"))
        self.router.queue("task-create", cp([], 0, stdout=ok_envelope({"task": {"id": "harvest-t-n"}})))
        self.router.queue("worker-start-retry", cp([], 0, stdout=ok_envelope({"dispatchId": "remount-d-n"})))

        code, _out, _err = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-nospec"])
        self.assertEqual(code, dg.EXIT_OK)

        submitted = self._harvest_spec_submitted()
        self.assertIn("Original task id", submitted)
        self.assertNotIn("Original task spec text", submitted)

    def test_recovery_degrades_when_the_task_is_absent_from_the_listing(self) -> None:
        self.router.queue("worker-start", cp([], 1, stdout=_stalled_worker_start_body("orig-d-absent")))
        self.router.queue("task-list", cp([], 0, stdout=_task_list_body(task_id="a-different-task")))
        self.router.queue("terminal-wait", cp([], 0, stdout=ok_envelope({})))
        self.router.queue("terminal-read", cp([], 0, stdout="tail"))
        self.router.queue("task-create", cp([], 0, stdout=ok_envelope({"task": {"id": "harvest-t-a"}})))
        self.router.queue("worker-start-retry", cp([], 0, stdout=ok_envelope({"dispatchId": "remount-d-a"})))

        code, _out, _err = self.run_cli(["start", "--task", "orig-t", "--terminal", "term-absent"])
        self.assertEqual(code, dg.EXIT_OK)
        self.assertNotIn("Original task spec text", self._harvest_spec_submitted())


# ---------------------------------------------------------------------------
# `start`: genuine (non-false-positive) failure passes through unchanged
# ---------------------------------------------------------------------------


class GenuineFailurePassthroughTests(DispatchGuardTestCase):
    def test_genuine_failure_unchanged(self) -> None:
        real_stdout = err_envelope("some_other_real_problem", "a real, unrelated error message")
        real_stderr = "a real, unrelated error message\n"
        self.router.queue("worker-start", cp([], 7, stdout=real_stdout, stderr=real_stderr))

        code, out, err = self.run_cli(["start", "--task", "t-1", "--terminal", "term-genuine"])

        self.assertEqual(code, 7, "must be the exact exit code worker-start itself reported")
        self.assertEqual(out, real_stdout, "stdout must pass through byte-for-byte unchanged")
        self.assertEqual(err, real_stderr, "stderr must pass through byte-for-byte unchanged")
        self.assertEqual(len(self.router.calls), 1, "no recovery calls should ever be attempted for a genuine failure")
        journal_dir = dg.STATE_ROOT / dg.JOURNAL_DIRNAME
        self.assertFalse(journal_dir.exists())


# ---------------------------------------------------------------------------
# Concurrency: two real threads dispatching against the SAME terminal handle
# ---------------------------------------------------------------------------


class ConcurrencyTests(DispatchGuardTestCase):
    def test_two_concurrent_starts_same_terminal_never_overlap_and_never_lose_a_remount(self) -> None:
        # Both threads' FIRST worker-start attempt fails with the SAME
        # original dispatch id (a contrived but useful stress scenario for
        # this tool's own bookkeeping: two callers racing to recover the
        # exact same original dispatch). Each queued response is consumed
        # exactly once, FIFO, by whichever thread's call reaches the router
        # first -- the per-terminal lock is what must make that "first"
        # well-defined instead of a genuine race.
        for _ in range(2):
            self.router.queue("worker-start", cp([], 1, stdout=_stalled_worker_start_body("shared-orig-d")))
        self.router.queue(
            "task-list",
            cp([], 0, stdout=_task_list_body(task_id="orig-task")),
            cp([], 0, stdout=_task_list_body(task_id="orig-task")),
        )
        self.router.queue("terminal-wait", cp([], 0, stdout=ok_envelope({})), cp([], 0, stdout=ok_envelope({})))
        self.router.queue("terminal-read", cp([], 0, stdout="tail-A"), cp([], 0, stdout="tail-B"))
        self.router.queue(
            "task-create",
            cp([], 0, stdout=ok_envelope({"task": {"id": "harvest-A"}})),
            cp([], 0, stdout=ok_envelope({"task": {"id": "harvest-B"}})),
        )
        self.router.queue(
            "worker-start-retry",
            cp([], 0, stdout=ok_envelope({"dispatchId": "remount-A"})),
            cp([], 0, stdout=ok_envelope({"dispatchId": "remount-B"})),
        )

        results: list[int | None] = [None, None]

        def worker(idx: int) -> None:
            results[idx] = dg.main(["start", "--task", "orig-task", "--terminal", "shared-term"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertFalse(any(t.is_alive() for t in threads), "threads must have finished within the timeout")
        self.assertEqual(results, [dg.EXIT_OK, dg.EXIT_OK])
        self.assertLessEqual(self.router.max_active, 1, "the per-terminal lock must fully serialize the two callers' orca calls")

        journal = self.read_journal_for("shared-orig-d")
        self.assertEqual(journal["remount_count"], 2, "both remount attempts must be counted -- no lost update")
        self.assertEqual(sorted(journal["remount_dispatch_ids"]), ["remount-A", "remount-B"])
        self.assertIn(journal["captured_terminal_tail"], ("tail-A", "tail-B"), "exactly one first-capture must win, never overwritten by the second")


# ---------------------------------------------------------------------------
# `wait`: matching/timeout branches, against the REAL nested `orchestration
# check` envelope shape (result.messages, result.deliveryId).
# ---------------------------------------------------------------------------


class WaitCommandTests(DispatchGuardTestCase):
    def test_wait_matches_worker_done_and_acks_using_the_batch_delivery_id(self) -> None:
        # Matches live `orchestration check --wait --json` output: messages
        # live at result.messages, and the id to ack with is the BATCH
        # result.deliveryId -- never a per-message field. Each message's
        # own "id" (here "msg_1") is a different, unrelated identifier that
        # must never be used as the ack id -- this is exactly the P0-class
        # bug this round fixes (the previous code read match.get("delivery_id")
        # or match.get("id"), which would have used "msg_1").
        message = {
            "id": "msg_1",
            "type": "worker_done",
            "payload": json.dumps({"taskId": "t-1", "dispatchId": "d-1", "outcome": "succeeded"}),
        }
        check_body = ok_envelope({"runId": "run-1", "deliveryId": "delivery_abc", "messages": [message], "count": 1})
        self.router.queue("check", cp([], 0, stdout=check_body))
        self.router.queue("check-ack", cp([], 0, stdout=ok_envelope({})))

        code, out, _err = self.run_cli(["wait", "--run", "run-1", "--dispatch", "d-1", "--timeout-ms", "5000"])
        self.assertEqual(code, dg.EXIT_OK)
        self.assertIn("d-1", out)

        ack_call = self.router.calls[-1]
        self.assertEqual(_key_for(ack_call), "check-ack")
        self.assertIn("delivery_abc", ack_call, "must ack using the batch deliveryId from result.deliveryId")
        self.assertNotIn("msg_1", ack_call, "must never ack using an unrelated per-message id")

    def test_wait_does_not_ack_batch_containing_an_unrelated_sibling_message(self) -> None:
        # P1 regression: the previous implementation found ONE matching
        # worker_done and then acked the entire batch via the BATCH-level
        # deliveryId -- silently discarding any OTHER message riding along
        # in the same batch (a worker_done for a dispatch this call isn't
        # watching, here "d-other-not-watched"). Confirmed against the real
        # `orca orchestration check --help` text ("process every message
        # before acknowledging" -- there is no per-message ack), the fix
        # must refuse to ack while unrelated messages are still present.
        matched_message = {"id": "msg_1", "type": "worker_done", "payload": json.dumps({"dispatchId": "d-1"})}
        sibling_message = {
            "id": "msg_2",
            "type": "worker_done",
            "payload": json.dumps({"dispatchId": "d-other-not-watched"}),
        }
        check_body = ok_envelope(
            {
                "runId": "run-1",
                "deliveryId": "delivery_shared",
                "messages": [matched_message, sibling_message],
                "count": 2,
            }
        )
        self.router.queue("check", cp([], 0, stdout=check_body))
        # Deliberately NOT queuing "check-ack": if the code under test tried
        # to ack this batch anyway, the router would raise on the unqueued
        # call, failing this test outright.
        code, out, _err = self.run_cli(["wait", "--run", "run-1", "--dispatch", "d-1", "--timeout-ms", "5000"])
        self.assertEqual(code, dg.EXIT_OK)
        self.assertIn("d-1", out)
        self.assertEqual(
            len(self.router.calls), 1, "must not attempt any ack call while the batch still has an unrelated message"
        )
        payload = json.loads(out)
        self.assertFalse(payload["ack"]["attempted"])
        self.assertEqual(payload["ack"]["reason"], "batch_contains_other_messages")

    def test_wait_matches_via_remount_dispatch_id_when_only_original_was_watched(self) -> None:
        # P1 regression: `_recover_from_stalled_false_positive()` mints a
        # NEW dispatch id via a remounted worker-start and records it in the
        # journal's remount_dispatch_ids[], but a caller of `wait` only ever
        # passed the ORIGINAL id on the command line. Without folding the
        # journal's remount ids into the active watched set, the remount's
        # own real worker_done (which carries only the NEW id) can never
        # match and `wait` would time out despite genuine completion.
        dg.write_journal_atomic(
            {
                "original_dispatch_id": "orig-d-remount-wait",
                "terminal_handle": "term-remount-wait",
                "run_id": "run-remount",
                "remount_count": 1,
                "remount_dispatch_ids": ["remount-d-only"],
                "created_at": "2026-08-01T00:00:00Z",
                "last_attempt_at": "2026-08-01T00:00:00Z",
                "captured_terminal_tail": "tail",
                "status": "recovering",
            }
        )
        message = {"id": "msg-r", "type": "worker_done", "payload": json.dumps({"dispatchId": "remount-d-only"})}
        check_body = ok_envelope({"deliveryId": "delivery-remount", "messages": [message]})
        self.router.queue("check", cp([], 0, stdout=check_body))
        self.router.queue("check-ack", cp([], 0, stdout=ok_envelope({})))

        code, out, _err = self.run_cli(
            ["wait", "--run", "run-remount", "--dispatch", "orig-d-remount-wait", "--timeout-ms", "5000"]
        )
        self.assertEqual(
            code, dg.EXIT_OK, "must recognize the remount's worker_done even though only the original id was watched"
        )
        self.assertIn("remount-d-only", out)

    def test_wait_reports_failure_when_the_ack_call_itself_fails(self) -> None:
        # P2 regression: cmd_wait previously called `_orchestration_ack(...)`
        # and discarded its result entirely, so a real ack failure (stale
        # delivery, transient orca-side error) was still reported as ok:true
        # with exit 0.
        message = {"id": "msg_1", "type": "worker_done", "payload": json.dumps({"dispatchId": "d-ackfail"})}
        check_body = ok_envelope({"deliveryId": "delivery_ackfail", "messages": [message]})
        self.router.queue("check", cp([], 0, stdout=check_body))
        self.router.queue("check-ack", cp([], 1, stdout="", stderr="ack rejected: stale delivery"))

        code, out, _err = self.run_cli(["wait", "--run", "run-1", "--dispatch", "d-ackfail", "--timeout-ms", "5000"])
        self.assertNotEqual(code, dg.EXIT_OK, "must not silently report success when the ack itself failed")
        payload = json.loads(out)
        self.assertFalse(payload["ok"], "top-level ok must reflect the ack failure, not just the match")
        self.assertEqual(payload.get("warning"), "ack_failed")
        self.assertTrue(payload["ack"]["attempted"])
        self.assertFalse(payload["ack"]["ok"])

    def test_wait_times_out_honestly(self) -> None:
        # Queue generously more empty-message responses than any plausible
        # loop-iteration count for this small timeout budget -- the router's
        # own small per-call sleep makes the exact iteration count a timing
        # detail, not something this test should be sensitive to.
        empty = cp([], 0, stdout=ok_envelope({"messages": []}))
        self.router.queue("check", *([empty] * 30))
        code, out, _err = self.run_cli(["wait", "--run", "run-1", "--dispatch", "d-never", "--timeout-ms", "200"])
        self.assertEqual(code, dg.EXIT_FAILURE)
        self.assertIn("timeout", out)


# ---------------------------------------------------------------------------
# `wait` vs `start` recovery: a real-thread test proving the journal
# "recovered" write in `wait` is genuinely serialized against a
# concurrently-held per-terminal recovery lock, rather than racing it.
# ---------------------------------------------------------------------------


class WaitStartRaceTests(DispatchGuardTestCase):
    def test_wait_recovered_write_is_serialized_by_terminal_lock(self) -> None:
        # Seed a journal as though a recovery is already in flight for this
        # terminal (mirrors the state _recover_from_stalled_false_positive()
        # would have left after its FIRST write, before its second).
        dg.write_journal_atomic(
            {
                "original_dispatch_id": "orig-d-race",
                "terminal_handle": "term-race",
                "run_id": "run-race",
                "remount_count": 1,
                "remount_dispatch_ids": ["remount-d-race"],
                "created_at": "2026-08-01T00:00:00Z",
                "last_attempt_at": "2026-08-01T00:00:00Z",
                "captured_terminal_tail": "tail",
                "status": "recovering",
            }
        )

        message = {"id": "msg-race", "type": "worker_done", "payload": json.dumps({"dispatchId": "remount-d-race"})}
        self.router.queue(
            "check", cp([], 0, stdout=ok_envelope({"deliveryId": "delivery-race", "messages": [message]}))
        )
        self.router.queue("check-ack", cp([], 0, stdout=ok_envelope({})))

        # Simulate cmd_start's recovery still mid-flight: hold the SAME
        # per-terminal lock externally (this is exactly the lock
        # `_recover_from_stalled_false_positive()` runs under, held for its
        # entire duration per the module's own documented design), then
        # release it after a short delay from another thread.
        holder = dg.TerminalLock("term-race", timeout_seconds=5.0)
        holder.__enter__()
        release_at: list[float | None] = [None]

        def release_after_delay() -> None:
            time.sleep(0.3)
            release_at[0] = time.monotonic()
            holder.__exit__(None, None, None)

        releaser = threading.Thread(target=release_after_delay)
        releaser.start()

        started_at = time.monotonic()
        code, out, _err = self.run_cli(["wait", "--run", "run-race", "--dispatch", "remount-d-race", "--timeout-ms", "5000"])
        finished_at = time.monotonic()
        releaser.join(timeout=5)

        self.assertEqual(code, dg.EXIT_OK)
        self.assertIn("remount-d-race", out)
        self.assertGreaterEqual(
            finished_at - started_at, 0.25, "cmd_wait must genuinely block on the held per-terminal lock, not skip past it"
        )
        self.assertIsNotNone(release_at[0])
        self.assertGreaterEqual(finished_at, release_at[0], "the recovered write must land strictly after the lock was released")

        journal = self.read_journal_for("orig-d-race")
        self.assertEqual(journal["status"], "recovered", "must not be lost/clobbered by the concurrent lock holder")

    def test_wait_recovered_write_survives_a_holder_that_writes_twice_before_releasing(self) -> None:
        # The actual bug scenario, made deterministic (no sleep-based race):
        # `_recover_from_stalled_false_positive()` performs TWO journal
        # writes while holding ONE lock acquisition (before task-create, and
        # after the remount), reusing one in-memory dict across both. This
        # thread reproduces exactly that shape -- two writes under the same
        # held lock -- so that if `wait`'s own lock acquisition could ever
        # slip in BETWEEN them (i.e. the lock did not actually serialize the
        # two code paths), its "recovered" write would get clobbered back to
        # "recovering" by the second write. Because `wait` must fully
        # acquire the lock before writing, and this thread does not release
        # it until after both of its own writes, `wait`'s write can only
        # ever land strictly after both -- proving genuine serialization,
        # not just a favorable interleaving.
        dg.write_journal_atomic(
            {
                "original_dispatch_id": "orig-d-race2",
                "terminal_handle": "term-race2",
                "run_id": "run-race2",
                "remount_count": 1,
                "remount_dispatch_ids": [],
                "created_at": "2026-08-01T00:00:00Z",
                "last_attempt_at": "2026-08-01T00:00:00Z",
                "captured_terminal_tail": None,
                "status": "recovering",
            }
        )
        message = {"id": "msg-race2", "type": "worker_done", "payload": json.dumps({"dispatchId": "orig-d-race2"})}
        self.router.queue(
            "check", cp([], 0, stdout=ok_envelope({"deliveryId": "delivery-race2", "messages": [message]}))
        )
        self.router.queue("check-ack", cp([], 0, stdout=ok_envelope({})))

        holder = dg.TerminalLock("term-race2", timeout_seconds=5.0)
        holder.__enter__()

        def hold_write_twice_then_release() -> None:
            j = dg.read_journal("orig-d-race2")
            j["status"] = "recovering"  # mirrors the FIRST write, pre-task-create
            dg.write_journal_atomic(j)
            time.sleep(0.2)
            j["remount_dispatch_ids"] = ["remount-race2"]
            j["status"] = "recovering"  # mirrors the SECOND write, post-remount
            dg.write_journal_atomic(j)
            holder.__exit__(None, None, None)

        holder_thread = threading.Thread(target=hold_write_twice_then_release)
        holder_thread.start()

        code, _out, _err = self.run_cli(["wait", "--run", "run-race2", "--dispatch", "orig-d-race2", "--timeout-ms", "5000"])
        holder_thread.join(timeout=5)

        self.assertEqual(code, dg.EXIT_OK)
        self.assertFalse(holder_thread.is_alive())
        journal = self.read_journal_for("orig-d-race2")
        self.assertEqual(journal["status"], "recovered", "wait's write must survive, not be clobbered by the holder's second write")
        self.assertEqual(journal["remount_dispatch_ids"], ["remount-race2"], "the holder's own second write must also survive intact")


if __name__ == "__main__":
    unittest.main()
