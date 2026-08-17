from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
