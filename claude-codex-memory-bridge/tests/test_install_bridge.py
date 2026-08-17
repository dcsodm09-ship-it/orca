from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

import install_bridge as installer


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

    def test_verify_detects_hook_config_drift(self) -> None:
        installer.install()
        # Something outside this installer edits the config after install.
        payload = json.loads(self.main_config.read_bytes())
        payload["hooks"]["UserPromptSubmit"].append({"hooks": [{"type": "command", "command": "/bin/echo hi"}]})
        drifted = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        self.main_config.chmod(0o600)
        self.main_config.write_bytes(drifted)
        self.main_config.chmod(0o600)
        with self.assertRaises(installer.InstallError):
            installer.verify()

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

    def test_uninstall_tolerates_an_unrecognizable_file_inside_its_own_runtime_tree(self) -> None:
        # Round-11 self-check (2026-08-18, round-2 pressure-test finding):
        # closing the RUNTIME_BASE blind spot above by simply walking in
        # created a new lockout -- a single oversized or unparseable-under-
        # every-supported-encoding file happening to be named "hooks.json"
        # anywhere under RUNTIME_BASE (whose backups/releases directories
        # are, by this file's own design, never pruned and accumulate for
        # the tool's whole lifetime) would permanently hard-block both
        # uninstall() and recover_pending_install() with no self-healing
        # path, since nothing in this tool ever removes RUNTIME_BASE
        # content. Because every real file the bridge itself ever writes
        # there is never named literally "hooks.json", and RUNTIME_BASE is
        # 0o700/uid-exclusive, this ambiguity is deliberately tolerated
        # (treated as "nothing found there") specifically inside
        # RUNTIME_BASE -- unlike the equivalent case elsewhere under
        # local-homes, which stays fail-closed (see the sibling test
        # immediately below).
        installer.install()
        stray_dir = self.runtime_base / "backups/misc-staging/stray"
        stray_dir.mkdir(parents=True)
        stray_config = stray_dir / "hooks.json"
        # Not valid JSON under any supported encoding, but contains the
        # literal marker text as incidental byte padding -- exactly the
        # ambiguous-content shape _contains_owned_handler()'s Stage 2 would
        # otherwise fail closed on.
        stray_config.write_bytes(
            b"not valid json padding " * 3000 + f"--bridge-id {installer.BRIDGE_ID}".encode() + b" more padding"
        )
        self.assertGreater(stray_config.stat().st_size, 65_536)  # past the structural-detection size bound

        result = installer.uninstall()
        self.assertTrue(result["ok"])
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


if __name__ == "__main__":
    unittest.main()
