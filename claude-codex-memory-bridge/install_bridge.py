#!/usr/bin/env python3
"""Transactional installer for the Claude-native-memory to Codex hook.

All bridge code, policy, backups, and Codex hook configurations must resolve to
the configured Extreme SSD.  Existing hook handlers are preserved byte-for-byte
in private backups before any config is replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


BRIDGE_ID = "orca-claude-native-memory-v1"
POLICY_SCHEMA = "orca.claude-native-memory-bridge-policy.v1"
RECEIPT_SCHEMA = "orca.claude-native-memory-bridge-receipt.v1"
JOURNAL_SCHEMA = "orca.claude-native-memory-bridge-journal.v1"
SSD_ROOT = Path("/Volumes/Extreme SSD")
LOCAL_HOMES_ROOT = SSD_ROOT / "Orca/local-homes"
RUNTIME_BASE = LOCAL_HOMES_ROOT / ".shared-runtime/claude-codex-memory-bridge"
PENDING_PATH = RUNTIME_BASE / "pending-install.json"
SOURCE_SCRIPT = Path(__file__).with_name("claude_memory_hook.py")
MAX_MANAGED_FILE_BYTES = 4 * 1024 * 1024
DEFAULT_LIMITS = {
    "max_files": 32,
    "max_file_bytes": 262_144,
    "max_total_bytes": 786_432,
    "max_blocks": 4,
    "max_output_bytes": 7_000,
}


class InstallError(Exception):
    pass


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def strict_json(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise InstallError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("invalid hook JSON") from exc


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def volume_uuid(ssd_root: Path = SSD_ROOT) -> str:
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", os.fspath(ssd_root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=3,
        )
        payload = plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as exc:
        raise InstallError("unable to verify Extreme SSD") from exc
    value = payload.get("VolumeUUID") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        value,
    ):
        raise InstallError("Extreme SSD UUID unavailable")
    return value.upper()


def resolve_ssd_path(path: Path, *, must_exist: bool = True) -> Path:
    try:
        root = SSD_ROOT.resolve(strict=True)
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"path unavailable: {path}") from exc
    if not is_relative_to(resolved, root):
        raise InstallError(f"path is outside Extreme SSD: {path}")
    if must_exist and resolved.stat().st_dev != root.stat().st_dev:
        raise InstallError(f"path is on the wrong device: {path}")
    return resolved


def validate_owned_file(path: Path, *, private: bool = False) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o022
            or before.st_size > MAX_MANAGED_FILE_BYTES
        ):
            raise InstallError(f"unsafe file ownership or mode: {path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise InstallError(f"file identity changed: {path}")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise InstallError(f"cannot read {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or len(raw) != after.st_size:
        raise InstallError(f"file changed while reading: {path}")
    if private and after.st_mode & 0o077:
        raise InstallError(f"file is not private: {path}")
    return raw


def ensure_private_dir(path: Path) -> Path:
    if path.exists():
        try:
            info = path.lstat()
        except OSError as exc:
            raise InstallError(f"cannot inspect runtime directory: {path}") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise InstallError(f"unsafe runtime directory: {path}")
        return resolve_ssd_path(path)
    parent = resolve_ssd_path(path.parent)
    try:
        path.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise InstallError(f"cannot create runtime directory: {path}") from exc
    resolved = resolve_ssd_path(path)
    if resolved.parent != parent:
        raise InstallError(f"runtime directory escaped its parent: {path}")
    return resolved


def discover_hook_configs() -> list[Path]:
    expected_codex_home = resolve_ssd_path(LOCAL_HOMES_ROOT / ".codex")
    live_codex_home = resolve_ssd_path(Path.home() / ".codex")
    if live_codex_home != expected_codex_home:
        raise InstallError("live Codex home does not resolve to the canonical SSD home")
    configs = [resolve_ssd_path(live_codex_home / "hooks.json")]
    accounts_root = resolve_ssd_path(LOCAL_HOMES_ROOT / "codex-accounts")
    try:
        account_dirs = sorted(accounts_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise InstallError("cannot list Codex account homes") from exc
    for account_dir in account_dirs:
        try:
            info = account_dir.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            continue
        candidate = account_dir / "home/hooks.json"
        if candidate.is_file():
            configs.append(resolve_ssd_path(candidate))
    unique = list(dict.fromkeys(configs))
    if len(unique) < 2:
        raise InstallError("no isolated Codex account hook configs found")
    return unique


def owned_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    hooks = handler.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if isinstance(hook, dict) and BRIDGE_ID in str(hook.get("command", "")):
            return True
    return False


def make_handler(command: str) -> dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 5,
                "statusMessage": "Loading Claude memory from verified SSD",
            }
        ]
    }


def update_hook_config(raw: bytes, command: str, *, remove: bool = False) -> bytes:
    payload = strict_json(raw)
    if not isinstance(payload, dict) or set(payload) != {"hooks"} or not isinstance(payload["hooks"], dict):
        raise InstallError("unexpected hooks.json structure")
    event_handlers = payload["hooks"].get("UserPromptSubmit")
    if not isinstance(event_handlers, list):
        raise InstallError("missing UserPromptSubmit hook list")
    retained = [handler for handler in event_handlers if not owned_handler(handler)]
    if not remove:
        retained.append(make_handler(command))
    payload["hooks"]["UserPromptSubmit"] = retained
    return canonical_json(payload)


def atomic_write(path: Path, raw: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, mode)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def make_release() -> dict[str, Any]:
    source_script = resolve_ssd_path(SOURCE_SCRIPT)
    script_raw = validate_owned_file(source_script)
    script_sha = sha256_bytes(script_raw)
    expected_uuid = volume_uuid()
    claude_projects = resolve_ssd_path(Path.home() / ".claude/projects")
    expected_claude_projects = resolve_ssd_path(LOCAL_HOMES_ROOT / ".claude/projects")
    if claude_projects != expected_claude_projects:
        raise InstallError("Claude projects do not resolve to the canonical SSD home")
    release_key = canonical_json(
        {
            "script_sha256": script_sha,
            "volume_uuid": expected_uuid,
            "source_root": os.fspath(claude_projects),
            "limits": DEFAULT_LIMITS,
        }
    )
    release_id = sha256_bytes(release_key)
    release_dir = RUNTIME_BASE / "releases" / release_id
    policy = {
        "schema": POLICY_SCHEMA,
        "bridge_id": BRIDGE_ID,
        "enabled": True,
        "consumer": "codex",
        "ssd_root": os.fspath(SSD_ROOT),
        "volume_uuid": expected_uuid,
        "source_root": os.fspath(claude_projects),
        "runtime_root": os.fspath(release_dir),
        "limits": DEFAULT_LIMITS,
    }
    policy_raw = canonical_json(policy)
    policy_sha = sha256_bytes(policy_raw)
    installed_script = release_dir / "claude_memory_hook.py"
    installed_policy = release_dir / "policy.json"
    command_parts = [
        "/usr/bin/python3",
        os.fspath(installed_script),
        "--bridge-id",
        BRIDGE_ID,
        "--policy",
        os.fspath(installed_policy),
        "--expected-policy-sha256",
        policy_sha,
        "--expected-script-sha256",
        script_sha,
    ]
    command = " ".join(shlex.quote(part) for part in command_parts)
    return {
        "release_id": release_id,
        "release_dir": release_dir,
        "script_path": installed_script,
        "script_raw": script_raw,
        "script_sha256": script_sha,
        "policy_path": installed_policy,
        "policy_raw": policy_raw,
        "policy_sha256": policy_sha,
        "volume_uuid": expected_uuid,
        "command": command,
        "hook_configs": discover_hook_configs(),
    }


def write_runtime(release: dict[str, Any]) -> None:
    resolve_ssd_path(LOCAL_HOMES_ROOT)
    release_dir: Path = release["release_dir"]
    ensure_private_dir(RUNTIME_BASE.parent)
    ensure_private_dir(RUNTIME_BASE)
    ensure_private_dir(RUNTIME_BASE / "releases")
    ensure_private_dir(release_dir)
    for path_key, raw_key, digest_key in (
        ("script_path", "script_raw", "script_sha256"),
        ("policy_path", "policy_raw", "policy_sha256"),
    ):
        path: Path = release[path_key]
        expected_raw: bytes = release[raw_key]
        if path.exists():
            actual = validate_owned_file(resolve_ssd_path(path), private=True)
            if sha256_bytes(actual) != release[digest_key]:
                raise InstallError(f"immutable runtime collision: {path}")
        else:
            atomic_write(path, expected_raw, 0o600)


def timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def remove_file_durable(path: Path) -> None:
    try:
        path.unlink()
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise InstallError(f"cannot remove transaction file: {path}") from exc


def _receipt_rows(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rows = receipt.get("configs")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise InstallError("transaction has no config rows")
    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            raise InstallError("invalid transaction config row")
        required_strings = ("path", "backup", "before_sha256", "after_sha256")
        if any(not isinstance(raw_row.get(key), str) for key in required_strings):
            raise InstallError("invalid transaction config row")
        if any(
            re.fullmatch(r"[0-9a-f]{64}", raw_row[key]) is None
            for key in ("before_sha256", "after_sha256")
        ):
            raise InstallError("invalid transaction config digest")
        before_mode = raw_row.get("before_mode")
        after_mode = raw_row.get("after_mode")
        if (
            not isinstance(before_mode, int)
            or not isinstance(after_mode, int)
            or before_mode & ~0o777
            or after_mode != 0o600
        ):
            raise InstallError("invalid transaction config mode")
        path = resolve_ssd_path(Path(raw_row["path"]))
        backup = resolve_ssd_path(Path(raw_row["backup"]))
        backup_raw = validate_owned_file(backup, private=True)
        if sha256_bytes(backup_raw) != raw_row["before_sha256"]:
            raise InstallError(f"transaction backup digest mismatch: {backup}")
        current = validate_owned_file(path)
        current_mode = stat.S_IMODE(path.stat().st_mode)
        current_sha = sha256_bytes(current)
        before_matches = current_sha == raw_row["before_sha256"] and current_mode == before_mode
        after_matches = current_sha == raw_row["after_sha256"] and current_mode == after_mode
        if before_matches and after_matches:
            state = "both"
        elif before_matches:
            state = "before"
        elif after_matches:
            state = "after"
        else:
            state = "drift"
        rows.append(
            {
                **raw_row,
                "path_obj": path,
                "backup_obj": backup,
                "backup_raw": backup_raw,
                "state": state,
            }
        )
    return rows


def recover_pending_install() -> dict[str, Any]:
    if not PENDING_PATH.exists() and not PENDING_PATH.is_symlink():
        return {"ok": True, "state": "none"}
    pending_path = resolve_ssd_path(PENDING_PATH)
    pending_raw = validate_owned_file(pending_path, private=True)
    journal = strict_json(pending_raw)
    if (
        not isinstance(journal, dict)
        or set(journal) != {"schema", "receipt"}
        or journal.get("schema") != JOURNAL_SCHEMA
        or not isinstance(journal.get("receipt"), dict)
    ):
        raise InstallError("invalid pending install journal")
    receipt: dict[str, Any] = journal["receipt"]
    if (
        receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("bridge_id") != BRIDGE_ID
        or not isinstance(receipt.get("install_id"), str)
    ):
        raise InstallError("invalid pending install receipt")
    rows = _receipt_rows(receipt)
    latest_committed = False
    latest_path = RUNTIME_BASE / "latest-receipt.json"
    if latest_path.exists() or latest_path.is_symlink():
        latest_raw = validate_owned_file(resolve_ssd_path(latest_path), private=True)
        latest = strict_json(latest_raw)
        latest_committed = (
            isinstance(latest, dict)
            and latest.get("install_id") == receipt["install_id"]
            and latest_raw == canonical_json(receipt)
        )
    if latest_committed:
        drifted = [row for row in rows if row["state"] not in ("after", "both")]
        if drifted:
            raise InstallError("committed install journal has config drift")
        remove_file_durable(pending_path)
        return {"ok": True, "state": "committed", "install_id": receipt["install_id"]}
    drifted = [row for row in rows if row["state"] == "drift"]
    if drifted:
        raise InstallError("pending install cannot roll back because a config drifted")
    for row in reversed(rows):
        if row["state"] != "after":
            continue
        atomic_write(row["path_obj"], row["backup_raw"], row["before_mode"])
        restored = validate_owned_file(row["path_obj"])
        restored_mode = stat.S_IMODE(row["path_obj"].stat().st_mode)
        if sha256_bytes(restored) != row["before_sha256"] or restored_mode != row["before_mode"]:
            raise InstallError(f"transaction rollback verification failed: {row['path_obj']}")
    remove_file_durable(pending_path)
    return {"ok": True, "state": "rolled_back", "install_id": receipt["install_id"]}


def install() -> dict[str, Any]:
    recover_pending_install()
    release = make_release()
    configs: list[Path] = release["hook_configs"]
    originals: dict[Path, bytes] = {}
    updated: dict[Path, bytes] = {}
    original_modes: dict[Path, int] = {}
    for path in configs:
        raw = validate_owned_file(path)
        originals[path] = raw
        original_modes[path] = stat.S_IMODE(path.stat().st_mode)
        updated[path] = update_hook_config(raw, release["command"])
    write_runtime(release)
    install_id = timestamp_id()
    backup_dir = RUNTIME_BASE / "backups" / install_id
    ensure_private_dir(RUNTIME_BASE / "backups")
    ensure_private_dir(backup_dir)
    backups: dict[Path, Path] = {}
    for path, raw in originals.items():
        backup_name = f"{sha256_bytes(os.fspath(path).encode('utf-8'))}.json"
        backup_path = backup_dir / backup_name
        atomic_write(backup_path, raw, 0o600)
        backups[path] = backup_path
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "bridge_id": BRIDGE_ID,
        "install_id": install_id,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "release_id": release["release_id"],
        "release_dir": os.fspath(release["release_dir"]),
        "script_sha256": release["script_sha256"],
        "policy_sha256": release["policy_sha256"],
        "volume_uuid": release["volume_uuid"],
        "command": release["command"],
        "configs": [
            {
                "path": os.fspath(path),
                "backup": os.fspath(backups[path]),
                "before_sha256": sha256_bytes(originals[path]),
                "after_sha256": sha256_bytes(updated[path]),
                "before_mode": original_modes[path],
                "after_mode": 0o600,
            }
            for path in configs
        ],
    }
    receipt_raw = canonical_json(receipt)
    atomic_write(backup_dir / "receipt.json", receipt_raw, 0o600)
    atomic_write(
        PENDING_PATH,
        canonical_json({"schema": JOURNAL_SCHEMA, "receipt": receipt}),
        0o600,
    )
    try:
        for path in configs:
            if updated[path] != originals[path] or original_modes[path] != 0o600:
                current = validate_owned_file(path)
                current_mode = stat.S_IMODE(path.stat().st_mode)
                if current != originals[path] or current_mode != original_modes[path]:
                    raise InstallError(f"hook config changed during install: {path}")
                atomic_write(path, updated[path], 0o600)
                installed = validate_owned_file(path, private=True)
                if installed != updated[path]:
                    raise InstallError(f"hook config write verification failed: {path}")
        atomic_write(RUNTIME_BASE / "latest-receipt.json", receipt_raw, 0o600)
        outcome = recover_pending_install()
        if outcome.get("state") != "committed":
            raise InstallError("install commit journal did not finalize")
    except BaseException as exc:
        try:
            outcome = recover_pending_install()
        except BaseException as recovery_exc:
            raise InstallError(
                "install failed and the durable recovery journal remains pending"
            ) from recovery_exc
        if outcome.get("state") == "committed":
            return receipt
        raise InstallError("install failed; prior hook configs were restored") from exc
    return receipt


def read_receipt() -> dict[str, Any]:
    if PENDING_PATH.exists() or PENDING_PATH.is_symlink():
        raise InstallError("pending install journal must be recovered first")
    path = resolve_ssd_path(RUNTIME_BASE / "latest-receipt.json")
    raw = validate_owned_file(path, private=True)
    receipt = strict_json(raw)
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("bridge_id") != BRIDGE_ID
        or not isinstance(receipt.get("configs"), list)
    ):
        raise InstallError("invalid latest receipt")
    return receipt


def verify() -> dict[str, Any]:
    receipt = read_receipt()
    if volume_uuid() != receipt.get("volume_uuid"):
        raise InstallError("Extreme SSD UUID changed")
    release_dir = resolve_ssd_path(Path(receipt["release_dir"]))
    script_raw = validate_owned_file(resolve_ssd_path(release_dir / "claude_memory_hook.py"), private=True)
    policy_raw = validate_owned_file(resolve_ssd_path(release_dir / "policy.json"), private=True)
    if sha256_bytes(script_raw) != receipt.get("script_sha256"):
        raise InstallError("installed script digest mismatch")
    if sha256_bytes(policy_raw) != receipt.get("policy_sha256"):
        raise InstallError("installed policy digest mismatch")
    checked: list[str] = []
    for row in receipt["configs"]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise InstallError("invalid receipt config row")
        path = resolve_ssd_path(Path(row["path"]))
        raw = validate_owned_file(path, private=True)
        if sha256_bytes(raw) != row.get("after_sha256"):
            raise InstallError(f"hook config drift: {path}")
        payload = strict_json(raw)
        handlers = payload.get("hooks", {}).get("UserPromptSubmit", []) if isinstance(payload, dict) else []
        matches = [handler for handler in handlers if owned_handler(handler)]
        if len(matches) != 1:
            raise InstallError(f"owned hook count mismatch: {path}")
        checked.append(os.fspath(path))
    return {
        "ok": True,
        "release_id": receipt["release_id"],
        "script_sha256": receipt["script_sha256"],
        "policy_sha256": receipt["policy_sha256"],
        "volume_uuid": receipt["volume_uuid"],
        "configs": checked,
    }


def uninstall() -> dict[str, Any]:
    receipt = read_receipt()
    rows: list[tuple[Path, bytes, bytes, int]] = []
    for row in receipt["configs"]:
        if not isinstance(row, dict):
            raise InstallError("invalid receipt config row")
        path = resolve_ssd_path(Path(row["path"]))
        current = validate_owned_file(path, private=True)
        if sha256_bytes(current) != row.get("after_sha256"):
            raise InstallError(f"refusing uninstall because config changed: {path}")
        backup_path = resolve_ssd_path(Path(row["backup"]))
        backup = validate_owned_file(backup_path, private=True)
        if sha256_bytes(backup) != row.get("before_sha256"):
            raise InstallError(f"backup digest mismatch: {backup_path}")
        mode = row.get("before_mode")
        if not isinstance(mode, int) or mode & ~0o777:
            raise InstallError("invalid backup mode")
        rows.append((path, current, backup, mode))
    restored: list[tuple[Path, bytes]] = []
    try:
        for path, current, backup, mode in rows:
            restored.append((path, current))
            atomic_write(path, backup, mode)
    except BaseException as exc:
        rollback_errors: list[str] = []
        for path, current in reversed(restored):
            try:
                atomic_write(path, current, 0o600)
            except BaseException as rollback_exc:
                rollback_errors.append(f"{path}: {type(rollback_exc).__name__}")
        if rollback_errors:
            raise InstallError("uninstall failed and rollback was incomplete: " + "; ".join(rollback_errors)) from exc
        raise InstallError("uninstall failed; installed configs were restored") from exc
    return {"ok": True, "restored": [os.fspath(row[0]) for row in rows], "runtime_retained": True}


def plan() -> dict[str, Any]:
    release = make_release()
    pending = PENDING_PATH.exists() or PENDING_PATH.is_symlink()
    configs = []
    for path in release["hook_configs"]:
        raw = validate_owned_file(path)
        after = update_hook_config(raw, release["command"])
        configs.append(
            {
                "path": os.fspath(path),
                "before_sha256": sha256_bytes(raw),
                "after_sha256": sha256_bytes(after),
                "will_change": raw != after or stat.S_IMODE(path.stat().st_mode) != 0o600,
            }
        )
    return {
        "ok": not pending,
        "action": "plan",
        "release_id": release["release_id"],
        "script_sha256": release["script_sha256"],
        "policy_sha256": release["policy_sha256"],
        "volume_uuid": release["volume_uuid"],
        "runtime_on_ssd": True,
        "source_on_ssd": True,
        "pending_transaction": pending,
        "configs": configs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "install", "verify", "uninstall", "recover"))
    args = parser.parse_args()
    try:
        if args.action == "plan":
            result = plan()
        elif args.action == "install":
            result = install()
        elif args.action == "verify":
            result = verify()
        elif args.action == "uninstall":
            result = uninstall()
        else:
            result = recover_pending_install()
    except InstallError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
