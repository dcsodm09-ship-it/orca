from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

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
            before = b'{"hooks":{"UserPromptSubmit":[]}}\n'
            after = b'{"hooks":{"UserPromptSubmit":[{"hooks":[]}]}}\n'
            installer.atomic_write(config, after, 0o600)
            installer.atomic_write(backup, before, 0o600)
            receipt = {
                "schema": installer.RECEIPT_SCHEMA,
                "bridge_id": installer.BRIDGE_ID,
                "install_id": "install-1",
                "configs": [
                    {
                        "path": os.fspath(config),
                        "backup": os.fspath(backup),
                        "before_sha256": installer.sha256_bytes(before),
                        "after_sha256": installer.sha256_bytes(after),
                        "before_mode": 0o644,
                        "after_mode": 0o600,
                    }
                ],
            }
            pending = runtime / "pending-install.json"
            installer.atomic_write(
                pending,
                installer.canonical_json(
                    {"schema": installer.JOURNAL_SCHEMA, "receipt": receipt}
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
            before = b'{"hooks":{"UserPromptSubmit":[]}}\n'
            after = b'{"hooks":{"UserPromptSubmit":[{"hooks":[]}]}}\n'
            installer.atomic_write(config, after, 0o600)
            installer.atomic_write(backup, before, 0o600)
            receipt = {
                "schema": installer.RECEIPT_SCHEMA,
                "bridge_id": installer.BRIDGE_ID,
                "install_id": "install-2",
                "configs": [
                    {
                        "path": os.fspath(config),
                        "backup": os.fspath(backup),
                        "before_sha256": installer.sha256_bytes(before),
                        "after_sha256": installer.sha256_bytes(after),
                        "before_mode": 0o644,
                        "after_mode": 0o600,
                    }
                ],
            }
            pending = runtime / "pending-install.json"
            installer.atomic_write(
                pending,
                installer.canonical_json(
                    {"schema": installer.JOURNAL_SCHEMA, "receipt": receipt}
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


if __name__ == "__main__":
    unittest.main()
