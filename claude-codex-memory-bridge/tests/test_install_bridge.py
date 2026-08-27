from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

import claude_memory_hook as hook
import install_bridge as installer
import write_candidate_capture as wtc


def base_config() -> bytes:
    return (
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/usr/bin/true",
                                    "timeout": 2,
                                }
                            ]
                        }
                    ],
                }
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()


class InstallBridgeTests(unittest.TestCase):
    def test_update_preserves_existing_and_is_idempotent(self) -> None:
        command = f"/usr/bin/python3 hook.py --bridge-id {installer.BRIDGE_ID}"
        first = installer.update_hook_config(base_config(), command)
        second = installer.update_hook_config(first, command)
        payload = json.loads(second)
        handlers = payload["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(handlers), 2)
        self.assertEqual(handlers[0]["hooks"][0]["command"], "/usr/bin/true")
        self.assertEqual(handlers[1]["hooks"][0]["command"], command)
        self.assertEqual(first, second)

    def test_remove_owned_handler_only(self) -> None:
        command = f"run --bridge-id {installer.BRIDGE_ID}"
        installed = installer.update_hook_config(base_config(), command)
        removed = installer.update_hook_config(installed, command, remove=True)
        payload = json.loads(removed)
        handlers = payload["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0]["hooks"][0]["command"], "/usr/bin/true")

    def test_rejects_duplicate_keys_and_unknown_shape(self) -> None:
        with self.assertRaises(installer.InstallError):
            installer.update_hook_config(b'{"hooks":{},"hooks":{}}', "command")
        with self.assertRaises(installer.InstallError):
            installer.update_hook_config(b'{"hooks":{},"extra":1}', "command")

    # -- SessionEnd hook-registration capability (AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md
    # section 4.2): make_handler()/update_hook_config() gained an explicit `timeout`/`event`
    # parameter so a future caller can register a handler under an event other than
    # "UserPromptSubmit". Every test above this line calls these functions exactly as it did
    # before that change (no `event`/`timeout` kwarg) and still passes unchanged -- proving the
    # new parameters' defaults reproduce the old hardcoded behavior byte-for-byte. The tests
    # below exercise the new, explicit-event path itself.

    def test_make_handler_default_timeout_is_unchanged(self) -> None:
        # Pinned regression: DEFAULT_HOOK_TIMEOUT must still be the exact value every
        # already-installed UserPromptSubmit handler was created with.
        self.assertEqual(installer.DEFAULT_HOOK_TIMEOUT, 5)
        handler = installer.make_handler("some command")
        self.assertEqual(handler["hooks"][0]["timeout"], 5)

    def test_make_handler_accepts_an_explicit_timeout(self) -> None:
        handler = installer.make_handler("some command", timeout=99)
        self.assertEqual(handler["hooks"][0]["timeout"], 99)
        self.assertEqual(handler["hooks"][0]["command"], "some command")

    def test_update_hook_config_default_event_is_unchanged(self) -> None:
        self.assertEqual(installer.DEFAULT_HOOK_EVENT, "UserPromptSubmit")

    def test_update_hook_config_session_end_against_config_missing_the_key_entirely(self) -> None:
        # A real, currently-un-migrated hooks.json has no "SessionEnd" key at all (design doc
        # section 0/4.2). The presence check must now *initialize* an empty list for it rather
        # than raise InstallError, so this event can be registered for the first time.
        raw = json.dumps({"hooks": {}}, sort_keys=True).encode()
        command = f"/usr/bin/python3 write_candidate_capture.py --bridge-id {installer.BRIDGE_ID}"
        updated = installer.update_hook_config(raw, command, event="SessionEnd")
        payload = json.loads(updated)
        self.assertEqual(list(payload["hooks"].keys()), ["SessionEnd"])
        handlers = payload["hooks"]["SessionEnd"]
        self.assertEqual(len(handlers), 1)
        self.assertTrue(installer.owned_handler(handlers[0]))
        self.assertEqual(handlers[0]["hooks"][0]["command"], command)

    def test_update_hook_config_default_event_still_raises_when_key_entirely_missing(self) -> None:
        # Regression pin (independent dual review, 2026-08-20): the SessionEnd
        # auto-initialize-empty-list branch above must NOT apply to the default event.
        # install()/plan() call update_hook_config() with zero non-default args -- exactly this
        # shape -- and _enumerate_hook_configs() includes every codex-accounts/*/home/hooks.json
        # unconditionally, so a freshly-discovered account config can genuinely have no
        # "UserPromptSubmit" key at all (e.g. only a SessionStart hook so far). This must still
        # fail closed exactly as it did before the SessionEnd capability was added, not silently
        # create the key and proceed with install. No test pinned this before, which is why the
        # regression was not caught the first time.
        raw = json.dumps({"hooks": {"SessionStart": []}}, sort_keys=True).encode()
        with self.assertRaises(installer.InstallError):
            installer.update_hook_config(raw, "command")
        # Same for the fully-empty-hooks case, and with the default event passed explicitly.
        empty_raw = json.dumps({"hooks": {}}, sort_keys=True).encode()
        with self.assertRaises(installer.InstallError):
            installer.update_hook_config(empty_raw, "command")
        with self.assertRaises(installer.InstallError):
            installer.update_hook_config(empty_raw, "command", event=installer.DEFAULT_HOOK_EVENT)

    def test_update_hook_config_session_end_does_not_disturb_other_existing_events(self) -> None:
        # base_config() already has "SessionStart" (empty) and "UserPromptSubmit" (one
        # pre-existing unrelated handler) -- neither key exists as "SessionEnd" yet. Registering
        # SessionEnd must leave both of those completely untouched.
        raw = base_config()
        command = f"/usr/bin/python3 write_candidate_capture.py --bridge-id {installer.BRIDGE_ID}"
        updated = installer.update_hook_config(raw, command, event="SessionEnd")
        payload = json.loads(updated)
        self.assertEqual(payload["hooks"]["SessionStart"], [])
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)
        self.assertEqual(payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "/usr/bin/true")
        self.assertFalse(installer.owned_handler(payload["hooks"]["UserPromptSubmit"][0]))
        session_end_handlers = payload["hooks"]["SessionEnd"]
        self.assertEqual(len(session_end_handlers), 1)
        self.assertTrue(installer.owned_handler(session_end_handlers[0]))
        # And, unchanged from before this change: the original UserPromptSubmit content is
        # byte-for-byte the same JSON sub-document it always was.
        original_payload = json.loads(raw)
        self.assertEqual(payload["hooks"]["UserPromptSubmit"], original_payload["hooks"]["UserPromptSubmit"])

    def test_update_hook_config_session_end_is_idempotent_and_removable(self) -> None:
        raw = base_config()
        command = f"/usr/bin/python3 write_candidate_capture.py --bridge-id {installer.BRIDGE_ID}"
        first = installer.update_hook_config(raw, command, event="SessionEnd")
        second = installer.update_hook_config(first, command, event="SessionEnd")
        self.assertEqual(first, second)
        payload = json.loads(second)
        self.assertEqual(len(payload["hooks"]["SessionEnd"]), 1)
        removed = installer.update_hook_config(second, command, event="SessionEnd", remove=True)
        removed_payload = json.loads(removed)
        self.assertEqual(removed_payload["hooks"]["SessionEnd"], [])
        # UserPromptSubmit was never touched by any of this.
        self.assertEqual(len(removed_payload["hooks"]["UserPromptSubmit"]), 1)

    def test_update_hook_config_session_end_still_raises_on_malformed_existing_key(self) -> None:
        # A present-but-wrong-shape key is real corruption, not "absent" -- must still fail
        # closed exactly like the pre-existing UserPromptSubmit malformed-shape check does.
        raw = json.dumps({"hooks": {"SessionEnd": "not-a-list"}}, sort_keys=True).encode()
        with self.assertRaises(installer.InstallError):
            installer.update_hook_config(raw, "command", event="SessionEnd")

    def test_owned_shape_match_defaults_to_userpromptsubmit_and_ignores_other_events(self) -> None:
        # A payload shaped like ours under "UserPromptSubmit" (empty, no owned handler) but
        # carrying an owned handler under "SessionEnd" instead. The default call (no `event`
        # kwarg, exactly what every existing caller does) must report False, exactly as it did
        # before this parameterization existed -- SessionEnd is invisible unless explicitly
        # asked for.
        payload = {
            "hooks": {
                "UserPromptSubmit": [],
                "SessionEnd": [
                    {"hooks": [{"command": f"run --bridge-id {installer.BRIDGE_ID}"}]}
                ],
            }
        }
        self.assertFalse(installer._owned_shape_match(payload))
        self.assertTrue(installer._owned_shape_match(payload, event="SessionEnd"))

    def test_contains_owned_handler_defaults_to_userpromptsubmit_and_ignores_other_events(self) -> None:
        raw = json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [],
                    "SessionEnd": [
                        {"hooks": [{"command": f"run --bridge-id {installer.BRIDGE_ID}"}]}
                    ],
                }
            }
        ).encode()
        self.assertFalse(installer._contains_owned_handler(raw))
        self.assertTrue(installer._contains_owned_handler(raw, event="SessionEnd"))

    # -- Whole-candidate acceptance review, 2026-08-20: two cross-file gaps between this file's
    # SessionEnd capability and write_candidate_capture.py, its documented future consumer.
    #
    # Finding 1: owned_handler()/_owned_shape_match()/_attempt_structural_detection()/
    # _contains_owned_handler() all hardcoded BRIDGE_ID (this module's own constant), but
    # write_candidate_capture.py defines a distinct MODULE_ID ("orca-claude-codex-memory-write-
    # trigger-v1", write_candidate_capture.py:60) and requires any command it registers to carry
    # `--bridge-id <MODULE_ID>` (write_candidate_capture.py:2237), matching the design doc's own
    # example wiring (write_candidate_capture.py:2321, "NOT WIRED IN. Example only"). Reproduced
    # directly (isolated scratch copy with only this fix's hunks reverted, per this file's
    # established verification method -- see AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md section 17):
    # registering `--bridge-id <MODULE_ID>` through the pre-fix update_hook_config() three times
    # produced 1 -> 2 -> 3 duplicate SessionEnd handlers instead of being idempotent, remove=True
    # left all 3 in place, and both _contains_owned_handler() and owned_handler() reported False
    # for a handler that was, in fact, this installer's own. All four functions now accept an
    # explicit `bridge_id` parameter (default: BRIDGE_ID, so every existing call site -- none of
    # which pass it -- is unaffected).
    #
    # Finding 2: make_handler() unconditionally wrote a "timeout" key, but write_candidate_capture
    # .py's own documented SessionEnd wiring (write_candidate_capture.py:2330-2336) explicitly
    # carries no "timeout" key at all (its scan has unbounded latency by design; SessionEnd has no
    # output contract for Codex to enforce one against). Worse, update_hook_config() -- the only
    # real config-writing entry point -- never forwarded a `timeout` value to make_handler() at
    # all, so make_handler()'s existing `timeout` parameter was unreachable through the supported
    # API. make_handler() now accepts `timeout: int | None`, omitting the "timeout" key entirely
    # when `None` (default unchanged: DEFAULT_HOOK_TIMEOUT), and update_hook_config() now accepts
    # and forwards `timeout` too.

    def test_update_hook_config_bridge_id_reproduces_reviewers_exact_failure_shape_then_works(
        self,
    ) -> None:
        # The reviewer's exact repro: a command whose --bridge-id is write_candidate_capture's
        # MODULE_ID, not this module's own BRIDGE_ID. Passing bridge_id=<that different string>
        # must make registration idempotent, removal effective, and detection correct -- exactly
        # the three guarantees the pre-fix code failed (see the class comment above).
        write_trigger_bridge_id = "orca-claude-codex-memory-write-trigger-v1"  # write_candidate_capture.MODULE_ID
        command = f"/usr/bin/python3 write_candidate_capture.py scan --bridge-id {write_trigger_bridge_id}"
        raw = base_config()

        first = installer.update_hook_config(raw, command, event="SessionEnd", bridge_id=write_trigger_bridge_id)
        second = installer.update_hook_config(first, command, event="SessionEnd", bridge_id=write_trigger_bridge_id)
        self.assertEqual(first, second)  # idempotent: still exactly 1 handler, not 2
        payload = json.loads(second)
        session_end_handlers = payload["hooks"]["SessionEnd"]
        self.assertEqual(len(session_end_handlers), 1)

        self.assertTrue(installer.owned_handler(session_end_handlers[0], bridge_id=write_trigger_bridge_id))
        self.assertTrue(
            installer._contains_owned_handler(second, event="SessionEnd", bridge_id=write_trigger_bridge_id)
        )

        removed = installer.update_hook_config(
            second, command, event="SessionEnd", bridge_id=write_trigger_bridge_id, remove=True
        )
        removed_payload = json.loads(removed)
        self.assertEqual(removed_payload["hooks"]["SessionEnd"], [])

        # Untouched throughout: the pre-existing UserPromptSubmit handler from base_config().
        self.assertEqual(len(removed_payload["hooks"]["UserPromptSubmit"]), 1)
        self.assertEqual(removed_payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "/usr/bin/true")

    def test_bridge_id_default_still_only_recognizes_this_modules_own_bridge_id(self) -> None:
        # A handler carrying a DIFFERENT bridge_id must be invisible to every default (no
        # `bridge_id` kwarg) call -- the live, already-installed UserPromptSubmit path never
        # passes one, and must keep behaving exactly as it always has.
        other_bridge_id = "orca-claude-codex-memory-write-trigger-v1"
        handler = {"hooks": [{"command": f"run --bridge-id {other_bridge_id}"}]}
        self.assertFalse(installer.owned_handler(handler))
        self.assertTrue(installer.owned_handler(handler, bridge_id=other_bridge_id))

        raw = json.dumps(
            {"hooks": {"SessionEnd": [handler]}},
            sort_keys=True,
        ).encode()
        self.assertFalse(installer._contains_owned_handler(raw, event="SessionEnd"))
        self.assertTrue(installer._contains_owned_handler(raw, event="SessionEnd", bridge_id=other_bridge_id))
        self.assertFalse(installer._owned_shape_match(json.loads(raw), event="SessionEnd"))
        self.assertTrue(
            installer._owned_shape_match(json.loads(raw), event="SessionEnd", bridge_id=other_bridge_id)
        )

    def test_update_hook_config_userpromptsubmit_default_path_unaffected_by_bridge_id_parameter(self) -> None:
        # Zero-regression pin for the existing, live UserPromptSubmit path: identical to
        # test_update_preserves_existing_and_is_idempotent/test_remove_owned_handler_only above,
        # now with the new `bridge_id` parameter never mentioned at all -- proving its presence
        # alone changes nothing for a caller that does not use it.
        command = f"/usr/bin/python3 hook.py --bridge-id {installer.BRIDGE_ID}"
        first = installer.update_hook_config(base_config(), command)
        second = installer.update_hook_config(first, command)
        self.assertEqual(first, second)
        payload = json.loads(second)
        handlers = payload["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(handlers), 2)
        removed = installer.update_hook_config(second, command, remove=True)
        self.assertEqual(len(json.loads(removed)["hooks"]["UserPromptSubmit"]), 1)

    def test_make_handler_default_timeout_still_unchanged_and_none_omits_the_key(self) -> None:
        self.assertEqual(installer.make_handler("cmd")["hooks"][0]["timeout"], installer.DEFAULT_HOOK_TIMEOUT)
        handler = installer.make_handler("cmd", timeout=None)
        self.assertNotIn("timeout", handler["hooks"][0])
        self.assertEqual(handler["hooks"][0]["command"], "cmd")

    def test_update_hook_config_default_timeout_path_is_byte_identical_to_before(self) -> None:
        # No `timeout` argument passed anywhere (matching every real install()/plan() call site
        # today) must still produce "timeout": 5, byte-for-byte identical to before this
        # parameter existed.
        raw = json.dumps({"hooks": {}}, sort_keys=True).encode()
        command = "/usr/bin/python3 hook.py --bridge-id x"
        updated = installer.update_hook_config(raw, command, event="SessionEnd")
        handler = json.loads(updated)["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertEqual(handler["timeout"], 5)
        self.assertEqual(handler, installer.make_handler(command)["hooks"][0])

    def test_update_hook_config_timeout_none_is_reachable_end_to_end_and_omits_the_key(self) -> None:
        # The exact shape write_candidate_capture.py's SessionEnd handler requires (write_
        # candidate_capture.py:2330-2336): no "timeout" key at all, reached through
        # update_hook_config() itself -- the real, supported config-writing API -- not just by
        # calling make_handler() directly.
        raw = json.dumps({"hooks": {}}, sort_keys=True).encode()
        command = f"/usr/bin/python3 write_candidate_capture.py scan --bridge-id {installer.BRIDGE_ID}"
        updated = installer.update_hook_config(raw, command, event="SessionEnd", timeout=None)
        handler = json.loads(updated)["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertNotIn("timeout", handler)
        self.assertEqual(handler["command"], command)
        # Idempotent under timeout=None too, and removable.
        again = installer.update_hook_config(updated, command, event="SessionEnd", timeout=None)
        self.assertEqual(updated, again)
        removed = installer.update_hook_config(again, command, event="SessionEnd", timeout=None, remove=True)
        self.assertEqual(json.loads(removed)["hooks"]["SessionEnd"], [])

    def test_atomic_write_sets_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            installer.atomic_write(path, b"{}\n", 0o600)
            self.assertEqual(path.read_bytes(), b"{}\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_owned_handler_requires_bridge_id_in_command(self) -> None:
        self.assertFalse(installer.owned_handler({"hooks": [{"command": "/usr/bin/true"}]}))
        self.assertTrue(
            installer.owned_handler(
                {"hooks": [{"command": f"run --bridge-id {installer.BRIDGE_ID}"}]}
            )
        )

    def test_owned_handler_rejects_invalid_bridge_id_instead_of_silently_returning_false(self) -> None:
        # Pre-fix, owned_handler(handler, bridge_id=None) never raised -- it just never matched any
        # token (a silent no-op returning False, despite contradicting the `str` annotation), and
        # bridge_id="" would structurally match the bare `--bridge-id ` marker text present in
        # essentially any owned-shaped command. Both must now fail closed with a clear InstallError
        # instead (AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md section 19.3 item 4 / section 26.1).
        handler = {"hooks": [{"command": f"run --bridge-id {installer.BRIDGE_ID}"}]}
        with self.assertRaises(installer.InstallError):
            installer.owned_handler(handler, bridge_id=None)  # type: ignore[arg-type]
        with self.assertRaises(installer.InstallError):
            installer.owned_handler(handler, bridge_id="")
        # Unaffected: the real, only-ever-exercised default-bridge_id call shape.
        self.assertTrue(installer.owned_handler(handler))

    def test_update_hook_config_rejects_invalid_bridge_id_and_event(self) -> None:
        raw = base_config()
        command = f"/usr/bin/python3 hook.py --bridge-id {installer.BRIDGE_ID}"
        for bad_bridge_id in (None, ""):
            with self.assertRaises(installer.InstallError):
                installer.update_hook_config(raw, command, bridge_id=bad_bridge_id)  # type: ignore[arg-type]
        for bad_event in (None, "", "not a valid event!", "123StartsWithDigit"):
            with self.assertRaises(installer.InstallError):
                installer.update_hook_config(raw, command, event=bad_event)  # type: ignore[arg-type]
        # Unaffected: the real, only-ever-exercised default-parameter call shape still works.
        updated = installer.update_hook_config(raw, command)
        self.assertEqual(len(json.loads(updated)["hooks"]["UserPromptSubmit"]), 2)

    def test_find_untracked_owned_configs_rejects_invalid_bridge_id_and_event_before_touching_disk(self) -> None:
        # Validated as the very first statements of the function, before any real filesystem
        # access -- proven here by calling it with none of the SSD-fixture path mocking that
        # InstallEndToEndTests-style classes set up: a pre-fix call would either have walked the
        # real local-homes tree looking for a degenerate marker, or (for bridge_id=None) silently
        # found nothing everywhere instead of rejecting the bad value outright. This also covers
        # the whole internal detection chain (_contains_owned_handler() and everything it calls),
        # which has no other call site in this file. AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md
        # section 19.3 item 4 / section 26.1.
        for bad_bridge_id in (None, ""):
            with self.assertRaises(installer.InstallError):
                installer._find_untracked_owned_configs(set(), bridge_id=bad_bridge_id)  # type: ignore[arg-type]
        for bad_event in (None, "", "not a valid event!"):
            with self.assertRaises(installer.InstallError):
                installer._find_untracked_owned_configs(set(), event=bad_event)  # type: ignore[arg-type]

    def test_owned_handler_rejects_bridge_id_as_a_mere_substring(self) -> None:
        # A substring check would wrongly treat any of these as "owned by
        # this installer" and update_hook_config() would delete/replace
        # them -- destroying a hook this installer never created (found in
        # the full-audit Workflow, 2026-08-17, deferred at the time because
        # install_bridge.py had never been run).
        self.assertFalse(
            installer.owned_handler(
                {"hooks": [{"command": f"echo not-really-{installer.BRIDGE_ID}-anything"}]}
            )
        )
        self.assertFalse(
            installer.owned_handler(
                {"hooks": [{"command": f"echo # mentions {installer.BRIDGE_ID} in a comment only"}]}
            )
        )
        self.assertFalse(
            installer.owned_handler(
                {"hooks": [{"command": f"run --other-flag {installer.BRIDGE_ID} --bridge-id something-else"}]}
            )
        )
        # A genuine shell comment -- `--bridge-id <BRIDGE_ID>` is present as
        # an adjacent pair, but only after an unquoted `#`. A shell running
        # this command line would never execute it, so it must not count as
        # evidence this handler is ours (P3-1, independent Claude opus5/max
        # review, 2026-08-17, round 1: the previous version of this test's
        # comment vector, above, never actually tested this -- it had an
        # unrelated word between `#` and BRIDGE_ID, so it passed for the
        # wrong reason (no adjacent flag at all), not because comments were
        # excluded).
        self.assertFalse(
            installer.owned_handler(
                {"hooks": [{"command": f"/usr/bin/true # --bridge-id {installer.BRIDGE_ID}"}]}
            )
        )
        # The real generated form -- flag and value as adjacent argv tokens,
        # value exactly equal to BRIDGE_ID -- still matches.
        self.assertTrue(
            installer.owned_handler(
                {
                    "hooks": [
                        {
                            "command": (
                                f"/usr/bin/python3 hook.py --bridge-id {installer.BRIDGE_ID} "
                                "--policy p.json"
                            )
                        }
                    ]
                }
            )
        )

    def test_mode_bits_raises_install_error_instead_of_crashing(self) -> None:
        # _mode_bits() replaced several unguarded path.stat() call sites
        # that let a bare OSError (deleted/racing file, permission error)
        # crash the installer with a Python traceback instead of the clean
        # {"ok": false, "error": ...} every other failure mode produces.
        missing = Path(tempfile.mkdtemp()) / "does-not-exist"
        with self.assertRaises(installer.InstallError):
            installer._mode_bits(missing)

    def test_pending_transaction_rolls_back_only_owned_after_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime = root / "runtime"
            backup_dir = runtime / "backups" / "install-1"
            backup_dir.mkdir(parents=True)
            config = root / "hooks.json"
            backup = backup_dir / "config.json"
            after_backup = backup_dir / "config-after.json"
            before = b'{"hooks":{"UserPromptSubmit":[]}}\n'
            after = b'{"hooks":{"UserPromptSubmit":[{"hooks":[]}]}}\n'
            installer.atomic_write(config, after, 0o600)
            installer.atomic_write(backup, before, 0o600)
            installer.atomic_write(after_backup, after, 0o600)
            receipt = {
                "schema": installer.RECEIPT_SCHEMA,
                "bridge_id": installer.BRIDGE_ID,
                "install_id": "install-1",
                "release_id": "release-1",
                "release_dir": os.fspath(runtime / "releases" / "r1"),
                "script_sha256": "a" * 64,
                "policy_sha256": "b" * 64,
                "volume_uuid": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
                "configs": [
                    {
                        "path": os.fspath(config),
                        "backup": os.fspath(backup),
                        "before_sha256": installer.sha256_bytes(before),
                        "before_mode": 0o644,
                        # First-ever install: "prev" (this transaction's own
                        # starting point) is the same as "before" (true
                        # pristine) -- see install()'s comment.
                        "prev_backup": os.fspath(backup),
                        "prev_sha256": installer.sha256_bytes(before),
                        "prev_mode": 0o644,
                        "after_backup": os.fspath(after_backup),
                        "after_sha256": installer.sha256_bytes(after),
                        "after_mode": 0o600,
                    }
                ],
            }
            pending = runtime / "pending-install.json"
            installer.atomic_write(
                pending,
                installer.canonical_json(
                    {"schema": installer.JOURNAL_SCHEMA, "kind": "install", "receipt": receipt}
                ),
                0o600,
            )
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "RUNTIME_BASE", runtime),
                mock.patch.object(installer, "PENDING_PATH", pending),
            ):
                result = installer.recover_pending_install()
            self.assertEqual(result["state"], "rolled_back")
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o644)
            self.assertFalse(pending.exists())

    def test_pending_transaction_finalizes_existing_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime = root / "runtime"
            backup_dir = runtime / "backups" / "install-2"
            backup_dir.mkdir(parents=True)
            config = root / "hooks.json"
            backup = backup_dir / "config.json"
            after_backup = backup_dir / "config-after.json"
            before = b'{"hooks":{"UserPromptSubmit":[]}}\n'
            after = b'{"hooks":{"UserPromptSubmit":[{"hooks":[]}]}}\n'
            installer.atomic_write(config, after, 0o600)
            installer.atomic_write(backup, before, 0o600)
            installer.atomic_write(after_backup, after, 0o600)
            receipt = {
                "schema": installer.RECEIPT_SCHEMA,
                "bridge_id": installer.BRIDGE_ID,
                "install_id": "install-2",
                "release_id": "release-2",
                "release_dir": os.fspath(runtime / "releases" / "r2"),
                "script_sha256": "a" * 64,
                "policy_sha256": "b" * 64,
                "volume_uuid": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
                "configs": [
                    {
                        "path": os.fspath(config),
                        "backup": os.fspath(backup),
                        "before_sha256": installer.sha256_bytes(before),
                        "before_mode": 0o644,
                        "prev_backup": os.fspath(backup),
                        "prev_sha256": installer.sha256_bytes(before),
                        "prev_mode": 0o644,
                        "after_backup": os.fspath(after_backup),
                        "after_sha256": installer.sha256_bytes(after),
                        "after_mode": 0o600,
                    }
                ],
            }
            pending = runtime / "pending-install.json"
            installer.atomic_write(
                pending,
                installer.canonical_json(
                    {"schema": installer.JOURNAL_SCHEMA, "kind": "install", "receipt": receipt}
                ),
                0o600,
            )
            installer.atomic_write(runtime / "latest-receipt.json", installer.canonical_json(receipt), 0o600)
            with (
                mock.patch.object(installer, "SSD_ROOT", root),
                mock.patch.object(installer, "RUNTIME_BASE", runtime),
                mock.patch.object(installer, "PENDING_PATH", pending),
            ):
                result = installer.recover_pending_install()
            self.assertEqual(result["state"], "committed")
            self.assertEqual(config.read_bytes(), after)
            self.assertFalse(pending.exists())

    def test_contains_owned_handler_recognizes_encoding_variants_without_crashing(self) -> None:
        # Round-11 self-check (2026-08-18): _contains_owned_handler() used
        # to call strict_json() (canonical UTF-8 only) and treat any
        # decode/parse failure as "not one of ours" -- so a live, owned
        # handler saved with a UTF-8 BOM, a duplicate top-level "hooks" key,
        # trailing comment text, or UTF-16 encoding was silently reported as
        # absent. Fails against commit 6fb376c671 (round 10): every variant
        # below returns False there instead of True.
        owned_command = f"/usr/bin/python3 hook.py --bridge-id {installer.BRIDGE_ID}"
        canonical = json.dumps(
            {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": owned_command}]}]}}
        )
        variants = {
            "utf8_bom": b"\xef\xbb\xbf" + canonical.encode("utf-8"),
            "utf16": canonical.encode("utf-16"),
            "trailing_comment": canonical.encode("utf-8") + b"\n// exported by some tool\n",
            "duplicate_key": (
                '{"hooks": {"UserPromptSubmit": []}, "hooks": '
                + json.dumps({"UserPromptSubmit": [{"hooks": [{"type": "command", "command": owned_command}]}]})
                + "}"
            ).encode("utf-8"),
        }
        for name, raw in variants.items():
            with self.subTest(variant=name):
                self.assertTrue(installer._contains_owned_handler(raw))

    def test_contains_owned_handler_does_not_crash_on_pathologically_nested_content(self) -> None:
        # Round-11 self-check (2026-08-18, round-1 pressure-test P1
        # finding): a deeply nested payload (well within
        # MAX_MANAGED_FILE_BYTES) raises RecursionError out of the
        # underlying json.loads()/JSONDecoder().raw_decode() call, which
        # this function's original strict_json()-only parse (and the
        # lenient fallback later added to fix the encoding variants above)
        # both left uncaught -- escaping this function's own "must never be
        # left to raise past this function" contract all the way out
        # through main()'s `except InstallError` as a bare traceback. Plain
        # UTF-8 so this reproduces the crash at Layer 0 (the same call
        # baseline's strict_json()-only parse already made) -- confirmed by
        # hand against commit 6fb376c671: this exact payload raises an
        # uncaught RecursionError there instead of returning False.
        depth = 2000
        nested = ("[" * depth + "1" + "]" * depth).encode("utf-8")
        self.assertLess(len(nested), 65_536)  # within the structural-detection size bound
        self.assertFalse(installer._contains_owned_handler(nested))

    def test_contains_owned_handler_does_not_escalate_a_proven_unrelated_config(self) -> None:
        # Round-11 self-check (2026-08-18, round-1 pressure-test P2
        # finding): the raw byte-marker fallback (Stage 2) that fails
        # closed on genuine ambiguity used to run even when Stage 1 had
        # already structurally parsed the content and definitively proven
        # every handler in it is not ours -- so a legitimate, unrelated
        # hooks.json that merely mentioned the bridge marker in an
        # unrelated field (documenting an unrelated past migration, for
        # example) was wrongly escalated to InstallError instead of
        # returning the already-proven False.
        unrelated = json.dumps(
            {
                "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}]},
                "_migration_note": f"previously used --bridge-id {installer.BRIDGE_ID}, since replaced",
            }
        ).encode("utf-8")
        self.assertFalse(installer._contains_owned_handler(unrelated))

    def test_contains_owned_handler_does_not_crash_on_a_huge_json_integer(self) -> None:
        # Round-12 fix (2026-08-18, independent Claude opus5/max review,
        # round 11, R11-P1-A): CPython >= 3.9.14/3.10.7/3.11 caps int<->str
        # conversion at 4300 digits, and json.loads() raises a bare
        # ValueError (not json.JSONDecodeError) for an integer literal
        # longer than that. _safe_parse_strict_utf8()'s except clause missed
        # it even though the sibling function added in the same commit
        # (_lenient_parse_last_key_wins()) already caught it -- an internal
        # inconsistency in the round-11 patch. On an interpreter without the
        # limit this content just parses normally to a definitive False
        # (structurally proven not ours, no marker present either way) --
        # the property under test ("does not crash") holds regardless of
        # interpreter, which is why this doesn't need a version guard.
        huge_int = "7" * 4301
        poison = (
            '{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", '
            '"command": "/usr/local/bin/other-tool"}]}]}, "nonce": ' + huge_int + "}"
        ).encode()
        self.assertFalse(installer._contains_owned_handler(poison))

    def test_stream_scan_oversized_finds_a_marker_split_exactly_across_a_chunk_boundary(self) -> None:
        # Round-13 fix (2026-08-19): _stream_scan_oversized_for_bridge_marker()
        # reads in 1 MiB chunks with a small overlap window so a marker that
        # happens to straddle two chunks isn't missed -- this constructs that
        # exact shape rather than trusting the sliding-window logic by
        # inspection. Also confirms the no-marker and marker-not-split cases.
        marker = f"--bridge-id {installer.BRIDGE_ID}".encode()
        chunk_size = 1_048_576
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            split_path = root / "split.bin"
            # Position the marker so it starts a few bytes before the chunk
            # boundary and ends a few bytes after it.
            straddle_offset = chunk_size - 5
            split_path.write_bytes(b"a" * straddle_offset + marker + b"b" * 2000)
            self.assertTrue(installer._stream_scan_oversized_for_bridge_marker(split_path))

            clean_path = root / "clean.bin"
            clean_path.write_bytes(b"a" * (chunk_size * 2))
            self.assertFalse(installer._stream_scan_oversized_for_bridge_marker(clean_path))

            whole_path = root / "whole.bin"
            whole_path.write_bytes(b"a" * 100 + marker + b"b" * (chunk_size * 2))
            self.assertTrue(installer._stream_scan_oversized_for_bridge_marker(whole_path))

    def test_stream_scan_oversized_respects_a_non_default_bridge_id(self) -> None:
        # Both independent reviewers' repro (2026-08-20): unlike its non-streaming sibling
        # _raw_bytes_contain_bridge_marker() (already parameterized in the prior round),
        # _stream_scan_oversized_for_bridge_marker() used to hardcode BRIDGE_ID with no way to
        # search for any other identity at all -- so an oversized file carrying a real handler
        # under write_candidate_capture.MODULE_ID (or any other non-default bridge_id) was
        # invisible to this scanner no matter what was passed, while the SAME content under the
        # module's own default BRIDGE_ID was correctly found. This constructs exactly that pair
        # and checks both directions plus the byte-identical default-identity behavior.
        other_bridge_id = "orca-claude-codex-memory-write-trigger-v1"  # write_candidate_capture.MODULE_ID
        other_marker = f"--bridge-id {other_bridge_id}".encode()
        default_marker = f"--bridge-id {installer.BRIDGE_ID}".encode()
        padding = b"a" * (installer.MAX_MANAGED_FILE_BYTES + 1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            other_id_path = root / "other-bridge-id.bin"
            other_id_path.write_bytes(padding + other_marker + b"b" * 2000)
            self.assertGreater(other_id_path.stat().st_size, installer.MAX_MANAGED_FILE_BYTES)
            # Pre-fix behavior for this exact content: not found under any bridge_id, since the
            # function never accepted one to search for.
            self.assertFalse(installer._stream_scan_oversized_for_bridge_marker(other_id_path))
            # Post-fix: passing the matching bridge_id finds it.
            self.assertTrue(
                installer._stream_scan_oversized_for_bridge_marker(other_id_path, bridge_id=other_bridge_id)
            )
            # Searching for a THIRD, still-different bridge_id must not match either.
            self.assertFalse(
                installer._stream_scan_oversized_for_bridge_marker(other_id_path, bridge_id="some-unrelated-id")
            )

            # Default-identity oversized-file behavior is byte-for-byte/result-for-result
            # unaffected when no override is passed at all -- same content shape, this module's
            # own BRIDGE_ID instead.
            default_id_path = root / "default-bridge-id.bin"
            default_id_path.write_bytes(padding + default_marker + b"b" * 2000)
            self.assertTrue(installer._stream_scan_oversized_for_bridge_marker(default_id_path))

    # -- §19.3 P2-1/P2-2/P2-3 fixes (2026-08-21): see AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md
    # section 19.3 for the original 3 documented-not-fixed gaps this closes.

    def test_raw_bytes_marker_finds_a_double_quoted_bridge_id_in_real_json_serialized_bytes(self) -> None:
        # P2-1: _bridge_id_marker_value_forms() used to search for an unescaped, literal
        # `"<id>"` double-quote form, which can never match real hooks.json bytes -- JSON always
        # escapes an embedded `"` as `\"` on disk. This builds a REAL hooks.json-shaped fixture
        # the way it would actually be produced -- a Python command string containing literal
        # double quotes around the bridge_id, serialized with json.dumps() exactly like
        # canonical_json() does -- not a hand-simplified stand-in.
        command = f'/usr/bin/python3 hook.py --bridge-id "{installer.BRIDGE_ID}" --timeout 5'
        raw = json.dumps(
            {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]}},
            indent=2,
        ).encode("utf-8")
        # Pin down the fixture is genuinely realistic: the on-disk bytes contain the JSON-escaped
        # `\"` form around the bridge_id, never a literal unescaped `"`.
        self.assertIn(f'\\"{installer.BRIDGE_ID}\\"'.encode(), raw)
        self.assertNotIn(f'"{installer.BRIDGE_ID}"'.encode(), raw)
        self.assertTrue(installer._raw_bytes_contain_bridge_marker(raw))
        # A different bridge_id must still not match this content.
        self.assertFalse(installer._raw_bytes_contain_bridge_marker(raw, bridge_id="some-unrelated-id"))

    def test_raw_bytes_marker_is_whitespace_tolerant_like_the_structural_detector(self) -> None:
        # P2-2: owned_handler()'s shlex.split()-based structural detector (Stage 1) already treats
        # a double space or a tab between `--bridge-id` and its value as an ordinary token
        # separator -- but the raw-bytes fallback scanners used to search for a single literal
        # space only, missing both variants. Builds both shapes as real JSON-serialized bytes (a
        # literal tab is itself a JSON control character requiring the `\t` escape on disk -- this
        # fixture goes through json.dumps() for real) and confirms Stage 1 and the raw-bytes
        # scanner now agree for both.
        for separator, label in ((" " * 2, "double-space"), ("\t", "tab")):
            with self.subTest(label=label):
                command = f"/usr/bin/python3 hook.py --bridge-id{separator}{installer.BRIDGE_ID} --timeout 5"
                handler = {"hooks": [{"type": "command", "command": command}]}
                self.assertTrue(installer.owned_handler(handler))  # Stage 1 already got this right
                raw = json.dumps({"hooks": {"UserPromptSubmit": [handler]}}).encode()
                self.assertTrue(installer._raw_bytes_contain_bridge_marker(raw))

    def test_raw_bytes_marker_finds_u_escaped_json_variants_a_different_encoder_could_legitimately_write(
        self,
    ) -> None:
        # P2-A fix (independent Claude opus5/max review, 2026-08-21): _json_string_body() -- and so
        # both raw-bytes marker scanners built on it -- only ever recognized json.dumps()'s OWN
        # choice of escaped rendering (`\"`, `\t`, ...). RFC 8259 section 7 equally permits
        # representing the same characters as `\uXXXX` numeric escapes instead, which real encoders
        # other than json.dumps() legitimately choose by default (.NET's System.Text.Json is the
        # reviewer's cited example). The reviewer reproduced legal-JSON variants where the
        # structural detector (Stage 1, via owned_handler()) says True but the raw-bytes scanner
        # (Stage 2) said False, because Stage 2 only ever searched for json.dumps()'s shorthand --
        # traced to a real failure path: an oversized, relocated hooks.json written by such an
        # alternate encoder (not by this tool -- Stage 2 exists precisely to scan files this tool
        # did NOT write) carrying a genuine handler would make the streaming scanner return False
        # even though the handler is genuinely present, and uninstall() would then delete the
        # receipt while the handler stays live.
        #
        # Builds each of the 6 characters this file's own marker text can ever need to escape --
        # quote, single-quote, backslash, tab, CR, LF -- as REAL, valid, json.loads()-parseable JSON
        # bytes with that ONE character rendered via `\uXXXX` instead of json.dumps()'s shorthand --
        # not a hand-simplified stand-in -- and confirms the raw-bytes scanner now finds each one.
        # CR/LF/backslash also get an uppercase-hex-digit variant (their hex representation contains
        # a letter, unlike quote/single-quote/tab's), confirming both cases a real encoder could
        # choose are recognized, not just one.
        bridge_id = installer.BRIDGE_ID
        backslash_bridge_id = "back\\slash-id"  # a literal backslash inside the bridge_id itself

        def hooks_json_bytes(command_body: str) -> bytes:
            # Deliberately NOT built via json.dumps(): `command_body` already carries its own
            # `\uXXXX`/literal escaping exactly as the fixture wants it on disk, and json.dumps()
            # would double-escape it (turning our literal `"` text into `\\u0022`) instead of
            # leaving it as the single escape sequence a real encoder would write once.
            return (
                '{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "'
                + command_body
                + '"}]}]}}'
            ).encode()

        cases = [
            ("quote", f"/usr/bin/python3 hook.py --bridge-id \\u0022{bridge_id}\\u0022 --timeout 5", bridge_id),
            ("single-quote", f"/usr/bin/python3 hook.py --bridge-id \\u0027{bridge_id}\\u0027 --timeout 5", bridge_id),
            ("tab", f"/usr/bin/python3 hook.py --bridge-id\\u0009{bridge_id} --timeout 5", bridge_id),
            ("cr-lower", f"/usr/bin/python3 hook.py --bridge-id\\u000d{bridge_id} --timeout 5", bridge_id),
            ("cr-upper", f"/usr/bin/python3 hook.py --bridge-id\\u000D{bridge_id} --timeout 5", bridge_id),
            ("lf-lower", f"/usr/bin/python3 hook.py --bridge-id\\u000a{bridge_id} --timeout 5", bridge_id),
            ("lf-upper", f"/usr/bin/python3 hook.py --bridge-id\\u000A{bridge_id} --timeout 5", bridge_id),
            (
                # Single-quoted (shlex.split() keeps a backslash inside single quotes completely
                # literal, unlike outside quotes where it is an escape character consuming the next
                # character -- an unquoted `back\slash-id` would shlex-parse to `backslash-id`, not
                # this bridge_id, so the single-quoted marker-value-form is the one that actually
                # round-trips through Stage 1 for a bridge_id containing a literal backslash).
                "backslash-lower",
                "/usr/bin/python3 hook.py --bridge-id \\u0027back\\u005cslash-id\\u0027 --timeout 5",
                backslash_bridge_id,
            ),
            (
                "backslash-upper",
                "/usr/bin/python3 hook.py --bridge-id \\u0027back\\u005Cslash-id\\u0027 --timeout 5",
                backslash_bridge_id,
            ),
        ]
        for label, command_body, case_bridge_id in cases:
            with self.subTest(label=label):
                raw = hooks_json_bytes(command_body)
                # Fixture sanity: genuinely valid JSON (json.loads() must not raise), and its
                # on-disk bytes genuinely use a `\u` numeric escape rather than accidentally
                # collapsing to json.dumps()'s own shorthand or an unescaped literal byte.
                decoded = json.loads(raw)
                handler = decoded["hooks"]["UserPromptSubmit"][0]
                self.assertTrue(installer.owned_handler(handler, bridge_id=case_bridge_id))
                self.assertIn(b"\\u0", raw)
                self.assertTrue(installer._raw_bytes_contain_bridge_marker(raw, bridge_id=case_bridge_id))
                # A different bridge_id must still not match this content.
                self.assertFalse(
                    installer._raw_bytes_contain_bridge_marker(raw, bridge_id="some-unrelated-id")
                )

    def test_stream_scan_oversized_is_whitespace_tolerant_across_a_chunk_boundary(self) -> None:
        # P2-2, oversized-streaming twin: the same whitespace tolerance as the test above, but for
        # _stream_scan_oversized_for_bridge_marker(), including a marker straddling a chunk
        # boundary the way test_stream_scan_oversized_finds_a_marker_split_exactly_across_a_chunk_
        # boundary already covers for the plain bare-space marker.
        #
        # P2-B fix (independent Claude opus5/max review, 2026-08-21): the original version of this
        # test padded with `chunk_size - 5` bytes and then wrote the WHOLE json.dumps()-serialized
        # document (`raw`) starting there, on the theory that this put the marker "a few bytes
        # before the boundary" -- but the marker text sits well INSIDE `raw`, after the
        # `{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/
        # python3 hook.py ` prefix (dozens of bytes), so the marker actually landed dozens of bytes
        # PAST the chunk boundary, entirely inside the second chunk -- never straddling it at all.
        # Proof this was not exercising the overlap-window logic it claimed to: shrinking
        # `overlap_len` in _stream_scan_oversized_for_bridge_marker() (this file, ~line 976) still
        # left every test in this suite passing, including this one, even though that change
        # genuinely makes the scanner miss real straddling offsets. This rewrite computes the
        # marker's own exact byte offset inside `raw` and positions the padding so the chunk
        # boundary falls strictly inside the marker's own byte span -- verified explicitly below,
        # not just assumed from the padding arithmetic -- so a corrupted overlap window has
        # something genuine to fail against.
        command = f"/usr/bin/python3 hook.py --bridge-id\t{installer.BRIDGE_ID} --timeout 5"
        raw = json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]}}).encode()
        # The exact on-disk marker bytes: "--bridge-id", the JSON-escaped tab (`\t`, two literal
        # characters -- json.dumps() always escapes a raw tab this way), then BRIDGE_ID.
        marker_bytes = (
            "--bridge-id" + installer._json_string_body("\t") + installer.BRIDGE_ID
        ).encode()
        marker_offset_in_raw = raw.index(marker_bytes)
        chunk_size = 1_048_576
        straddle_into = 5  # bytes of the marker that must land in the FIRST chunk
        self.assertGreater(len(marker_bytes), straddle_into)  # marker must also reach the 2nd chunk
        padding_len = chunk_size - marker_offset_in_raw - straddle_into
        self.assertGreater(padding_len, 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            straddle_path = root / "tab_straddle.bin"
            straddle_path.write_bytes(b"a" * padding_len + raw + b"b" * 2000)
            # Confirm the marker genuinely straddles the chunk boundary before trusting the
            # scanner's result on it -- byte index `chunk_size` must fall strictly inside the
            # marker's own span in the file, not merely somewhere inside the surrounding document.
            marker_start = padding_len + marker_offset_in_raw
            marker_end = marker_start + len(marker_bytes)
            self.assertLess(marker_start, chunk_size)
            self.assertGreater(marker_end, chunk_size)
            self.assertTrue(installer._stream_scan_oversized_for_bridge_marker(straddle_path))

    # -- `install-write-trigger` CLI action (AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md,
    # "install-write-trigger" section): make_handler()'s new `status_message` parameter, and
    # _load_write_trigger_config()'s fail-closed policy validation. The full transactional
    # install/dry-run/idempotency/uninstall/recover cycle is in InstallWriteTriggerEndToEndTests
    # below, which needs the isolated fake-SSD fixture; these test the two pieces that do not.

    def test_make_handler_default_status_message_is_unchanged(self) -> None:
        self.assertEqual(installer.DEFAULT_STATUS_MESSAGE, "Loading Claude memory from verified SSD")
        handler = installer.make_handler("some command")
        self.assertEqual(handler["hooks"][0]["statusMessage"], installer.DEFAULT_STATUS_MESSAGE)

    def test_make_handler_accepts_an_explicit_status_message(self) -> None:
        handler = installer.make_handler("some command", status_message="custom message")
        self.assertEqual(handler["hooks"][0]["statusMessage"], "custom message")

    def test_update_hook_config_status_message_is_reachable_end_to_end(self) -> None:
        # Mirrors §17.2's `timeout=None` reachability fix: the parameter must actually be
        # forwarded by update_hook_config(), the one real config-writing entry point, not only
        # exercisable by calling make_handler() directly.
        raw = json.dumps({"hooks": {}}, sort_keys=True).encode()
        command = f"/usr/bin/python3 write_candidate_capture.py scan --bridge-id {wtc.MODULE_ID}"
        updated = installer.update_hook_config(
            raw, command, event="SessionEnd", bridge_id=wtc.MODULE_ID, timeout=None,
            status_message="Scanning session for durable memory candidates",
        )
        payload = json.loads(updated)
        handler = payload["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertEqual(handler["statusMessage"], "Scanning session for durable memory candidates")
        self.assertNotIn("timeout", handler)
        # Default (no status_message kwarg) is still unchanged.
        default_updated = installer.update_hook_config(raw, "cmd", event="SessionEnd")
        default_handler = json.loads(default_updated)["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertEqual(default_handler["statusMessage"], installer.DEFAULT_STATUS_MESSAGE)

    @staticmethod
    def _write_trigger_policy_bytes(
        *, enabled: bool = True, max_candidates_per_project: int = 50, max_candidate_bytes: int = 1500
    ) -> bytes:
        return json.dumps(
            {
                "write_trigger": {
                    "enabled": enabled,
                    "max_candidates_per_project": max_candidates_per_project,
                    "max_candidate_bytes": max_candidate_bytes,
                }
            }
        ).encode()

    def test_load_write_trigger_config_requires_a_readable_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.json"
            with self.assertRaises(installer.InstallError):
                installer._load_write_trigger_config(missing)

    def test_load_write_trigger_config_requires_a_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_bytes(b"[]")
            with self.assertRaises(installer.InstallError):
                installer._load_write_trigger_config(path)

    def test_load_write_trigger_config_fails_closed_when_write_trigger_key_is_absent(self) -> None:
        # No `write_trigger` key at all -- write_candidate_capture._parse_write_trigger_block(None)
        # returns enabled=False, so this must fail closed exactly like an explicit
        # `"enabled": false` does, not silently install an inert handler.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_bytes(json.dumps({"schema": "irrelevant-for-this-function"}).encode())
            with self.assertRaises(installer.InstallError) as ctx:
                installer._load_write_trigger_config(path)
            self.assertIn("write_trigger.enabled", str(ctx.exception))

    def test_load_write_trigger_config_fails_closed_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_bytes(self._write_trigger_policy_bytes(enabled=False))
            with self.assertRaises(installer.InstallError) as ctx:
                installer._load_write_trigger_config(path)
            self.assertIn("write_trigger.enabled", str(ctx.exception))

    def test_load_write_trigger_config_fails_closed_on_malformed_write_trigger_block(self) -> None:
        # write_candidate_capture.WriteCaptureError (unexpected keys) must surface as this file's
        # own InstallError, not propagate as a foreign exception type.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_bytes(json.dumps({"write_trigger": {"enabled": True}}).encode())
            with self.assertRaises(installer.InstallError):
                installer._load_write_trigger_config(path)

    def test_load_write_trigger_config_succeeds_and_returns_module_id_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_bytes(
                self._write_trigger_policy_bytes(max_candidates_per_project=77, max_candidate_bytes=1234)
            )
            config = installer._load_write_trigger_config(path)
            self.assertEqual(
                config,
                {"bridge_id": wtc.MODULE_ID, "max_candidates_per_project": 77, "max_candidate_bytes": 1234},
            )

    def test_write_trigger_bridge_id_constant_matches_write_candidate_capture_module_id(self) -> None:
        # Round-52 fix (converged independent Claude opus/max + Codex gpt-5.6-sol/max review,
        # 2026-08-21): install_bridge.WRITE_TRIGGER_BRIDGE_ID is a hardcoded literal, deliberately
        # NOT imported from write_candidate_capture (see that constant's own comment for why: base-
        # only paths must not depend on that sibling module being importable at all). This is the
        # drift guard the reviewers explicitly asked for: if a future change to either module's own
        # literal ever desynchronizes them, this test -- not a confusing runtime symptom somewhere
        # else in the file -- is what fails.
        self.assertEqual(installer.WRITE_TRIGGER_BRIDGE_ID, wtc.MODULE_ID)

    def test_import_write_candidate_capture_does_not_grow_sys_path_unboundedly(self) -> None:
        # P2 fix, lower priority (independent Claude opus5/max review, 2026-08-21):
        # _import_write_candidate_capture() used to unconditionally `sys.path.insert(0, ...)` on
        # every call -- uninstall()/recover_pending_install() each call it (indirectly, via
        # _untracked_owned_including_write_trigger() pre-fix, or verify() post-fix) once per action,
        # so a long-lived caller invoking this module's actions repeatedly grew sys.path by one
        # duplicate entry per call, unboundedly. Fixed by checking membership first.
        module_dir = os.fspath(Path(installer.__file__).resolve().parent)
        original_sys_path = list(sys.path)
        try:
            # The test file's own bootstrap (this file's line 17) already inserted this exact
            # directory once -- strip every occurrence first so this test genuinely exercises
            # dedup-across-repeated-calls rather than starting from an already-ambiguous state.
            sys.path[:] = [entry for entry in sys.path if entry != module_dir]
            self.assertNotIn(module_dir, sys.path)  # fixture precondition
            for _ in range(5):
                installer._import_write_candidate_capture()
            self.assertEqual(sys.path.count(module_dir), 1)
        finally:
            sys.path[:] = original_sys_path


class InstallEndToEndTests(unittest.TestCase):
    # plan()/install()/verify()/uninstall() were entirely uncovered by the
    # existing suite -- every prior test exercised a helper function
    # directly, never the actual top-level actions main() dispatches to
    # (found in the full-audit Workflow, 2026-08-17, deferred at the time
    # because install_bridge.py had never been run; now in scope ahead of
    # an actual install). This builds a fully isolated fake "Extreme SSD"
    # so these real, unmocked functions run against real files with real
    # permission checks, not a monkeypatched shortcut.

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.temp = self._stack.enter_context(tempfile.TemporaryDirectory())
        root = Path(self.temp).resolve()
        ssd_root = root / "ssd"
        local_homes = ssd_root / "Orca/local-homes"
        runtime_base = local_homes / ".shared-runtime/claude-codex-memory-bridge"
        pending_path = runtime_base / "pending-install.json"
        source_dir = ssd_root / "source"
        source_script = source_dir / "claude_memory_hook.py"

        local_homes.mkdir(parents=True)
        (local_homes / ".codex").mkdir()
        (local_homes / ".claude/projects").mkdir(parents=True)
        (local_homes / "codex-accounts/acct-one/home").mkdir(parents=True)
        source_dir.mkdir()

        self._write(local_homes / ".codex/hooks.json", self._base_hooks_json())
        self._write(local_homes / "codex-accounts/acct-one/home/hooks.json", self._base_hooks_json())
        self._write(source_script, b"#!/usr/bin/env python3\n# fixture hook script\n")

        self._stack.enter_context(mock.patch.object(installer, "SSD_ROOT", ssd_root))
        self._stack.enter_context(mock.patch.object(installer, "LOCAL_HOMES_ROOT", local_homes))
        self._stack.enter_context(mock.patch.object(installer, "RUNTIME_BASE", runtime_base))
        self._stack.enter_context(mock.patch.object(installer, "PENDING_PATH", pending_path))
        self._stack.enter_context(mock.patch.object(installer, "SOURCE_SCRIPT", source_script))
        self._stack.enter_context(mock.patch.object(installer, "volume_uuid", lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"))
        self._stack.enter_context(mock.patch.object(Path, "home", lambda: local_homes))

        self.local_homes = local_homes
        self.runtime_base = runtime_base
        self.source_script = source_script
        self.main_config = local_homes / ".codex/hooks.json"
        self.account_config = local_homes / "codex-accounts/acct-one/home/hooks.json"

    @staticmethod
    def _write(path: Path, raw: bytes, mode: int = 0o644) -> None:
        path.write_bytes(raw)
        path.chmod(mode)

    @staticmethod
    def _base_hooks_json() -> bytes:
        return (
            json.dumps(
                {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}]}},
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()

    def test_plan_reports_pending_changes_before_install(self) -> None:
        result = installer.plan()
        self.assertTrue(result["ok"])
        self.assertFalse(result["pending_transaction"])
        self.assertEqual(len(result["configs"]), 2)
        self.assertTrue(all(row["will_change"] for row in result["configs"]))

    def test_install_verify_round_trip(self) -> None:
        receipt = installer.install()
        self.assertEqual(receipt["bridge_id"], installer.BRIDGE_ID)
        self.assertEqual(len(receipt["configs"]), 2)

        # The owned handler is actually present in both configs now, and the
        # pre-existing unrelated handler survived untouched.
        for config_path in (self.main_config, self.account_config):
            payload = json.loads(config_path.read_bytes())
            handlers = payload["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(handlers), 2)
            self.assertTrue(installer.owned_handler(handlers[1]))
            self.assertEqual(handlers[0]["hooks"][0]["command"], "/usr/bin/true")
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

        verify_result = installer.verify()
        self.assertTrue(verify_result["ok"])
        self.assertEqual(verify_result["release_id"], receipt["release_id"])
        self.assertEqual(sorted(verify_result["configs"]), sorted(os.fspath(p) for p in (self.main_config, self.account_config)))

    def test_make_release_bridge_id_fragment_is_findable_by_the_raw_bytes_marker_search_for_a_single_quote_value(
        self,
    ) -> None:
        # §19.3 P2-3 (2026-08-21): make_release() used to build its `--bridge-id <value>` fragment
        # via an inline shlex.quote() call, entirely independent of _bridge_id_marker_value_forms()
        # -- coincidentally compatible for a typical value, but nothing enforced or tested the
        # coupling. A bridge_id containing a literal single quote is exactly the case that breaks
        # the coincidence: shlex.quote() renders it via an embedded `'...'"'"'...'` form (which
        # itself contains a literal `"` that real JSON serialization would escape), a shape neither
        # of the two hand-rolled quoted marker forms (`"<id>"`, `'<id>'`) could ever match on their
        # own. This calls the REAL make_release() (not a simplified stand-in) with such a bridge_id
        # and confirms its actual constructed command -- embedded in a genuinely
        # json.dumps()-serialized hooks.json, not just checked as a bare string -- is genuinely
        # findable by the raw-bytes marker search.
        weird_bridge_id = "orca-can't-quote-me"
        with mock.patch.object(installer, "BRIDGE_ID", weird_bridge_id):
            release = installer.make_release()
        command = release["command"]
        # Sanity: this is a genuine, round-trippable command line, not a hand-simplified stand-in
        # -- shlex.split() recovers the exact original value.
        tokens = shlex.split(command)
        self.assertEqual(tokens[tokens.index("--bridge-id") + 1], weird_bridge_id)
        self.assertIn("'", command)  # confirms this test actually exercises shlex.quote()'s embedded-quote path

        raw = json.dumps(
            {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]}}
        ).encode()
        self.assertTrue(installer._raw_bytes_contain_bridge_marker(raw, bridge_id=weird_bridge_id))
        # A different bridge_id must not match this content.
        self.assertFalse(installer._raw_bytes_contain_bridge_marker(raw, bridge_id="some-unrelated-id"))

    def test_userpromptsubmit_full_cycle_snapshot_unchanged_by_session_end_capability(self) -> None:
        # Regression/snapshot guard for the SessionEnd hook-registration capability (AUTO-LEARN-
        # TRIGGER-DESIGN-2026-08-19.md section 4.2). This drives the exact same top-level
        # plan()/install()/verify()/uninstall() path every real UserPromptSubmit-only install
        # goes through, with none of make_handler()/update_hook_config()/_owned_shape_match()/
        # _attempt_structural_detection()/_contains_owned_handler() ever passed a non-default
        # `event`/`timeout` -- and pins the exact resulting hooks.json bytes (not just counts/
        # shape, the way the other tests in this class do) plus the exact verify()/uninstall()
        # result shape, so any accidental behavior drift introduced while adding the new
        # parameters would fail this test even if every other test in this file still passed.
        plan_result = installer.plan()
        self.assertTrue(plan_result["ok"])
        self.assertTrue(all(row["will_change"] for row in plan_result["configs"]))

        receipt = installer.install()
        command = receipt["command"]
        # The command line must still route through the plain UserPromptSubmit hook script --
        # nothing about this change makes a plain install reference write_candidate_capture.py
        # or any other event's handler.
        self.assertIn("claude_memory_hook.py", command)
        self.assertNotIn("write_candidate_capture.py", command)

        expected_config = installer.canonical_json(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "/usr/bin/true"}]},
                        installer.make_handler(command),
                    ]
                }
            }
        )
        # Both fixture configs started byte-identical, so both must land on the same exact
        # post-install bytes.
        self.assertEqual(self.main_config.read_bytes(), expected_config)
        self.assertEqual(self.account_config.read_bytes(), expected_config)
        for config_path in (self.main_config, self.account_config):
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            payload = json.loads(config_path.read_bytes())
            # Only ever the one event key -- SessionEnd (or any other event) must never appear
            # in a plain UserPromptSubmit-only install's output.
            self.assertEqual(list(payload["hooks"].keys()), ["UserPromptSubmit"])

        verify_result = installer.verify()
        self.assertEqual(
            verify_result,
            {
                "ok": True,
                # Round-4 (2026-08-28): `hook_functional` answers "is the redaction hook actually
                # running on every managed config?" on its own, separately from `ok` (which also
                # folds in account coverage), and `summary` says it in one readable line. `broken`
                # and `drift` are the two lists that used to be one raise -- see verify()'s own
                # comment for why conflating them caused a week of unredacted production.
                "hook_functional": True,
                "summary": (
                    "HEALTHY: the redaction hook is correctly wired and functional on every "
                    "managed config."
                ),
                "release_id": receipt["release_id"],
                "script_sha256": receipt["script_sha256"],
                "policy_sha256": receipt["policy_sha256"],
                "volume_uuid": receipt["volume_uuid"],
                "configs": sorted(os.fspath(p) for p in (self.main_config, self.account_config)),
                "broken": [],
                "drift": [],
                "unreachable": [],
                # Always present as of the fail-closed account-coverage fix (2026-08-27); empty
                # here because this fixture's home directory has no Orca account registry at all
                # (see RealOrcaAccountRegistryEnumerationTests for the populated case).
                "unmanaged": [],
            },
        )

        uninstall_result = installer.uninstall()
        self.assertTrue(uninstall_result["ok"])
        self.assertEqual(
            sorted(uninstall_result["restored"]),
            sorted(os.fspath(p) for p in (self.main_config, self.account_config)),
        )
        self.assertEqual(uninstall_result["unreachable"], [])
        # Exactly restored to the pristine pre-install bytes -- the SessionEnd capability must
        # leave zero trace in a plain UserPromptSubmit-only uninstall.
        self.assertEqual(self.main_config.read_bytes(), self._base_hooks_json())
        self.assertEqual(self.account_config.read_bytes(), self._base_hooks_json())

    def test_install_is_idempotent_replan_shows_no_change(self) -> None:
        installer.install()
        result = installer.plan()
        self.assertFalse(result["pending_transaction"])
        self.assertTrue(all(not row["will_change"] for row in result["configs"]))
        # Installing again on top of an already-installed state must not
        # duplicate the owned handler.
        installer.install()
        payload = json.loads(self.main_config.read_bytes())
        handlers = payload["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(handlers), 2)

    def test_verify_detects_installed_script_tampering(self) -> None:
        receipt = installer.install()
        installed_script = Path(receipt["release_dir"]) / "claude_memory_hook.py"
        installed_script.chmod(0o600)
        installed_script.write_bytes(b"# tampered\n")
        installed_script.chmod(0o600)
        with self.assertRaises(installer.InstallError):
            installer.verify()

    def test_verify_reports_a_foreign_edit_without_calling_the_hook_broken(self) -> None:
        # Round-4 (2026-08-28) contract change, deliberate, replacing this test's previous
        # `assertRaises(InstallError)` body. Something outside this installer adding its OWN handler
        # used to abort verify() with `hook config drift: <path>` -- the same message, the same exit
        # code and the same abort as `~/.codex/hooks.json` being DELETED, which is how the
        # 2026-08-21 incident stayed unnoticed for a week (see verify()'s own comment).
        #
        # It is not hypothetical drift either: Orca does exactly this on this machine today, adding
        # six of its own hook events to the shared `.codex/hooks.json`. So the requirement is not
        # "stop noticing" -- the edit must still be reported, by path and by classification -- but
        # "stop calling it broken", because the redaction hook is untouched and still firing.
        installer.install()
        payload = json.loads(self.main_config.read_bytes())
        payload["hooks"]["UserPromptSubmit"].append({"hooks": [{"type": "command", "command": "/bin/echo hi"}]})
        drifted = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        self.main_config.chmod(0o600)
        self.main_config.write_bytes(drifted)
        self.main_config.chmod(0o600)

        result = installer.verify()
        self.assertTrue(result["hook_functional"])
        self.assertEqual(result["broken"], [])
        self.assertTrue(result["ok"])
        drift = [record for record in result["drift"] if record["config"] == os.fspath(self.main_config)]
        self.assertEqual(len(drift), 1, f"the foreign edit was not reported at all: {result['drift']}")
        self.assertEqual(drift[0]["classification"], "foreign_change")
        self.assertTrue(drift[0]["hook_functional"])
        self.assertIn("does NOT affect it", result["summary"])

    def test_verify_calls_a_reformatted_config_cosmetic_not_broken(self) -> None:
        # The other half of the same distinction, and the one that made verify() permanently red:
        # a writer that rewrites the file with different formatting but an identical object graph.
        installer.install()
        payload = json.loads(self.main_config.read_bytes())
        reserialized = json.dumps(payload, sort_keys=False, indent=4).encode()
        self.assertNotEqual(reserialized, self.main_config.read_bytes())
        self.main_config.chmod(0o600)
        self.main_config.write_bytes(reserialized)
        self.main_config.chmod(0o600)

        result = installer.verify()
        self.assertTrue(result["ok"])
        self.assertTrue(result["hook_functional"])
        self.assertEqual(result["broken"], [])
        drift = [record for record in result["drift"] if record["config"] == os.fspath(self.main_config)]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["classification"], "reserialized")

    def test_verify_is_broken_when_the_owned_handler_is_removed(self) -> None:
        # The state the 2026-08-21 incident actually left behind, in its survivable form: the file
        # is present and valid, but our entry is gone. This MUST be `ok: false`, and it must say so
        # in words a human can act on -- distinguishably from either drift case above.
        installer.install()
        payload = json.loads(self.main_config.read_bytes())
        payload["hooks"]["UserPromptSubmit"] = [
            handler for handler in payload["hooks"]["UserPromptSubmit"] if not installer.owned_handler(handler)
        ]
        self.main_config.chmod(0o600)
        self.main_config.write_bytes((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())
        self.main_config.chmod(0o600)

        result = installer.verify()
        self.assertFalse(result["ok"])
        self.assertFalse(result["hook_functional"])
        self.assertEqual(len(result["broken"]), 1)
        self.assertEqual(result["broken"][0]["config"], os.fspath(self.main_config))
        self.assertEqual(result["broken"][0]["problems"][0]["kind"], "hook_missing")
        self.assertIn("NOT being redacted", result["summary"])

    def test_verify_is_broken_when_the_handler_points_at_another_release(self) -> None:
        # "The hook is running, but not this release" -- a stale handler left behind pointing at an
        # older release directory. Byte-comparison caught this only incidentally; it is now checked
        # on its own terms, against the handler's own self-declared arguments.
        installer.install()
        payload = json.loads(self.main_config.read_bytes())
        for handler in payload["hooks"]["UserPromptSubmit"]:
            if not installer.owned_handler(handler):
                continue
            for hook in handler["hooks"]:
                hook["command"] = hook["command"].replace(
                    "--expected-script-sha256 ", "--expected-script-sha256 " + "0" * 64 + " ignored-"
                )
        self.main_config.chmod(0o600)
        self.main_config.write_bytes((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())
        self.main_config.chmod(0o600)

        result = installer.verify()
        self.assertFalse(result["ok"])
        self.assertFalse(result["hook_functional"])
        self.assertEqual(result["broken"][0]["problems"][0]["kind"], "hook_points_elsewhere")

    def test_verify_checks_every_config_instead_of_aborting_on_the_first(self) -> None:
        # The abort was its own defect: one drifted account hid the state of every account after it,
        # so an operator could not tell whether the rest were fine or simply never looked at. Both
        # fixture configs are broken here; both must be named.
        installer.install()
        for config in (self.main_config, self.account_config):
            payload = json.loads(config.read_bytes())
            payload["hooks"]["UserPromptSubmit"] = [
                handler
                for handler in payload["hooks"]["UserPromptSubmit"]
                if not installer.owned_handler(handler)
            ]
            config.chmod(0o600)
            config.write_bytes((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())
            config.chmod(0o600)

        result = installer.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(
            sorted(record["config"] for record in result["broken"]),
            sorted(os.fspath(p) for p in (self.main_config, self.account_config)),
        )

    # ------------------------------------------------------------------------------ doctor
    def test_doctor_is_healthy_after_a_plain_install(self) -> None:
        installer.install()
        result = installer.doctor()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["findings"], [])
        self.assertIn("HEALTHY", result["summary"])

    def test_doctor_names_a_deleted_hooks_json_as_the_2026_08_21_incident_shape(self) -> None:
        # The exact production failure this whole blocker is about: a Codex app upgrade removed
        # `~/.codex/hooks.json` and the machine ran an unredacted hook for about a week. verify()
        # cannot be the check for this on its own -- it treats an absent managed path as a tolerated
        # "unreachable" (a deleted account), by deliberate design since round 3. doctor() is the
        # check that says it out loud.
        installer.install()
        self.main_config.unlink()
        result = installer.doctor()
        self.assertFalse(result["ok"])
        kinds = {finding["kind"] for finding in result["findings"]}
        self.assertIn("config_missing", kinds)
        finding = next(f for f in result["findings"] if f["kind"] == "config_missing")
        self.assertEqual(finding["target"], os.fspath(self.main_config))
        self.assertIn("2026-08-21", finding["detail"])
        self.assertIn("ATTENTION", result["summary"])

    def test_doctor_names_a_config_that_lost_its_redaction_entry(self) -> None:
        installer.install()
        payload = json.loads(self.main_config.read_bytes())
        payload["hooks"]["UserPromptSubmit"] = [
            handler for handler in payload["hooks"]["UserPromptSubmit"] if not installer.owned_handler(handler)
        ]
        self.main_config.chmod(0o600)
        self.main_config.write_bytes((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())
        self.main_config.chmod(0o600)
        result = installer.doctor()
        self.assertFalse(result["ok"])
        self.assertIn("hook_missing", {finding["kind"] for finding in result["findings"]})

    def test_doctor_reports_a_missing_receipt_instead_of_raising(self) -> None:
        # doctor() must answer even in the states verify() refuses to start in -- "there is no
        # receipt" is a finding, not an exception, or an unattended runner just sees a crash.
        result = installer.doctor()
        self.assertFalse(result["ok"])
        self.assertIn("receipt_unavailable", {finding["kind"] for finding in result["findings"]})

    def test_doctor_still_answers_the_only_question_that_matters_without_a_receipt(self) -> None:
        # With the receipt gone, doctor() can no longer say WHICH release should be wired -- but it
        # can still say whether a redaction handler exists at all, which is the difference between
        # "unredacted" and "redacted by something older".
        installer.install()
        (installer.RUNTIME_BASE / "latest-receipt.json").unlink()
        healthy = installer.doctor()
        self.assertIn("receipt_unavailable", {finding["kind"] for finding in healthy["findings"]})
        self.assertNotIn("hook_missing", {finding["kind"] for finding in healthy["findings"]})

        payload = json.loads(self.main_config.read_bytes())
        payload["hooks"]["UserPromptSubmit"] = [
            handler for handler in payload["hooks"]["UserPromptSubmit"] if not installer.owned_handler(handler)
        ]
        self.main_config.chmod(0o600)
        self.main_config.write_bytes((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())
        self.main_config.chmod(0o600)
        stripped = installer.doctor()
        self.assertIn("hook_missing", {finding["kind"] for finding in stripped["findings"]})

    def test_doctor_reports_a_tampered_script_the_live_handler_points_at(self) -> None:
        receipt = installer.install()
        installed_script = Path(receipt["release_dir"]) / "claude_memory_hook.py"
        installed_script.chmod(0o600)
        installed_script.write_bytes(b"# tampered\n")
        installed_script.chmod(0o600)
        result = installer.doctor()
        self.assertFalse(result["ok"])
        self.assertIn("script_digest_mismatch", {finding["kind"] for finding in result["findings"]})

    def test_doctor_flags_the_launchd_tcc_hazard_only_for_an_apple_platform_interpreter(self) -> None:
        # The Ego reaper on this machine was silently dead for ~2 weeks because of this, and every
        # path this tool manages is on /Volumes by design -- so a health check that could die the
        # same way must say so about itself. The condition is narrower than "external volume",
        # though: the measured denial (ego-reaper-launchd/README.md, real throwaway launchd job,
        # 2026-08-28) is specific to Apple platform binaries -- /opt/homebrew/bin/python3 reads the
        # same paths fine, which is why the dispatch reaper's own plist uses it.
        installer.install()
        external = os.fspath(installer.RUNTIME_BASE).startswith("/Volumes/")
        for executable, expected in (
            ("/usr/bin/python3", external),
            # `/usr/bin/python3` is a stub that re-execs into Xcode, so `sys.executable` reads as
            # the Xcode path and a naive `/usr/bin/` check reports the denied case as safe.
            ("/Applications/Xcode.app/Contents/Developer/usr/bin/python3", external),
            ("/opt/homebrew/bin/python3", False),
        ):
            with mock.patch.object(installer.sys, "executable", executable):
                result = installer.doctor()
            with self.subTest(executable=executable):
                self.assertEqual(result["launchd_tcc_risk"], expected)
                self.assertEqual(result["interpreter"], executable)
        self.assertIn("launchd", result["launchd_tcc_note"])

    def test_uninstall_restores_original_configs_exactly(self) -> None:
        before_main = self.main_config.read_bytes()
        before_account = self.account_config.read_bytes()
        before_mode = stat.S_IMODE(self.main_config.stat().st_mode)
        installer.install()
        result = installer.uninstall()
        self.assertTrue(result["ok"])
        self.assertEqual(self.main_config.read_bytes(), before_main)
        self.assertEqual(self.account_config.read_bytes(), before_account)
        self.assertEqual(stat.S_IMODE(self.main_config.stat().st_mode), before_mode)
        # The owned handler is gone -- back to exactly one handler.
        payload = json.loads(self.main_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_uninstall_refuses_when_config_changed_since_install(self) -> None:
        installer.install()
        payload = json.loads(self.main_config.read_bytes())
        payload["hooks"]["UserPromptSubmit"].append({"hooks": [{"type": "command", "command": "/bin/echo hi"}]})
        drifted = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        self.main_config.chmod(0o600)
        self.main_config.write_bytes(drifted)
        self.main_config.chmod(0o600)
        with self.assertRaises(installer.InstallError):
            installer.uninstall()
        # Refusing to uninstall must not have touched the drifted config.
        self.assertEqual(self.main_config.read_bytes(), drifted)

    def test_recover_pending_install_is_a_noop_when_nothing_pending(self) -> None:
        result = installer.recover_pending_install()
        self.assertEqual(result, {"ok": True, "state": "none"})
        installer.install()
        result = installer.recover_pending_install()
        self.assertEqual(result["state"], "none")

    def test_reinstall_does_not_poison_the_uninstall_baseline(self) -> None:
        # P1-1 (independent Claude opus5/max review, 2026-08-17, round 1):
        # a second install() used to capture the *already-installed*
        # on-disk content as the "before" baseline, so uninstall() would
        # "successfully" restore back to an installed state -- silently,
        # with every tool (verify/uninstall/recover/plan) reporting
        # ok:true while the bridge kept running. A same-release re-install
        # is an ordinary, designed-to-work idempotent operation (see
        # test_install_is_idempotent_replan_shows_no_change), so this must
        # not depend on anything unusual happening.
        pristine_main = self.main_config.read_bytes()
        pristine_account = self.account_config.read_bytes()
        installer.install()
        installer.install()  # same release_id -- idempotent re-install
        installer.uninstall()
        self.assertEqual(self.main_config.read_bytes(), pristine_main)
        self.assertEqual(self.account_config.read_bytes(), pristine_account)
        main_payload = json.loads(self.main_config.read_bytes())
        self.assertEqual(len(main_payload["hooks"]["UserPromptSubmit"]), 1)

    def test_upgrade_reinstall_does_not_poison_the_uninstall_baseline(self) -> None:
        # Same as above, but across an upgrade (a new claude_memory_hook.py
        # -- and therefore a new release_id/command) -- opus's report notes
        # this path is worse under the original bug: uninstall leaves a
        # handler pointing at a stale, previous release's command line
        # still executing on every Codex prompt.
        pristine_main = self.main_config.read_bytes()
        pristine_account = self.account_config.read_bytes()
        installer.install()
        self.source_script.chmod(0o644)
        self.source_script.write_bytes(b"#!/usr/bin/env python3\n# upgraded fixture hook script\n")
        self.source_script.chmod(0o644)
        installer.install()
        installer.uninstall()
        self.assertEqual(self.main_config.read_bytes(), pristine_main)
        self.assertEqual(self.account_config.read_bytes(), pristine_account)

    def test_interrupted_upgrade_install_recovers_to_the_previous_working_state(self) -> None:
        # R2-P1-A (independent Claude opus5/max review, 2026-08-17, round
        # 2): an interrupted UPGRADE install used to be classified as
        # unrecoverable "drift" -- the not-yet-rewritten config still held
        # the *previous* install's content, which matched neither the
        # redefined before_* (now true pristine) nor this transaction's
        # after_*. recover/verify/install/uninstall all refused forever,
        # with only `plan` still (misleadingly) reporting ok:true, and the
        # bridge stayed active in every config the whole time. No crash
        # needed to trigger it -- an ordinary write failure partway through
        # an upgrade was enough, which is what this test injects.
        #
        # This test originally injected the failure at a fixed atomic_write
        # call count (#7). On the exact round-3 candidate that count landed
        # on backup/<install_id>/receipt.json -- BEFORE the pending journal
        # was even written -- so the test never actually reached the
        # mixed-v1/v2-live-writes recovery branch it claimed to cover; it
        # passed for an unrelated reason (independent Codex sol/xhigh
        # review, 2026-08-17, round 3, P2-R3-TEST). Rewritten to be
        # path-and-phase-addressed instead of call-count-addressed: fail
        # exactly the second live config's write, and assert the pending
        # journal is already durable at that moment -- this is structurally
        # guaranteed to land inside the intended window regardless of how
        # many internal atomic_write calls precede it.
        v1_receipt = installer.install()
        v1_command = v1_receipt["command"]
        self.source_script.chmod(0o644)
        self.source_script.write_bytes(b"#!/usr/bin/env python3\n# upgraded fixture hook script\n")
        self.source_script.chmod(0o644)

        real_atomic_write = installer.atomic_write
        observed = {}

        def failing_atomic_write(path, raw, mode=0o600):
            if path == self.account_config:
                # Captured *inside* the injected failure, before install()'s
                # own except-block self-heal (see below) has a chance to run
                # -- this is the only point at which the genuine mid-
                # transaction mixed v1/v2 state is actually observable on
                # disk.
                observed["pending_durable"] = installer.PENDING_PATH.exists()
                observed["main_command_mid_transaction"] = json.loads(self.main_config.read_bytes())[
                    "hooks"
                ]["UserPromptSubmit"][1]["hooks"][0]["command"]
                raise installer.InstallError("simulated disk error")
            return real_atomic_write(path, raw, mode)

        with mock.patch.object(installer, "atomic_write", side_effect=failing_atomic_write):
            with self.assertRaises(installer.InstallError):
                installer.install()

        # Proves the injected failure genuinely landed inside the live-write
        # phase -- pending journal already durable, first live config
        # (main_config, discovered before account_config) already rewritten
        # to v2 -- not before the pending journal existed, which is exactly
        # what made the original fixed-call-count version of this test
        # vacuous (P2-R3-TEST).
        self.assertIn("pending_durable", observed, "the injected failure never fired")
        self.assertTrue(observed["pending_durable"])
        self.assertNotEqual(observed["main_command_mid_transaction"], v1_command)

        # install() self-heals synchronously: recover_pending_install() runs
        # inside install()'s own except block before it raises. So by the
        # time install() has actually returned control here, both configs
        # are already rolled back to PREV (v1, this transaction's own
        # starting point) -- not left mixed, and not over-rolled-back to
        # true pristine. Independently confirmed by Codex sol/xhigh's round
        # 3 report using its own path-addressed probe ("install 返回
        # ...; 三份配置 byte-exact 回到 v1 installed bytes").
        for config_path in (self.main_config, self.account_config):
            payload = json.loads(config_path.read_bytes())
            handlers = payload["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(handlers), 2)
            self.assertTrue(installer.owned_handler(handlers[1]))
            self.assertEqual(handlers[1]["hooks"][0]["command"], v1_command)

        # Every tool action must still work -- this is the crux of R2-P1-A:
        # before the fix, every one of these raised "config drifted" forever.
        self.assertTrue(installer.verify()["ok"])
        self.assertTrue(installer.plan()["ok"])
        # A subsequent explicit recover is a clean no-op: install() already
        # finished the rollback and cleared the pending journal itself.
        self.assertEqual(installer.recover_pending_install(), {"ok": True, "state": "none"})

        installer.uninstall()
        pristine_payload = json.loads(self.main_config.read_bytes())
        self.assertEqual(len(pristine_payload["hooks"]["UserPromptSubmit"]), 1)

    def test_install_carries_forward_a_temporarily_undiscovered_config_without_losing_baseline(self) -> None:
        # P1-R2-1 (independent Codex sol/xhigh review, 2026-08-17, round
        # 2): a config path covered by the latest receipt but not
        # discovered by *this* install call (e.g. a Codex account's
        # hooks.json briefly renamed away and back) used to be silently
        # treated as a brand-new path the next time it reappeared, using
        # its current -- already bridged -- content as the new "pristine"
        # baseline. uninstall() would then never revert it, while
        # reporting ok:true.
        #
        # Round 3's first fix (commit 870810d468) closed this by refusing
        # the whole install outright the instant any managed path went
        # undiscovered -- but round 3's own independent review found that
        # this reintroduces the exact "every action refuses forever except
        # plan" lockout signature for the far more common case: an account
        # permanently retired or re-provisioned under a new path, not a
        # temporary rename (independent Claude opus5/max review,
        # 2026-08-17, round 3, R3-P1-A; independent Codex sol/xhigh review,
        # 2026-08-17, round 3, P1-R3-1 -- both reproduced this
        # independently). The fix is to carry the undiscovered row forward
        # unchanged instead of refusing -- this test covers the temporary
        # case; test_permanently_retired_account_does_not_lock_out_other_
        # accounts below covers the permanent case both reviews demanded.
        # discover_hook_configs() itself requires at least 2 configs, so a
        # second account is needed here -- otherwise renaming the only
        # account's hooks.json away trips that pre-existing guard instead
        # of the one this test targets.
        second_account = self.local_homes / "codex-accounts/acct-two/home/hooks.json"
        second_account.parent.mkdir(parents=True)
        self._write(second_account, self._base_hooks_json())
        installer.install()
        missing = self.account_config.parent / "hooks.json.missing"
        self.account_config.rename(missing)
        try:
            receipt = installer.install()
            self.assertIn(os.fspath(self.account_config), {row["path"] for row in receipt["configs"]})
            self.assertTrue(installer.verify()["ok"])
        finally:
            missing.rename(self.account_config)
        # The account config's content was never touched while it was
        # undiscovered -- its row still points at the real original
        # baseline, so a normal uninstall now (with every path discoverable
        # again) must restore it exactly, byte-for-byte, back to pristine.
        installer.uninstall()
        payload = json.loads(self.account_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_permanently_retired_account_does_not_lock_out_other_accounts(self) -> None:
        # R3-P1-A / P1-R3-1 (independent Claude opus5/max review AND
        # independent Codex sol/xhigh review, 2026-08-17, round 3, found
        # separately): permanently retiring a receipt-managed account
        # (deleted, or re-provisioned under a fresh path -- indistinguishable
        # from deletion to this installer, and something the real account
        # tooling genuinely does) used to lock install/verify/uninstall
        # completely: install refused (missing path), verify/uninstall both
        # raised on the same missing path, recover was a no-op (state:none),
        # and only plan still reported ok:true. The still-existing accounts'
        # bridge handlers stayed active forever with no tool-level way to
        # remove them. Reproduces round 3's Codex report's exact repro
        # shape: retire one of several managed accounts, then drive every
        # tool action, then add a brand-new account.
        second_account = self.local_homes / "codex-accounts/acct-two/home/hooks.json"
        second_account.parent.mkdir(parents=True)
        self._write(second_account, self._base_hooks_json())
        installer.install()
        retired_dir = self.account_config.parent
        gone = self.local_homes / "codex-accounts/acct-one-DELETED-not-restored/home/hooks.json.gone"
        gone.parent.mkdir(parents=True)
        self.account_config.rename(gone)
        retired_dir.rmdir()
        # No `finally` restore -- this account is gone for good, unlike the
        # temporary-rename test above.

        # verify() must not raise: it reports the retired path as
        # unreachable and still checks the surviving accounts.
        verify_result = installer.verify()
        self.assertTrue(verify_result["ok"])
        self.assertIn(os.fspath(self.account_config), verify_result["unreachable"])
        self.assertIn(os.fspath(self.main_config), verify_result["configs"])
        self.assertIn(os.fspath(second_account), verify_result["configs"])

        # plan()/install() must keep working for the surviving accounts.
        self.assertTrue(installer.plan()["ok"])
        receipt = installer.install()
        self.assertIn(os.fspath(self.account_config), {row["path"] for row in receipt["configs"]})

        # A brand-new account must still be installable while the retired
        # one remains carried forward.
        fresh_account = self.local_homes / "codex-accounts/acct-three/home/hooks.json"
        fresh_account.parent.mkdir(parents=True)
        self._write(fresh_account, self._base_hooks_json())
        receipt = installer.install()
        self.assertIn(os.fspath(fresh_account), {row["path"] for row in receipt["configs"]})

        # uninstall() must not raise either: it restores every surviving,
        # currently-existing account to pristine and reports the retired
        # path as unreachable rather than refusing the whole transaction.
        uninstall_result = installer.uninstall()
        self.assertTrue(uninstall_result["ok"])
        self.assertIn(os.fspath(self.account_config), uninstall_result["unreachable"])
        for surviving in (self.main_config, second_account, fresh_account):
            payload = json.loads(surviving.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_uninstall_survives_a_pruned_prior_install_backup_directory(self) -> None:
        # R3-P2-A (independent Claude opus5/max review, 2026-08-17, round
        # 3): a receipt row's `prev_backup`/`after_backup` can point into a
        # *previous* install transaction's backup directory (this file's
        # own comments document backup directories as never pruned, but
        # nothing enforces that on disk -- an operator or disk-cleanup tool
        # could still remove an old one). uninstall() never reads
        # prev_backup or after_backup at all -- only `backup`, the
        # permanent pristine baseline -- so it must not fail just because
        # a stale, unrelated prior-transaction backup directory is gone.
        v1_receipt = installer.install()
        self.source_script.chmod(0o644)
        self.source_script.write_bytes(b"#!/usr/bin/env python3\n# upgraded fixture hook script\n")
        self.source_script.chmod(0o644)
        installer.install()  # v2 (upgrade) -- its receipt's prev_backup fields point into v1's backup dir
        v1_backup_dir = self.runtime_base / "backups" / v1_receipt["install_id"]
        self.assertTrue(v1_backup_dir.is_dir())
        for entry in v1_backup_dir.iterdir():
            entry.chmod(0o600)
            entry.unlink()
        v1_backup_dir.chmod(0o700)
        v1_backup_dir.rmdir()

        # uninstall() must still succeed and restore every config exactly
        # to true pristine -- it never needed the pruned v1 backup dir.
        result = installer.uninstall()
        self.assertTrue(result["ok"])
        for config_path in (self.main_config, self.account_config):
            payload = json.loads(config_path.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_verify_and_uninstall_fail_closed_on_an_unreadable_managed_config(self) -> None:
        # R4-P1-A (independent Claude opus5/max review, 2026-08-17, round
        # 4): the round-4 "absent" check used bare Path.exists()/
        # Path.is_symlink(), which silently swallow ANY stat() failure --
        # not just ENOENT (genuinely deleted). A managed config that is
        # merely unreadable right now (EACCES from a parent directory that
        # lost +x; EIO from a flaky external disk) was misclassified as
        # "absent" and treated as nothing-to-do. On interpreters where
        # Path.exists() swallows PermissionError, uninstall() reported
        # ok:true, deleted latest-receipt.json, and left the bridge handler
        # permanently active with the true baseline lost -- P1-1 (round 1's
        # headline finding) resurrected through the new "absent" path. The
        # config here is never deleted or renamed -- only its parent
        # directory's permissions are tightened -- so this must never be
        # treated as "absent"; it must fail closed with a clean
        # InstallError, exactly like every other unreadable-path failure
        # this file already handles.
        installer.install()
        unreadable_dir = self.account_config.parent
        original_mode = stat.S_IMODE(unreadable_dir.stat().st_mode)
        os.chmod(unreadable_dir, 0o000)
        self.addCleanup(lambda: unreadable_dir.exists() and os.chmod(unreadable_dir, original_mode))

        with self.assertRaises(installer.InstallError):
            installer.verify()
        with self.assertRaises(installer.InstallError):
            installer.uninstall()

        # The receipt must survive the refusal -- unlike a genuinely absent
        # row, a merely-unreachable one must not have latest-receipt.json
        # deleted out from under it.
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())

        os.chmod(unreadable_dir, original_mode)
        # Once the transient condition clears, every action must work
        # exactly as if nothing had happened -- full recovery, not a
        # permanent scar.
        self.assertTrue(installer.verify()["ok"])
        installer.uninstall()
        payload = json.loads(self.account_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_install_survives_a_carried_forward_rows_pruned_backup_directory(self) -> None:
        # R4-P2-A (independent Claude opus5/max review, 2026-08-17, round
        # 4): a carried-forward row (see install()'s carried_forward_rows
        # comment) is copied verbatim into every new receipt without its
        # `backup` field ever being re-copied into the current
        # transaction's own backup directory -- so it can keep pointing at
        # an *older* transaction's directory indefinitely. Before this
        # fix, `_receipt_rows()` eagerly read and digest-checked every
        # row's `backup` unconditionally, so a carried-forward row's pruned
        # backup broke install()'s own internal commit-finalizing
        # recover_pending_install() call -- wedging the pending journal
        # with no tool-level recovery (worse than R3-P2-A: that one only
        # broke uninstall(), this one blocked every action).
        second_account = self.local_homes / "codex-accounts/acct-two/home/hooks.json"
        second_account.parent.mkdir(parents=True)
        self._write(second_account, self._base_hooks_json())
        installer.install()  # v1: main + account_config + second_account
        v1_receipt = json.loads((self.runtime_base / "latest-receipt.json").read_bytes())
        v1_backup_dir = self.runtime_base / "backups" / v1_receipt["install_id"]

        missing = self.account_config.parent / "hooks.json.missing"
        self.account_config.rename(missing)
        self.addCleanup(lambda: missing.exists() and missing.rename(self.account_config))
        installer.install()  # v2: account_config carried forward, backup still -> v1_backup_dir

        for entry in v1_backup_dir.iterdir():
            entry.chmod(0o600)
            entry.unlink()
        v1_backup_dir.chmod(0o700)
        v1_backup_dir.rmdir()

        # A further install (still without account_config discoverable)
        # carries the same row forward again. Its own internal recovery
        # check must not need account_config's now-pruned v1 backup.
        receipt = installer.install()
        self.assertIn(os.fspath(self.account_config), {row["path"] for row in receipt["configs"]})

        # uninstall() must also succeed, reporting the still-undiscovered
        # account as unreachable rather than failing over its pruned
        # backup, and restore the surviving configs exactly to pristine.
        result = installer.uninstall()
        self.assertTrue(result["ok"])
        self.assertIn(os.fspath(self.account_config), result["unreachable"])
        for surviving in (self.main_config, second_account):
            payload = json.loads(surviving.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_uninstall_leaves_no_pending_journal_when_a_live_rows_backup_is_unloadable(self) -> None:
        # R5-P1-A (independent Claude opus5/max review, 2026-08-17, round
        # 5): making `backup` lazy per-row (the round-4 fix for R4-P2-A)
        # silently removed a precondition uninstall() had relied on since
        # round 4: _receipt_rows() used to read and digest-check every
        # row's `backup` *before* uninstall() wrote its durable journal, so
        # an unloadable backup was always a clean, nothing-happened
        # refusal. Once that read moved inside the write loop, the same
        # input instead surfaced *after* the journal was already durable
        # and after earlier rows had already been reverted -- a half-
        # uninstalled system with a pending journal that recover_pending_
        # install() itself could not clear (it hits the identical
        # unloadable backup), leaving every tool action refusing forever
        # if the backup was permanently gone. This is the negative
        # invariant opus's report calls out as missing: a failed uninstall
        # must leave no pending journal behind, and must not have reverted
        # anything, when the trigger was a single live row's own current
        # backup being gone -- not a carried-forward row's stale one
        # (that's R4-P2-A, already covered above).
        second_account = self.local_homes / "codex-accounts/acct-two/home/hooks.json"
        second_account.parent.mkdir(parents=True)
        self._write(second_account, self._base_hooks_json())
        installer.install()
        receipt = json.loads((self.runtime_base / "latest-receipt.json").read_bytes())
        backup_dir = self.runtime_base / "backups" / receipt["install_id"]
        self.assertTrue(backup_dir.is_dir())
        for entry in backup_dir.iterdir():
            entry.chmod(0o600)
            entry.unlink()
        backup_dir.chmod(0o700)
        backup_dir.rmdir()

        with self.assertRaises(installer.InstallError):
            installer.uninstall()

        # Nothing must have become durable, and nothing must have been
        # reverted -- a clean, pre-transaction refusal, exactly round 4's
        # behavior for the same input.
        self.assertFalse(installer.PENDING_PATH.exists())
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        for config_path in (self.main_config, self.account_config, second_account):
            payload = json.loads(config_path.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)

        # Every other action must still work normally -- this is not a
        # lockout, it is a refusal over data that is genuinely gone.
        self.assertTrue(installer.verify()["ok"])
        self.assertTrue(installer.plan()["ok"])
        self.assertEqual(installer.recover_pending_install(), {"ok": True, "state": "none"})

    def test_install_refuses_before_writing_anything_when_a_carried_forward_row_is_indeterminate(self) -> None:
        # R5-P2-A (independent Claude opus5/max review, 2026-08-17, round
        # 5): discover_hook_configs() can silently drop an account from
        # discovery on ANY stat() failure it treats as "not found" -- its
        # own instance of the same class of bug R4-P1-A/R5-P1-A fixed
        # elsewhere in this file (tracked separately as R5-P3-A / R3-P3-B /
        # R6-P2-B; the underlying unguarded `candidate.is_file()` call was
        # itself fixed in round 7 -- see _enumerate_hook_configs()'s own
        # comment -- as a byproduct of uninstall()'s new R7-P1-A safety
        # scan needing it to fail closed instead of crashing). Whatever the
        # reason a path goes undiscovered, before this fix install()'s
        # carried-forward row for it was only classified at the very end,
        # inside its own commit-finalizing recover_pending_install() call
        # -- by which point every discovered config and
        # latest-receipt.json had already been written. This test
        # exercises the actual fixed code path (the pre-flight loop over
        # carried_forward_rows in install()) directly and deterministically
        # via mocking, independent of *why* a path went undiscovered,
        # rather than relying on the specific (now also fixed) discovery
        # failure mode.
        second_account = self.local_homes / "codex-accounts/acct-two/home/hooks.json"
        second_account.parent.mkdir(parents=True)
        self._write(second_account, self._base_hooks_json())
        installer.install()  # main + account_config + second_account, all bridged

        unreadable_dir = self.account_config.parent
        original_mode = stat.S_IMODE(unreadable_dir.stat().st_mode)
        os.chmod(unreadable_dir, 0o000)
        self.addCleanup(lambda: unreadable_dir.exists() and os.chmod(unreadable_dir, original_mode))

        with mock.patch.object(
            installer,
            "discover_hook_configs",
            return_value=[self.main_config, second_account],
        ):
            with self.assertRaises(installer.InstallError):
                installer.install()

        # Nothing must have become durable, and -- crucially, unlike round
        # 5's bug -- neither discovered config was rewritten either: the
        # refusal happened before any live write, not after every
        # discovered config was already bridged.
        self.assertFalse(installer.PENDING_PATH.exists())
        for config_path in (self.main_config, second_account):
            payload = json.loads(config_path.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)

        os.chmod(unreadable_dir, original_mode)
        self.assertTrue(installer.verify()["ok"])
        installer.uninstall()
        payload = json.loads(self.account_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_install_refuses_to_adopt_an_already_bridged_config_as_pristine_after_a_rename(self) -> None:
        # R6-P1-A (independent Claude opus5/max review, 2026-08-17, round
        # 6): a pooled account directory renamed or moved between two
        # installs, with its hooks.json content untouched, resolves to a
        # path string with no entry in the previous receipt --
        # previous_rows_by_path.get(new_path) is None, exactly as if it
        # were a genuinely brand-new account. Before this fix, install()
        # adopted that already-bridged content as its own "pristine"
        # baseline; uninstall() then compared against that
        # self-referential baseline, reported ok:true, and left the bridge
        # handler live and undetectable forever, with latest-receipt.json
        # deleted in the same operation -- round 1's P1-1 outcome,
        # reproduced on every prior revision of this file with nothing
        # more than a single directory rename (no crash, race, privilege,
        # or mock), through a door P1-1's own fix never covered.
        installer.install()
        account_dir = self.account_config.parent.parent  # codex-accounts/acct-one
        renamed_dir = account_dir.parent / "acct-one-renamed"
        account_dir.rename(renamed_dir)
        renamed_config = renamed_dir / "home" / "hooks.json"
        self.assertTrue(renamed_config.is_file())

        with self.assertRaises(installer.InstallError) as ctx:
            installer.install()
        self.assertIn("already-bridged", str(ctx.exception))

        # Nothing must have become durable, and the renamed config's
        # content must be untouched -- a clean, pre-transaction refusal,
        # not a partial adoption.
        self.assertFalse(installer.PENDING_PATH.exists())
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        payload = json.loads(renamed_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)

        # Renaming it back to its original path must let a normal install
        # and uninstall proceed exactly as before -- the carried-forward
        # machinery for a genuinely *temporary* rename is untouched by
        # this fix.
        renamed_dir.rename(account_dir)
        installer.install()
        installer.uninstall()
        payload = json.loads(self.account_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_uninstall_refuses_when_a_discovered_config_carries_an_untracked_owned_handler(self) -> None:
        # R7-P1-A / R7-P1-B (independent Claude opus5/max review AND
        # independent Codex sol/xhigh review, 2026-08-17, round 7, found
        # separately): round 6's install()-side fix for R6-P1-A only
        # closed the *adoption* door -- calling uninstall() directly while
        # an account directory is still renamed was never guarded at all,
        # and round 6's own error message recommended exactly that as the
        # remediation ("...run uninstall first"). Following it made
        # uninstall() revert whatever it could see (the old, now-absent
        # path), delete the only receipt as a normal successful commit,
        # and report ok:true -- while the relocated config kept executing
        # the bridge with no record left anywhere that it existed (R6-P1-A's
        # exact stated harm, never actually closed by round 6's fix, just
        # moved one door over). Worse, once the receipt was gone, renaming
        # the directory back did not help either: install() now refused to
        # adopt the still-bridged content (round 6's own fix, correctly),
        # and no other action could recover it -- every action refused
        # forever except plan, a brand-new permanent lockout opus's report
        # rates worse than the original bug.
        installer.install()
        account_dir = self.account_config.parent.parent
        renamed_dir = account_dir.parent / "acct-one-renamed"
        account_dir.rename(renamed_dir)
        self.addCleanup(lambda: renamed_dir.exists() and renamed_dir.rename(account_dir))
        renamed_config = renamed_dir / "home" / "hooks.json"
        receipt_before = (self.runtime_base / "latest-receipt.json").read_bytes()

        # Following the tool's own advice ("run uninstall first") while the
        # path is still renamed must now refuse cleanly, not report
        # ok:true.
        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("does not track", str(ctx.exception))

        # Nothing must have become durable or changed: the receipt survives
        # byte-exact, no pending journal, and the relocated config's
        # content is completely untouched -- the always-safe remediation
        # (restore the path to where the receipt expects it) remains
        # available.
        self.assertFalse(installer.PENDING_PATH.exists())
        self.assertEqual((self.runtime_base / "latest-receipt.json").read_bytes(), receipt_before)
        payload = json.loads(renamed_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)

        # No permanent lockout: renaming the directory back to where the
        # receipt expects it lets a completely normal uninstall proceed
        # and restore every config byte+mode exact.
        renamed_dir.rename(account_dir)
        installer.uninstall()
        payload = json.loads(self.account_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_uninstall_catches_an_account_relocated_outside_the_one_level_discovery_shape(self) -> None:
        # R8-P1-B (independent Claude opus5/max review, 2026-08-17, round
        # 8): the round-7 scan only ever looked at
        # codex-accounts/<one-level>/home/hooks.json -- exactly the shape
        # ordinary discovery understands. An account moved into a
        # subfolder, moved out of codex-accounts entirely, or with its own
        # home/ subdirectory renamed was invisible to it, so uninstall()
        # still reported ok:true, deleted the receipt, and left the
        # relocated config live and untracked -- R7-P1-A's exact harm,
        # reached through a search shape narrower than the harm it was
        # meant to guard.
        installer.install()
        account_dir = self.account_config.parent.parent
        archive_dir = self.local_homes / "codex-accounts/archive"
        archive_dir.mkdir()
        relocated_dir = archive_dir / "acct-one"
        account_dir.rename(relocated_dir)
        self.addCleanup(lambda: relocated_dir.exists() and relocated_dir.rename(account_dir))
        relocated_config = relocated_dir / "home/hooks.json"
        receipt_before = (self.runtime_base / "latest-receipt.json").read_bytes()

        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("does not track", str(ctx.exception))
        self.assertFalse(installer.PENDING_PATH.exists())
        self.assertEqual((self.runtime_base / "latest-receipt.json").read_bytes(), receipt_before)
        payload = json.loads(relocated_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)

        relocated_dir.rename(account_dir)
        installer.uninstall()
        payload = json.loads(self.account_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_uninstall_catches_a_relocated_account_saved_with_a_utf8_bom(self) -> None:
        # Round-10-vs-round-11 encoding-bypass finding (self-check Workflow,
        # 2026-08-18, escalating an independent Claude opus5/max round-10
        # unit-level finding to a full end-to-end chain): the round-10 scan's
        # _contains_owned_handler() called strict_json() (canonical UTF-8
        # only) and treated ANY decode/parse failure as "not one of ours".
        # A relocated, still-live account's hooks.json re-saved with a
        # leading UTF-8 BOM -- something several editors/export tools add
        # by default, not a contrived adversarial encoding -- decode-failed
        # strict_json() and was silently waved through: install() returned
        # a normal receipt, verify() reported ok:true, and uninstall()
        # reported ok:true while the orphan's real, byte-identical,
        # hash-verifiable handler stayed live with no receipt referencing
        # it again. Fails against commit 6fb376c671 (round 10): uninstall()
        # there returns ok:true instead of raising.
        installer.install()
        account_dir = self.account_config.parent.parent
        archive_dir = self.local_homes / "codex-accounts/archive"
        archive_dir.mkdir()
        relocated_dir = archive_dir / "acct-one"
        account_dir.rename(relocated_dir)
        self.addCleanup(lambda: relocated_dir.exists() and relocated_dir.rename(account_dir))
        relocated_config = relocated_dir / "home/hooks.json"

        live_command_bytes = relocated_config.read_bytes()
        self.assertIn(f"--bridge-id {installer.BRIDGE_ID}".encode(), live_command_bytes)
        relocated_config.write_bytes(b"\xef\xbb\xbf" + live_command_bytes)

        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("does not track", str(ctx.exception))
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        self.assertFalse(installer.PENDING_PATH.exists())
        self.assertTrue(relocated_config.read_bytes().endswith(live_command_bytes))

        relocated_config.write_bytes(live_command_bytes)
        relocated_dir.rename(account_dir)
        installer.uninstall()

    def test_uninstall_catches_an_account_relocated_into_the_bridges_own_runtime_tree(self) -> None:
        # Round-10-vs-round-11 RUNTIME_BASE-prune finding (self-check
        # Workflow, 2026-08-18, on top of a one-sentence, never-reproduced
        # round-9 mention): the round-10 scan unconditionally pruned its own
        # RUNTIME_BASE subtree from the walk on the theory that the bridge's
        # own backup files are never named literally "hooks.json" -- true
        # for what the bridge itself writes, but it says nothing about a
        # THIRD PARTY (an operator, a restore/migration script) relocating
        # an already-live, already-bridged account directory BY HAND into
        # that same tree. A plain rename plus a plain uninstall() call was
        # enough to make the relocated account permanently invisible. Fails
        # against commit 6fb376c671 (round 10): uninstall() there returns
        # ok:true instead of raising.
        installer.install()
        account_dir = self.account_config.parent.parent
        staged_dir = self.runtime_base / "backups/misc-staging/acct-one-relocated"
        staged_dir.parent.mkdir(parents=True)
        account_dir.rename(staged_dir)
        self.addCleanup(lambda: staged_dir.exists() and staged_dir.rename(account_dir))
        relocated_config = staged_dir / "home/hooks.json"
        self.assertTrue(relocated_config.exists())

        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("does not track", str(ctx.exception))
        self.assertIn(os.fspath(relocated_config), str(ctx.exception))
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        self.assertFalse(installer.PENDING_PATH.exists())

        staged_dir.rename(account_dir)
        installer.uninstall()

    def test_uninstall_still_fails_closed_on_a_marker_bearing_file_inside_its_own_runtime_tree(self) -> None:
        # Round-12 correction (2026-08-18, independent Claude opus5/max
        # review, round 11, R11-P1-A): round 11 tolerated ANY unparseable-
        # but-marker-suspicious file under RUNTIME_BASE, on the theory that
        # every real file the bridge itself writes there is never named
        # literally "hooks.json" so the ambiguity was assumed low-value.
        # That reasoning conflated two different situations: a genuine
        # ABSENCE of evidence (still tolerated -- see the sibling
        # oversized/unreadable tests below) and POSITIVE evidence (a raw
        # match on this tool's own --bridge-id marker) that the content
        # merely couldn't be structurally verified. Tolerating the latter
        # let a live, valid, plain-UTF-8, merely-oversized relocated handler
        # under RUNTIME_BASE be silently abandoned -- exactly the failure
        # mode this whole round's fix exists to close -- so a Stage-2 marker
        # hit is no longer tolerated anywhere, in or out of RUNTIME_BASE.
        installer.install()
        stray_dir = self.runtime_base / "backups/misc-staging/stray"
        stray_dir.mkdir(parents=True)
        stray_config = stray_dir / "hooks.json"
        # Not valid JSON under any supported encoding, but contains the
        # literal marker text as incidental byte padding -- Stage 2 fires on
        # this and must refuse, not silently pass, even inside RUNTIME_BASE.
        stray_config.write_bytes(
            b"not valid json padding " * 3000 + f"--bridge-id {installer.BRIDGE_ID}".encode() + b" more padding"
        )
        self.assertLess(stray_config.stat().st_size, installer.MAX_MANAGED_FILE_BYTES)

        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("cannot rule out", str(ctx.exception))
        self.assertIn(os.fspath(stray_config), str(ctx.exception))
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        self.assertFalse(installer.PENDING_PATH.exists())

    def test_uninstall_catches_a_large_valid_relocated_handler_inside_its_own_runtime_tree(self) -> None:
        # Round-12 fix (2026-08-18, independent Claude opus5/max review,
        # round 11, R11-P1-B): a live, owned, relocated config inside
        # RUNTIME_BASE was silently abandoned whenever it exceeded
        # _STRUCTURAL_DETECTION_MAX_BYTES (65536 bytes) -- padding an
        # otherwise perfectly ordinary, plain, canonical-JSON hooks.json
        # (no encoding trick, no malformation) past that bound made
        # uninstall() return ok:true and delete latest-receipt.json while
        # the orphan handler stayed executable on disk. _contains_owned_
        # handler() is now called with MAX_MANAGED_FILE_BYTES (not the
        # smaller default) as its structural-detection bound for RUNTIME_BASE
        # candidates specifically, so a genuine relocated handler of this
        # size gets a definitive, structurally-verified True instead of
        # falling through to the ambiguous marker-only stage at all.
        installer.install()
        account_dir = self.account_config.parent.parent
        staged_dir = self.runtime_base / "backups/misc-staging/padded-account"
        staged_dir.parent.mkdir(parents=True, exist_ok=True)
        account_dir.rename(staged_dir)
        self.addCleanup(lambda: staged_dir.exists() and staged_dir.rename(account_dir))
        relocated_config = staged_dir / "home/hooks.json"
        payload = json.loads(relocated_config.read_bytes())
        # An ordinary, plausible padding shape (extra operator-notes data),
        # not a contrived encoding trick -- stays plain, valid, canonical
        # JSON with the owned handler untouched.
        payload["_operator_notes"] = ["padding"] * 20_000
        padded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        self.assertGreater(len(padded), 65_536)
        self.assertLess(len(padded), installer.MAX_MANAGED_FILE_BYTES)
        relocated_config.write_bytes(padded)

        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("does not track", str(ctx.exception))
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        self.assertFalse(installer.PENDING_PATH.exists())
        self.assertEqual(relocated_config.read_bytes(), padded)

        del payload["_operator_notes"]
        relocated_config.write_bytes(installer.canonical_json(payload))
        staged_dir.rename(account_dir)
        installer.uninstall()

    def test_uninstall_catches_an_oversized_valid_relocated_handler_inside_its_own_runtime_tree(self) -> None:
        # Round-13 fix (2026-08-19, independent Claude opus5/max AND Codex
        # sol/gpt-5.6-terra round-12 reviews -- both found this, disagreeing
        # only on severity label, P2 vs P1; per the standing rule any
        # reproducible P1 from either path blocks regardless of the other
        # reviewer's grade): round 12's raised structural-detection bound
        # (MAX_MANAGED_FILE_BYTES for RUNTIME_BASE candidates) only helps up
        # to that same cap -- a relocated handler padded past
        # MAX_MANAGED_FILE_BYTES itself makes _read_for_detection() refuse
        # ("too large"), and that refusal used to be blanket-tolerated under
        # RUNTIME_BASE as absence-of-evidence, silently abandoning a live,
        # valid, plain-UTF-8 owned handler exactly the way R11-P1-B did.
        # _read_for_detection()'s "too large" raise is now a distinct
        # exception type routed to a bounded streaming marker scan instead
        # (_stream_scan_oversized_for_bridge_marker()), so a genuine handler
        # of ANY size still gets caught.
        installer.install()
        account_dir = self.account_config.parent.parent
        staged_dir = self.runtime_base / "backups/misc-staging/oversized-account"
        staged_dir.parent.mkdir(parents=True, exist_ok=True)
        account_dir.rename(staged_dir)
        self.addCleanup(lambda: staged_dir.exists() and staged_dir.rename(account_dir))
        relocated_config = staged_dir / "home/hooks.json"
        payload = json.loads(relocated_config.read_bytes())
        payload["_operator_notes"] = "x" * (installer.MAX_MANAGED_FILE_BYTES + 16_384)
        padded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        self.assertGreater(len(padded), installer.MAX_MANAGED_FILE_BYTES)
        relocated_config.write_bytes(padded)

        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("cannot rule out", str(ctx.exception))
        self.assertIn(os.fspath(relocated_config), str(ctx.exception))
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        self.assertFalse(installer.PENDING_PATH.exists())
        self.assertEqual(relocated_config.read_bytes(), padded)

        del payload["_operator_notes"]
        relocated_config.write_bytes(installer.canonical_json(payload))
        staged_dir.rename(account_dir)
        installer.uninstall()

    def test_uninstall_tolerates_a_file_that_is_both_oversized_and_unreadable_inside_its_own_runtime_tree(self) -> None:
        # Round-14 fix (2026-08-19, independent Claude opus5/max review,
        # round 13, R13-P2-A): the two existing tolerance tests each cover
        # one half of this shape (a 33-byte unreadable file; a readable
        # MAX+1-byte file) but never their conjunction. A candidate that is
        # BOTH oversized AND unreadable under RUNTIME_BASE -- e.g. a
        # root-owned quarantine copy left by `sudo cp` in the never-pruned
        # backups/ tree -- routed into _stream_scan_oversized_for_bridge_
        # marker()'s own os.open()/os.read() calls, which then failed with
        # EACCES; that failure was escalating unconditionally instead of
        # being tolerated as the genuine absence-of-evidence it is,
        # regressing the exact invariant round 12 (and this file's own
        # long-standing comment) already established -- and hard-blocked
        # uninstall()/install()/recover_pending_install(), leaving a pending
        # journal when hit through install().
        installer.install()
        stray_dir = self.runtime_base / "backups/misc-staging/oversized-and-unreadable"
        stray_dir.mkdir(parents=True)
        stray_config = stray_dir / "hooks.json"
        stray_config.write_bytes(b"\x00" * (installer.MAX_MANAGED_FILE_BYTES + 1))
        os.chmod(stray_config, 0o000)
        self.addCleanup(lambda: stray_config.exists() and os.chmod(stray_config, 0o600))

        result = installer.uninstall()
        self.assertTrue(result["ok"])
        self.assertFalse(installer.PENDING_PATH.exists())

    def test_find_untracked_owned_configs_detects_an_oversized_non_default_bridge_id_handler_when_matching_bridge_id_is_passed(
        self,
    ) -> None:
        # Both independent reviewers' exact repro (2026-08-20), at the level of the real caller
        # (_find_untracked_owned_configs()) rather than just the leaf scanner it delegates to:
        # _stream_scan_oversized_for_bridge_marker() used to hardcode BRIDGE_ID with no `bridge_id`
        # parameter at all, and its sole caller here did not accept or forward one either -- so an
        # oversized (> MAX_MANAGED_FILE_BYTES) hooks.json under RUNTIME_BASE carrying a real
        # SessionEnd handler under write_candidate_capture.MODULE_ID (a non-default bridge_id) was
        # silently invisible to this whole safety scan no matter what was passed, while the SAME
        # content under this module's own default BRIDGE_ID was already correctly found (round-13/
        # 14 fixes). This means a future wiring step that threads a non-default bridge_id into this
        # function would have the untracked-owned-handler scan work for hooks.json files at or
        # under the size cap but go silently blind for larger ones -- reopening the exact defect
        # round 13 already closed for the default identity.
        other_bridge_id = "orca-claude-codex-memory-write-trigger-v1"  # write_candidate_capture.MODULE_ID
        handler_command = f"/usr/bin/python3 write_candidate_capture.py scan --bridge-id {other_bridge_id}"
        payload = {
            "hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": handler_command}]}]},
            "_operator_notes": "x" * (installer.MAX_MANAGED_FILE_BYTES + 16_384),
        }
        padded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        self.assertGreater(len(padded), installer.MAX_MANAGED_FILE_BYTES)

        stray_dir = self.runtime_base / "backups/misc-staging/other-bridge-id-account"
        stray_dir.mkdir(parents=True)
        stray_config = stray_dir / "hooks.json"
        stray_config.write_bytes(padded)

        # Default-identity behavior (the real, only-ever-exercised call shape today --
        # install()/uninstall()/recover_pending_install() never pass bridge_id) is completely
        # unaffected: this content was never claimed to be owned by BRIDGE_ID, so it must still
        # find nothing here, exactly as it did before this fix.
        self.assertEqual(installer._find_untracked_owned_configs(set()), [])

        # The matching bridge_id now catches it -- positive evidence, fails closed with the
        # candidate's path in the error, same as the default-identity oversized case.
        with self.assertRaises(installer.InstallError) as ctx:
            installer._find_untracked_owned_configs(set(), bridge_id=other_bridge_id)
        self.assertIn("cannot rule out", str(ctx.exception))
        self.assertIn(os.fspath(stray_config), str(ctx.exception))

        # A third, still-different bridge_id must not match either.
        self.assertEqual(installer._find_untracked_owned_configs(set(), bridge_id="some-unrelated-id"), [])

    def test_find_untracked_owned_configs_detects_a_normal_sized_non_default_event_handler_when_matching_event_is_passed(
        self,
    ) -> None:
        # The inverted, reintroduced version of the gap the test above closes -- same shape,
        # `event` instead of `bridge_id` (2026-08-20). _find_untracked_owned_configs() had no
        # `event` parameter at all, so its call to _contains_owned_handler() always checked the
        # candidate's DEFAULT_HOOK_EVENT ("UserPromptSubmit") handler list, never whatever event
        # a real non-default handler actually lived under. Unlike the bridge_id gap, this one
        # was reachable at ANY file size -- no oversized-file requirement -- because it broke
        # Stage 1 (_attempt_structural_detection(), the whole-file-read path), not just the
        # oversized-streaming fallback: a NORMAL-sized hooks.json (well under
        # MAX_MANAGED_FILE_BYTES) containing both an existing UserPromptSubmit handler and a
        # real SessionEnd handler under a non-default bridge_id was silently invisible to this
        # scan no matter what bridge_id was passed, while the exact same content over
        # MAX_MANAGED_FILE_BYTES was already correctly found by the event-agnostic streaming
        # scanner (the sibling test above) -- the inversion the reviewer found.
        other_bridge_id = "orca-claude-codex-memory-write-trigger-v1"  # write_candidate_capture.MODULE_ID
        handler_command = f"/usr/bin/python3 write_candidate_capture.py scan --bridge-id {other_bridge_id}"
        payload = {
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": handler_command}]}],
            }
        }
        raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        self.assertLess(len(raw), installer.MAX_MANAGED_FILE_BYTES)  # normal-sized, not oversized

        stray_dir = self.local_homes / "codex-accounts/other-acct/home"
        stray_dir.mkdir(parents=True)
        stray_config = stray_dir / "hooks.json"
        stray_config.write_bytes(raw)

        # Default-identity, default-event behavior (the real, only-ever-exercised call shape
        # today -- install()/uninstall()/recover_pending_install() never pass either override)
        # is completely unaffected: this content's UserPromptSubmit handler is not this
        # bridge's own, so it must still find nothing here, exactly as before this fix.
        self.assertEqual(installer._find_untracked_owned_configs(set()), [])

        # bridge_id alone, with no event override, still misses it -- it checks the matching
        # bridge_id against the WRONG event's handler list (UserPromptSubmit, which carries no
        # marker for this bridge at all). This pins down that bridge_id threading (the prior
        # round's fix) is not, by itself, sufficient without event threading too.
        self.assertEqual(installer._find_untracked_owned_configs(set(), bridge_id=other_bridge_id), [])

        # The matching bridge_id AND matching event now correctly finds it. Unlike the
        # oversized-file sibling test above (which only ever reaches Stage 2's ambiguous
        # marker-only raise), a normal-sized file's SessionEnd handler is structurally
        # verified by Stage 1 (_attempt_structural_detection()) and returned as a definitive,
        # positively-identified match in the result list -- closing the specific inversion the
        # reviewer found: this now detects correctly, matching (not contradicting) what the
        # oversized-file path already did correctly before this fix.
        found = installer._find_untracked_owned_configs(set(), bridge_id=other_bridge_id, event="SessionEnd")
        self.assertEqual(found, [os.fspath(stray_config)])

        # A third, unrelated bridge_id under the correct event must still not match.
        self.assertEqual(
            installer._find_untracked_owned_configs(set(), bridge_id="some-unrelated-id", event="SessionEnd"),
            [],
        )

    def test_uninstall_still_fails_closed_on_an_oversized_file_outside_runtime_base_even_with_a_marker(self) -> None:
        # Sibling control for the fix above: the round-13 streaming-scan
        # fallback is deliberately scoped to RUNTIME_BASE only.  Outside it,
        # an oversized file must stay exactly as fail-closed as any other
        # inspection failure there, regardless of whether it happens to
        # contain the bridge marker -- this scan has no special reason to
        # trust a genuinely oversized third-party hooks.json the way it
        # trusts RUNTIME_BASE's own never-writes-hooks.json invariant.
        installer.install()
        stray_dir = self.local_homes / "codex-accounts/oversized-tool-cache"
        stray_dir.mkdir()
        stray_config = stray_dir / "hooks.json"
        marker = f"--bridge-id {installer.BRIDGE_ID}".encode()
        stray_config.write_bytes(b"not json " * 500_000 + marker + b" more junk")
        self.assertGreater(stray_config.stat().st_size, installer.MAX_MANAGED_FILE_BYTES)

        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("too large", str(ctx.exception))
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        self.assertFalse(installer.PENDING_PATH.exists())

    def test_uninstall_tolerates_an_oversized_file_inside_its_own_runtime_tree(self) -> None:
        # Round-11 self-check (2026-08-18, final-check pressure test,
        # independently confirmed by two separate agents): an earlier
        # version of the tolerance fix above only wrapped
        # _contains_owned_handler()'s own raise -- _read_for_detection()'s
        # raise for a file over MAX_MANAGED_FILE_BYTES sits one call
        # earlier in the same loop body and was left unwrapped, so an
        # oversized (but otherwise ordinary) "hooks.json"-named file under
        # RUNTIME_BASE -- e.g. a legitimate large backup, or debris from an
        # imperfect restore tool -- still permanently locked out
        # uninstall()/recover_pending_install()/install(), exactly the
        # failure mode the RUNTIME_BASE tolerance exists to close.
        installer.install()
        stray_dir = self.runtime_base / "backups/misc-staging/oversized"
        stray_dir.mkdir(parents=True)
        stray_config = stray_dir / "hooks.json"
        stray_config.write_bytes(b"0" * (installer.MAX_MANAGED_FILE_BYTES + 1))

        result = installer.uninstall()
        self.assertTrue(result["ok"])
        self.assertFalse(installer.PENDING_PATH.exists())

    def test_uninstall_tolerates_an_unreadable_file_inside_its_own_runtime_tree(self) -> None:
        # Sibling to the oversized-file tolerance test above, same
        # unwrapped-_read_for_detection() gap, different trigger: a
        # "hooks.json"-named file under RUNTIME_BASE this process cannot
        # read at all (a UID mismatch after moving the external SSD
        # between machines is this repo's own stated example scenario).
        installer.install()
        stray_dir = self.runtime_base / "backups/misc-staging/unreadable"
        stray_dir.mkdir(parents=True)
        stray_config = stray_dir / "hooks.json"
        stray_config.write_bytes(b'{"hooks":{"UserPromptSubmit":[]}}\n')
        os.chmod(stray_config, 0o000)
        self.addCleanup(lambda: stray_config.exists() and os.chmod(stray_config, 0o600))

        result = installer.uninstall()
        self.assertTrue(result["ok"])
        self.assertFalse(installer.PENDING_PATH.exists())

    def test_uninstall_tolerates_an_unsearchable_directory_inside_its_own_runtime_tree(self) -> None:
        # Round-12 fix (2026-08-18, independent Claude opus5/max review,
        # round 11, R11-P2-A): the RUNTIME_BASE tolerance wrapped
        # _read_for_detection()/_contains_owned_handler() but not
        # _is_regular_file(), which sat outside every try block in the loop.
        # A directory that is readable (os.walk()'s own scandir() succeeds,
        # so the onerror callback never fires) but not searchable -- mode
        # 0o400 or 0o600, missing the execute bit -- makes the later
        # stat(<dir>/hooks.json) call inside _is_regular_file() fail with
        # EACCES, which used to escape this loop entirely unwrapped and
        # hard-block uninstall()/recover_pending_install()/install().
        installer.install()
        stray_dir = self.runtime_base / "backups/misc-staging/unsearchable"
        stray_dir.mkdir(parents=True)
        (stray_dir / "hooks.json").write_bytes(b'{"hooks":{"UserPromptSubmit":[]}}\n')
        os.chmod(stray_dir, 0o400)
        self.addCleanup(lambda: stray_dir.exists() and os.chmod(stray_dir, 0o700))

        result = installer.uninstall()
        self.assertTrue(result["ok"])
        self.assertFalse(installer.PENDING_PATH.exists())

    def test_uninstall_still_fails_closed_on_an_ambiguous_file_outside_runtime_base(self) -> None:
        # Sibling control for the tolerance test above: the same
        # unparseable-but-marker-suspicious content, placed OUTSIDE
        # RUNTIME_BASE (an ordinary location under local-homes this scan
        # has no special reason to trust), must still fail closed rather
        # than silently pass -- and the error must name the offending path,
        # so an operator hitting this rarer, legitimate ambiguity can
        # actually find and act on it (round-11 self-check, 2026-08-18,
        # round-2 pressure-test finding: an earlier version of this fix
        # omitted the path from this specific error, unlike the sibling
        # "cannot list" EACCES error which already includes exc.filename).
        installer.install()
        stray_dir = self.local_homes / "codex-accounts/stray-tool-cache"
        stray_dir.mkdir()
        stray_config = stray_dir / "hooks.json"
        stray_config.write_bytes(
            b"not valid json padding " * 3000 + f"--bridge-id {installer.BRIDGE_ID}".encode() + b" more padding"
        )
        self.assertGreater(stray_config.stat().st_size, 65_536)  # past the structural-detection size bound

        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("cannot rule out", str(ctx.exception))
        self.assertIn(os.fspath(stray_config), str(ctx.exception))
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        self.assertFalse(installer.PENDING_PATH.exists())

    def test_uninstall_still_works_when_the_codex_home_itself_is_unreachable(self) -> None:
        # R8-P1-C (independent Claude opus5/max review, 2026-08-17, round
        # 8): the round-7/8 scan's own enumeration required the Codex home
        # and accounts root to both exist (must_exist=True), so a receipt
        # row becoming unreachable for a completely unrelated reason -- the
        # whole .codex directory genuinely deleted, content and all, not
        # renamed elsewhere -- made the safety scan abort uninstall()
        # entirely instead of letting the existing, already-validated
        # per-row absent/unreachable handling (R3-P1-A) do its normal job
        # for the accounts that are still there. (A rename that preserves
        # content is the R8-P1-B scenario covered above, and the scan is
        # *supposed* to catch that one.)
        second_account = self.local_homes / "codex-accounts/acct-two/home/hooks.json"
        second_account.parent.mkdir(parents=True)
        self._write(second_account, self._base_hooks_json())
        installer.install()

        self.main_config.chmod(0o600)
        self.main_config.unlink()
        self.main_config.parent.rmdir()

        result = installer.uninstall()
        self.assertTrue(result["ok"])
        self.assertIn(os.fspath(self.main_config), result["unreachable"])
        for surviving in (self.account_config, second_account):
            payload = json.loads(surviving.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_uninstall_fails_closed_when_an_unrelated_directory_becomes_unlistable(self) -> None:
        # R8-P2-B (independent Claude opus5/max review, 2026-08-17, round
        # 8): os.walk()'s own default silently skips any directory it
        # cannot list, regardless of *why* -- exactly the fail-open
        # swallowing this file's stat-guard pattern exists to avoid
        # everywhere else, and exactly what made the round-7 `is_file()`
        # guard dead code on Python 3.13+ (its own `is_file()` call
        # already swallows EACCES there, so the guard's `except OSError`
        # never fires). An unrelated directory under local-homes becoming
        # unlistable (a permissions-repair pass, a mid-move race) must
        # make the safety scan -- and therefore uninstall() -- refuse, not
        # silently report ok:true having never actually looked inside it.
        installer.install()
        blocked_dir = self.local_homes / "codex-accounts/blocked-dir"
        blocked_dir.mkdir()
        os.chmod(blocked_dir, 0o000)
        self.addCleanup(lambda: blocked_dir.exists() and os.chmod(blocked_dir, 0o700))

        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("cannot list", str(ctx.exception))
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        self.assertFalse(installer.PENDING_PATH.exists())

        os.chmod(blocked_dir, 0o700)
        blocked_dir.rmdir()
        installer.uninstall()

    def test_uninstall_refuses_a_relocated_account_even_after_its_mode_or_owner_looks_unusual(self) -> None:
        # R9-P1-B (independent Claude opus5/max review, 2026-08-17, round
        # 9): the round-8/9 scan's own `except InstallError: continue`
        # around reading an untracked candidate reused
        # validate_owned_file()'s write-safety checks (uid match, not
        # group/other-writable, size cap) to decide "does this look like
        # one of our handlers" -- the wrong question. A relocated,
        # still-live account that the scan correctly refuses stops being
        # refused the instant its mode picks up a stray write bit (the
        # kind of thing `cp`/`rsync`/an archive restore under a permissive
        # umask does routinely): validate_owned_file() then raises
        # "unsafe file ownership or mode", the scan's guard swallows that
        # as "not one of ours", and uninstall() reports ok:true with the
        # handler still live and the receipt gone.
        installer.install()
        account_dir = self.account_config.parent.parent
        renamed_dir = self.local_homes / "codex-accounts/acct-one-relocated"
        account_dir.rename(renamed_dir)
        self.addCleanup(lambda: renamed_dir.exists() and renamed_dir.rename(account_dir))
        renamed_config = renamed_dir / "home/hooks.json"

        # Control: the plain relocation is refused, exactly as the other
        # relocation tests in this file already establish.
        with self.assertRaises(installer.InstallError):
            installer.uninstall()

        # A single mode change that a routine copy/restore could produce
        # -- still fully readable, still fully live -- must not flip the
        # outcome to a silent success.
        os.chmod(renamed_config, 0o664)
        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("does not track", str(ctx.exception))
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())
        self.assertFalse(installer.PENDING_PATH.exists())
        payload = json.loads(renamed_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)

        # Restore the installed mode (0o600, not the pristine 0o644) --
        # anything else would trip the pre-existing, unrelated drift check
        # once the path is back under receipt tracking.
        os.chmod(renamed_config, 0o600)
        renamed_dir.rename(account_dir)
        installer.uninstall()
        payload = json.loads(self.account_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)

    def test_uninstall_ignores_untracked_configs_with_malformed_or_unexpected_content(self) -> None:
        # R8-P1-D / R8-P2-A (independent Claude opus5/max review,
        # 2026-08-17, round 8): the round-8 safety scan's own payload
        # parsing re-implemented the `payload.get("hooks", {}).get(...)`
        # idiom without a structure check on `hooks` itself, so an
        # untracked config shaped like {"hooks": []}/{"hooks": null}/
        # {"hooks": "x"} made uninstall() die with a bare AttributeError
        # traceback and empty stdout (R8-P1-D); and strict_json() sat
        # outside the scan's own except-and-skip guard, so an untracked
        # config with malformed JSON (not even valid enough to parse) made
        # uninstall() refuse with an undiagnosable message naming no path
        # (R8-P2-A). Neither shape is one of this tool's own configs --
        # everything this tool writes is well-formed JSON with exactly the
        # expected structure -- so both must be silently skipped, not
        # crash and not block a legitimate uninstall.
        #
        # R9-P3-C (independent Claude opus5/max review, 2026-08-17, round
        # 9): this test's payloads sit outside codex-accounts/<name>/home/
        # -- a location round 8's own (one-level-deep) scan never looked
        # at, so this exact test passed unchanged on the round-8 baseline
        # and did not actually prove the crash/block bugs were fixed at
        # the shape that mattered. It is still worth keeping as broad
        # content-shape coverage; the companion test right below places a
        # single variant at the one-level shape specifically to be
        # non-vacuous against round 8.
        installer.install()
        # Deliberately outside codex-accounts/<name>/home/ -- ordinary
        # discovery/install() must never touch this path; only the
        # broader safety-scan walk should ever look at it.
        untracked_dir = self.local_homes / "orphaned-config"
        untracked_dir.mkdir()
        untracked_config = untracked_dir / "hooks.json"
        for content in (
            b'{"hooks": []}',
            b'{"hooks": null}',
            b'{"hooks": "x"}',
            b"not json at all",
            b"",
        ):
            self._write(untracked_config, content)
            result = installer.uninstall()
            self.assertTrue(result["ok"])
            payload = json.loads(self.main_config.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)
            installer.install()  # re-bridge for the next content variant
        untracked_config.unlink()
        installer.uninstall()

    def test_uninstall_ignores_an_untracked_config_with_malformed_content_at_the_one_level_shape(self) -> None:
        # R9-P3-C companion (independent Claude opus5/max review,
        # 2026-08-17, round 9): places malformed content exactly where
        # round 8's own scan looked (codex-accounts/<name>/home/
        # hooks.json) rather than outside it, so this test genuinely
        # distinguishes the round-8 baseline (bare AttributeError crash,
        # R8-P1-D) from the round-9 fix. install() runs *before* this
        # untracked account exists, so ordinary discovery never touches
        # its malformed content.
        installer.install()
        untracked_dir = self.local_homes / "codex-accounts/acct-untracked/home"
        untracked_dir.mkdir(parents=True)
        self._write(untracked_dir / "hooks.json", b'{"hooks": []}')

        result = installer.uninstall()
        self.assertTrue(result["ok"])

    def test_verify_and_uninstall_report_not_installed_after_uninstall(self) -> None:
        # P2-4 (independent Claude opus5/max review, 2026-08-17, round 1):
        # latest-receipt.json used to never be cleared by uninstall() at
        # all, so this state was unreachable through the real tool.
        installer.install()
        installer.uninstall()
        with self.assertRaises(installer.InstallError) as ctx:
            installer.verify()
        self.assertIn("not installed", str(ctx.exception))
        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("not installed", str(ctx.exception))

    def test_atomic_write_wraps_bare_oserror_in_install_error(self) -> None:
        # P2-1 (independent Claude opus5/max review, 2026-08-17, round 1):
        # a bare OSError used to propagate past main()'s
        # `except InstallError` as a raw Python traceback.
        unwritable_dir = Path(self.temp) / "unwritable"
        unwritable_dir.mkdir(mode=0o500)
        self.addCleanup(unwritable_dir.chmod, 0o700)
        target = unwritable_dir / "sub" / "file.json"
        with self.assertRaises(installer.InstallError):
            installer.atomic_write(target, b"{}\n", 0o600)

    def test_main_install_succeeds_end_to_end_when_shared_runtime_does_not_yet_exist(self) -> None:
        # R2-P1-B (independent Claude opus5/max review, 2026-08-17, round
        # 2): `_acquire_exclusive_lock()`'s first version relied on
        # `Path.mkdir(parents=True)`, which does not apply `mode` to
        # intermediate directories it creates -- so on a machine where
        # `.shared-runtime` does not exist yet (confirmed to be the real
        # state of the actual target machine at review time -- this is
        # what the very first real `install` on a fresh machine hits), it
        # was created at the default umask mode (0o755, not 0o700),
        # ensure_private_dir() then permanently refused it, and nothing
        # ever chmods it back. This drives the real, unmocked main()
        # end-to-end (not install() called directly, which every other
        # test in this class does and which never exercises the lock at
        # all) via a real argv, on a fixture where setUp() deliberately
        # never creates `.shared-runtime` -- the exact fresh-machine shape
        # this bug needed to reproduce.
        self.assertFalse(self.runtime_base.exists())
        argv = ["install_bridge.py", "install"]
        with mock.patch.object(sys, "argv", argv):
            exit_code = installer.main()
        self.assertEqual(exit_code, 0)
        self.assertEqual(stat.S_IMODE(self.runtime_base.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.runtime_base.stat().st_mode), 0o700)
        payload = json.loads(self.main_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)
        self.assertTrue(installer.owned_handler(payload["hooks"]["UserPromptSubmit"][1]))
        # A second real `main()` install (idempotent re-install) and a
        # real `main()` uninstall must also both succeed end to end.
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(installer.main(), 0)
        with mock.patch.object(sys, "argv", ["install_bridge.py", "uninstall"]):
            self.assertEqual(installer.main(), 0)
        restored = json.loads(self.main_config.read_bytes())
        self.assertEqual(len(restored["hooks"]["UserPromptSubmit"]), 1)

    def test_concurrent_installer_invocations_fail_the_lock_instead_of_interleaving(self) -> None:
        # P2-3 (independent Claude opus5/max review, 2026-08-17, round 1):
        # without this lock, a second concurrent `install`/`uninstall`/
        # `recover` invocation's own internal recover_pending_install()
        # call could consume the first invocation's still-in-flight
        # pending journal, producing a report/disk-state mismatch. Two
        # separate os.open() calls in the same process still get distinct
        # open file descriptions, so this reliably exercises real flock()
        # contention without needing a second process.
        lock_descriptor = installer._acquire_exclusive_lock()
        try:
            with self.assertRaises(installer.InstallError):
                installer._acquire_exclusive_lock()
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
        # Released -- a fresh acquisition now succeeds.
        lock_descriptor = installer._acquire_exclusive_lock()
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    def test_corrupted_receipt_row_raises_install_error_not_a_bare_exception(self) -> None:
        # P2-2 (independent Claude opus5/max review, 2026-08-17, round 1):
        # a receipt row missing a required field used to raise a bare
        # KeyError/TypeError out of read_receipt()'s callers.
        #
        # This deletes the top-level "release_id" field specifically
        # (independent Claude opus5/max review, 2026-08-17, round 2,
        # R2-P3): an earlier version of this test deleted
        # configs[0]["path"] instead, which round 1's own inline check in
        # verify() (`if not isinstance(row.get("path"), str): raise ...`)
        # already caught *before* this test's P2-2 fix was even added --
        # so it passed against the pre-P2-2 baseline too and did not
        # actually exercise _validate_receipt_shape() at all.
        # "release_id" is read unguarded at verify()'s
        # `receipt["release_id"]` and round 1's shape validator did not
        # cover it (that gap was round 2's R2-P2-B, fixed alongside this
        # test).
        installer.install()
        latest_path = self.runtime_base / "latest-receipt.json"
        receipt = json.loads(latest_path.read_bytes())
        del receipt["release_id"]
        latest_path.chmod(0o600)
        latest_path.write_bytes(installer.canonical_json(receipt))
        latest_path.chmod(0o600)
        with self.assertRaises(installer.InstallError):
            installer.verify()


class UninstallCrashRecoveryTests(unittest.TestCase):
    # Real SIGKILL, not a simulated in-process exception -- P1-2 was
    # specifically about what happens when the *process itself* dies
    # mid-uninstall, which a Python try/except cannot observe (independent
    # Claude opus5/max review, 2026-08-17, round 1: exp3/exp4 in that
    # review used the same real-SIGKILL methodology and it is what
    # surfaced this class of bug in the first place -- an in-process
    # exception test would not have).

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.temp = self._stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.temp).resolve()
        self.ssd_root = self.root / "ssd"
        self.local_homes = self.ssd_root / "Orca/local-homes"
        self.runtime_base = self.local_homes / ".shared-runtime/claude-codex-memory-bridge"
        self.pending_path = self.runtime_base / "pending-install.json"
        source_dir = self.ssd_root / "source"
        self.source_script = source_dir / "claude_memory_hook.py"

        self.local_homes.mkdir(parents=True)
        (self.local_homes / ".codex").mkdir()
        (self.local_homes / ".claude/projects").mkdir(parents=True)
        (self.local_homes / "codex-accounts/acct-one/home").mkdir(parents=True)
        source_dir.mkdir()

        self.main_config = self.local_homes / ".codex/hooks.json"
        self.account_config = self.local_homes / "codex-accounts/acct-one/home/hooks.json"
        InstallEndToEndTests._write(self.main_config, InstallEndToEndTests._base_hooks_json())
        InstallEndToEndTests._write(self.account_config, InstallEndToEndTests._base_hooks_json())
        InstallEndToEndTests._write(self.source_script, b"#!/usr/bin/env python3\n# fixture hook script\n")

        self._stack.enter_context(mock.patch.object(installer, "SSD_ROOT", self.ssd_root))
        self._stack.enter_context(mock.patch.object(installer, "LOCAL_HOMES_ROOT", self.local_homes))
        self._stack.enter_context(mock.patch.object(installer, "RUNTIME_BASE", self.runtime_base))
        self._stack.enter_context(mock.patch.object(installer, "PENDING_PATH", self.pending_path))
        self._stack.enter_context(mock.patch.object(installer, "SOURCE_SCRIPT", self.source_script))
        self._stack.enter_context(
            mock.patch.object(
                installer, "volume_uuid", lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
            )
        )
        self._stack.enter_context(mock.patch.object(Path, "home", lambda: self.local_homes))

        installer.install()  # reach a real, fully installed state in-process first

    def _run_killed_child(self, kill_after_atomic_write_calls: int) -> subprocess.CompletedProcess:
        module_dir = str(Path(installer.__file__).resolve().parent)
        script = f"""
import os, signal, sys
sys.path.insert(0, {module_dir!r})
from pathlib import Path
import install_bridge as installer

installer.SSD_ROOT = Path({str(self.ssd_root)!r})
installer.LOCAL_HOMES_ROOT = Path({str(self.local_homes)!r})
installer.RUNTIME_BASE = Path({str(self.runtime_base)!r})
installer.PENDING_PATH = Path({str(self.pending_path)!r})
installer.SOURCE_SCRIPT = Path({str(self.source_script)!r})
installer.volume_uuid = lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
Path.home = classmethod(lambda cls: Path({str(self.local_homes)!r}))

real_atomic_write = installer.atomic_write
calls = {{"n": 0}}
def killing_atomic_write(path, raw, mode=0o600):
    calls["n"] += 1
    if calls["n"] == {kill_after_atomic_write_calls}:
        os.kill(os.getpid(), signal.SIGKILL)
    return real_atomic_write(path, raw, mode)
installer.atomic_write = killing_atomic_write

installer.uninstall()
"""
        script_path = Path(self.temp) / "child.py"
        script_path.write_text(script)
        return subprocess.run([sys.executable, os.fspath(script_path)])

    def test_sigkill_mid_uninstall_is_fully_completed_by_recovery(self) -> None:
        # kill_after=3: call #1 writes the pending journal, call #2 reverts
        # the first config, call #3 (reverting the second config) is where
        # the process dies -- a real "one account reverted, one still
        # installed" mixed state on disk, with no in-process exception ever
        # running.
        result = self._run_killed_child(kill_after_atomic_write_calls=3)
        self.assertEqual(result.returncode, -signal.SIGKILL, "child must have been SIGKILLed, not exited normally")

        pristine_main = self.local_homes / ".codex/hooks.json"
        pristine_account = self.local_homes / "codex-accounts/acct-one/home/hooks.json"
        pending_path = self.runtime_base / "pending-install.json"
        self.assertTrue(pending_path.exists(), "a durable journal must survive the kill")

        with (
            mock.patch.object(installer, "SSD_ROOT", self.ssd_root),
            mock.patch.object(installer, "LOCAL_HOMES_ROOT", self.local_homes),
            mock.patch.object(installer, "RUNTIME_BASE", self.runtime_base),
            mock.patch.object(installer, "PENDING_PATH", pending_path),
            mock.patch.object(installer, "SOURCE_SCRIPT", self.source_script),
            mock.patch.object(
                installer, "volume_uuid", lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
            ),
            mock.patch.object(Path, "home", lambda: self.local_homes),
        ):
            outcome = installer.recover_pending_install()
            self.assertEqual(outcome["state"], "uninstalled")
            self.assertFalse(pending_path.exists())
            with self.assertRaises(installer.InstallError):
                installer.verify()

        main_payload = json.loads(pristine_main.read_bytes())
        account_payload = json.loads(pristine_account.read_bytes())
        self.assertEqual(len(main_payload["hooks"]["UserPromptSubmit"]), 1)
        self.assertEqual(len(account_payload["hooks"]["UserPromptSubmit"]), 1)

    def test_sigkill_mid_uninstall_then_relocating_the_unreverted_account_refuses_instead_of_deleting_the_receipt(
        self,
    ) -> None:
        # R8-P1-A (independent Claude opus5/max review, 2026-08-17, round
        # 8): a real SIGKILL mid-uninstall, followed by relocating the
        # account the interrupted attempt had not yet reached, followed by
        # an ordinary recovery attempt, used to delete the receipt (a
        # normal, successful "finish the interrupted uninstall" commit)
        # while the relocated config -- never touched by any of this --
        # stayed live and became completely untracked, with every
        # subsequent action refusing forever (round 7's exact permanent
        # lockout, reached through the one commit path --
        # recover_pending_install()'s own finishing pass -- that round
        # 7/8's original uninstall()-only scan never guarded).
        result = self._run_killed_child(kill_after_atomic_write_calls=3)
        self.assertEqual(result.returncode, -signal.SIGKILL, "child must have been SIGKILLed, not exited normally")

        pending_path = self.runtime_base / "pending-install.json"
        self.assertTrue(pending_path.exists(), "a durable journal must survive the kill")
        latest_path = self.runtime_base / "latest-receipt.json"
        receipt_before = latest_path.read_bytes()

        # Relocate the account the interrupted attempt never reached (still
        # bridged at the moment of the kill).
        account_dir = self.local_homes / "codex-accounts/acct-one"
        renamed_dir = self.local_homes / "codex-accounts/acct-one-relocated"
        account_dir.rename(renamed_dir)
        renamed_config = renamed_dir / "home/hooks.json"

        with (
            mock.patch.object(installer, "SSD_ROOT", self.ssd_root),
            mock.patch.object(installer, "LOCAL_HOMES_ROOT", self.local_homes),
            mock.patch.object(installer, "RUNTIME_BASE", self.runtime_base),
            mock.patch.object(installer, "PENDING_PATH", pending_path),
            mock.patch.object(installer, "SOURCE_SCRIPT", self.source_script),
            mock.patch.object(
                installer, "volume_uuid", lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
            ),
            mock.patch.object(Path, "home", lambda: self.local_homes),
        ):
            with self.assertRaises(installer.InstallError) as ctx:
                installer.recover_pending_install()
            self.assertIn("does not track", str(ctx.exception))

            # The receipt must survive -- this is the crux of R8-P1-A:
            # before the fix, this exact recovery attempt deleted it.
            self.assertTrue(latest_path.exists())
            self.assertEqual(latest_path.read_bytes(), receipt_before)
            self.assertTrue(pending_path.exists())
            payload = json.loads(renamed_config.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)

            # No permanent lockout: moving it back lets recovery finish
            # normally, exactly as if the relocation never happened.
            renamed_dir.rename(account_dir)
            outcome = installer.recover_pending_install()
            self.assertEqual(outcome["state"], "uninstalled")
            self.assertFalse(pending_path.exists())
            payload = json.loads(self.account_config.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)


class InstallCrashRecoveryTests(unittest.TestCase):
    # Mirrors UninstallCrashRecoveryTests's real-SIGKILL methodology, but
    # for the "kind == install" rollback branch specifically -- R9-P1-A
    # needs a genuinely fresh, never-installed machine, so it cannot share
    # UninstallCrashRecoveryTests's setUp(), which installs before any
    # test body runs.

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.temp = self._stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.temp).resolve()
        self.ssd_root = self.root / "ssd"
        self.local_homes = self.ssd_root / "Orca/local-homes"
        self.runtime_base = self.local_homes / ".shared-runtime/claude-codex-memory-bridge"
        self.pending_path = self.runtime_base / "pending-install.json"
        source_dir = self.ssd_root / "source"
        self.source_script = source_dir / "claude_memory_hook.py"

        self.local_homes.mkdir(parents=True)
        (self.local_homes / ".codex").mkdir()
        (self.local_homes / ".claude/projects").mkdir(parents=True)
        (self.local_homes / "codex-accounts/acct-one/home").mkdir(parents=True)
        source_dir.mkdir()

        self.main_config = self.local_homes / ".codex/hooks.json"
        self.account_config = self.local_homes / "codex-accounts/acct-one/home/hooks.json"
        InstallEndToEndTests._write(self.main_config, InstallEndToEndTests._base_hooks_json())
        InstallEndToEndTests._write(self.account_config, InstallEndToEndTests._base_hooks_json())
        InstallEndToEndTests._write(self.source_script, b"#!/usr/bin/env python3\n# fixture hook script\n")

        self._stack.enter_context(mock.patch.object(installer, "SSD_ROOT", self.ssd_root))
        self._stack.enter_context(mock.patch.object(installer, "LOCAL_HOMES_ROOT", self.local_homes))
        self._stack.enter_context(mock.patch.object(installer, "RUNTIME_BASE", self.runtime_base))
        self._stack.enter_context(mock.patch.object(installer, "PENDING_PATH", self.pending_path))
        self._stack.enter_context(mock.patch.object(installer, "SOURCE_SCRIPT", self.source_script))
        self._stack.enter_context(
            mock.patch.object(
                installer, "volume_uuid", lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
            )
        )
        self._stack.enter_context(mock.patch.object(Path, "home", lambda: self.local_homes))
        # Deliberately NOT installed yet -- R9-P1-A needs a fresh machine.

    def _run_killed_child(self, kill_after_atomic_write_calls: int) -> subprocess.CompletedProcess:
        module_dir = str(Path(installer.__file__).resolve().parent)
        script = f"""
import os, signal, sys
sys.path.insert(0, {module_dir!r})
from pathlib import Path
import install_bridge as installer

installer.SSD_ROOT = Path({str(self.ssd_root)!r})
installer.LOCAL_HOMES_ROOT = Path({str(self.local_homes)!r})
installer.RUNTIME_BASE = Path({str(self.runtime_base)!r})
installer.PENDING_PATH = Path({str(self.pending_path)!r})
installer.SOURCE_SCRIPT = Path({str(self.source_script)!r})
installer.volume_uuid = lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
Path.home = classmethod(lambda cls: Path({str(self.local_homes)!r}))

real_atomic_write = installer.atomic_write
calls = {{"n": 0}}
def killing_atomic_write(path, raw, mode=0o600):
    calls["n"] += 1
    if calls["n"] == {kill_after_atomic_write_calls}:
        os.kill(os.getpid(), signal.SIGKILL)
    return real_atomic_write(path, raw, mode)
installer.atomic_write = killing_atomic_write

installer.install()
"""
        script_path = Path(self.temp) / "child.py"
        script_path.write_text(script)
        return subprocess.run([sys.executable, os.fspath(script_path)])

    def test_sigkill_mid_install_then_relocating_the_bridged_account_refuses_instead_of_finishing_rollback(
        self,
    ) -> None:
        # R9-P1-A (independent Claude opus5/max review, 2026-08-17, round
        # 9): recover_pending_install()'s "kind == install" rollback
        # branch had no untracked-owned-handler scan at all (round 9 only
        # added one to the "kind == uninstall" branch). A real SIGKILL
        # during a first-ever install, after both live configs were
        # written but before latest-receipt.json, followed by relocating
        # one of the just-bridged accounts, followed by an ordinary
        # recovery attempt, used to report {"ok": true, "state":
        # "rolled_back"}, delete the pending journal, and abandon the
        # relocated account's live handler with NO receipt ever having
        # existed to reveal it -- strictly worse than R8-P1-A, since
        # there is not even a receipt left behind to suggest something is
        # wrong.
        #
        # Real write order for this 2-config fixture (measured, not
        # guessed): #1-2 release files, #3-6 per-config backups, #7
        # receipt.json, #8 the pending journal, #9-#10 the two live
        # configs, #11 latest-receipt.json. Killing at #11 leaves both
        # live configs bridged but the install not yet committed.
        result = self._run_killed_child(kill_after_atomic_write_calls=11)
        self.assertEqual(result.returncode, -signal.SIGKILL, "child must have been SIGKILLed, not exited normally")

        self.assertTrue(self.pending_path.exists(), "a durable journal must survive the kill")
        latest_path = self.runtime_base / "latest-receipt.json"
        self.assertFalse(latest_path.exists(), "a first-ever install must not have committed yet")

        account_dir = self.account_config.parent.parent
        renamed_dir = self.local_homes / "codex-accounts/acct-one-relocated"
        account_dir.rename(renamed_dir)
        renamed_config = renamed_dir / "home/hooks.json"

        with (
            mock.patch.object(installer, "SSD_ROOT", self.ssd_root),
            mock.patch.object(installer, "LOCAL_HOMES_ROOT", self.local_homes),
            mock.patch.object(installer, "RUNTIME_BASE", self.runtime_base),
            mock.patch.object(installer, "PENDING_PATH", self.pending_path),
            mock.patch.object(installer, "SOURCE_SCRIPT", self.source_script),
            mock.patch.object(
                installer, "volume_uuid", lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
            ),
            mock.patch.object(Path, "home", lambda: self.local_homes),
        ):
            with self.assertRaises(installer.InstallError) as ctx:
                installer.recover_pending_install()
            self.assertIn("does not track", str(ctx.exception))

            # Nothing must have finished: the journal survives, no receipt
            # was written, and the relocated config is untouched.
            self.assertTrue(self.pending_path.exists())
            self.assertFalse(latest_path.exists())
            payload = json.loads(renamed_config.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)

            # No permanent lockout: moving it back lets the rollback
            # finish normally.
            renamed_dir.rename(account_dir)
            outcome = installer.recover_pending_install()
            self.assertEqual(outcome["state"], "rolled_back")
            self.assertFalse(self.pending_path.exists())
            payload = json.loads(self.account_config.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 1)


class InstallWriteTriggerEndToEndTests(unittest.TestCase):
    # Full transactional cycle for the `install-write-trigger` CLI action
    # (installer.install_write_trigger()): dry-run, install, non-disturbance of the existing
    # UserPromptSubmit handler, idempotent re-install, verify (including both-scripts hash-
    # pinning), fail-closed-when-disabled, uninstall (rollback), and a genuine injected-mid-
    # transaction-failure recovery -- through the same isolated fake-SSD fixture pattern
    # InstallEndToEndTests already uses, extended with a second fixture source file for
    # write_candidate_capture.py.

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.temp = self._stack.enter_context(tempfile.TemporaryDirectory())
        root = Path(self.temp).resolve()
        ssd_root = root / "ssd"
        local_homes = ssd_root / "Orca/local-homes"
        runtime_base = local_homes / ".shared-runtime/claude-codex-memory-bridge"
        pending_path = runtime_base / "pending-install.json"
        source_dir = ssd_root / "source"
        source_script = source_dir / "claude_memory_hook.py"
        write_trigger_source_script = source_dir / "write_candidate_capture.py"

        local_homes.mkdir(parents=True)
        (local_homes / ".codex").mkdir()
        (local_homes / ".claude/projects").mkdir(parents=True)
        (local_homes / "codex-accounts/acct-one/home").mkdir(parents=True)
        source_dir.mkdir()

        InstallEndToEndTests._write(local_homes / ".codex/hooks.json", InstallEndToEndTests._base_hooks_json())
        InstallEndToEndTests._write(
            local_homes / "codex-accounts/acct-one/home/hooks.json", InstallEndToEndTests._base_hooks_json()
        )
        InstallEndToEndTests._write(source_script, b"#!/usr/bin/env python3\n# fixture hook script\n")
        InstallEndToEndTests._write(
            write_trigger_source_script, b"#!/usr/bin/env python3\n# fixture write-trigger script\n"
        )

        self._stack.enter_context(mock.patch.object(installer, "SSD_ROOT", ssd_root))
        self._stack.enter_context(mock.patch.object(installer, "LOCAL_HOMES_ROOT", local_homes))
        self._stack.enter_context(mock.patch.object(installer, "RUNTIME_BASE", runtime_base))
        self._stack.enter_context(mock.patch.object(installer, "PENDING_PATH", pending_path))
        self._stack.enter_context(mock.patch.object(installer, "SOURCE_SCRIPT", source_script))
        self._stack.enter_context(
            mock.patch.object(installer, "WRITE_TRIGGER_SOURCE_SCRIPT", write_trigger_source_script)
        )
        self._stack.enter_context(
            mock.patch.object(
                installer, "volume_uuid", lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
            )
        )
        self._stack.enter_context(mock.patch.object(Path, "home", lambda: local_homes))

        self.runtime_base = runtime_base
        self.main_config = local_homes / ".codex/hooks.json"
        self.account_config = local_homes / "codex-accounts/acct-one/home/hooks.json"

        self.policy_path = root / "write-trigger-policy.json"
        self.policy_path.write_bytes(
            json.dumps(
                {"write_trigger": {"enabled": True, "max_candidates_per_project": 50, "max_candidate_bytes": 1500}}
            ).encode()
        )
        self.disabled_policy_path = root / "write-trigger-policy-disabled.json"
        self.disabled_policy_path.write_bytes(
            json.dumps(
                {"write_trigger": {"enabled": False, "max_candidates_per_project": 50, "max_candidate_bytes": 1500}}
            ).encode()
        )

    def _session_end_handler(self, payload: dict) -> dict:
        handlers = payload["hooks"].get("SessionEnd", [])
        self.assertEqual(len(handlers), 1)
        return handlers[0]["hooks"][0]

    def test_dry_run_reports_would_change_without_writing_anything(self) -> None:
        result = installer.install_write_trigger(self.policy_path, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "plan")
        self.assertIn("write_trigger_script_sha256", result)
        self.assertTrue(all(row["will_change"] for row in result["configs"]))

        self.assertFalse((self.runtime_base / "pending-install.json").exists())
        self.assertFalse((self.runtime_base / "latest-receipt.json").exists())
        self.assertEqual(self.main_config.read_bytes(), InstallEndToEndTests._base_hooks_json())
        self.assertEqual(self.account_config.read_bytes(), InstallEndToEndTests._base_hooks_json())

    def test_install_registers_session_end_alongside_userpromptsubmit_with_both_scripts_hash_pinned(self) -> None:
        receipt = installer.install_write_trigger(self.policy_path)
        self.assertEqual(receipt["bridge_id"], installer.BRIDGE_ID)
        self.assertEqual(receipt["write_trigger_bridge_id"], wtc.MODULE_ID)

        for config_path in (self.main_config, self.account_config):
            payload = json.loads(config_path.read_bytes())
            user_prompt_handlers = payload["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(user_prompt_handlers), 2)
            self.assertTrue(installer.owned_handler(user_prompt_handlers[1]))
            session_end_handler = self._session_end_handler(payload)
            self.assertTrue(installer.owned_handler({"hooks": [session_end_handler]}, bridge_id=wtc.MODULE_ID))
            # Not owned under this module's own default bridge_id -- the two handlers carry
            # genuinely different identities, not just different events.
            self.assertFalse(installer.owned_handler({"hooks": [session_end_handler]}))
            self.assertNotIn("timeout", session_end_handler)
            self.assertEqual(session_end_handler["statusMessage"], "Scanning session for durable memory candidates")
            self.assertIn("write_candidate_capture.py", session_end_handler["command"])
            self.assertIn(" scan ", session_end_handler["command"])
            self.assertIn(f"--bridge-id {wtc.MODULE_ID}", session_end_handler["command"])

        release_dir = Path(receipt["release_dir"])
        write_trigger_script = release_dir / "write_candidate_capture.py"
        self.assertTrue(write_trigger_script.exists())
        self.assertEqual(
            installer.sha256_bytes(write_trigger_script.read_bytes()), receipt["write_trigger_script_sha256"]
        )
        self.assertTrue((release_dir / "claude_memory_hook.py").exists())
        self.assertEqual(installer.sha256_bytes((release_dir / "claude_memory_hook.py").read_bytes()), receipt["script_sha256"])

        verify_result = installer.verify()
        self.assertTrue(verify_result["ok"])
        self.assertEqual(verify_result["write_trigger_script_sha256"], receipt["write_trigger_script_sha256"])

    def test_install_does_not_disturb_the_existing_base_install_unrelated_handler_or_ownership(self) -> None:
        # Layering the write-trigger handler onto an already-installed base bridge is an ordinary
        # "upgrade" in this file's existing sense (a new release_id/command -- see
        # test_upgrade_reinstall_does_not_poison_the_uninstall_baseline's own comment for why that
        # is expected, not a regression: write_trigger's own config/script hash is part of the
        # release-content-addressing key, exactly like claude_memory_hook.py's own script hash
        # already was). What must NOT change is the pre-existing, unrelated handler, and exactly
        # one owned UserPromptSubmit handler must be present throughout -- verified here, not just
        # assumed from the generic upgrade machinery working for the plain case.
        installer.install()
        for config_path in (self.main_config, self.account_config):
            payload = json.loads(config_path.read_bytes())
            self.assertEqual(payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "/usr/bin/true")

        installer.install_write_trigger(self.policy_path)

        for config_path in (self.main_config, self.account_config):
            payload = json.loads(config_path.read_bytes())
            handlers = payload["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(handlers), 2)
            self.assertEqual(handlers[0]["hooks"][0]["command"], "/usr/bin/true")
            self.assertTrue(installer.owned_handler(handlers[1]))
            self.assertEqual(len(payload["hooks"]["SessionEnd"]), 1)

    def test_install_is_idempotent_no_duplicate_handler(self) -> None:
        installer.install_write_trigger(self.policy_path)
        first_bytes = self.main_config.read_bytes()
        second_receipt = installer.install_write_trigger(self.policy_path)
        second_bytes = self.main_config.read_bytes()
        self.assertEqual(first_bytes, second_bytes)
        payload = json.loads(second_bytes)
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)
        self.assertEqual(len(payload["hooks"]["SessionEnd"]), 1)
        self.assertTrue(installer.verify()["ok"])
        self.assertEqual(second_receipt["release_id"], installer.read_receipt()["release_id"])

    def test_make_release_rejects_a_none_write_trigger_bridge_id_before_it_reaches_shlex_quote(self) -> None:
        # P2-C fix (independent Claude opus5/max review, 2026-08-21): the round that added
        # _validate_bridge_id()/_validate_hook_event() wired them into update_hook_config(),
        # owned_handler(), and _find_untracked_owned_configs() -- but NOT into make_release(),
        # which is where write_trigger["bridge_id"] actually first gets consumed (passed to
        # shlex.quote() while building the registered write-trigger command), and which runs
        # BEFORE the validating update_hook_config() call in both install()'s and plan()'s own
        # sequences. Pre-fix, write_trigger["bridge_id"] = None was silently accepted by
        # shlex.quote()'s own `if not s: return ''` branch, baking an EMPTY bridge id into the
        # actually-registered command -- permanently undetectable by any real
        # owned_handler(bridge_id=...) search expecting a real id.
        #
        # Round-52 fix (converged independent Claude opus/max + Codex gpt-5.6-sol/max review,
        # 2026-08-21): _validate_write_trigger_argument() now requires write_trigger["bridge_id"] to
        # EXACTLY equal WRITE_TRIGGER_BRIDGE_ID, not merely "any non-empty string" -- so the
        # assertion below now checks the new, more specific exact-identity message rather than the
        # old generic "non-empty string" one; `None` still fails to equal the constant, so this
        # still raises the same InstallError-not-TypeError guarantee the original P2-C fix intended.
        #
        # Deliberately calls the REAL make_release() directly with a hand-built write_trigger dict
        # (bypassing install_write_trigger()'s own always-valid _load_write_trigger_config() path)
        # inside THIS class's fully-mocked SSD fixture -- both SOURCE_SCRIPT and
        # WRITE_TRIGGER_SOURCE_SCRIPT are mocked here (unlike InstallBridgeTests' plain unit tests),
        # so a pre-fix call genuinely reaches shlex.quote() instead of coincidentally raising an
        # unrelated InstallError from an unmocked path first -- confirmed below by asserting on the
        # validator's own exact message, not merely "raises InstallError", so this test cannot be
        # fooled by an unrelated failure the way an earlier draft of this test (caught during this
        # same fix) was.
        write_trigger = {
            "bridge_id": None,
            "max_candidates_per_project": 50,
            "max_candidate_bytes": 1500,
        }
        with self.assertRaises(installer.InstallError) as ctx:
            installer.make_release(write_trigger=write_trigger)  # type: ignore[arg-type]
        self.assertIn(f"must be {installer.WRITE_TRIGGER_BRIDGE_ID!r}", str(ctx.exception))

    def test_make_release_rejects_a_non_str_write_trigger_bridge_id_instead_of_a_bare_typeerror(self) -> None:
        # P2-C fix, sibling of the None case above: pre-fix, write_trigger["bridge_id"] = 123 (a
        # non-str) raised a bare, uncaught TypeError out of shlex.quote() -- escaping past main()'s
        # own `except InstallError` handler entirely, a genuine unhandled-exception crash for any
        # library caller of make_release()/install_write_trigger(), not the clean, expected
        # InstallError every other entry point that accepts a caller-supplied bridge_id already
        # raises for bad input. See the None-case test above for why this runs inside this class's
        # fully-mocked fixture rather than as a bare unit test, and for why the assertion below now
        # checks the round-52 exact-identity message instead of the old generic one.
        write_trigger = {
            "bridge_id": 123,
            "max_candidates_per_project": 50,
            "max_candidate_bytes": 1500,
        }
        with self.assertRaises(installer.InstallError) as ctx:
            installer.make_release(write_trigger=write_trigger)  # type: ignore[arg-type]
        self.assertIn(f"must be {installer.WRITE_TRIGGER_BRIDGE_ID!r}", str(ctx.exception))

    def test_make_release_rejects_a_write_trigger_dict_missing_the_bridge_id_key(self) -> None:
        # Not explicitly one of the reviewer's two reproduced failure modes, but the same exact-key-
        # set check the round-52 fix added (_validate_write_trigger_argument(), see its own comment)
        # means a write_trigger dict missing the key entirely fails this same clean way too, instead
        # of a bare KeyError -- now via the exact-key-set message rather than the old
        # "bridge_id must be a non-empty string" one, since a missing key is caught before bridge_id
        # is ever read at all.
        write_trigger = {"max_candidates_per_project": 50, "max_candidate_bytes": 1500}
        with self.assertRaises(installer.InstallError) as ctx:
            installer.make_release(write_trigger=write_trigger)
        self.assertIn("write_trigger must have exactly these keys", str(ctx.exception))

    # P2-1 fix (independent Claude opus5/max + Codex review, 2026-08-21): the prior round
    # (test_make_release_rejects_*_write_trigger_bridge_id_* above) validated only `bridge_id` in
    # make_release()'s write_trigger argument. The two sibling limit fields --
    # `max_candidates_per_project` / `max_candidate_bytes` -- were still consumed via bare dict
    # subscripts, unvalidated, and fed straight into canonical_json()/the release-key computation.
    # Both reviewers independently reproduced the same three failure modes and specifically called
    # out the third as the worst: a value that is technically present but wrong-shaped passes
    # make_release() cleanly, gets baked into the installed policy.json, and makes install()/verify()
    # both report ok: true -- while every REAL SessionEnd invocation afterward fails inside
    # write_candidate_capture._parse_write_trigger_block() at runtime, silently swallowed by scan()'s
    # own fail-closed `except Exception`, permanently and silently inerting the write-trigger handler
    # with no external signal anything is wrong. These tests cover every one of the specific
    # silent-failure input shapes both reviewers listed for BOTH limit fields: "5" (numeric string),
    # 5.0 (float), True/False (bool -- Python's bool is an int subtype, so a naive isinstance(x, int)
    # check would wrongly accept it), None, [5] (list), -1 (negative), 0 (zero, below the >=1 floor),
    # and 10**30 (an absurdly large number, above the hard ceiling) -- plus a missing sibling key for
    # each field and a non-dict write_trigger entirely.
    #
    # Verified by reverting _validate_write_trigger_argument()'s call in make_release() (replacing it
    # with the prior round's bare `_validate_bridge_id(write_trigger.get("bridge_id"))`) in isolation:
    # every subTest below then failed to raise at all for the numeric-string/float/bool/None/list
    # cases (make_release() returned a result cleanly -- the exact silent-success failure mode both
    # reviewers reported) or raised a bare KeyError/TypeError instead of InstallError for the
    # missing-key/out-of-range cases, confirming this test suite genuinely exercises the fix rather
    # than a precondition that was already true.

    _WRITE_TRIGGER_BAD_LIMIT_VALUES: list[tuple[str, Any]] = [
        ("numeric_string", "5"),
        ("float", 5.0),
        ("bool_true", True),
        ("bool_false", False),
        ("none", None),
        ("list", [5]),
        ("negative", -1),
        ("zero", 0),
        ("huge", 10**30),
    ]

    def _assert_make_release_rejects_before_any_io(self, write_trigger: Any) -> None:
        with self.assertRaises(installer.InstallError):
            installer.make_release(write_trigger=write_trigger)  # type: ignore[arg-type]
        # "before any I/O" -- none of make_release()'s own writes (it does not itself write files,
        # but install()/install_write_trigger() built on top of it must never get far enough to
        # create a pending journal or receipt from a call this rejects) and the live hooks.json
        # configs are byte-for-byte untouched.
        self.assertFalse((self.runtime_base / "pending-install.json").exists())
        self.assertFalse((self.runtime_base / "latest-receipt.json").exists())
        self.assertEqual(self.main_config.read_bytes(), InstallEndToEndTests._base_hooks_json())
        self.assertEqual(self.account_config.read_bytes(), InstallEndToEndTests._base_hooks_json())

    def test_make_release_rejects_every_bad_shaped_max_candidates_per_project(self) -> None:
        for label, value in self._WRITE_TRIGGER_BAD_LIMIT_VALUES:
            with self.subTest(field="max_candidates_per_project", shape=label):
                write_trigger = {
                    "bridge_id": wtc.MODULE_ID,
                    "max_candidates_per_project": value,
                    "max_candidate_bytes": 1500,
                }
                self._assert_make_release_rejects_before_any_io(write_trigger)

    def test_make_release_rejects_every_bad_shaped_max_candidate_bytes(self) -> None:
        for label, value in self._WRITE_TRIGGER_BAD_LIMIT_VALUES:
            with self.subTest(field="max_candidate_bytes", shape=label):
                write_trigger = {
                    "bridge_id": wtc.MODULE_ID,
                    "max_candidates_per_project": 50,
                    "max_candidate_bytes": value,
                }
                self._assert_make_release_rejects_before_any_io(write_trigger)

    def test_make_release_rejects_write_trigger_missing_max_candidates_per_project_key(self) -> None:
        write_trigger = {"bridge_id": wtc.MODULE_ID, "max_candidate_bytes": 1500}
        self._assert_make_release_rejects_before_any_io(write_trigger)

    def test_make_release_rejects_write_trigger_missing_max_candidate_bytes_key(self) -> None:
        write_trigger = {"bridge_id": wtc.MODULE_ID, "max_candidates_per_project": 50}
        self._assert_make_release_rejects_before_any_io(write_trigger)

    def test_make_release_rejects_a_non_dict_write_trigger(self) -> None:
        # `write_trigger=None` is the valid sentinel meaning "no write-trigger release at all" (every
        # call site except install-write-trigger) and must NOT be included here -- that is covered by
        # every other test in this file that calls make_release()/install() with no write_trigger=.
        for label, value in [
            ("string", "not-a-dict"),
            ("list", [wtc.MODULE_ID, 50, 1500]),
            ("int", 1),
        ]:
            with self.subTest(shape=label):
                self._assert_make_release_rejects_before_any_io(value)

    # Round-52 fix (converged independent Claude opus/max + Codex gpt-5.6-sol/max review,
    # 2026-08-21). Two prior rounds' NO-GO both converged on the same root cause from different
    # angles: receipt/argument-field-based tracking of the write-trigger identity is fragile. The
    # architectural fix is two changes:
    #   (a) write_trigger["bridge_id"] must EXACTLY equal WRITE_TRIGGER_BRIDGE_ID -- not merely be
    #       "any non-empty string" (the prior round's own validation, see the moved
    #       test_make_release_rejects_a_none_write_trigger_bridge_id_*/test_make_release_rejects_a_
    #       non_str_write_trigger_bridge_id_* tests above, which now assert the new message).
    #   (b) write_trigger's key set must be EXACTLY {"bridge_id", "max_candidates_per_project",
    #       "max_candidate_bytes"} -- checked before any field is consumed or reaches
    #       canonical_json().
    # The tests below cover (a) with a syntactically well-formed but WRONG identity (the actual gap
    # Codex reproduced end-to-end: install succeeds, verify() says ok:true, the registered command
    # is permanently, silently inert), and (b) with the specific silent-failure shapes both
    # reviewers called out: an extra key (including `enabled`, investigated below rather than
    # assumed -- see WRITE_TRIGGER_BRIDGE_ID's own comment and _validate_write_trigger_argument()'s),
    # a non-JSON-serializable extra value, and a non-string key.

    def test_make_release_rejects_a_custom_but_well_formed_write_trigger_bridge_id(self) -> None:
        # THE core P1 fix, verified by reverting _validate_write_trigger_argument()'s exact-identity
        # check in isolation (replacing `if write_trigger["bridge_id"] != WRITE_TRIGGER_BRIDGE_ID:
        # raise ...` with the prior round's bare `_validate_bridge_id(write_trigger.get("bridge_id"))`):
        # this exact write_trigger dict then passed cleanly -- make_release() returned a result, no
        # exception at all -- confirming this test genuinely exercises the fix rather than a
        # precondition that was already true. A syntactically well-formed, non-empty string that is
        # simply NOT the one real identity is exactly the shape the prior round's validation could
        # never catch (it only checked "is this a non-empty string", which a custom id trivially
        # satisfies) -- and exactly the shape Codex proved installs a real, permanently silently
        # inert handler that this file's own untracked-owned-handler safety scan and verify() can
        # never detect, because both only ever look for WRITE_TRIGGER_BRIDGE_ID.
        write_trigger = {
            "bridge_id": "some-other-custom-identity-v1",
            "max_candidates_per_project": 50,
            "max_candidate_bytes": 1500,
        }
        with self.assertRaises(installer.InstallError) as ctx:
            installer.make_release(write_trigger=write_trigger)
        self.assertIn(f"must be {installer.WRITE_TRIGGER_BRIDGE_ID!r}", str(ctx.exception))
        self.assertIn("some-other-custom-identity-v1", str(ctx.exception))
        # Also confirmed through the real install_write_trigger()/install() path, not just the bare
        # make_release() unit call above -- no pending journal, no receipt, no config file touched.
        self._assert_make_release_rejects_before_any_io(write_trigger)
        with self.assertRaises(installer.InstallError):
            installer.install(write_trigger=write_trigger)
        self.assertFalse((self.runtime_base / "pending-install.json").exists())
        self.assertFalse((self.runtime_base / "latest-receipt.json").exists())

    def test_make_release_rejects_write_trigger_with_an_unexpected_extra_key(self) -> None:
        # P2 fix, opus + Codex (same finding): the prior round's synthetic-dict-rebuild approach in
        # _validate_write_trigger_argument() silently dropped any extra/unexpected key before it
        # reached the real validator (_parse_write_trigger_block()), and the ORIGINAL caller dict --
        # not the synthetic one -- is what make_release() later feeds into canonical_json() for the
        # release-key computation. Verified by reverting the exact-key-set check in isolation: every
        # subTest below then passed straight through to canonical_json() (the "set"/"path" cases
        # raised a bare, uncaught TypeError there instead of a clean InstallError; the "enabled_*"
        # cases were silently ignored -- make_release() returned a result cleanly, using the
        # unconditionally-hardcoded `enabled: True` regardless of the value the caller actually
        # passed -- confirming this genuinely exercises the fix, not an already-true precondition).
        base = {
            "bridge_id": wtc.MODULE_ID,
            "max_candidates_per_project": 50,
            "max_candidate_bytes": 1500,
        }
        extra_cases: list[tuple[str, Any]] = [
            ("enabled_true", True),
            ("enabled_false", False),
            ("enabled_non_bool", "not-a-bool"),
            ("plain_unexpected_key", None),
        ]
        for label, enabled_value in extra_cases:
            with self.subTest(shape=label):
                write_trigger = dict(base)
                key = "enabled" if label != "plain_unexpected_key" else "some_unexpected_key"
                write_trigger[key] = enabled_value
                with self.assertRaises(installer.InstallError) as ctx:
                    installer.make_release(write_trigger=write_trigger)
                self.assertIn("write_trigger must have exactly these keys", str(ctx.exception))
                self._assert_make_release_rejects_before_any_io(write_trigger)

    def test_make_release_rejects_write_trigger_extra_key_before_a_non_serializable_value_reaches_canonical_json(
        self,
    ) -> None:
        # The specific worst-case both reviewers called out: an extra key whose VALUE is not
        # JSON-serializable (a `set` here; `Path`/`bytes`/an arbitrary object are equally not
        # serializable by json.dumps()) used to reach canonical_json() unguarded and raise a bare
        # TypeError -- the exact failure class this whole validation exists to eliminate. The
        # exact-key-set check now runs BEFORE any field is consumed, so this never gets anywhere
        # near canonical_json() -- confirmed by asserting the exception is exactly InstallError (a
        # bare TypeError is not a subclass of InstallError, so assertRaises below would itself fail
        # if this regressed) and that no partial write of any kind occurred.
        write_trigger = {
            "bridge_id": wtc.MODULE_ID,
            "max_candidates_per_project": 50,
            "max_candidate_bytes": 1500,
            "not_json_serializable": {1, 2, 3},
        }
        with self.assertRaises(installer.InstallError) as ctx:
            installer.make_release(write_trigger=write_trigger)
        self.assertIn("write_trigger must have exactly these keys", str(ctx.exception))
        self._assert_make_release_rejects_before_any_io(write_trigger)

    def test_make_release_rejects_a_write_trigger_with_a_non_string_key(self) -> None:
        # Sibling of the extra-key tests above: a non-string key used to reach
        # canonical_json()'s own `sort_keys=True` and raise a bare TypeError from comparing an int
        # key against str keys. The exact-key-set check catches this the same way (the non-string
        # key is simply not a member of the expected key set either), before canonical_json() is
        # ever reached.
        write_trigger = {
            "bridge_id": wtc.MODULE_ID,
            "max_candidates_per_project": 50,
            "max_candidate_bytes": 1500,
            7: "seven",
        }
        with self.assertRaises(installer.InstallError) as ctx:
            installer.make_release(write_trigger=write_trigger)  # type: ignore[arg-type]
        self.assertIn("write_trigger must have exactly these keys", str(ctx.exception))
        self._assert_make_release_rejects_before_any_io(write_trigger)

    def test_install_write_trigger_custom_id_ordering_finding_is_now_unreachable(self) -> None:
        # Reproduces Codex's exact reported sequence -- well-known-ID write-trigger install, then a
        # SECOND write-trigger install attempt under a DIFFERENT, custom identity, then a plain
        # install -- and confirms the middle step is now unreachable: it fails cleanly, before any
        # I/O, so there is never a second, differently-identified live handler for the
        # untracked-owned-handler safety net to miss in the first place. Reproduction of the
        # reviewer's finding, not merely an assertion that make_release() rejects a bad shape in
        # isolation (see test_make_release_rejects_a_custom_but_well_formed_write_trigger_bridge_id
        # above for that unit-level coverage).
        receipt1 = installer.install_write_trigger(self.policy_path)
        self.assertEqual(receipt1["write_trigger_bridge_id"], wtc.MODULE_ID)
        main_bytes_after_step1 = self.main_config.read_bytes()
        account_bytes_after_step1 = self.account_config.read_bytes()
        latest_receipt_after_step1 = (self.runtime_base / "latest-receipt.json").read_bytes()

        custom_write_trigger = {
            "bridge_id": "attacker-or-typo-custom-identity",
            "max_candidates_per_project": 50,
            "max_candidate_bytes": 1500,
        }
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install(write_trigger=custom_write_trigger)
        self.assertIn(f"must be {installer.WRITE_TRIGGER_BRIDGE_ID!r}", str(ctx.exception))

        # Nothing changed: no second handler under the custom identity was ever written, no pending
        # journal, receipt untouched -- proving there is no orphaned-under-a-different-identity state
        # for uninstall()'s/recover_pending_install()'s safety net to have to catch.
        self.assertFalse((self.runtime_base / "pending-install.json").exists())
        self.assertEqual((self.runtime_base / "latest-receipt.json").read_bytes(), latest_receipt_after_step1)
        self.assertEqual(self.main_config.read_bytes(), main_bytes_after_step1)
        self.assertEqual(self.account_config.read_bytes(), account_bytes_after_step1)
        for config_path in (self.main_config, self.account_config):
            payload = json.loads(config_path.read_bytes())
            self.assertFalse(
                any(
                    installer.owned_handler(handler, bridge_id="attacker-or-typo-custom-identity")
                    for handler in payload["hooks"].get("SessionEnd", [])
                )
            )

        # An ordinary, unrelated plain install() afterward still succeeds cleanly -- the rejected
        # attempt left no wedge behind.
        result = installer.install()
        self.assertTrue(result.get("release_id"))
        self.assertTrue(installer.verify()["ok"])

    def test_fails_closed_and_changes_nothing_when_write_trigger_not_enabled(self) -> None:
        with self.assertRaises(installer.InstallError) as ctx:
            installer.install_write_trigger(self.disabled_policy_path)
        self.assertIn("write_trigger.enabled", str(ctx.exception))
        self.assertFalse((self.runtime_base / "pending-install.json").exists())
        self.assertFalse((self.runtime_base / "latest-receipt.json").exists())
        self.assertEqual(self.main_config.read_bytes(), InstallEndToEndTests._base_hooks_json())
        self.assertEqual(self.account_config.read_bytes(), InstallEndToEndTests._base_hooks_json())

    def test_uninstall_removes_both_handlers_full_rollback(self) -> None:
        installer.install_write_trigger(self.policy_path)
        result = installer.uninstall()
        self.assertTrue(result["ok"])
        self.assertEqual(self.main_config.read_bytes(), InstallEndToEndTests._base_hooks_json())
        self.assertEqual(self.account_config.read_bytes(), InstallEndToEndTests._base_hooks_json())
        self.assertFalse((self.runtime_base / "latest-receipt.json").exists())

    def test_uninstall_catches_an_untracked_write_trigger_handler_the_base_identity_scan_alone_misses(
        self,
    ) -> None:
        # P2-D fix (independent Claude opus5/max review, 2026-08-21): the untracked-owned-handler
        # safety scan uninstall()/recover_pending_install() run before finishing (see
        # _untracked_owned_including_write_trigger()'s own comment) used to run ONLY under the base
        # identity (BRIDGE_ID, UserPromptSubmit) -- never under the write-trigger identity, even
        # though nothing in the scan's own design prevented it, and a comment nearby described a
        # caller passing the write-trigger identity here as if it already existed when it did not.
        # This constructs a shape the base-identity-only scan genuinely cannot see: an untracked
        # hooks.json carrying ONLY a write-trigger SessionEnd handler, no UserPromptSubmit handler
        # under BRIDGE_ID at all, so the base-identity scan finds nothing and the write-trigger-
        # identity scan is the only thing that can catch it -- proving this fix is load-bearing, not
        # merely a comment correction.
        installer.install_write_trigger(self.policy_path)
        local_homes = self.main_config.parent.parent
        stray_dir = local_homes / "codex-accounts/acct-stray/home"
        stray_dir.mkdir(parents=True)
        stray_config = stray_dir / "hooks.json"
        stray_command = f"/usr/bin/python3 write_candidate_capture.py scan --bridge-id {wtc.MODULE_ID}"
        InstallEndToEndTests._write(
            stray_config,
            json.dumps(
                {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": stray_command}]}]}}
            ).encode(),
        )

        receipt = installer.read_receipt()
        self.assertIn("write_trigger_bridge_id", receipt)  # fixture precondition
        receipt_paths = {row["path"] for row in receipt["configs"]}
        resolved_stray = os.fspath(installer.resolve_ssd_path(stray_config))

        # Confirm the fixture genuinely isolates the write-trigger-identity half: the base-identity
        # scan alone (the function's own pre-fix call shape) does not see this stray file at all.
        self.assertEqual(installer._find_untracked_owned_configs(receipt_paths), [])

        # The combined helper uninstall()/recover_pending_install() now both use DOES catch it.
        combined = installer._untracked_owned_including_write_trigger(receipt, receipt_paths)
        self.assertIn(resolved_stray, combined)

        # And uninstall() itself now refuses because of it, leaving everything untouched.
        receipt_before = (self.runtime_base / "latest-receipt.json").read_bytes()
        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("does not track", str(ctx.exception))
        self.assertFalse(installer.PENDING_PATH.exists())
        self.assertEqual((self.runtime_base / "latest-receipt.json").read_bytes(), receipt_before)

        # No permanent lockout: removing the stray file lets uninstall() proceed normally.
        stray_config.unlink()
        result = installer.uninstall()
        self.assertTrue(result["ok"])

    def test_plain_reinstall_after_write_trigger_does_not_disarm_the_untracked_write_trigger_safety_net(
        self,
    ) -> None:
        # P2-2 fix (independent Claude opus5/max review, 2026-08-21). Reproduces the reviewer's exact
        # sequence: the prior round's fix decided whether to ALSO scan for the write-trigger identity
        # by checking `receipt.get("write_trigger_bridge_id") is not None` on the ONE receipt currently
        # in hand -- a real, reproduced gap, because a LATER, entirely ordinary plain install() (no
        # write_trigger= argument -- e.g. redeploying just a claude_memory_hook.py fix, independent of
        # any write-trigger decision) overwrites latest-receipt.json with a receipt that has no
        # write_trigger_bridge_id field at all, even though nothing about that plain install touches an
        # already-registered SessionEnd handler a PRIOR install-write-trigger call added
        # (update_hook_config()'s per-event layering is additive, not a full reset). Pre-fix, the
        # safety net would then silently stop checking the write-trigger identity for every subsequent
        # uninstall()/recover_pending_install() call, and the very next uninstall() would succeed and
        # delete the receipt while an orphaned write-trigger handler stayed live in hooks.json.
        #
        # The stray untracked write-trigger handler this test constructs is deliberately introduced
        # AFTER the plain reinstall (not before): install()'s own finalizing recover_pending_install()
        # call already runs this exact untracked-owned-handler safety scan for the BASE identity today,
        # unconditionally, completely independent of write-trigger (R9-P1-A, pre-existing, unrelated to
        # this fix -- confirmed empirically: an ordinary second install() with an untracked BASE-
        # identity handler present anywhere already refuses today, with or without this fix). This
        # fix's job is only to make the write-trigger identity get that exact same, already-established
        # treatment -- so once it does, a stray write-trigger handler present DURING a plain reinstall
        # would correctly block that reinstall too, for the same pre-existing reason an untracked base
        # handler already would. Introducing it afterward isolates the ONE thing this fix actually
        # changes: whether the safety net still checks the write-trigger identity for a receipt that no
        # longer records write_trigger_bridge_id, independent of that separate, pre-existing behavior.
        #
        # Sequence: install WITH write_trigger (safety net correctly catches an orphaned write-trigger
        # handler; verified first, exactly like the P2-D test above) -> plain install() with NO
        # write_trigger, succeeding cleanly (the receipt's write_trigger_bridge_id field genuinely
        # disappears -- checked explicitly below, not assumed) -> a write-trigger handler becomes
        # orphaned -> the safety net must STILL catch it using this NEW, write_trigger_bridge_id-less
        # receipt (proving the check no longer depends on that one receipt field) -> uninstall() must
        # still refuse, not silently succeed and abandon the handler.
        installer.install_write_trigger(self.policy_path)
        receipt1 = installer.read_receipt()
        self.assertIn("write_trigger_bridge_id", receipt1)  # fixture precondition

        # Plain reinstall with NO write_trigger -- an ordinary, unrelated "redeploy the base bridge
        # only" operation, independent of any write-trigger decision (matches the same operation
        # already discussed and performed for real earlier in this project). Must succeed: nothing
        # untracked exists yet at this point.
        result = installer.install()
        self.assertTrue(result.get("release_id"))

        receipt2 = installer.read_receipt()
        # Precondition confirming this genuinely reproduces the reviewer's scenario: the new receipt
        # has lost the write_trigger_bridge_id field entirely, even though the write-trigger handler a
        # prior install-write-trigger call registered is untouched and still live in the TRACKED
        # configs (additive per-event layering, not a full reset -- checked explicitly, not assumed).
        self.assertNotIn("write_trigger_bridge_id", receipt2)
        self.assertNotIn("write_trigger_script_sha256", receipt2)
        for config_path in (self.main_config, self.account_config):
            payload = json.loads(config_path.read_bytes())
            self.assertEqual(len(payload["hooks"].get("SessionEnd", [])), 1)
        receipt_paths2 = {row["path"] for row in receipt2["configs"]}

        # NOW a write-trigger handler becomes orphaned: an account directory carrying ONLY a
        # write-trigger SessionEnd handler (no UserPromptSubmit handler at all), nested one level
        # deeper than _enumerate_hook_configs()'s own one-level-deep shape
        # (codex-accounts/<name>/home/hooks.json -- see that function's own comment) so it is genuinely
        # untracked -- not something a fresh discover_hook_configs() call would adopt as a normal new
        # account (matching the R8-P1-B scenario: a relocated account whose own home/ subdirectory was
        # renamed).
        local_homes = self.main_config.parent.parent
        stray_dir = local_homes / "codex-accounts/acct-stray/relocated/home"
        stray_dir.mkdir(parents=True)
        stray_config = stray_dir / "hooks.json"
        stray_command = f"/usr/bin/python3 write_candidate_capture.py scan --bridge-id {wtc.MODULE_ID}"
        InstallEndToEndTests._write(
            stray_config,
            json.dumps(
                {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": stray_command}]}]}}
            ).encode(),
        )
        resolved_stray = os.fspath(installer.resolve_ssd_path(stray_config))

        # The safety net must catch the stray handler using receipt2 -- the receipt with NO
        # write_trigger_bridge_id field -- proving the check no longer silently stops just because this
        # particular receipt lost that field.
        combined = installer._untracked_owned_including_write_trigger(receipt2, receipt_paths2)
        self.assertIn(resolved_stray, combined)

        # And uninstall() itself must still correctly refuse, not silently succeed and abandon the
        # orphaned write-trigger handler.
        receipt_before = (self.runtime_base / "latest-receipt.json").read_bytes()
        with self.assertRaises(installer.InstallError) as ctx:
            installer.uninstall()
        self.assertIn("does not track", str(ctx.exception))
        self.assertFalse(installer.PENDING_PATH.exists())
        self.assertEqual((self.runtime_base / "latest-receipt.json").read_bytes(), receipt_before)

        # No permanent lockout: removing the stray file lets uninstall() proceed normally.
        stray_config.unlink()
        result = installer.uninstall()
        self.assertTrue(result["ok"])

    def test_recover_after_injected_failure_mid_write_trigger_upgrade_rolls_back_to_prev(self) -> None:
        # Base install first (v1: UserPromptSubmit only), then attempt the write-trigger upgrade
        # with a failure injected on the second config's live write -- mirrors this file's own
        # test_interrupted_upgrade_install_recovers_to_the_previous_working_state pattern
        # (path-addressed in-process atomic_write side_effect, not a real SIGKILL), proving
        # install()'s existing self-heal/recover_pending_install() rollback genuinely covers this
        # new write_trigger= call shape too, not just the plain-UserPromptSubmit one -- checked
        # explicitly, not assumed.
        installer.install()

        real_atomic_write = installer.atomic_write

        def failing_atomic_write(path, raw, mode=0o600):
            if path == self.account_config:
                raise installer.InstallError("simulated disk error")
            return real_atomic_write(path, raw, mode)

        with mock.patch.object(installer, "atomic_write", side_effect=failing_atomic_write):
            with self.assertRaises(installer.InstallError):
                installer.install_write_trigger(self.policy_path)

        for config_path in (self.main_config, self.account_config):
            payload = json.loads(config_path.read_bytes())
            self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)
            self.assertNotIn("SessionEnd", payload["hooks"])

        self.assertTrue(installer.verify()["ok"])
        self.assertTrue(installer.plan()["ok"])
        self.assertEqual(installer.recover_pending_install(), {"ok": True, "state": "none"})

        # A subsequent, un-injected retry completes cleanly -- the recovery left a genuinely
        # working, retryable state, not a permanent wedge.
        receipt = installer.install_write_trigger(self.policy_path)
        self.assertIn("write_trigger_bridge_id", receipt)
        payload = json.loads(self.account_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["SessionEnd"]), 1)

    def test_verify_tolerates_a_non_list_pre_existing_session_end_value_on_a_plain_install(self) -> None:
        # P2-A fix regression (round-53, independent Claude opus/max review, 2026-08-21): a NEW
        # regression this round's verify() rewrite introduced, unrelated to write-trigger. install()
        # never inspects or normalizes SessionEnd on a plain (non-write-trigger) install -- only the
        # event actually being registered gets validated -- so a user's PRE-EXISTING SessionEnd key
        # (legitimately some other tool's own hook, nothing to do with this bridge) is written back
        # verbatim, whatever shape it has, including a non-list JSON scalar. The old (pre-this-round)
        # verify() returned ok=True in every one of these cases; the unguarded rewrite instead crashed
        # with an uncaught TypeError iterating a non-iterable.
        for bad_session_end in (None, 0, True, "not-a-list-of-handlers"):
            with self.subTest(bad_session_end=bad_session_end):
                main_payload = {
                    "hooks": {
                        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}],
                        "SessionEnd": bad_session_end,
                    }
                }
                InstallEndToEndTests._write(self.main_config, json.dumps(main_payload).encode())
                InstallEndToEndTests._write(self.account_config, InstallEndToEndTests._base_hooks_json())

                try:
                    receipt = installer.install()
                    self.assertNotIn("write_trigger_bridge_id", receipt)  # fixture precondition: plain install

                    result = installer.verify()
                    self.assertTrue(result["ok"], result)
                finally:
                    # Reset to a clean not-installed baseline for the next variant regardless of
                    # whether the assertions above passed -- keeps a real failure on one variant from
                    # cascading into unrelated "hook config no longer matches" errors on the rest.
                    installer.uninstall()

    def test_verify_error_on_a_config_missing_the_write_trigger_hook_names_the_remediation(self) -> None:
        # P2-B fix regression (round-53, independent Claude opus/max review, 2026-08-21). Sequence:
        # install-write-trigger on the existing configs -> a NEW Codex account config appears (this
        # machine legitimately runs multiple pooled Codex account homes) -> an ordinary plain
        # install() (no write_trigger= argument) correctly picks up the new config and registers ONLY
        # the base UserPromptSubmit handler on it (plain install must never silently also add a
        # SessionEnd handler nobody asked for). verify() then correctly hard-fails -- that part was
        # already right -- but the message named no remediation and gave no hint this is an EXPECTED
        # consequence of a plain install after write-trigger was enabled elsewhere, leaving an operator
        # with no idea what to do next. Asserts on the message's actual substance, not just that
        # InstallError was raised.
        installer.install_write_trigger(self.policy_path)

        local_homes = self.main_config.parent.parent  # main_config == local_homes / ".codex/hooks.json"
        new_account_home = local_homes / "codex-accounts/acct-two/home"
        new_account_home.mkdir(parents=True)
        new_account_config = new_account_home / "hooks.json"
        InstallEndToEndTests._write(new_account_config, InstallEndToEndTests._base_hooks_json())

        receipt = installer.install()
        self.assertNotIn("write_trigger_bridge_id", receipt)  # fixture precondition: plain install this time
        payload = json.loads(new_account_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["UserPromptSubmit"]), 2)  # base handler present
        self.assertNotIn("SessionEnd", payload["hooks"])  # write-trigger handler correctly NOT added

        with self.assertRaises(installer.InstallError) as ctx:
            installer.verify()
        message = str(ctx.exception)
        self.assertIn(os.fspath(new_account_config), message)
        self.assertIn("base", message.lower())
        self.assertIn("write-trigger", message.lower())
        self.assertIn("expected", message.lower())
        self.assertIn("install-write-trigger", message)  # names the actual remediation action

    def test_verify_detects_write_trigger_script_tampering(self) -> None:
        receipt = installer.install_write_trigger(self.policy_path)
        release_dir = Path(receipt["release_dir"])
        write_trigger_script = release_dir / "write_candidate_capture.py"
        original_mode = stat.S_IMODE(write_trigger_script.stat().st_mode)
        write_trigger_script.chmod(0o600)
        write_trigger_script.write_bytes(b"#!/usr/bin/env python3\n# tampered\n")
        write_trigger_script.chmod(original_mode)
        with self.assertRaises(installer.InstallError) as ctx:
            installer.verify()
        self.assertIn("write-trigger script digest mismatch", str(ctx.exception))

    def test_verify_detects_write_trigger_script_tampering_after_a_plain_reinstall_drops_the_receipt_fields(
        self,
    ) -> None:
        # Round-52 fix (converged independent Claude opus/max + Codex gpt-5.6-sol/max review,
        # 2026-08-21). Reproduces Codex's exact sequence: install-write-trigger (receipt1 records
        # write_trigger_script_sha256) -> an ordinary, unrelated plain install() (no write_trigger=
        # argument -- e.g. redeploying just a claude_memory_hook.py fix) succeeds and overwrites
        # latest-receipt.json with a receipt that has NEITHER write_trigger_* field, even though the
        # SessionEnd handler a prior install-write-trigger call registered is untouched and still
        # genuinely live (update_hook_config()'s per-event layering is additive, not a full reset) ->
        # tamper with the ACTUAL FILE the still-live handler's own command line references (a
        # DIFFERENT, OLDER release directory than the one the CURRENT receipt now points at, since a
        # plain reinstall's release_id is content-addressed without any write_trigger fields at all
        # -- see make_release()'s release_key_fields) -> verify() must now catch it, not report
        # ok: true.
        #
        # Verified by reverting verify()'s independent, live-hooks.json-driven write-trigger
        # detection in isolation (replacing it with the prior round's
        # `if receipt.get("write_trigger_script_sha256") is not None:`-gated block, which reads
        # write_candidate_capture.py from receipt["release_dir"] -- the WRONG, newer directory after
        # a plain reinstall): verify() then returned {"ok": True, ...} for this exact tampered state
        # -- the write-trigger check silently never ran at all, since receipt2 (below) has neither
        # field -- confirming this test genuinely exercises the fix rather than an already-true
        # precondition.
        receipt1 = installer.install_write_trigger(self.policy_path)
        self.assertIn("write_trigger_script_sha256", receipt1)  # fixture precondition
        live_release_dir = Path(receipt1["release_dir"])
        live_write_trigger_script = live_release_dir / "write_candidate_capture.py"
        self.assertTrue(live_write_trigger_script.exists())

        # Ordinary, unrelated plain reinstall -- succeeds, and drops both write_trigger_* receipt
        # fields (checked explicitly, not assumed -- matches the precondition
        # test_plain_reinstall_after_write_trigger_does_not_disarm_the_untracked_write_trigger_safety_net
        # already established for the untracked-handler safety net; this test covers verify()
        # instead). The new receipt's own release_dir is genuinely a DIFFERENT directory, and does
        # not even contain a write_candidate_capture.py file at all (write_runtime() only writes the
        # write-trigger script triple when write_trigger= is passed) -- checked explicitly, since
        # this is exactly what makes the pre-fix release_dir-relative check silently wrong instead
        # of merely silently skipped.
        result = installer.install()
        self.assertTrue(result.get("release_id"))
        receipt2 = installer.read_receipt()
        self.assertNotIn("write_trigger_script_sha256", receipt2)
        self.assertNotIn("write_trigger_bridge_id", receipt2)
        self.assertNotEqual(receipt2["release_dir"], receipt1["release_dir"])
        self.assertFalse((Path(receipt2["release_dir"]) / "write_candidate_capture.py").exists())

        # Pre-tamper: verify() must still pass -- the live SessionEnd handler is genuinely untouched
        # and its referenced script genuinely matches what its own command line asserts.
        self.assertTrue(installer.verify()["ok"])

        # Tamper with the file the STILL-LIVE handler's own command references -- the OLD release
        # directory, not receipt2["release_dir"].
        original_mode = stat.S_IMODE(live_write_trigger_script.stat().st_mode)
        live_write_trigger_script.chmod(0o600)
        live_write_trigger_script.write_bytes(b"#!/usr/bin/env python3\n# tampered after plain reinstall\n")
        live_write_trigger_script.chmod(original_mode)

        with self.assertRaises(installer.InstallError) as ctx:
            installer.verify()
        self.assertIn("write-trigger script digest mismatch", str(ctx.exception))

    def test_cli_dry_run_and_missing_policy_flag_and_real_install(self) -> None:
        # Drives the real, unmocked main()/argparse dispatch (mirrors this file's own
        # test_main_install_succeeds_end_to_end_when_shared_runtime_does_not_yet_exist pattern) --
        # not install_write_trigger() called directly, which every other test in this class does
        # and which never exercises the CLI argument parsing or the exclusive-lock acquisition at
        # all. Still fully isolated: SSD_ROOT/RUNTIME_BASE/etc. are the fixture's own mocked temp
        # paths throughout, so this never touches a real path.
        with mock.patch.object(sys, "argv", ["install_bridge.py", "install-write-trigger"]):
            self.assertEqual(installer.main(), 1)  # missing --write-trigger-policy: fails closed
        self.assertFalse((self.runtime_base / "latest-receipt.json").exists())

        argv = ["install_bridge.py", "install-write-trigger", "--write-trigger-policy", str(self.policy_path)]
        with mock.patch.object(sys, "argv", argv + ["--dry-run"]):
            self.assertEqual(installer.main(), 0)
        self.assertFalse((self.runtime_base / "latest-receipt.json").exists())
        self.assertEqual(self.main_config.read_bytes(), InstallEndToEndTests._base_hooks_json())

        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(installer.main(), 0)
        payload = json.loads(self.main_config.read_bytes())
        self.assertEqual(len(payload["hooks"]["SessionEnd"]), 1)
        self.assertTrue((self.runtime_base / "latest-receipt.json").exists())


class BaseOnlyPathsWithoutWriteCandidateCaptureModuleTests(unittest.TestCase):
    """P1 fix regression (converged independent Claude opus/max + Codex gpt-5.6-sol/max review,
    2026-08-21): install_bridge.py must not depend on write_candidate_capture.py being importable AT
    ALL for its base-only actions (plain install/uninstall/recover/verify -- no write_trigger=
    anywhere in the call). write_candidate_capture.py is genuinely an UNTRACKED file in this exact
    repo's own git history today (independently confirmed via `git status --short` at review time),
    so `git checkout`/`git clean`/a fresh clone that does not preserve untracked files can leave
    install_bridge.py present without its sibling. Before this fix,
    _untracked_owned_including_write_trigger() -- called unconditionally by both uninstall() and
    recover_pending_install()'s own finishing pass, for every action, including a plain one -- did an
    unconditional _import_write_candidate_capture(), so a plain, no-write-trigger uninstall()/
    recover_pending_install() call raised an uncaught ModuleNotFoundError straight past main()'s own
    `except InstallError` handler: a total lockout, including of the emergency-recovery path itself.

    This class proves the fix with a REAL subprocess whose sys.path contains ONLY a fresh, isolated
    copy of install_bridge.py -- never the real repo directory write_candidate_capture.py actually
    lives in -- run under `-I` (isolated mode: ignores PYTHONPATH, user site-packages, and the
    script's own directory), so `import write_candidate_capture` genuinely raises
    ModuleNotFoundError if anything on the base-only call path still attempts it. An in-process
    mock.patch-based test could not tell "never imports" apart from "imports, but happens to succeed
    because the real module is already on this process's own sys.path anyway" -- exactly the
    distinction this fix is about.
    """

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.temp = self._stack.enter_context(tempfile.TemporaryDirectory())
        root = Path(self.temp).resolve()

        self.isolated_dir = root / "isolated-install-bridge-only"
        self.isolated_dir.mkdir()
        shutil.copy2(installer.__file__, self.isolated_dir / "install_bridge.py")
        # Deliberately NOT copying write_candidate_capture.py -- reproduces install_bridge.py being
        # present without its sibling.
        self.assertFalse((self.isolated_dir / "write_candidate_capture.py").exists())  # fixture precondition

        self.ssd_root = root / "ssd"
        self.local_homes = self.ssd_root / "Orca/local-homes"
        self.runtime_base = self.local_homes / ".shared-runtime/claude-codex-memory-bridge"
        self.pending_path = self.runtime_base / "pending-install.json"
        source_dir = self.ssd_root / "source"
        self.source_script = source_dir / "claude_memory_hook.py"

        self.local_homes.mkdir(parents=True)
        (self.local_homes / ".codex").mkdir()
        (self.local_homes / ".claude/projects").mkdir(parents=True)
        (self.local_homes / "codex-accounts/acct-one/home").mkdir(parents=True)
        source_dir.mkdir()

        InstallEndToEndTests._write(self.local_homes / ".codex/hooks.json", InstallEndToEndTests._base_hooks_json())
        InstallEndToEndTests._write(
            self.local_homes / "codex-accounts/acct-one/home/hooks.json", InstallEndToEndTests._base_hooks_json()
        )
        InstallEndToEndTests._write(self.source_script, b"#!/usr/bin/env python3\n# fixture hook script\n")

    def _run_isolated_child(self, body: str) -> subprocess.CompletedProcess:
        script = f"""
import sys
sys.path.insert(0, {str(self.isolated_dir)!r})
try:
    import write_candidate_capture  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    raise SystemExit("FAIL: write_candidate_capture unexpectedly importable -- fixture isolation broken")

from pathlib import Path
import install_bridge as installer

installer.SSD_ROOT = Path({str(self.ssd_root)!r})
installer.LOCAL_HOMES_ROOT = Path({str(self.local_homes)!r})
installer.RUNTIME_BASE = Path({str(self.runtime_base)!r})
installer.PENDING_PATH = Path({str(self.pending_path)!r})
installer.SOURCE_SCRIPT = Path({str(self.source_script)!r})
installer.volume_uuid = lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
Path.home = classmethod(lambda cls: Path({str(self.local_homes)!r}))

{body}

assert "write_candidate_capture" not in sys.modules, "write_candidate_capture must never be imported on this path"
print("ISOLATED-OK")
"""
        script_path = Path(self.temp) / "child.py"
        script_path.write_text(script)
        # -I: isolated mode -- ignores PYTHONPATH/user site-packages and excludes the script's own
        # directory from sys.path, so the only way this subprocess can find `write_candidate_capture`
        # at all is via the one directory this test itself inserted (self.isolated_dir, which
        # deliberately does not contain it) -- a robust reproduction, not merely "no import statement
        # was typed at module level".
        return subprocess.run(
            [sys.executable, "-I", os.fspath(script_path)], capture_output=True, text=True, timeout=60
        )

    def test_plain_install_uninstall_recover_verify_all_work_without_the_module_present(self) -> None:
        body = """
outcome0 = installer.recover_pending_install()
assert outcome0 == {"ok": True, "state": "none"}, outcome0

receipt = installer.install()
assert "write_trigger_bridge_id" not in receipt, receipt
assert "write_trigger_script_sha256" not in receipt, receipt

verify_result = installer.verify()
assert verify_result["ok"] is True, verify_result

uninstall_result = installer.uninstall()
assert uninstall_result["ok"] is True, uninstall_result

outcome1 = installer.recover_pending_install()
assert outcome1 == {"ok": True, "state": "none"}, outcome1
"""
        result = self._run_isolated_child(body)
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertIn("ISOLATED-OK", result.stdout)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_verify_of_a_live_write_trigger_handler_raises_installerror_not_modulenotfounderror(self) -> None:
        # P1 fix regression (round-53, independent Claude opus/max review, 2026-08-21). A comment
        # directly above verify()'s own _import_write_candidate_capture() call claimed that a missing
        # write_candidate_capture.py there "surfaces as a clean InstallError from verify() alone, not
        # a lockout of install()/uninstall()/recover()" -- false: the import itself was unguarded, so
        # it raised an uncaught ModuleNotFoundError straight past main()'s own `except InstallError`
        # handler (empty stdout, rc=1, a raw traceback on stderr -- unlike every other failure mode in
        # this file).
        #
        # Distinct from this class's other test above: that one never installs a write-trigger handler
        # at all, so it never even reaches this import. This test installs one FOR REAL first (needs
        # write_candidate_capture genuinely importable -- done in-process here, where install_bridge.py
        # (this real repo file, `installer.__file__`) has its real, genuinely-present sibling
        # write_candidate_capture.py next to it), then calls verify() from a SEPARATE, genuinely fresh
        # `-I`-isolated child process (this class's own `_run_isolated_child`) that has never imported
        # write_candidate_capture and cannot find it on its own sys.path -- reproducing "module
        # genuinely absent" rather than merely deleting the file's content, which would NOT reproduce
        # this defect: once a process has imported write_candidate_capture, it stays cached in
        # sys.modules regardless of what happens to the file on disk afterward.
        write_trigger_source_script = self.ssd_root / "source/write_candidate_capture.py"
        InstallEndToEndTests._write(
            write_trigger_source_script, b"#!/usr/bin/env python3\n# fixture write-trigger script\n"
        )
        policy_path = Path(self.temp) / "write-trigger-policy.json"
        policy_path.write_bytes(InstallBridgeTests._write_trigger_policy_bytes())

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(installer, "SSD_ROOT", self.ssd_root))
            stack.enter_context(mock.patch.object(installer, "LOCAL_HOMES_ROOT", self.local_homes))
            stack.enter_context(mock.patch.object(installer, "RUNTIME_BASE", self.runtime_base))
            stack.enter_context(mock.patch.object(installer, "PENDING_PATH", self.pending_path))
            stack.enter_context(mock.patch.object(installer, "SOURCE_SCRIPT", self.source_script))
            stack.enter_context(
                mock.patch.object(installer, "WRITE_TRIGGER_SOURCE_SCRIPT", write_trigger_source_script)
            )
            stack.enter_context(
                mock.patch.object(
                    installer, "volume_uuid", lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
                )
            )
            stack.enter_context(mock.patch.object(Path, "home", lambda: self.local_homes))
            receipt = installer.install_write_trigger(policy_path)
        self.assertIn("write_trigger_bridge_id", receipt)  # fixture precondition: a live handler exists
        self.assertTrue("write_candidate_capture" in sys.modules)  # fixture precondition: really imported here

        # The receipt/hooks.json state above is now durable on disk under self.ssd_root -- the
        # isolated child below reads it fresh, from a process that never imported
        # write_candidate_capture and cannot reach it (see _run_isolated_child's own `-I` isolation).
        body = """
try:
    installer.verify()
except installer.InstallError as exc:
    message = str(exc)
    assert "cannot verify write-trigger liveness" in message, message
    assert "write_candidate_capture module unavailable" in message, message
else:
    raise SystemExit("FAIL: verify() unexpectedly succeeded with write_candidate_capture unimportable")
"""
        result = self._run_isolated_child(body)
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertIn("ISOLATED-OK", result.stdout)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class WriteCandidateCaptureImportErrorGuardTests(unittest.TestCase):
    """Round-54 fix (independent Claude opus5/max GO + Codex gpt-5.6-sol/max NO-GO dual review,
    2026-08-21, issues 1-3 from that round). Three gaps in how this file's 3 call sites that lazily
    import write_candidate_capture (via _import_write_candidate_capture()) handle an import-time
    failure:

      1. [Issue 1] The exc.name-blind message on verify()'s own guard (round-53 fix) always blamed
         write_candidate_capture, even when write_candidate_capture.py itself was genuinely present
         and importable but ITS OWN `import claude_memory_hook` failed -- the real missing module's
         name was visible only inside the parenthetical `(exc)` suffix, not the message's own claim
         about which module was unavailable.
      2. [Issue 2] _load_write_trigger_config() and _validate_write_trigger_argument() made the
         identical unguarded _import_write_candidate_capture() call verify() had before round-53 --
         so `install-write-trigger` (dry-run and real alike) still raised a raw, uncaught
         ModuleNotFoundError with write_candidate_capture.py genuinely absent, the same failure
         shape round-53 fixed for verify() but never propagated to this action's own entry points.
      3. [Issue 3] All 3 call sites caught only ModuleNotFoundError, not SyntaxError -- a corrupted
         or half-written write_candidate_capture.py (an interrupted copy, a bad merge) raises
         SyntaxError at import time, not ModuleNotFoundError, and was uncaught everywhere.

    Every test here runs the guarded call in a genuinely fresh, `-I`-isolated subprocess whose only
    source of `write_candidate_capture` is this test's own fixture directory -- never this test
    process's own sys.path/sys.modules (mirrors BaseOnlyPathsWithoutWriteCandidateCaptureModuleTests'
    own rationale, above: an in-process mock.patch-based test cannot tell "module genuinely
    missing/broken" apart from "already cached in sys.modules from an earlier import in this same
    process").
    """

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.temp = self._stack.enter_context(tempfile.TemporaryDirectory())
        self.isolated_dir = Path(self.temp) / "isolated"
        self.isolated_dir.mkdir()
        shutil.copy2(installer.__file__, self.isolated_dir / "install_bridge.py")
        self.policy_path = Path(self.temp) / "write-trigger-policy.json"
        self.policy_path.write_bytes(InstallBridgeTests._write_trigger_policy_bytes())

    def _write_transitive_failure_fixture(self) -> None:
        # write_candidate_capture.py genuinely present and importable in isolation, but its own
        # `import claude_memory_hook` fails: copies the REAL write_candidate_capture.py (so its
        # content, and thus its own `import claude_memory_hook`, is exactly what production runs)
        # into self.isolated_dir WITHOUT also copying its sibling claude_memory_hook.py --
        # reproduces the transitive-dependency gap specifically, as the task calls for ("hiding/
        # renaming claude_memory_hook.py specifically, not write_candidate_capture.py"). Distinct
        # from the module-itself-missing scenario (below), which never writes this file at all.
        real_write_candidate_capture = Path(installer.__file__).resolve().parent / "write_candidate_capture.py"
        shutil.copy2(real_write_candidate_capture, self.isolated_dir / "write_candidate_capture.py")
        self.assertFalse((self.isolated_dir / "claude_memory_hook.py").exists())  # fixture precondition

    def _write_corrupted_module_fixture(self) -> None:
        (self.isolated_dir / "write_candidate_capture.py").write_text("def broken(:\n")

    def _run_isolated(self, body: str) -> subprocess.CompletedProcess:
        script = f"""
import sys
sys.path.insert(0, {str(self.isolated_dir)!r})
from pathlib import Path
import install_bridge as installer

{body}

print("ISOLATED-OK")
"""
        script_path = Path(self.temp) / "child.py"
        script_path.write_text(script)
        # -I: isolated mode, exactly like BaseOnlyPathsWithoutWriteCandidateCaptureModuleTests'
        # own _run_isolated_child -- the only way this subprocess can find `write_candidate_capture`
        # at all is via self.isolated_dir, which each fixture method above controls precisely.
        return subprocess.run(
            [sys.executable, "-I", os.fspath(script_path)], capture_output=True, text=True, timeout=60
        )

    def _assert_clean_and_closed(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertIn("ISOLATED-OK", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    # -- Issue 1: exc.name-based message distinguishes the two ModuleNotFoundError shapes ---------

    def test_transitive_claude_memory_hook_failure_is_named_correctly_not_write_candidate_capture(self) -> None:
        self._write_transitive_failure_fixture()
        body = """
try:
    installer._import_write_candidate_capture()
except (ModuleNotFoundError, SyntaxError, ImportError) as exc:
    err = installer._wrap_write_candidate_capture_import_error(exc, "cannot verify write-trigger liveness")
    message = str(err)
    assert "claude_memory_hook" in message, message
    assert "write_candidate_capture.py exists but failed to import" in message, message
    # The pre-fix wording always claimed write_candidate_capture ITSELF was unavailable, even here
    # -- the actual bug this test guards against.
    assert "write_candidate_capture module unavailable" not in message, message
else:
    raise SystemExit("FAIL: import unexpectedly succeeded -- fixture isolation broken")
"""
        result = self._run_isolated(body)
        self._assert_clean_and_closed(result)

    def test_write_candidate_capture_itself_missing_keeps_the_original_wording(self) -> None:
        # write_candidate_capture.py is never written into self.isolated_dir at all here --
        # confirms the Issue 1 fix did not change behavior for the pre-existing, already-tested
        # module-itself-missing scenario (verify()'s own end-to-end coverage of this exact wording
        # lives in BaseOnlyPathsWithoutWriteCandidateCaptureModuleTests, above).
        body = """
try:
    installer._import_write_candidate_capture()
except (ModuleNotFoundError, SyntaxError, ImportError) as exc:
    err = installer._wrap_write_candidate_capture_import_error(exc, "cannot verify write-trigger liveness")
    message = str(err)
    assert message == (
        "cannot verify write-trigger liveness: write_candidate_capture module unavailable "
        "(No module named 'write_candidate_capture')"
    ), message
else:
    raise SystemExit("FAIL: import unexpectedly succeeded -- fixture isolation broken")
"""
        result = self._run_isolated(body)
        self._assert_clean_and_closed(result)

    # -- Issue 2: _load_write_trigger_config() and _validate_write_trigger_argument() are guarded --

    def test_load_write_trigger_config_fails_closed_not_raw_modulenotfounderror(self) -> None:
        # write_candidate_capture.py is genuinely absent from self.isolated_dir.
        body = f"""
try:
    installer._load_write_trigger_config(Path({str(self.policy_path)!r}))
except installer.InstallError as exc:
    assert "write_candidate_capture module unavailable" in str(exc), str(exc)
else:
    raise SystemExit("FAIL: _load_write_trigger_config() unexpectedly succeeded")
"""
        result = self._run_isolated(body)
        self._assert_clean_and_closed(result)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_validate_write_trigger_argument_fails_closed_not_raw_modulenotfounderror(self) -> None:
        # write_candidate_capture.py is genuinely absent from self.isolated_dir.
        body = """
write_trigger = {
    "bridge_id": installer.WRITE_TRIGGER_BRIDGE_ID,
    "max_candidates_per_project": 50,
    "max_candidate_bytes": 1500,
}
try:
    installer._validate_write_trigger_argument(write_trigger)
except installer.InstallError as exc:
    assert "write_candidate_capture module unavailable" in str(exc), str(exc)
else:
    raise SystemExit("FAIL: _validate_write_trigger_argument() unexpectedly succeeded")
"""
        result = self._run_isolated(body)
        self._assert_clean_and_closed(result)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_install_write_trigger_action_fails_closed_dry_run_and_real_not_raw_modulenotfounderror(self) -> None:
        # Reproduces the action's own contract, not just the underlying helper: both dry_run=True
        # (the --dry-run CLI flag's own code path, calling plan()) and dry_run=False (the real
        # install() path) must fail closed the same way, since _load_write_trigger_config() runs
        # before either branch. Calls install_write_trigger() directly rather than through main()'s
        # own argparse dispatch for the dry_run=False case -- that path additionally acquires
        # main()'s exclusive lock first (unrelated machinery this fix does not touch), and the
        # failure under test here is entirely inside _load_write_trigger_config(), before install()
        # or plan() -- and therefore before dry_run is even branched on -- ever run.
        body = f"""
for dry_run in (True, False):
    try:
        installer.install_write_trigger(Path({str(self.policy_path)!r}), dry_run=dry_run)
    except installer.InstallError as exc:
        assert "write_candidate_capture module unavailable" in str(exc), (dry_run, str(exc))
    else:
        raise SystemExit(f"FAIL: install_write_trigger(dry_run={{dry_run}}) unexpectedly succeeded")
"""
        result = self._run_isolated(body)
        self._assert_clean_and_closed(result)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    # -- Issue 3: SyntaxError (a corrupted module) is caught at all 3 guarded call sites -----------

    def test_syntaxerror_from_corrupted_module_is_caught_at_verify_call_site(self) -> None:
        self._write_corrupted_module_fixture()
        body = """
try:
    installer._import_write_candidate_capture()
except (ModuleNotFoundError, SyntaxError, ImportError) as exc:
    err = installer._wrap_write_candidate_capture_import_error(exc, "cannot verify write-trigger liveness")
    message = str(err)
    assert "write_candidate_capture module exists but failed to import" in message, message
    assert "invalid syntax" in message, message
else:
    raise SystemExit("FAIL: import unexpectedly succeeded -- fixture isolation broken")
"""
        result = self._run_isolated(body)
        self._assert_clean_and_closed(result)
        self.assertNotIn("SyntaxError", result.stderr)

    def test_syntaxerror_from_corrupted_module_is_caught_at_load_write_trigger_config(self) -> None:
        self._write_corrupted_module_fixture()
        body = f"""
try:
    installer._load_write_trigger_config(Path({str(self.policy_path)!r}))
except installer.InstallError as exc:
    assert "write_candidate_capture module exists but failed to import" in str(exc), str(exc)
    assert "invalid syntax" in str(exc), str(exc)
else:
    raise SystemExit("FAIL: _load_write_trigger_config() unexpectedly succeeded")
"""
        result = self._run_isolated(body)
        self._assert_clean_and_closed(result)
        self.assertNotIn("SyntaxError", result.stderr)

    def test_syntaxerror_from_corrupted_module_is_caught_at_validate_write_trigger_argument(self) -> None:
        self._write_corrupted_module_fixture()
        body = """
write_trigger = {
    "bridge_id": installer.WRITE_TRIGGER_BRIDGE_ID,
    "max_candidates_per_project": 50,
    "max_candidate_bytes": 1500,
}
try:
    installer._validate_write_trigger_argument(write_trigger)
except installer.InstallError as exc:
    assert "write_candidate_capture module exists but failed to import" in str(exc), str(exc)
    assert "invalid syntax" in str(exc), str(exc)
else:
    raise SystemExit("FAIL: _validate_write_trigger_argument() unexpectedly succeeded")
"""
        result = self._run_isolated(body)
        self._assert_clean_and_closed(result)
        self.assertNotIn("SyntaxError", result.stderr)


class InstallWriteTriggerRealCommandEndToEndTests(unittest.TestCase):
    """P0 regression: an independent review found the registered SessionEnd
    command was a permanent, silent no-op in production. Root cause:
    write_candidate_capture.default_write_candidates_root() (invoked when no
    `write_candidates_root` is passed explicitly) lazily does `import
    install_bridge` to derive the default write-candidates storage path --
    which works from this repo's own working tree, and in every pre-existing
    test (every call site passes `write_candidates_root` explicitly,
    bypassing this function entirely), but NOT inside the isolated release
    directory the REGISTERED command actually runs from, which contains only
    write_candidate_capture.py + policy.json -- never install_bridge.py. The
    resulting ModuleNotFoundError was silently swallowed by scan()'s own
    fail-closed `except Exception: return None`: exit 0, empty stdout/
    stderr, write-candidates/ never created, forever, on every future
    invocation, with no error surfaced anywhere.

    Fix: install_bridge.py's make_release() now resolves the correct
    write-candidates-root itself, at install/registration time (it already
    has RUNTIME_BASE, the one constant default_write_candidates_root()
    derives from, directly in scope), and bakes it into the registered
    command as an explicit `--write-candidates-root` flag -- the same
    pattern `--policy`/`--expected-policy-sha256`/`--expected-script-sha256`/
    `--bridge-id` already use. write_candidate_capture.py's own `_main_scan`
    now accepts that flag as an optional 5th `--flag value` pair.

    Unlike InstallWriteTriggerEndToEndTests above (whose setUp() installs a
    one-line STUB in place of write_candidate_capture.py -- sufficient for
    testing installation mechanics, but incapable of reproducing this bug: a
    stub has no `import install_bridge` to fail), this class installs the
    REAL claude_memory_hook.py and the REAL write_candidate_capture.py, then
    executes the ACTUAL registered command as a REAL subprocess -- exactly
    the reproduction method the independent review used -- against a real
    transcript file containing a genuine human correction.

    One deliberate, narrow exception to "every module-level path constant
    is the fixture's own mocked temp path": `SSD_ROOT` itself is NOT mocked
    away from the real `/Volumes/Extreme SSD` mount point here, unlike every
    other test class in this file. `claude_memory_hook._disk_volume_uuid`
    (the volume-UUID check `hook.verify_storage` runs, independently, at
    real scan time -- not the separate `install_bridge.volume_uuid` every
    other test mocks away) shells out to the REAL `diskutil info -plist
    <ssd_root>`, which only resolves a UUID for an actual mounted volume's
    own path, not an arbitrary fake subdirectory -- confirmed empirically
    while building this test (a fake `ssd_root` under a plain tempdir
    produces `diskutil: returned non-zero exit status 1` /
    `BridgeError("cannot verify SSD volume")` inside the REAL subprocess,
    not a reproduction of the bug under test). This is the one thing a real,
    unmocked subprocess run cannot avoid depending on: it genuinely needs to
    run on a machine with this exact volume mounted, matching this whole
    project's own hardcoded `SSD_ROOT = Path("/Volumes/Extreme SSD")`
    portability assumption (install_bridge.py:90). `LOCAL_HOMES_ROOT`,
    `RUNTIME_BASE`, `SOURCE_SCRIPT`, `WRITE_TRIGGER_SOURCE_SCRIPT`, and
    `Path.home` remain fully isolated under a throwaway temp directory
    (itself created under `/Volumes/Extreme SSD`, cleaned up in `tearDown`)
    that is NOT the real production `Orca/local-homes` tree -- no real
    Codex/Claude config, no real installed bridge state, is ever touched.
    `installer.volume_uuid` is deliberately left UNMOCKED too (unlike every
    other test class): both the install-time computation and the real
    scan-time re-verification independently call the real `diskutil` against
    the real mount and must naturally agree, the most faithful reproduction
    possible -- mocking one side without the other is exactly what silently
    hid the second gap (`atomic_write`'s own lazy import, see that
    function's docstring) this test discovered.
    """

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        # Anchored under the REAL /Volumes/Extreme SSD mount -- see this class's own docstring for
        # why SSD_ROOT cannot be faked here the way every other test class in this file fakes it.
        # `Orca/tmp` (not the SSD root itself, which is not user-writable) is this same project's
        # own established scratch location on this volume.
        self.temp = self._stack.enter_context(
            tempfile.TemporaryDirectory(prefix="p0-real-command-repro-", dir="/Volumes/Extreme SSD/Orca/tmp")
        )
        root = Path(self.temp).resolve()
        ssd_root = Path("/Volumes/Extreme SSD")
        local_homes = root / "local-homes"
        runtime_base = local_homes / ".shared-runtime/claude-codex-memory-bridge"
        pending_path = runtime_base / "pending-install.json"
        source_dir = root / "source"
        source_script = source_dir / "claude_memory_hook.py"
        write_trigger_source_script = source_dir / "write_candidate_capture.py"
        claude_projects = local_homes / ".claude/projects"

        local_homes.mkdir(parents=True)
        (local_homes / ".codex").mkdir()
        claude_projects.mkdir(parents=True)
        (local_homes / "codex-accounts/acct-one/home").mkdir(parents=True)
        source_dir.mkdir()

        InstallEndToEndTests._write(local_homes / ".codex/hooks.json", InstallEndToEndTests._base_hooks_json())
        InstallEndToEndTests._write(
            local_homes / "codex-accounts/acct-one/home/hooks.json", InstallEndToEndTests._base_hooks_json()
        )
        # The REAL claude_memory_hook.py AND the REAL write_candidate_capture.py, both copied
        # byte-for-byte -- unlike InstallWriteTriggerEndToEndTests' stubs, write_candidate_capture.py
        # does `import claude_memory_hook as hook` at its own module level and calls real attributes
        # off it (e.g. `hook._disk_volume_uuid`) as function-default arguments evaluated at import
        # time, so a one-line stub fails this test in an unrelated way (ImportError/AttributeError
        # before scan() is ever reached) rather than reproducing the actual P0 bug under test.
        InstallEndToEndTests._write(source_script, Path(hook.__file__).read_bytes())
        InstallEndToEndTests._write(write_trigger_source_script, Path(wtc.__file__).read_bytes())

        self._stack.enter_context(mock.patch.object(installer, "SSD_ROOT", ssd_root))
        self._stack.enter_context(mock.patch.object(installer, "LOCAL_HOMES_ROOT", local_homes))
        self._stack.enter_context(mock.patch.object(installer, "RUNTIME_BASE", runtime_base))
        self._stack.enter_context(mock.patch.object(installer, "PENDING_PATH", pending_path))
        self._stack.enter_context(mock.patch.object(installer, "SOURCE_SCRIPT", source_script))
        self._stack.enter_context(
            mock.patch.object(installer, "WRITE_TRIGGER_SOURCE_SCRIPT", write_trigger_source_script)
        )
        self._stack.enter_context(mock.patch.object(Path, "home", lambda: local_homes))

        self.runtime_base = runtime_base
        self.claude_projects = claude_projects
        self.main_config = local_homes / ".codex/hooks.json"

        self.policy_path = root / "write-trigger-policy.json"
        self.policy_path.write_bytes(
            json.dumps(
                {"write_trigger": {"enabled": True, "max_candidates_per_project": 50, "max_candidate_bytes": 1500}}
            ).encode()
        )

    def test_registered_session_end_command_writes_a_real_pending_candidate(self) -> None:
        installer.install_write_trigger(self.policy_path)
        payload = json.loads(self.main_config.read_bytes())
        session_end_handlers = payload["hooks"]["SessionEnd"]
        self.assertEqual(len(session_end_handlers), 1)
        command = session_end_handlers[0]["hooks"][0]["command"]
        # The whole point of this test: assert on the REGISTERED command exactly as install()
        # wrote it, never a hand-built equivalent.
        self.assertIn("--write-candidates-root", command)

        # A real transcript, under the fixture's own mocked ~/.claude/projects, containing a
        # genuine human correction (the exact text TriggerMatchingTests.test_t1_explicit_correction
        # already proves triggers a real T1 match).
        cwd = "/Users/tester/p0-repro-project"
        project_dirname = hook.claude_project_dirname(cwd)
        project_dir = self.claude_projects / project_dirname
        project_dir.mkdir(mode=0o700, parents=True)
        session_id = "33333333-3333-3333-3333-333333333333"
        transcript_path = project_dir / f"{session_id}.jsonl"
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I'll delete the old config file to clean things up."}],
                },
                "sessionId": session_id,
                "cwd": cwd,
                "uuid": "uuid-0",
                "parentUuid": None,
                "isSidechain": False,
                "timestamp": "2026-08-20T12:00:00.000Z",
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "No, don't delete it, revert that -- we still need it for the legacy importer.",
                },
                "sessionId": session_id,
                "cwd": cwd,
                "uuid": "uuid-1",
                "parentUuid": "uuid-0",
                "isSidechain": False,
                "timestamp": "2026-08-20T12:00:01.000Z",
            },
        ]
        with transcript_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        transcript_path.chmod(0o600)

        stdin_payload = json.dumps({"hook_event_name": "SessionEnd", "cwd": cwd, "session_id": session_id}).encode()

        # The REAL registered command, executed as a REAL subprocess -- not scan() called directly
        # in-process, not a hand-simplified equivalent. This is exactly the reproduction method the
        # independent review used to find the original bug: before the P0 fix, this exact command
        # exited 0 with empty stdout/stderr and never created write-candidates/ at all.
        result = subprocess.run(command, shell=True, input=stdin_payload, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

        write_candidates_root = self.runtime_base / "write-candidates"
        self.assertTrue(
            write_candidates_root.exists(), "the registered command must actually create write-candidates/"
        )
        project_ref = wtc.project_ref_for(project_dir)
        pending = wtc.list_pending(write_candidates_root, project_ref)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["trigger"], "T1")


class RealOrcaAccountRegistryEnumerationTests(unittest.TestCase):
    # Regression coverage for the P1 fixed on 2026-08-27 (see install_bridge.ORCA_ACCOUNTS_SUBPATH):
    # account enumeration was rooted at `LOCAL_HOMES_ROOT / "codex-accounts"`, a stale snapshot of
    # Orca's real registry, and verify() -- which only ever re-checked the receipt install() built
    # from that same enumeration -- reported `ok: true` while an account that genuinely exists and
    # is genuinely running had no bridge handler at all.
    #
    # Unlike InstallEndToEndTests, this fixture puts the HOME DIRECTORY OUTSIDE the fake SSD, which
    # is the shape the real machine has: `~` is on the internal disk, `~/.codex` is a symlink into
    # the SSD's local-homes tree, and `~/Library/Application Support/orca/codex-accounts/<id>/home`
    # is EITHER a symlink into local-homes (older accounts) OR a real directory that never touches
    # the SSD (newer ones). Only with the home outside the SSD can the second shape be reproduced
    # at all -- the exact reason it is worth a fixture of its own.

    def setUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.temp = self._stack.enter_context(tempfile.TemporaryDirectory())
        root = Path(self.temp).resolve()

        ssd_root = root / "ssd"
        local_homes = ssd_root / "Orca/local-homes"
        runtime_base = local_homes / ".shared-runtime/claude-codex-memory-bridge"
        source_dir = ssd_root / "source"
        source_script = source_dir / "claude_memory_hook.py"

        local_homes.mkdir(parents=True)
        (local_homes / ".codex").mkdir()
        (local_homes / ".claude/projects").mkdir(parents=True)
        (local_homes / "codex-accounts/acct-symlinked/home").mkdir(parents=True)
        source_dir.mkdir()

        # The home directory itself: deliberately NOT under ssd_root.
        home = root / "home"
        home.mkdir()
        (home / ".codex").symlink_to(local_homes / ".codex")
        (home / ".claude").mkdir()
        (home / ".claude/projects").symlink_to(local_homes / ".claude/projects")
        # Spelled out literally rather than read from installer.ORCA_ACCOUNTS_SUBPATH, so this
        # fixture reproduces the real machine's layout independently of the module under test --
        # against the pre-fix module these tests then fail on the actual defect (verify() reporting
        # ok: true over an account it cannot see) rather than on a missing attribute.
        # test_registry_subpath_constant_matches_the_real_orca_layout pins the two together.
        registry = home / "Library/Application Support/orca/codex-accounts"
        registry.mkdir(parents=True)

        # Account 1: registry entry whose `home` is a symlink into local-homes. This is the shape
        # the old, local-homes-only enumeration could already see, and it must keep working.
        (registry / "acct-symlinked" / "home").parent.mkdir()
        (registry / "acct-symlinked/home").symlink_to(local_homes / "codex-accounts/acct-symlinked/home")

        self._write(local_homes / ".codex/hooks.json", self._base_hooks_json())
        self._write(local_homes / "codex-accounts/acct-symlinked/home/hooks.json", self._base_hooks_json())
        self._write(source_script, b"#!/usr/bin/env python3\n# fixture hook script\n")

        self._stack.enter_context(mock.patch.object(installer, "SSD_ROOT", ssd_root))
        self._stack.enter_context(mock.patch.object(installer, "LOCAL_HOMES_ROOT", local_homes))
        self._stack.enter_context(mock.patch.object(installer, "RUNTIME_BASE", runtime_base))
        self._stack.enter_context(
            mock.patch.object(installer, "PENDING_PATH", runtime_base / "pending-install.json")
        )
        self._stack.enter_context(mock.patch.object(installer, "SOURCE_SCRIPT", source_script))
        self._stack.enter_context(
            mock.patch.object(installer, "volume_uuid", lambda ssd_root=None: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
        )
        self._stack.enter_context(mock.patch.object(Path, "home", lambda: home))

        self.local_homes = local_homes
        self.home = home
        self.registry = registry
        self.main_config = local_homes / ".codex/hooks.json"
        self.symlinked_config = local_homes / "codex-accounts/acct-symlinked/home/hooks.json"

    @staticmethod
    def _write(path: Path, raw: bytes, mode: int = 0o644) -> None:
        path.write_bytes(raw)
        path.chmod(mode)

    @staticmethod
    def _base_hooks_json() -> bytes:
        return (
            json.dumps(
                {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}]}},
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()

    def _add_offssd_registry_account(self, account_id: str) -> Path:
        # An Orca account that exists ONLY in the real registry: its `home` is a real directory that
        # is not on the SSD and has no local-homes counterpart at all. This is the exact shape of
        # the live account the old enumeration was blind to.
        account_home = self.registry / account_id / "home"
        account_home.mkdir(parents=True)
        config = account_home / "hooks.json"
        self._write(config, self._base_hooks_json())
        self.assertFalse(
            (self.local_homes / "codex-accounts" / account_id).exists(),
            "fixture precondition: this account must NOT exist in the local-homes tree",
        )
        return config

    def test_registry_subpath_constant_matches_the_real_orca_layout(self) -> None:
        # Pins the constant to the literal path this fixture (and the real machine) uses, so the
        # module and these tests cannot drift apart silently.
        self.assertEqual(installer.ORCA_ACCOUNTS_SUBPATH, "Library/Application Support/orca/codex-accounts")
        self.assertEqual(installer.orca_accounts_root(), self.registry)

    def test_registry_is_authoritative_and_local_homes_only_resolves_homes(self) -> None:
        # Baseline: registry + local-homes agree, everything is manageable.
        self.assertEqual(
            installer._enumerate_hook_configs(),
            [self.main_config, self.symlinked_config],
        )
        _, registry_accounts = installer._enumerate_accounts()
        self.assertEqual(registry_accounts, {"acct-symlinked": self.symlinked_config})

        # Now the account that exists only in the live registry, off the SSD.
        ghost_config = self._add_offssd_registry_account("acct-offssd")
        # It is genuinely unmanageable (nothing this tool may write to), so it must NOT appear as a
        # managed config...
        self.assertEqual(
            installer._enumerate_hook_configs(),
            [self.main_config, self.symlinked_config],
        )
        # ...but it must no longer be invisible: the registry knows it exists.
        _, registry_accounts = installer._enumerate_accounts()
        self.assertEqual(
            registry_accounts,
            {"acct-symlinked": self.symlinked_config, "acct-offssd": None},
        )
        self.assertTrue(ghost_config.exists())

    def test_verify_is_not_ok_for_a_registry_account_it_cannot_manage(self) -> None:
        # The regression itself. Install against the accounts this tool can see, confirm verify()
        # is healthy, then make one more account appear in the live Orca registry only.
        installer.install()
        healthy = installer.verify()
        self.assertTrue(healthy["ok"])
        # .get() only for this precondition line, so that against the pre-fix module the FIRST
        # failure this test produces is the defect itself (`ok: true` below), not a missing key.
        self.assertEqual(healthy.get("unmanaged", []), [])

        self._add_offssd_registry_account("acct-offssd")

        result = installer.verify()
        # Before the fix this asserted-on value was True: verify() re-checked only the receipt
        # install() had built from the same blind enumeration, so a whole live account being absent
        # from `configs` was reported as success.
        self.assertFalse(result["ok"])
        self.assertEqual([entry["account_id"] for entry in result["unmanaged"]], ["acct-offssd"])
        self.assertEqual(
            result["unmanaged"][0]["registry_path"],
            os.fspath(self.registry / "acct-offssd"),
        )
        self.assertIn("Extreme SSD", result["unmanaged"][0]["reason"])
        # The configs it CAN manage are still reported, and still healthy -- this is an added
        # finding, not a loss of the existing output.
        self.assertEqual(
            sorted(result["configs"]),
            sorted(os.fspath(p) for p in (self.main_config, self.symlinked_config)),
        )

    def test_verify_is_not_ok_for_a_manageable_registry_account_missing_from_the_receipt(self) -> None:
        # The other half of fail-closed coverage: an account this tool COULD manage, that appeared
        # after the last install, must not be waved through as verified either.
        installer.install()
        self.assertTrue(installer.verify()["ok"])

        (self.local_homes / "codex-accounts/acct-late/home").mkdir(parents=True)
        self._write(self.local_homes / "codex-accounts/acct-late/home/hooks.json", self._base_hooks_json())
        (self.registry / "acct-late").mkdir()
        (self.registry / "acct-late/home").symlink_to(self.local_homes / "codex-accounts/acct-late/home")

        result = installer.verify()
        self.assertFalse(result["ok"])
        self.assertEqual([entry["account_id"] for entry in result["unmanaged"]], ["acct-late"])
        self.assertIn("not covered by the install receipt", result["unmanaged"][0]["reason"])

        # Re-installing brings it under management and verify() goes healthy again.
        installer.install()
        healed = installer.verify()
        self.assertTrue(healed["ok"])
        self.assertEqual(healed["unmanaged"], [])
        self.assertEqual(len(healed["configs"]), 3)

    def test_verify_cli_action_exits_nonzero_when_an_account_is_unmanaged(self) -> None:
        installer.install()
        self._add_offssd_registry_account("acct-offssd")
        with mock.patch.object(sys, "argv", ["install_bridge.py", "verify"]), contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout:
            exit_code = installer.main()
        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual([entry["account_id"] for entry in payload["unmanaged"]], ["acct-offssd"])

    def test_missing_account_registry_falls_back_to_the_local_homes_tree(self) -> None:
        # A machine with no Orca account registry at all (an older layout, or a test fixture) must
        # keep enumerating exactly as it always did, with no spurious unmanaged findings.
        shutil.rmtree(self.registry)
        self.assertEqual(
            installer._enumerate_hook_configs(),
            [self.main_config, self.symlinked_config],
        )
        installer.install()
        result = installer.verify()
        self.assertTrue(result["ok"])
        self.assertEqual(result["unmanaged"], [])

    def _add_shadowed_offssd_registry_account(self, account_id: str) -> tuple[Path, Path]:
        # The exact shape of the P1 found by independent review on 2026-08-28: the SAME account id
        # exists BOTH in the live Orca registry -- with a real, off-SSD home this tool may not touch
        # -- AND in the SSD's legacy `local-homes/codex-accounts` snapshot. The two are unrelated
        # directories that merely share a name; nothing makes the snapshot a copy of, or a stand-in
        # for, the live account's configuration.
        live_home = self.registry / account_id / "home"
        live_home.mkdir(parents=True)
        live_config = live_home / "hooks.json"
        self._write(live_config, self._base_hooks_json())

        legacy_home = self.local_homes / "codex-accounts" / account_id / "home"
        legacy_home.mkdir(parents=True)
        legacy_config = legacy_home / "hooks.json"
        self._write(legacy_config, self._base_hooks_json())

        self.assertFalse(
            live_home.is_symlink(),
            "fixture precondition: the live account's home must be a real off-SSD directory",
        )
        return live_config, legacy_config

    def _mentions_bridge(self, config: Path) -> bool:
        return installer.BRIDGE_ID in config.read_text()

    def test_live_registry_account_is_never_satisfied_by_a_same_id_legacy_snapshot(self) -> None:
        # Regression for the P1 of 2026-08-28. `_resolve_account_hook_config()` tried the registry
        # path and then the legacy `local-homes/codex-accounts` path UNCONDITIONALLY, so an account
        # that really is in the live registry but whose real home lives off the SSD was silently
        # "resolved" to whatever same-id directory the legacy snapshot happened to contain. That
        # snapshot then got the bridge handler, the receipt covered it, verify() found the account
        # covered and reported ok: true -- while the account Orca actually runs still had no
        # redaction hook. That is the ORIGINAL P1 (verify green over an unprotected live account)
        # reproduced through a second code path, so it must fail closed exactly the same way.
        live_config, legacy_config = self._add_shadowed_offssd_registry_account("acct-shadowed")

        # Resolution itself: a live-registry id must resolve to the live account's own hooks.json or
        # to nothing at all. It must never resolve to the same-id legacy snapshot.
        _, registry_accounts = installer._enumerate_accounts()
        self.assertIn("acct-shadowed", registry_accounts)
        self.assertIsNone(
            registry_accounts["acct-shadowed"],
            "a live registry account whose own home is unreachable must resolve to None, "
            "never to a same-id legacy snapshot",
        )

        installer.install()
        result = installer.verify()

        # Before the fix: ok=True, unmanaged=[].
        self.assertFalse(result["ok"])
        self.assertEqual([entry["account_id"] for entry in result["unmanaged"]], ["acct-shadowed"])
        self.assertEqual(
            result["unmanaged"][0]["registry_path"],
            os.fspath(self.registry / "acct-shadowed"),
        )
        self.assertIn("Extreme SSD", result["unmanaged"][0]["reason"])
        # And the report must not name the legacy snapshot as this account's config -- that claim is
        # precisely the falsehood being fixed.
        self.assertNotIn("config", result["unmanaged"][0])

        # The ground truth the whole fix exists to protect: the account Orca actually runs has no
        # bridge handler, so verify() must not be green -- whatever the legacy snapshot contains.
        self.assertFalse(self._mentions_bridge(live_config))
        self.assertTrue(legacy_config.exists())

        # The accounts this tool genuinely can manage are still managed and still reported.
        self.assertEqual(
            sorted(result["configs"]),
            sorted(os.fspath(p) for p in (self.main_config, self.symlinked_config, legacy_config)),
        )

    def test_legacy_only_account_still_resolves_through_the_local_homes_tree(self) -> None:
        # The complement, pinning the narrowed fallback's remaining scope: an id ABSENT from the
        # live registry is not a live account being shadowed, it is a leftover this tool has always
        # been able to manage, and the local-homes fallback must keep resolving it. Without this the
        # fix above could be "achieved" by deleting the fallback outright.
        legacy_home = self.local_homes / "codex-accounts/acct-legacy-only/home"
        legacy_home.mkdir(parents=True)
        legacy_config = legacy_home / "hooks.json"
        self._write(legacy_config, self._base_hooks_json())
        self.assertFalse((self.registry / "acct-legacy-only").exists())

        configs, registry_accounts = installer._enumerate_accounts()
        self.assertIn(legacy_config, configs)
        # Not a registry account, so it is not held to registry coverage and raises no finding.
        self.assertNotIn("acct-legacy-only", registry_accounts)

        installer.install()
        result = installer.verify()
        self.assertTrue(result["ok"])
        self.assertEqual(result["unmanaged"], [])
        self.assertTrue(self._mentions_bridge(legacy_config))


if __name__ == "__main__":
    unittest.main()
