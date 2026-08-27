#!/usr/bin/env python3
"""Transactional installer for the Claude-native-memory to Codex hook.

All bridge code, policy, backups, and Codex hook configurations must resolve to
the configured Extreme SSD.  Existing hook handlers are preserved byte-for-byte
in private backups before any config is replaced.
"""

from __future__ import annotations

import argparse
import fcntl
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
# The only Codex hook event this tool has ever registered a handler under. Every function that
# reads, writes, or detects hook-config content (update_hook_config(), _owned_shape_match(),
# _attempt_structural_detection(), _contains_owned_handler(), _find_untracked_owned_configs())
# takes an optional `event` parameter defaulting to this constant, so any call site that does not
# pass a different event -- every call site in this file today -- behaves exactly as it always
# has. Parameterized (not made a second hardcoded event) so a future caller can register a handler
# under a different Codex hook event (e.g. "SessionEnd") without this file's own detection/rewrite
# logic silently missing it; see AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md section 4.2 and its
# "SessionEnd hook-registration capability" section for the full rationale. This file does not
# itself register anything under any event other than DEFAULT_HOOK_EVENT. make_handler() itself
# takes NO `event` parameter -- it only ever builds the "hooks" list value that goes under
# whichever event key the caller chooses; event parameterization lives entirely in the functions
# listed above, not in handler construction. make_handler()'s own optional parameters are
# `timeout` and `status_message` (see its own docstring). (Corrected 2026-08-21 -- this paragraph
# previously, incorrectly, listed make_handler() among the event-optional functions; see
# AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md section 19.3 item 5 / section 26.2.)
# _find_untracked_owned_configs() forwards `event` only to _contains_owned_handler() -- Stage 2's
# raw-marker scanners (_raw_bytes_contain_bridge_marker(), _stream_scan_oversized_for_bridge_marker())
# are event-agnostic by construction, so there is nothing to forward it into there (fix round,
# 2026-08-20: this parameter was missing from _find_untracked_owned_configs() entirely until this
# round, the same shape of gap as the bridge_id one closed the round before, but reachable at any
# hooks.json file size, not just oversized ones -- see this file's test suite and
# AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md for the reproduction).
DEFAULT_HOOK_EVENT = "UserPromptSubmit"
# make_handler()'s command-handler timeout, unchanged from the value every UserPromptSubmit
# handler this tool has ever installed has always used.
DEFAULT_HOOK_TIMEOUT = 5
# The same detection/rewrite chain -- owned_handler(), _owned_shape_match(),
# _attempt_structural_detection(), _contains_owned_handler(), Stage 2's
# _raw_bytes_contain_bridge_marker(), and Stage 2's fixed-memory streaming twin
# _stream_scan_oversized_for_bridge_marker() (the alternate path taken only for a hooks.json
# candidate too large for _read_for_detection() to load whole, i.e. over MAX_MANAGED_FILE_BYTES;
# see that function's own comment) -- all six take an optional `bridge_id` parameter defaulting to
# BRIDGE_ID, parameterized for the identical reason `event` is above: write_candidate_capture.py
# defines its own distinct MODULE_ID and requires any command it registers to carry
# `--bridge-id <MODULE_ID>`, not this module's own BRIDGE_ID (write_candidate_capture.py:60,
# :2237). Without this, ownership detection/rewrite for a handler shaped that way silently failed
# end-to-end -- idempotency, removal, and detection all broken for exactly the handler shape this
# capability exists to support (independent Claude opus5/max whole-candidate acceptance review,
# 2026-08-20). _find_untracked_owned_configs(), the caller that ties the whole-file-read detection
# path and the oversized-streaming detection path together for the untracked-owned-handler safety
# scan, also takes and forwards the same `bridge_id` parameter -- without that, an initial pass at
# this parameterization left _stream_scan_oversized_for_bridge_marker() and its sole caller
# hardcoded to BRIDGE_ID even after every other function in the chain was fixed, so the
# oversized-file half of the safety scan stayed silently blind to any other bridge_id (converged
# independent Claude opus/max and Codex gpt-5.6-sol/max review, 2026-08-20). A future caller
# registering that module's SessionEnd handler passes bridge_id=write_candidate_capture.MODULE_ID
# explicitly -- as a plain string value, the same way `command` itself is already a plain value --
# not by importing write_candidate_capture into this file (that would add a new cross-module
# dependency this installer does not otherwise have). Every call site in this file today passes
# neither an `event` nor a `bridge_id` override, so detection/rewrite for the live, already-installed
# UserPromptSubmit handler under BRIDGE_ID is completely unaffected by this parameterization.
POLICY_SCHEMA = "orca.claude-native-memory-bridge-policy.v1"
# v2 (bumped from v1 without a version change at the time -- independent
# Codex sol/xhigh review, 2026-08-17, round 3, P2-R3-COMPAT): round 3 added
# required per-row fields (prev_backup/prev_sha256/prev_mode/after_backup)
# but left this constant at v1, so a receipt written by the prior candidate
# passed the top-level schema check here and only failed later, deep inside
# per-row validation, with the same generic "invalid receipt config row"
# error a genuinely corrupted receipt produces -- indistinguishable from
# actual corruption. Bumping to v2 makes an old-shaped receipt fail the
# top-level schema check immediately and unambiguously instead. No target
# machine has ever completed a real install with any prior schema version
# (confirmed at every review round so far), so there is no live v1 receipt
# to migrate; this is a clean version bump, not a migration.
RECEIPT_SCHEMA = "orca.claude-native-memory-bridge-receipt.v2"
JOURNAL_SCHEMA = "orca.claude-native-memory-bridge-journal.v1"
SSD_ROOT = Path("/Volumes/Extreme SSD")
LOCAL_HOMES_ROOT = SSD_ROOT / "Orca/local-homes"
# Orca's REAL, live pooled-Codex-account registry, relative to the user's home directory. This --
# not the SSD's own `local-homes/codex-accounts/` tree -- is the authoritative answer to "which
# Codex accounts exist on this machine": Orca creates one directory here per account, each with a
# `home` entry that is EITHER a symlink into local-homes (the accounts provisioned when this bridge
# was written) OR a real directory living somewhere else entirely (every account provisioned since).
#
# Enumerating only `LOCAL_HOMES_ROOT / "codex-accounts"` -- which is all _enumerate_hook_configs()
# used to do -- therefore enumerates a STALE SNAPSHOT, not the live registry: on the real target
# machine that snapshot holds 2 accounts while the registry holds 3, and the missing one is the
# only account actually in active use. Because verify() checked exactly the same (receipt-derived,
# hence same-snapshot) set, it reported `ok: true` while completely blind to that account's
# hooks.json -- a shared blind spot between install and verify, not an independent check
# (P1, 2026-08-27). The registry is now the authoritative account list; the local-homes tree is
# kept only as a supplementary/legacy way to LOCATE an already-known account's home, and any
# registry account this tool cannot locate an SSD-resident hooks.json for is reported explicitly
# as unmanaged by verify() instead of silently vanishing from its output.
#
# Deliberately a home-relative subpath resolved through Path.home() at call time (see
# orca_accounts_root()), not a path baked in at import: _enumerate_hook_configs() already anchors
# everything else it does to Path.home(), so a caller (or a test fixture) that redirects the home
# directory redirects the account registry with it, instead of the two silently disagreeing.
ORCA_ACCOUNTS_SUBPATH = "Library/Application Support/orca/codex-accounts"
RUNTIME_BASE = LOCAL_HOMES_ROOT / ".shared-runtime/claude-codex-memory-bridge"
PENDING_PATH = RUNTIME_BASE / "pending-install.json"
SOURCE_SCRIPT = Path(__file__).with_name("claude_memory_hook.py")
# Sibling source for the `install-write-trigger` CLI action (see that function and
# make_release()'s `write_trigger` parameter) -- same directory as this file and
# claude_memory_hook.py, resolved through the same resolve_ssd_path()/validate_owned_file()
# hash-pinning path SOURCE_SCRIPT already uses, not a parallel mechanism.
WRITE_TRIGGER_SOURCE_SCRIPT = Path(__file__).with_name("write_candidate_capture.py")
# The exact, and ONLY, identity write_candidate_capture.py's SessionEnd handler is ever registered
# under (write_candidate_capture.MODULE_ID) -- hardcoded here as a plain literal, NOT imported,
# specifically so this file's base-only paths (plain install/uninstall/recover/verify, and the
# untracked-owned-handler safety scan) never depend on that sibling module being importable at all.
# A regression test (test_write_trigger_bridge_id_constant_matches_write_candidate_capture_module_id)
# asserts this constant equals write_candidate_capture.MODULE_ID, so the two can never silently
# drift apart without a test failure calling it out.
#
# This is also, as of the fix below, the ONLY value make_release()'s write_trigger["bridge_id"] may
# ever hold -- see _validate_write_trigger_argument() (P1 fix, converged independent Claude opus/max
# + Codex gpt-5.6-sol/max review, 2026-08-21). Before this fix, ANY non-empty string was accepted
# there, so a caller-chosen bridge_id different from this constant installed a real, permanently
# live SessionEnd handler that this file's own untracked-owned-handler safety scan and verify()
# (both of which only ever scanned/checked this one well-known identity) could never detect or
# report on -- a real registered handler, indistinguishable from a healthy one by `ok: true`, that
# in fact silently produced zero output on every session forever. Locking the argument to this one
# exact value is safe precisely because no real caller has ever used, or had any supported way to
# use, any other value: the only real producer of a write_trigger argument
# (_load_write_trigger_config(), the install-write-trigger CLI action's own request construction)
# has always hardcoded this same identity via write_candidate_capture.MODULE_ID.
WRITE_TRIGGER_BRIDGE_ID = "orca-claude-codex-memory-write-trigger-v1"
MAX_MANAGED_FILE_BYTES = 4 * 1024 * 1024
DEFAULT_LIMITS = {
    "max_files": 32,
    "max_file_bytes": 262_144,
    "max_total_bytes": 786_432,
    "max_blocks": 4,
    "max_output_bytes": 7_000,
}
# make_handler()'s default "statusMessage" -- unchanged from the value every UserPromptSubmit
# handler this tool has ever installed has always used. The `install-write-trigger` action passes
# a different, accurate message for the SessionEnd handler it registers (see that function).
DEFAULT_STATUS_MESSAGE = "Loading Claude memory from verified SSD"


class InstallError(Exception):
    pass


class _CandidateTooLargeForDetection(InstallError):
    # Raised only by _read_for_detection() when a candidate is a readable,
    # regular file whose sole problem is exceeding MAX_MANAGED_FILE_BYTES --
    # deliberately a distinct subclass of InstallError (not just a specially
    # worded InstallError) so _find_untracked_owned_configs() can route this
    # one case to a bounded streaming marker scan instead of the blanket
    # absence-of-evidence tolerance every other _read_for_detection() failure
    # (EACCES, identity-changed-mid-read) still gets under RUNTIME_BASE (round
    # 13, closing R12-P1-A / P2-R12-A: independent Claude opus5/max and Codex
    # sol/gpt-5.6-terra round-12 reviews both found -- agreeing on the fix,
    # disagreeing only on severity label, P2 vs P1 -- that a genuine live
    # relocated handler merely padded past MAX_MANAGED_FILE_BYTES under
    # RUNTIME_BASE was silently abandoned because this exact raise used to be
    # blanket-tolerated there as if the file's oversized-ness meant nothing
    # could be known about it). Every existing caller that catches
    # InstallError (or Exception) broadly continues to work unchanged.
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
    # Flake investigation (2026-08-20): the previous `timeout=3` was measured against this
    # exact `diskutil info -plist` invocation on this exact machine -- a shared, often
    # heavily-loaded dev box (load averages 25-35 observed) -- and empirically exceeded
    # even with NO artificial contention: 15/15 sequential calls with a 10s timeout ranged
    # 0.10s-3.74s, with several individual calls landing above both this function's old 3s
    # bound and write_candidate_capture's old 2s bound (see claude_memory_hook.py's
    # `_disk_volume_uuid`, the same command against the same path). Reproduced directly via
    # `tests/test_install_bridge.py`'s `InstallWriteTriggerRealCommandEndToEndTests`, which
    # runs the real, unmocked `diskutil` call: 20 isolated re-runs on a busy moment of this
    # machine produced 6 `InstallError: unable to verify Extreme SSD` failures, every single
    # one a `subprocess.TimeoutExpired` on this exact call (confirmed with temporary timing
    # instrumentation, not guessed) -- not a parsing bug, not a race in make_release() or
    # atomic_write(). 15s gives >4x headroom over the worst latency actually observed here,
    # while still bounding a genuinely hung/unresponsive diskutil rather than hanging forever.
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", os.fspath(ssd_root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=15,
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
    if must_exist:
        # These two stat() calls used to run unguarded: a TOCTOU race (the
        # path vanishing between resolve() above and here) or any other
        # OSError (e.g. a permission change) would propagate as a raw,
        # uncaught exception -- main()'s except clause only catches
        # InstallError, so this crashed the whole installer with a Python
        # traceback instead of the clean {"ok": false, "error": ...} every
        # other failure mode in this file produces (found in the full-audit
        # Workflow, 2026-08-17, deferred at the time because install_bridge.py
        # had never been run; now in scope ahead of an actual install).
        try:
            same_device = resolved.stat().st_dev == root.stat().st_dev
        except OSError as exc:
            raise InstallError(f"path unavailable: {path}") from exc
        if not same_device:
            raise InstallError(f"path is on the wrong device: {path}")
    return resolved


def _mode_bits(path: Path) -> int:
    # Shared guarded replacement for the several `stat.S_IMODE(path.stat().st_mode)`
    # call sites below that used to call path.stat() directly and let a bare
    # OSError (deleted/racing file, permission error) crash the installer
    # instead of producing a clean InstallError (see resolve_ssd_path's
    # comment above for the same class of bug and its origin).
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise InstallError(f"cannot stat {path}") from exc


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


def _read_for_detection(path: Path) -> bytes | None:
    # Used only by the untracked-owned-handler safety scan
    # (_find_untracked_owned_configs(), below) to inspect content this tool
    # did not write and does not manage -- deliberately NOT
    # validate_owned_file(), whose checks (uid match, not group/other-
    # writable, size cap) are the right questions for "may I safely
    # rewrite this file?" and the wrong ones for "does this file currently
    # contain a live handler of mine?": every one of them silently answers
    # "no handler here" for a file that is merely unowned, loosely
    # permissioned, or oversized -- none of which means a handler actually
    # inside it is not real and not executing (independent Claude opus5/max
    # review, 2026-08-17, round 9, R9-P1-B, reproduced with nothing more
    # than `chmod 0664` on a relocated, still-live config -- the *same*
    # relocation the scan otherwise correctly refuses, silently waved
    # through the instant its mode or ownership stopped matching
    # validate_owned_file()'s unrelated write-safety policy).
    #
    # Returns None only for conditions that genuinely mean "this cannot be
    # one of our configs" (not a regular file; a symlink -- O_NOFOLLOW
    # refuses it at open time, and _is_regular_file() already screened the
    # common case). Every other failure -- cannot open, cannot read,
    # identity changed mid-read -- escalates to InstallError, matching
    # this file's fail-closed discipline everywhere else stat-level
    # ambiguity comes up (_path_is_absent(), _is_regular_file()). This is
    # deliberately narrower than a blanket "let read errors propagate":
    # the real target machine's local-homes tree already holds several
    # unrelated hooks.json files (other tools' hook configs), and giving
    # any one of them a permanent veto over uninstall the moment its mode
    # or owner looks unusual would trade R9-P1-B's silent-abandonment
    # risk for an equally real denial-of-service risk -- so only actual
    # read failures escalate, not "not owned by us" or "not private".
    descriptor = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            return None
        if before.st_size > MAX_MANAGED_FILE_BYTES:
            raise _CandidateTooLargeForDetection(f"cannot safely inspect {path}: too large")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise InstallError(f"file identity changed while inspecting {path}")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise InstallError(f"cannot read {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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


def _is_regular_file(path: Path) -> bool:
    # NOT `path.is_file()`. That method's error-swallowing range is not
    # consistent across Python versions -- round 7's guard (`except OSError:
    # raise InstallError`) closed the crash on /usr/bin/python3 3.9, where
    # is_file() re-raises PermissionError, but on 3.13+ is_file() delegates
    # to os.path.isfile(), which swallows EVERY OSError including EACCES,
    # so that same guard is dead code there and the fail-open half of
    # R5-P3-A/R3-P3-B/R6-P2-B survives on the newer interpreter (independent
    # Claude opus5/max review, 2026-08-17, round 8, R8-P2-B). Calling
    # `Path.stat()` directly and discriminating errno ourselves -- exactly
    # `_path_is_absent()`'s pattern -- behaves identically on every version:
    # only ENOENT (and the family pathlib itself always normalizes into
    # FileNotFoundError) means "not present"; anything else means "cannot
    # determine" and must fail closed.
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise InstallError(f"cannot determine whether {path} is a managed config") from exc
    return stat.S_ISREG(info.st_mode)


def orca_accounts_root() -> Path:
    # See ORCA_ACCOUNTS_SUBPATH. Resolved from Path.home() on every call, exactly like
    # _enumerate_hook_configs()'s own `Path.home() / ".codex"` check, so the account registry and
    # the live Codex home can never be anchored to two different home directories.
    return Path.home() / ORCA_ACCOUNTS_SUBPATH


def _list_account_ids(root: Path) -> list[str] | None:
    # Account-id directory names directly under one account root. `None` means the root itself does
    # not exist -- "this machine has no such registry", deliberately distinguished from "the
    # registry exists and is empty", because the first must fall back to the other root while the
    # second is authoritative information (there really are no accounts).
    #
    # NOTE this returns bare NAMES, never paths, and never follows anything: locating an account's
    # actual hooks.json is _resolve_account_hook_config()'s job, and every path it builds goes
    # through resolve_ssd_path(), so accepting a symlinked entry here cannot widen what this tool is
    # willing to write to.
    try:
        entries = sorted(item.name for item in root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise InstallError(f"cannot list Codex account homes: {root}") from exc
    ids: list[str] = []
    for name in entries:
        try:
            info = (root / name).stat()
        except FileNotFoundError:
            continue
        except OSError:
            # Cannot determine what this entry is. Keep the id rather than silently dropping it:
            # for a registry account, being undeterminable is precisely the state verify() must
            # surface as unmanaged instead of omitting from its output.
            ids.append(name)
            continue
        if stat.S_ISDIR(info.st_mode):
            ids.append(name)
    return ids


def _hook_config_under(root: Path, account_id: str) -> Path | None:
    # One account root's `<root>/<id>/home/hooks.json`, resolved, or None if this tool cannot manage
    # it. Goes through resolve_ssd_path(), so an account whose home is a REAL directory outside the
    # Extreme SSD (Orca's current provisioning shape) resolves to None -- this tool's entire write
    # path requires SSD residency, so "not on the SSD" genuinely means "not manageable", and the
    # honest report for it is `unmanaged`, not silent omission.
    candidate = root / account_id / "home/hooks.json"
    try:
        resolved = resolve_ssd_path(candidate)
    except InstallError:
        return None
    try:
        if _is_regular_file(resolved):
            return resolved
    except InstallError:
        return None
    return None


def _resolve_account_hook_config(account_id: str, *, in_live_registry: bool) -> Path | None:
    # Locate one account's manageable hooks.json, or None if this tool cannot manage it at all.
    #
    # `in_live_registry` is NOT a convenience flag -- it is the whole correctness boundary (P1,
    # independent review, 2026-08-28). This function used to try the registry path and then the
    # legacy `local-homes/codex-accounts` path UNCONDITIONALLY, which meant that an account really
    # present in the live registry, but whose real home is an off-SSD directory this tool may not
    # touch, was silently "resolved" to whatever same-id directory the legacy SSD snapshot happened
    # to contain. Those two directories are unrelated; sharing a name makes neither a copy of nor a
    # stand-in for the other. The snapshot then received the bridge handler, the receipt covered it,
    # and verify() -- seeing the account covered -- reported ok: true while the account Orca
    # actually runs still had no redaction hook at all. That is the very P1 this whole registry
    # rework exists to fix (see ORCA_ACCOUNTS_SUBPATH), reproduced through a second code path.
    #
    # So: once an id is confirmed present in the live registry, ONLY that account's own registry
    # home may satisfy it. `<registry>/<id>/home/hooks.json` still resolves onto the SSD for the
    # accounts whose `home` is a symlink into local-homes -- the common case -- and for every other
    # shape the answer is None, i.e. `unmanaged`, i.e. verify() fails closed.
    #
    # The legacy location survives only in its documented, originally intended scope: an id ABSENT
    # from the live registry (including a machine with no registry at all, where `registry_ids is
    # None` makes every id absent). Such an id is not a live account being shadowed, it is a
    # leftover home this tool has always been able to manage, and dropping it would break older
    # layouts for no safety gain.
    located = _hook_config_under(orca_accounts_root(), account_id)
    if located is not None or in_live_registry:
        return located
    return _hook_config_under(LOCAL_HOMES_ROOT / "codex-accounts", account_id)


def _enumerate_accounts() -> tuple[list[Path], dict[str, Path | None]]:
    # The raw enumeration discover_hook_configs() is built on, split out so
    # a caller that only wants to know "what managed-shaped configs
    # currently exist" (uninstall()'s R7-P1-A safety scan, below) can reuse
    # it without also inheriting discover_hook_configs()'s own "at least 2"
    # policy -- that minimum is specific to *installing* (this bridge is
    # only meant to run against isolated multi-account setups) and has
    # nothing to do with what a safety scan needs, which must keep working
    # even when only one config remains discoverable.
    #
    # Returns both halves of the picture, because they are not the same set (see
    # ORCA_ACCOUNTS_SUBPATH):
    #   [0] every hook config this tool can actually manage, main Codex home first;
    #   [1] the live Orca registry's accounts mapped to the config located for each, with None for
    #       any registry account no manageable hooks.json could be found for. Empty dict when this
    #       machine has no registry at all -- the enumeration then behaves exactly as it always did.
    expected_codex_home = resolve_ssd_path(LOCAL_HOMES_ROOT / ".codex")
    live_codex_home = resolve_ssd_path(Path.home() / ".codex")
    if live_codex_home != expected_codex_home:
        raise InstallError("live Codex home does not resolve to the canonical SSD home")
    configs = [resolve_ssd_path(live_codex_home / "hooks.json")]

    registry_ids = _list_account_ids(orca_accounts_root())
    legacy_ids = _list_account_ids(LOCAL_HOMES_ROOT / "codex-accounts")
    if registry_ids is None and legacy_ids is None:
        # Neither account root exists -- the same hard failure the single-root version produced when
        # its one root was missing.
        raise InstallError("cannot list Codex account homes")

    registry_id_set = set(registry_ids or [])
    legacy_id_set = set(legacy_ids or [])
    registry_accounts: dict[str, Path | None] = {}
    for account_id in sorted(registry_id_set | legacy_id_set):
        in_live_registry = account_id in registry_id_set
        located = _resolve_account_hook_config(account_id, in_live_registry=in_live_registry)
        if in_live_registry:
            registry_accounts[account_id] = located
        if located is not None:
            configs.append(located)
        elif in_live_registry and account_id in legacy_id_set:
            # The live account is unmanageable AND a same-id legacy snapshot exists -- the shadowing
            # case _resolve_account_hook_config() now refuses to conflate. The snapshot must never
            # count as this account's configuration (registry_accounts keeps the None above, so
            # verify() still reports it unmanaged), but the snapshot file itself may hold a handler
            # some earlier install wrote into it, so it stays in the managed-config list and
            # install/uninstall keep being able to reach and clean it. Coverage and reachability are
            # two different questions; only the first one was ever allowed to be answered by an id
            # match.
            legacy = _hook_config_under(LOCAL_HOMES_ROOT / "codex-accounts", account_id)
            if legacy is not None:
                configs.append(legacy)
    return list(dict.fromkeys(configs)), registry_accounts


def _enumerate_hook_configs() -> list[Path]:
    return _enumerate_accounts()[0]


def discover_hook_configs() -> list[Path]:
    unique = _enumerate_hook_configs()
    if len(unique) < 2:
        raise InstallError("no isolated Codex account hook configs found")
    return unique


def _validate_bridge_id(bridge_id: Any) -> None:
    # Shared by every entry point that accepts a caller-supplied bridge_id identity --
    # update_hook_config(), owned_handler(), _find_untracked_owned_configs() -- not scoped to
    # update_hook_config() alone (AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md section 19.3 item 4 /
    # section 26.1 -- an earlier round's own §18.2 mischaracterized update_hook_config() as "the
    # one public API boundary" needing this check). Without it, bridge_id=None was silently
    # accepted by owned_handler() (compared against every token, never matching -- a silent
    # no-op, not a crash) and bridge_id="" would structurally match essentially any command
    # containing the bare `--bridge-id ` marker text at all, since `--bridge-id` is one of the
    # tokens shlex.split() itself already always produces around it -- neither is what any real
    # caller intends.
    if not isinstance(bridge_id, str) or not bridge_id:
        raise InstallError(f"bridge_id must be a non-empty string, got {bridge_id!r}")


def _validate_hook_event(event: Any) -> None:
    # Sibling of _validate_bridge_id(), same rationale. Every real Codex hook event name
    # (UserPromptSubmit, SessionStart, SessionEnd, ...) is a bare CamelCase identifier, so this
    # rejects the obviously malformed/empty cases without validating against Codex's full
    # hook-event enum -- this file has no authoritative list of every event Codex might ever add,
    # and hardcoding one here would make a legitimate future event name a spurious rejection
    # instead of Codex's own problem to reject.
    if not isinstance(event, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", event):
        raise InstallError(f"event must look like a Codex hook event name, got {event!r}")


def _import_write_candidate_capture() -> Any:
    # Lazily imports write_candidate_capture.py -- the module that owns the real, authoritative
    # write_trigger schema (MODULE_ID, _parse_write_trigger_block()) this file's own write_trigger
    # handling must stay in sync with. Shared by every caller in this file that needs it
    # (_load_write_trigger_config(), make_release()'s write_trigger validation via
    # _validate_write_trigger_argument(), and _untracked_owned_including_write_trigger()'s safety-net
    # scan) instead of three independent copies of the same sys.path-seeding + import (P2-1/P2-2 fix,
    # independent Claude opus5/max + Codex review, 2026-08-21).
    #
    # Deliberately still a function-local, lazy import -- never hoisted to module level -- for the
    # same reason _load_write_trigger_config() originally did it this way: the low-level detection/
    # rewrite chain (owned_handler(), _find_untracked_owned_configs(), update_hook_config(), and
    # friends) must stay free of this import and take bridge_id as a plain string instead (BRIDGE_ID's
    # own comment / section 13.3), and importing write_candidate_capture unconditionally at module
    # level here would make every plain, base-only action in this file -- which has nothing to do with
    # the write-trigger feature -- depend on that module being importable too. sys.path is seeded the
    # same way write_candidate_capture.py itself seeds it to import claude_memory_hook, so this import
    # resolves whether install_bridge.py was imported as a module (tests) or run as a script directly.
    #
    # Deduplicated (P2 fix, independent Claude opus5/max review, 2026-08-21): this function can be
    # called many times across one process's lifetime (uninstall()/recover_pending_install() each
    # call it, indirectly, once per action, and a long-lived caller may invoke this module's actions
    # repeatedly) -- an unconditional `sys.path.insert(0, ...)` grew sys.path by one entry per call,
    # unboundedly, all pointing at the identical directory. Checking membership first keeps this
    # idempotent instead.
    _module_dir = os.fspath(Path(__file__).resolve().parent)
    if _module_dir not in sys.path:
        sys.path.insert(0, _module_dir)
    import write_candidate_capture as _wtc

    return _wtc


def _wrap_write_candidate_capture_import_error(exc: BaseException, context: str) -> InstallError:
    # Shared message construction for every call site that wraps _import_write_candidate_capture()
    # in a try/except (round-54 fix, independent Claude opus5/max GO + Codex gpt-5.6-sol/max NO-GO
    # dual review, 2026-08-21, issues 1-3) -- avoids three divergent copies of this branching. Three
    # distinct failure shapes, distinguished here:
    #
    #   - `exc` is a ModuleNotFoundError/ImportError with `exc.name == "write_candidate_capture"`:
    #     the module itself is genuinely missing (e.g. an untracked file lost to `git clean`/a fresh
    #     clone). Reports the original, already-tested wording.
    #   - `exc` is a ModuleNotFoundError/ImportError with a DIFFERENT `exc.name`: write_candidate_
    #     capture.py exists and was found, but ITS OWN `import claude_memory_hook` (or any other
    #     transitive dependency) failed. The unconditional "write_candidate_capture module
    #     unavailable" wording used before this fix was misleading here -- it always named
    #     write_candidate_capture as the missing piece even when the real gap was one of its own
    #     imports, with the actual name visible only inside the parenthetical `(exc)` suffix.
    #   - `exc` is a SyntaxError (no `.name` attribute at all, unlike ImportError): a corrupted or
    #     half-written write_candidate_capture.py (an interrupted copy, a bad merge) fails to parse,
    #     not to be found.
    if isinstance(exc, SyntaxError):
        return InstallError(f"{context}: write_candidate_capture module exists but failed to import: {exc}")
    name = getattr(exc, "name", None)
    if name is None or name == "write_candidate_capture":
        return InstallError(f"{context}: write_candidate_capture module unavailable ({exc})")
    return InstallError(
        f"{context}: write_candidate_capture.py exists but failed to import because its own "
        f"dependency {name!r} is unavailable ({exc})"
    )


def owned_handler(handler: Any, *, bridge_id: str = BRIDGE_ID) -> bool:
    # Public-named (no leading underscore) and reachable independently of update_hook_config() --
    # verify()/plan() call it directly, and any external caller of this module can too -- so its
    # own bridge_id must be validated here, not only assumed already-valid by an upstream caller.
    _validate_bridge_id(bridge_id)
    # Structural match against the exact `--bridge-id <bridge_id>` argv pair
    # make_release() generates, not a raw substring search over the whole
    # command string. A substring check treats bridge_id appearing *anywhere*
    # in an unrelated hook's command -- inside a comment, a log message, an
    # unrelated flag's value, or another bridge's command that merely mentions
    # this one -- as "owned by this installer", and update_hook_config()
    # deletes/replaces whatever owned_handler() returns True for. That would
    # silently destroy a hook this installer never created (found in the
    # full-audit Workflow, 2026-08-17, deferred at the time because
    # install_bridge.py had never been run; now in scope ahead of an actual
    # install). Parsing the command the way a shell would and requiring
    # bridge_id to be the exact token immediately following an exact
    # `--bridge-id` token closes that: bridge_id showing up as a substring of
    # some other token, or without the adjacent flag, no longer matches.
    # `bridge_id` defaults to this module's own BRIDGE_ID -- see BRIDGE_ID's
    # own comment for why this is parameterized (a future SessionEnd caller
    # for write_candidate_capture.py owns a different bridge_id, MODULE_ID).
    if not isinstance(handler, dict):
        return False
    hooks = handler.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = hook.get("command")
        if not isinstance(command, str):
            continue
        try:
            # comments=True: a shell actually executing this command line
            # would stop at an unquoted `#` too, so text after it is not
            # part of what runs and must not be treated as evidence this
            # handler is ours (independent Claude opus5/max review,
            # 2026-08-17, round 1, P3-1: shlex.split()'s default
            # comments=False made `/usr/bin/true # --bridge-id <BRIDGE_ID>`
            # match and get deleted by update_hook_config(), even though
            # that text is a comment and BRIDGE_ID never actually runs).
            tokens = shlex.split(command, comments=True)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            if token == "--bridge-id" and index + 1 < len(tokens) and tokens[index + 1] == bridge_id:
                return True
    return False


def _owned_handler_command(handler: Any, *, bridge_id: str = BRIDGE_ID) -> str | None:
    # Sibling of owned_handler() above, same structural match (deliberately not deduplicated into
    # one function returning both -- owned_handler() is the hot, allocation-free path Layer 0's
    # per-handler shape check runs for every handler in every hooks.json this file ever inspects;
    # this one is reached only from verify()'s independent write-trigger liveness detection, which
    # needs the actual live command TEXT (to parse its own --policy/--expected-*-sha256 arguments
    # out of it), not merely a yes/no answer. Returns the first matching hook's raw "command" string,
    # or None if `handler` does not structurally contain one under `bridge_id` at all.
    if not isinstance(handler, dict):
        return None
    hooks = handler.get("hooks")
    if not isinstance(hooks, list):
        return None
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = hook.get("command")
        if not isinstance(command, str):
            continue
        try:
            tokens = shlex.split(command, comments=True)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            if token == "--bridge-id" and index + 1 < len(tokens) and tokens[index + 1] == bridge_id:
                return command
    return None


_NOT_PARSED = object()  # sentinel: "this attempt did not yield a JSON value" (None is a real JSON value)

# Codecs retried, in this order, once _safe_parse_strict_utf8() (canonical UTF-8, no BOM
# tolerance, duplicate keys rejected) fails to parse a candidate file's bytes at all.
# "utf-8-sig" strips a leading BOM when present and otherwise decodes identically to plain
# "utf-8", so it alone covers both the BOM and no-BOM UTF-8 cases -- a separate bare "utf-8"
# entry would only ever produce a duplicate of a text already tried. The utf-16/utf-32 families
# each include the BOM-sensing form plus both explicit byte orders, since a naive backup/
# restore tool or manual re-save in a text editor can plausibly produce any of the three
# (independent Claude opus5/max review, 2026-08-17, round 10, encoding-bypass finding).
_DETECTION_ENCODINGS: tuple[str, ...] = (
    "utf-8-sig",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
)

# Every hooks.json this tool has ever written, and every realistic third-party tool's hooks.json,
# is a handful of DEFAULT_HOOK_EVENT handler entries -- a few KB at most. Both of the expensive
# per-file operations below -- Layer 1's multi-encoding decode/parse, AND Layer 0's own
# per-handler owned_handler() shape check, which is NOT free either (each call runs
# shlex.split() on that handler's command string) -- are only ever needed at that size. Bounding
# BOTH under this one threshold keeps a large, unrelated, or deliberately oversized
# "hooks.json"-named file from turning a routine uninstall()/recover_pending_install() call into
# a multi-second-to-minutes exclusive-lock hold (both call sites hold the installer lock for the
# whole safety scan -- see _find_untracked_owned_configs()'s own comment). A file over this bound
# skips straight to Layer 2's byte-pattern marker check, which stays cheap regardless of size (a
# `bytes in bytes` substring search, not a decode, independent of handler count) -- crossing this
# bound trades the precise structural answer for the cheap, fail-closed-on-ambiguity one; it does
# not silently drop detection to nothing (self-check Workflow design round, 2026-08-18, round 2
# pressure-test: a ~3.9MB well-formed hooks.json-shaped file cost 1.0-1.15s in Layer 0's own
# shape-check loop alone, and 1.8-5.9s through Layer 1's decode/parse fan-out, before this bound
# existed).
_STRUCTURAL_DETECTION_MAX_BYTES = 65_536


def _safe_parse_strict_utf8(raw: bytes) -> Any:
    # Layer 0: the exact canonical interpretation (strict UTF-8, duplicate keys rejected) every
    # file this tool has ever written satisfies. Returns _NOT_PARSED, never raises: `raw` is
    # untrusted, arbitrary bytes here (see _contains_owned_handler()'s own comment), and this
    # catches RecursionError as well as strict_json()'s own InstallError -- a deeply-nested but
    # otherwise well-formed UTF-8 JSON document is well within MAX_MANAGED_FILE_BYTES (self-check
    # Workflow design round, 2026-08-18, round 2 pressure-test P1: an earlier version of this
    # detection path only caught JSONDecodeError, letting RecursionError escape this module's
    # "must never be left to raise past this function" contract for _contains_owned_handler()).
    # Also explicitly catches ValueError to match _lenient_parse_last_key_wins()'s own except
    # clause below (independent Claude opus5/max review, 2026-08-18, round 11, R11-P1-A: CPython
    # >= 3.9.14/3.10.7/3.11 caps int<->str conversion at 4300 digits, and an integer literal
    # longer than that makes json.loads() raise a bare ValueError, not json.JSONDecodeError --
    # this function's own except clause missed it even though the sibling function added in the
    # same commit already caught it, an internal inconsistency in the round-11 patch). See
    # _contains_owned_handler()'s own broader except-Exception wrapper around Stage 1 as a whole
    # for why this specific catch is defense-in-depth, not the only thing standing between an
    # unenumerated exception type and a bare traceback.
    try:
        return strict_json(raw)
    except (InstallError, RecursionError, ValueError):
        return _NOT_PARSED


def _lenient_decode_candidates(raw: bytes) -> list[str]:
    # Best-effort decode of `raw` under each of _DETECTION_ENCODINGS. Never raises: a codec that
    # simply cannot decode these particular bytes (wrong byte count for a 2-/4-byte encoding, an
    # invalid code unit) is omitted from the result rather than treated as an error -- exactly
    # like strict_json()'s own UnicodeDecodeError-to-"not this" handling, just across more than
    # one codec. Identical decoded text produced by two different codec names (e.g. a BOM'd
    # "utf-16" and the matching explicit "utf-16-le"/"utf-16-be") is kept only once, so the
    # caller's parse-and-check work is never duplicated.
    candidates: list[str] = []
    seen_text: set[str] = set()
    for encoding in _DETECTION_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if text in seen_text:
            continue
        seen_text.add(text)
        candidates.append(text)
    return candidates


def _lenient_parse_last_key_wins(text: str) -> Any:
    # Layer 1's tolerant JSON parse, deliberately more forgiving than strict_json() in two ways a
    # naive backup/restore tool or manual re-save can plausibly produce for content that started
    # life as one of this installer's own files: last-key-wins on a duplicate object key (rather
    # than strict_json()'s reject-on-duplicate), and tolerance of trailing bytes after the JSON
    # value ends (raw_decode() only consumes a single leading value; strict_json()'s full
    # json.loads() would reject anything left over). Returns _NOT_PARSED, never raises, for
    # anything that is not a recognizable JSON document, including a RecursionError from
    # pathologically deep nesting, caught here at the actual recursive call (see
    # _safe_parse_strict_utf8()'s comment for the same class of bug).
    stripped = text.lstrip()
    if not stripped:
        return _NOT_PARSED

    def last_key_wins(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        return dict(pairs)

    decoder = json.JSONDecoder(object_pairs_hook=last_key_wins)
    try:
        value, _end = decoder.raw_decode(stripped)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return _NOT_PARSED
    return value


def _owned_shape_match(payload: Any, *, event: str = DEFAULT_HOOK_EVENT, bridge_id: str = BRIDGE_ID) -> bool | None:
    # The single decision point both parse layers funnel every successfully-parsed payload
    # through. None means `payload` is not recognizable as one of our hooks.json documents at
    # all (dict -> "hooks":dict -> "<event>":list) -- genuinely ambiguous, the caller
    # should keep looking under a different encoding/strategy. True or False means `payload`
    # unambiguously IS shaped like one of our configs, definitively containing (True) or not
    # containing (False) an owned handler -- final for that parse attempt. `event`/`bridge_id`
    # default to DEFAULT_HOOK_EVENT/BRIDGE_ID; every caller in this file today relies on both
    # defaults, so this stays byte-for-byte identical to the previous hardcoded-"UserPromptSubmit"/
    # BRIDGE_ID behavior unless a caller explicitly passes a different event/bridge_id.
    if not isinstance(payload, dict):
        return None
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return None
    handlers = hooks.get(event)
    if not isinstance(handlers, list):
        return None
    return any(owned_handler(handler, bridge_id=bridge_id) for handler in handlers)


# One-or-more of these characters is exactly the whitespace run shlex.split() (owned_handler()'s
# structural detector, Stage 1) already treats as an ordinary token separator between
# `--bridge-id` and its value -- shlex.whitespace's own default set. Both raw-bytes fallback
# scanners below tolerate the same run (bounded by _BRIDGE_ID_MARKER_MAX_WHITESPACE_RUN) so they
# recognize the same command shapes Stage 1 does, instead of only a single literal space
# (independent review finding, 2026-08-21, P2-2: `--bridge-id  <id>` with a double space, or
# `--bridge-id\t<id>` with a tab, were structurally recognized as owned but invisible to both
# raw-bytes scanners).
_BRIDGE_ID_MARKER_WHITESPACE_CHARS = (" ", "\t", "\r", "\n")
# Space is the only one of the 4 chars above JSON allows to appear literally inside a string --
# tab/CR/LF are control characters (U+0000-U+001F) RFC 8259 requires a JSON encoder to escape, so
# json.dumps() (and this file's own canonical_json()) always renders them as the two literal
# characters `\t`/`\r`/`\n` (backslash + letter) in the on-disk bytes, never as the raw control
# byte -- the exact same root cause as the double-quote form in _bridge_id_marker_value_forms()
# (P2-1), just for whitespace instead of the quote character. `_json_string_body(" ")` is `" "`
# unchanged, so this one helper covers both cases without a special case for space.
# A bound, not an attempt to match shlex.split()'s literally-unbounded whitespace tolerance:
# _stream_scan_oversized_for_bridge_marker() needs a fixed maximum marker span to size its
# chunk-boundary overlap window (see that function's own comment) -- unbounded tolerance would
# mean an unbounded window. 8 whitespace characters is far beyond the double-space/tab cases this
# fix targets, while keeping that window small.
_BRIDGE_ID_MARKER_MAX_WHITESPACE_RUN = 8
# Every byte width _raw_bytes_contain_bridge_marker() and _stream_scan_oversized_for_bridge_marker()
# have always covered: ASCII/UTF-8/Latin-1 all encode this marker's characters identically as
# single bytes, so one "utf-8" pattern covers all three; UTF-16 and UTF-32, little- and
# big-endian, each need their own pattern.
_BRIDGE_MARKER_ENCODINGS: tuple[str, ...] = ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")


def _json_string_body(text: str) -> str:
    # The literal text a JSON-encoded string value renders `text` as, without the surrounding
    # quotes json.dumps() would add -- e.g. a tab (U+0009) becomes the two literal characters `\t`
    # (JSON requires control characters to be escaped), and a `"` becomes the two literal
    # characters `\"`, while a plain space or letter is unchanged. Both raw-bytes marker scanners
    # search real on-disk hooks.json bytes -- JSON-serialized text, not decoded string content --
    # so both _bridge_id_marker_patterns()'s whitespace-run matching (P2-2) and
    # _bridge_id_marker_value_forms()'s quoted-value matching (P2-1/P2-3) use this one function to
    # look for what a byte sequence actually looks like on disk, not its raw/decoded form.
    #
    # This is only ONE of the RFC 8259-legal ways to render an escaped character, though --
    # section 7 permits any character to instead be written as a `\uXXXX` numeric escape, and
    # json.dumps() choosing its own shorthand (`\"`, `\t`, ...) over that is Python's choice, not a
    # universal one. A hooks.json byte sequence produced by a different encoder can legitimately
    # use `\uXXXX` for the very same characters instead -- see _json_string_body_u_escaped()'s own
    # comment for the concrete encoder this was found against and the fix this is one half of (P2-A,
    # independent Claude opus5/max review, 2026-08-21).
    return json.dumps(text)[1:-1]


# The specific characters this file's own marker text can ever need to search for in escaped form:
# the two whitespace control characters JSON requires escaping (tab, CR -- LF is also required but
# handled the same way), the quote characters `_bridge_id_marker_value_forms()`'s quoted shapes and
# shlex.quote()'s own embedded-quote rendering can introduce (`"`, `'`), and the backslash that
# quoting form also introduces literally. NOT a blanket "escape every character" list -- an ordinary
# encoder never `\uXXXX`-escapes plain ASCII letters/digits, so escaping characters outside this set
# would search for byte sequences no real encoder produces.
_BRIDGE_ID_U_ESCAPABLE_CHARS = ('"', "'", "\\", "\t", "\r", "\n")


def _json_string_body_u_escaped(text: str, *, upper: bool = False) -> str:
    # Sibling rendering to _json_string_body(): the same on-disk-bytes contract, but using the
    # `\uXXXX` numeric-escape form RFC 8259 section 7 equally permits for
    # _BRIDGE_ID_U_ESCAPABLE_CHARS, instead of json.dumps()'s own single-character shorthand. Real
    # encoders other than Python's json.dumps() legitimately choose this form by default -- .NET's
    # System.Text.Json is the reviewer's cited example: its default JavascriptEncoder escapes quote,
    # backslash, and the control characters this way, PLUS single-quote (not JSON-required at all,
    # but RFC-legal, and done by that encoder for HTML/JS-embedding safety) -- and the reviewer
    # reproduced 6 of 8 legal-JSON variants this way diverging from what the raw-bytes scanners used
    # to search for (P2-A fix, independent Claude opus5/max review, 2026-08-21). Traced to a real
    # failure path: an oversized, relocated hooks.json written by such an alternate encoder (not by
    # this tool itself -- Stage 2 exists precisely to scan files this tool did NOT write) carrying a
    # genuine handler would make the streaming scanner return False even though the handler is
    # genuinely present, and uninstall() would then delete the receipt while the handler stays live
    # -- reproducing the exact harm shape R11-P1-B already fixed for a different cause.
    #
    # `upper` picks the hex-digit case (e.g. backslash-u-005c vs backslash-u-005C for a backslash
    # character) -- JSON parsing itself is case-insensitive for numeric-escape hex digits, but
    # these raw-bytes scanners match literal bytes, not parsed values, so both cases a real encoder
    # could choose must be searched for explicitly; see _bridge_id_marker_value_forms()'s and
    # _bridge_id_marker_patterns()'s own comments for where both are generated. Characters outside
    # _BRIDGE_ID_U_ESCAPABLE_CHARS pass through unchanged -- this is not a blanket per-character
    # escape (see that constant's own comment for why).
    hex_format = "04X" if upper else "04x"
    return "".join(
        f"\\u{ord(ch):{hex_format}}" if ch in _BRIDGE_ID_U_ESCAPABLE_CHARS else ch for ch in text
    )


def _bridge_id_command_fragment(bridge_id: str) -> str:
    # The exact `--bridge-id <value>` text a real command line embeds for this bridge_id --
    # shlex.quote() renders the value bare when it needs no escaping, single-quoted when it
    # contains something like a space, or a `'...'"'"'...'`-style embedded form when the value
    # itself contains a single quote. make_release() (the sole real producer of this fragment, for
    # both the base and install-write-trigger commands) and _bridge_id_marker_value_forms() below
    # (which must recognize whatever make_release() actually emits) both call this exact function
    # instead of each independently re-deriving the same quoting, so a future change to either side
    # cannot silently desynchronize them (P2-3 fix, independent review 2026-08-21). Previously
    # make_release() built this fragment inline via its own shlex.quote() call, coincidentally
    # compatible with the marker helper's separately hand-rolled bare/single-quote forms for a
    # typical value, but with nothing enforcing or testing that coupling -- a bridge_id containing
    # a literal single quote produces shlex.quote()'s embedded-quote form, which neither hand-rolled
    # form could ever match.
    return f"--bridge-id {shlex.quote(bridge_id)}"


def _bridge_id_marker_value_forms(bridge_id: str) -> tuple[str, ...]:
    # Every textual rendering of a `--bridge-id` flag's VALUE this file's own detection or
    # construction logic could plausibly produce or need to recognize -- the single source of
    # truth both _bridge_id_marker_patterns() (the raw-bytes scanners' actual matching, below) and
    # make_release() (via _bridge_id_command_fragment(), the sole real producer of a command line)
    # draw from. Three base shapes:
    #   - bare/unquoted: the common case, and what make_release() emits for any bridge_id needing
    #     no shell escaping (this module's own BRIDGE_ID and write_candidate_capture.MODULE_ID
    #     both qualify today).
    #   - double-quoted: `"<id>"`.
    #   - single-quoted: `'<id>'`, structurally equivalent to the bare form under shlex.split()
    #     (owned_handler()'s own match).
    #   - whatever shlex.quote() actually renders for THIS value: covers every value shlex.quote()
    #     cannot express as a plain bare or single-quoted form -- concretely, a bridge_id
    #     containing a literal single quote, which shlex.quote() escapes as
    #     `'<part>'"'"'<part>'`, a form neither of the two hand-rolled quoted forms above can ever
    #     match (P2-3 fix).
    # Each base shape is then paired with every JSON-legal escaped rendering this file recognizes,
    # alongside its raw literal form: any `"` a shape contains -- from the double-quoted form
    # itself, or from a single-quote-containing bridge_id's shlex.quote() embedding, which uses a
    # literal `"` to splice segments -- is escaped in real hooks.json's on-disk bytes, either as
    # json.dumps()'s own shorthand `\"` (via _json_string_body()) or, equally legally under RFC 8259
    # section 7, as the numeric escape `\uXXXX` in either hex-digit case (via
    # _json_string_body_u_escaped()) -- a rendering json.dumps() itself never produces but other
    # encoders (e.g. .NET's System.Text.Json) legitimately do by default (P2-1 fix, independent
    # review 2026-08-21, for the json.dumps()-shorthand half: the unescaped literal-`"` forms this
    # used to search for can never match real hooks.json content byte-for-byte -- confirmed against
    # real json.dumps()-serialized fixtures, including a shlex.quote()-embedded-quote one, in this
    # file's test suite; P2-A fix, independent Claude opus5/max review, 2026-08-21, for the
    # \uXXXX-escaped half -- see _json_string_body_u_escaped()'s own comment for the concrete
    # divergence and failure path this closes). The raw literal form is kept alongside every escaped
    # one for the same reason Stage 2 exists at all: it may be scanning content that never parsed as
    # valid JSON in the first place, where JSON's escaping rules do not necessarily hold.
    # Deduplicated so a shape needing no escaping (the common case) does not produce duplicate
    # patterns.
    base_shapes = (bridge_id, f'"{bridge_id}"', f"'{bridge_id}'", shlex.quote(bridge_id))
    forms: list[str] = []
    seen: set[str] = set()
    for shape in base_shapes:
        for text in (
            shape,
            _json_string_body(shape),
            _json_string_body_u_escaped(shape, upper=False),
            _json_string_body_u_escaped(shape, upper=True),
        ):
            if text not in seen:
                seen.add(text)
                forms.append(text)
    return tuple(forms)


def _bridge_id_marker_patterns(bridge_id: str) -> tuple[tuple[re.Pattern[bytes], int], ...]:
    # Compiled `--bridge-id<whitespace-run><value>` byte regexes, one per (marker value form) x
    # (byte width) pair, paired with that pattern's own fixed maximum match length in bytes.
    # _raw_bytes_contain_bridge_marker() and _stream_scan_oversized_for_bridge_marker() both call
    # this one function and run its patterns against their own bytes, instead of each
    # independently re-deriving marker text AND re-implementing the match (P2-2 fix, independent
    # review 2026-08-21) -- before this, each scanner separately joined "--bridge-id" and a value
    # form with a single literal space, missing the whitespace-run variants Stage 1 already treats
    # as the same logical pair. Each whitespace position in the run is searched for under both its
    # raw literal byte(s) (what a non-JSON, e.g. malformed or ambiguous, candidate could contain
    # directly), its json.dumps()-style escaped rendering (what a real, valid hooks.json's on-disk
    # bytes actually contain for tab/CR/LF -- see _BRIDGE_ID_MARKER_WHITESPACE_CHARS's own comment),
    # AND its `\uXXXX`-style escaped rendering in both hex-digit cases (P2-A fix, independent Claude
    # opus5/max review, 2026-08-21 -- see _json_string_body_u_escaped()'s own comment; a real
    # hooks.json's on-disk tab/CR/LF bytes can legally take this form instead of json.dumps()'s
    # shorthand, and previously only the shorthand form was searched for). The
    # max-length half of each pair is exact (prefix length + the widest single whitespace
    # variant's encoded width * the whitespace-run bound + suffix length), not an estimate -- the
    # streaming scanner depends on it being a true upper bound, not just a typical one, to size its
    # chunk-boundary overlap window correctly.
    patterns: list[tuple[re.Pattern[bytes], int]] = []
    max_run = _BRIDGE_ID_MARKER_MAX_WHITESPACE_RUN
    for value_form in _bridge_id_marker_value_forms(bridge_id):
        for encoding in _BRIDGE_MARKER_ENCODINGS:
            prefix = "--bridge-id".encode(encoding)
            suffix = value_form.encode(encoding)
            whitespace_bytes = sorted(
                {
                    char.encode(encoding)
                    for char in _BRIDGE_ID_MARKER_WHITESPACE_CHARS
                }
                | {
                    _json_string_body(char).encode(encoding)
                    for char in _BRIDGE_ID_MARKER_WHITESPACE_CHARS
                }
                | {
                    _json_string_body_u_escaped(char, upper=upper).encode(encoding)
                    for char in _BRIDGE_ID_MARKER_WHITESPACE_CHARS
                    for upper in (False, True)
                }
            )
            widest_whitespace = max(len(chunk) for chunk in whitespace_bytes)
            whitespace_alternation = b"|".join(re.escape(chunk) for chunk in whitespace_bytes)
            pattern = re.compile(
                re.escape(prefix)
                + b"(?:"
                + whitespace_alternation
                + b"){1,"
                + str(max_run).encode()
                + b"}"
                + re.escape(suffix)
            )
            max_len = len(prefix) + widest_whitespace * max_run + len(suffix)
            patterns.append((pattern, max_len))
    return tuple(patterns)


def _raw_bytes_contain_bridge_marker(raw: bytes, *, bridge_id: str = BRIDGE_ID) -> bool:
    # Layer 2's final, genuine-ambiguity-only fallback: a whitespace-tolerant byte-pattern search
    # for the `--bridge-id <bridge_id>` marker (see _bridge_id_marker_patterns()) under every byte
    # width this file's own detection encodings could plausibly render it in. A compiled regex
    # search is not quite as cheap as the plain `bytes.__contains__` substring search this used to
    # be, but stays a single linear pass per pattern -- still independent of handler count and
    # cheap relative to Layer 1's structural parse, unlike Layer 1 which this file's own bound
    # (_STRUCTURAL_DETECTION_MAX_BYTES) exists specifically to keep away from large files. Only
    # ever reached when no encoding produced a structurally recognizable payload (or the file was
    # too large to attempt one), so a hit here is ambiguous evidence, not proof -- the caller fails
    # closed on it rather than trusting it as a positive detection. `bridge_id` defaults to
    # BRIDGE_ID -- see BRIDGE_ID's own comment; threaded through from _contains_owned_handler() so
    # Stage 2 searches for the same marker Stage 0/1 would have.
    return any(pattern.search(raw) is not None for pattern, _ in _bridge_id_marker_patterns(bridge_id))


def _attempt_structural_detection(
    raw: bytes, *, max_bytes: int, event: str = DEFAULT_HOOK_EVENT, bridge_id: str = BRIDGE_ID
) -> bool | None:
    # Layers 0+1 combined: the expensive structural parse-and-shape-check path (real JSON
    # parsing, plus -- on any successful parse -- owned_handler()'s per-handler shlex.split()
    # calls), attempted only when `raw` is within `max_bytes` (the caller picks the bound --
    # see _contains_owned_handler()'s and _find_untracked_owned_configs()'s own comments for why
    # a candidate under RUNTIME_BASE gets a much larger one). Returns None when nothing here
    # resolved a definitive answer, whether because `raw` was too large to attempt at all, or
    # because every attempt within budget failed to produce a recognizable payload -- the caller
    # (_contains_owned_handler()) treats both the same way, falling through to the cheap
    # marker-only Layer 2.
    #
    #   Layer 0 (_safe_parse_strict_utf8): the canonical interpretation. Succeeding at all --
    #   regardless of whether the parsed value then matches our shape -- is dispositive: bytes
    #   that are valid UTF-8 JSON have exactly one correct reading, so a structural mismatch here
    #   is a definitive negative, not a "try another encoding" signal.
    #
    #   Layer 1 (_lenient_decode_candidates / _lenient_parse_last_key_wins): only reached when
    #   Layer 0 found no reading at all. Retries a fixed list of plausible encodings with a more
    #   tolerant parse (last-key-wins duplicates, trailing garbage after the value). The moment
    #   any one attempt resolves to our recognizable shape (true OR false), that result is taken
    #   as final and every later encoding is skipped -- this is what keeps a proven,
    #   structurally-understood negative from ever reaching Layer 2 (self-check Workflow design
    #   round, 2026-08-18, round 1 pressure-test P2: without this short-circuit, a legitimate
    #   unrelated hooks.json that happened to mention the marker text in an unrelated field --
    #   e.g. a migration-history note, or another handler's own log-message argument -- was
    #   wrongly escalated to InstallError even though it had already been conclusively proven not
    #   to contain an owned handler; only reachable in practice for a document within `max_bytes`
    #   -- see _find_untracked_owned_configs()'s own comment for the accepted, documented residual
    #   gap above the bound it uses outside RUNTIME_BASE).
    if len(raw) > max_bytes:
        return None
    payload = _safe_parse_strict_utf8(raw)
    if payload is not _NOT_PARSED:
        return bool(_owned_shape_match(payload, event=event, bridge_id=bridge_id))
    for text in _lenient_decode_candidates(raw):
        payload = _lenient_parse_last_key_wins(text)
        if payload is _NOT_PARSED:
            continue
        match = _owned_shape_match(payload, event=event, bridge_id=bridge_id)
        if match is not None:
            return match
    return None


def _contains_owned_handler(
    raw: bytes,
    *,
    structural_max_bytes: int = _STRUCTURAL_DETECTION_MAX_BYTES,
    event: str = DEFAULT_HOOK_EVENT,
    bridge_id: str = BRIDGE_ID,
) -> bool:
    # Used only to inspect content this tool did NOT write (the
    # untracked-owned-handler safety scan below) -- unlike install()'s own
    # use of owned_handler() (always preceded by update_hook_config()'s
    # structure validation) and verify()'s (always preceded by an
    # after_sha256 digest match against a receipt this tool itself wrote),
    # this runs against genuinely arbitrary bytes: a stray file, a config
    # some other tool created, deliberately malformed content. Any parse or
    # structure failure means "cannot recognize this as one of our own
    # handlers" -- correct, since every config this tool has ever written
    # is well-formed JSON with exactly this shape -- and must never be left
    # to raise past this function (independent Claude opus5/max review,
    # 2026-08-17, round 8: R8-P1-D, the prior round's scan bare-crashed
    # with AttributeError on {"hooks": []}/{"hooks": null}/{"hooks": "x"};
    # R8-P2-A, the prior round's scan let a JSON parse failure escape past
    # its own except-and-skip guard).
    #
    # Round 11 rewrite (self-check Workflow, 2026-08-18, on top of an
    # independent Claude opus5/max round-10 finding this scan's own
    # unit-level testing had not chained through end-to-end calls): the
    # single strict_json()-or-nothing parse this used to do let a live,
    # well-formed, owned handler saved in ANY encoding other than plain
    # UTF-8 -- UTF-8 BOM, a duplicate top-level "hooks" key, trailing
    # comment text, or UTF-16 from a naive editor or backup/restore tool
    # being the realistic case, not just an adversarial one -- decode-fail
    # and silently return False, chaining through install()/verify()/
    # uninstall() into a durable, total bypass of this whole safety scan.
    # This now tries two stages, and stops at whichever produces a
    # definitive answer:
    #
    #   Stage 1 (_attempt_structural_detection): Layers 0+1, see that
    #   function's own comment -- the precise, structurally-verified answer,
    #   attempted only up to `structural_max_bytes` (round 11 default:
    #   _STRUCTURAL_DETECTION_MAX_BYTES; _find_untracked_owned_configs()
    #   passes MAX_MANAGED_FILE_BYTES -- the largest byte size that could
    #   ever reach this function at all, since _read_for_detection() already
    #   refuses anything bigger -- for a candidate under RUNTIME_BASE, since
    #   the round-11 default bound was what let a genuine, valid,
    #   plain-UTF-8, merely-large relocated handler under RUNTIME_BASE go
    #   unresolved by Stage 1 and fall to Stage 2 in the first place;
    #   independent Claude opus5/max review, 2026-08-18, round 11, R11-P1-B).
    #   Wrapped in a broad except so an unenumerated exception from the
    #   parsing chain (R11-P1-A: CPython's int-string-conversion ValueError,
    #   already caught explicitly by _safe_parse_strict_utf8() and
    #   _lenient_parse_last_key_wins() below -- this is defense-in-depth for
    #   whatever the *next* interpreter-specific parser exception turns out
    #   to be, not a replacement for those explicit catches) degrades to
    #   "Stage 1 inconclusive", exactly like a genuine parse failure, rather
    #   than escaping this function as a bare traceback.
    #
    #   Stage 2 (_raw_bytes_contain_bridge_marker): reached whenever Stage 1
    #   returned None -- either genuinely ambiguous (no attempt, across
    #   every encoding, ever produced a structurally recognizable payload)
    #   or skipped entirely for being oversized. This is a cheap,
    #   size-independent raw byte-pattern search for the marker text under
    #   any width. A hit means "cannot rule out an owned handler" and fails
    #   closed with InstallError (raising, not returning True: this stage
    #   never actually confirmed a structural match, so it must not report
    #   a false positive detection either -- only escalate the ambiguity to
    #   the caller). Unlike Stage 1's own inconclusive result, a Stage 2 hit
    #   is POSITIVE evidence (the exact marker this tool's own handlers
    #   carry, found in the raw bytes) rather than an absence of
    #   information, so -- as of round 12 -- _find_untracked_owned_configs()
    #   no longer tempers this particular raise for RUNTIME_BASE candidates
    #   the way it does for a genuine absence-of-evidence fault (unlistable
    #   directory, unreadable/oversized file): tolerating THIS raise there
    #   was exactly R11-P1-B, silently discarding positive evidence of a
    #   live handler the round's own fix exists to catch. No hit means a
    #   genuine, if softer, negative and returns False, matching this
    #   function's original conservative default for content nothing here
    #   can identify as ours.
    try:
        result = _attempt_structural_detection(
            raw, max_bytes=structural_max_bytes, event=event, bridge_id=bridge_id
        )
    except Exception:
        result = None
    if result is not None:
        return result
    if _raw_bytes_contain_bridge_marker(raw, bridge_id=bridge_id):
        raise InstallError(
            "cannot rule out an owned hook handler: content matches the bridge marker but does "
            "not parse as a recognizable hooks.json under any supported encoding"
        )
    return False


def _stream_scan_oversized_for_bridge_marker(path: Path, *, bridge_id: str = BRIDGE_ID) -> bool:
    # Bounded, constant-memory alternative to _raw_bytes_contain_bridge_marker()
    # for a regular, readable file that only failed _read_for_detection()'s
    # check for exceeding MAX_MANAGED_FILE_BYTES -- reading the whole thing
    # into one `bytes` object the way _read_for_detection() does is exactly
    # what that size cap exists to prevent. Reads in fixed-size chunks
    # directly from an O_NOFOLLOW file descriptor (same open-time symlink
    # protection as _read_for_detection()), keeping only a small overlap
    # window between chunks (one byte short of the marker's widest encoded
    # form) so a marker split across a chunk boundary is still found -- never
    # holds more than one chunk plus that overlap in memory at once,
    # regardless of total file size (round 13, closing R12-P1-A / P2-R12-A:
    # independent Claude opus5/max and Codex sol/gpt-5.6-terra round-12
    # reviews both found, and both suggested this exact fix shape -- a
    # genuine live relocated handler merely padded past MAX_MANAGED_FILE_BYTES
    # under RUNTIME_BASE was silently abandoned because _read_for_detection()'s
    # size-cap raise used to be blanket-tolerated there as if an oversized
    # file's content was simply unknowable, rather than cheaply scannable).
    # Only ever called on a candidate _find_untracked_owned_configs() has
    # already confirmed is a regular file under RUNTIME_BASE too large for
    # _read_for_detection(); does not re-derive that condition itself.
    # `bridge_id` defaults to BRIDGE_ID and is threaded through from
    # _find_untracked_owned_configs() -- see BRIDGE_ID's own comment and this file's header
    # comment. A fix round (2026-08-20) parameterized every other function in the detection chain
    # but missed this one and its caller, so an oversized hooks.json carrying a real handler under
    # any bridge_id OTHER than the module default silently returned False here even though the
    # same content, if it had fit under MAX_MANAGED_FILE_BYTES, was already correctly found by the
    # (already-parameterized) whole-file-read path -- converged independent Claude opus/max and
    # Codex gpt-5.6-sol/max review, 2026-08-20. As of 2026-08-21 (P2-2) the marker match itself is
    # whitespace-tolerant, shared with _raw_bytes_contain_bridge_marker() via
    # _bridge_id_marker_patterns() rather than each scanner hand-rolling its own literal-space-only
    # variant list -- see that function's own comment for the max-match-length bound this overlap
    # window relies on.
    patterns = _bridge_id_marker_patterns(bridge_id)
    overlap_len = max(max_len for _, max_len in patterns) - 1
    chunk_size = 1_048_576  # 1 MiB
    descriptor = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            return False
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise InstallError(f"file identity changed while inspecting {path}")
        tail = b""
        while True:
            chunk = os.read(descriptor, chunk_size)
            if not chunk:
                return False
            window = tail + chunk
            if any(pattern.search(window) is not None for pattern, _ in patterns):
                return True
            tail = window[-overlap_len:] if overlap_len else b""
    except OSError as exc:
        raise InstallError(f"cannot read {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _find_untracked_owned_configs(
    receipt_paths: set[str], *, bridge_id: str = BRIDGE_ID, event: str = DEFAULT_HOOK_EVENT
) -> list[str]:
    # Effectively public (called by uninstall()'s and recover_pending_install()'s own already-
    # validated paths today, but reachable independently by any external caller of this module, and
    # a future SessionEnd-wiring caller passes externally-influenced values here directly) --
    # validated the same way update_hook_config() validates its own bridge_id/event, via the shared
    # helpers, not assumed already-valid by an upstream caller (AUTO-LEARN-TRIGGER-DESIGN-
    # 2026-08-19.md section 19.3 item 4 / section 26.1). This also protects the whole internal
    # detection chain this function alone drives -- _contains_owned_handler(),
    # _attempt_structural_detection(), _owned_shape_match(), _raw_bytes_contain_bridge_marker(),
    # _stream_scan_oversized_for_bridge_marker() -- none of which has any other call site in this
    # file, so validating once here at the entry point covers all of them.
    _validate_bridge_id(bridge_id)
    _validate_hook_event(event)
    # `bridge_id` defaults to BRIDGE_ID and is forwarded, unchanged, to both detection calls this
    # loop makes below (_stream_scan_oversized_for_bridge_marker() for an oversized candidate under
    # RUNTIME_BASE, _contains_owned_handler() for every other candidate) -- see BRIDGE_ID's own
    # comment and this file's header comment for why. `event` defaults to DEFAULT_HOOK_EVENT and is
    # forwarded only to the _contains_owned_handler() call below -- _stream_scan_oversized_for_bridge_
    # marker()'s Stage-2-only raw-marker scan is event-agnostic by construction (it never inspects
    # which event key a handler lives under), so there is nothing to forward it into there. install()
    # itself never calls this function at all (it only writes handlers, via update_hook_config(), never
    # scans for untracked ones), so it is not a source of either override.
    #
    # As of the P2-D fix (independent Claude opus5/max review, 2026-08-21), this function IS actually
    # called a second (and, as of the P2-2 fix below, sometimes third) time with
    # bridge_id=<write-trigger identity>, event="SessionEnd" -- by
    # _untracked_owned_including_write_trigger(), the shared helper uninstall() and
    # recover_pending_install() both now call instead of this function directly. This corrects an
    # earlier version of this comment, which described a hypothetical "future SessionEnd-wiring caller"
    # passing these overrides as if it already existed; an independent review (2026-08-21) checked and
    # confirmed no such caller was reachable anywhere in the file at the time -- install() only ever
    # writes the base and write-trigger handlers together in one pass, never incrementally via a
    # separate untracked-scan-then-register step, and no call site passed remove=True for the
    # write-trigger identity, so the described caller was not just future work but a real,
    # defense-in-depth gap: the untracked-owned-handler safety net was never actually exercised for the
    # write-trigger identity in any reachable code path. That gap is now closed for uninstall()'s and
    # recover_pending_install()'s own safety scans (both call sites _find_untracked_owned_configs() has
    # today); a hypothetical incremental register-only-write-trigger flow, if one is ever added, would
    # still need its own explicit call here, not something this fix retroactively provides.
    #
    # P2-D's own first version gated the write-trigger-identity call entirely on
    # `receipt.get("write_trigger_bridge_id") is not None` -- a real, reproduced gap (P2-2 fix,
    # independent Claude opus5/max review, 2026-08-21): an ordinary, unrelated LATER plain install()
    # (no write_trigger= argument -- e.g. redeploying just a claude_memory_hook.py fix) overwrites
    # latest-receipt.json with a receipt that has no write_trigger_bridge_id field at all (install()'s
    # own write_trigger_receipt_fields is `{}` for that call), even though nothing about a plain
    # install() removes an already-registered SessionEnd handler a PRIOR install-write-trigger call
    # added (update_hook_config()'s layering is additive per-event, not a full reset) -- so the very
    # next uninstall()/recover_pending_install() silently stopped checking the write-trigger identity
    # at exactly the moment a genuinely live, possibly-orphaned write-trigger handler could still be
    # sitting at an untracked path, reintroducing the exact harm P2-D closed. See
    # _untracked_owned_including_write_trigger()'s own comment for the fix: it now always additionally
    # scans under the well-known write_candidate_capture.MODULE_ID identity (checked against real,
    # live hooks.json content, not derived from this one call's own receipt), regardless of what the
    # CURRENT receipt happens to record -- correct for every install-call ordering, not just the one
    # the reviewer reproduced.
    #
    # `event` threading itself (fix round, 2026-08-20: this parameter was missing entirely -- unlike
    # bridge_id, which a prior round in the same series threaded through both calls below -- so Stage 1
    # (_attempt_structural_detection(), reached via _contains_owned_handler()) always checked the
    # candidate's DEFAULT_HOOK_EVENT handler list even when the real handler being searched for lived
    # under a different event, definitively returning False/None-collapsed-to-False for an
    # ordinary-sized hooks.json containing both an existing UserPromptSubmit handler and a real
    # non-default-event handler, so Stage 2's marker-based fallback never even ran -- the same shape of
    # gap as the bridge_id one just closed, reachable at any file size, not just oversized ones).
    #
    # Broader-than-discovery safety scan for uninstall()'s /
    # recover_pending_install()'s untracked-owned-handler check (see their
    # comments): a directory move that relocates a managed account outside
    # the one specific shape _enumerate_hook_configs() understands (exactly
    # one level under codex-accounts/, at home/hooks.json) is still
    # invisible to ordinary discovery, but must not be invisible to the one
    # check whose entire job is proving no live handler is about to be
    # abandoned when a receipt is about to be permanently deleted
    # (independent Claude opus5/max review, 2026-08-17, round 8, R8-P1-B,
    # reproduced with an account moved into a subfolder, moved out of
    # codex-accounts entirely, and with its own home/ subdirectory
    # renamed -- none of which discover_hook_configs()'s one-level shape
    # can see, all of which still leave a live, owned handler running).
    #
    # Walks the whole local-homes tree -- bounded to real disk contents,
    # never following symlinked directories (so a symlink cycle cannot
    # cause an infinite walk) -- for every file named exactly "hooks.json",
    # INCLUDING inside this bridge's own RUNTIME_BASE (round 11, self-check
    # Workflow, 2026-08-18, on top of an independent Claude opus5/max
    # round-10 finding: an earlier version of this scan pruned RUNTIME_BASE
    # out of the walk entirely on the theory that its backup files are
    # never named literally "hooks.json" -- true, but that same reasoning
    # also means nothing real is lost by walking in, and a directory move
    # that relocates a managed account INTO somewhere under RUNTIME_BASE --
    # e.g. a naive backup/restore tool that preserves original filenames
    # while staging content there -- was invisible to the pruned version,
    # the exact class of blind spot this whole function exists to close for
    # every other subtree). A directory (or the root itself) that is simply
    # gone (ENOENT) degrades that branch to "nothing there" -- normal, and
    # what lets a genuinely deleted account (R3-P1-A) or a genuinely absent
    # local-homes tree not abort the caller's transaction. A directory that
    # EXISTS but cannot be listed for any other reason (EACCES, EIO, ...) is
    # exactly R8-P2-B's class of bug if silently skipped -- os.walk()'s own
    # default (`onerror=None`) does silently skip it, so an explicit
    # `onerror` callback below escalates that specific case to a clean
    # InstallError instead (independent Claude opus5/max review,
    # 2026-08-17, round 8, R8-P1-C for the ENOENT-tolerant half, R8-P2-B
    # for the EACCES-must-not-be-silent half) -- EXCEPT specifically inside
    # RUNTIME_BASE, where an unlistable directory, an unsearchable directory
    # (_is_regular_file()'s own raise), an unreadable file, and (since round
    # 13) an oversized-and-unreadable file all instead degrade to "nothing
    # found there", same as ENOENT (a readable-but-merely-oversized file is
    # scanned instead -- see _find_untracked_owned_configs()'s own
    # _CandidateTooLargeForDetection handling below, and
    # _stream_scan_oversized_for_bridge_marker()'s comment, for why that one
    # case gets a bounded look at its content rather than blanket tolerance;
    # independent Claude opus5/max review, 2026-08-19, round 13, R13-P2-A
    # corrected this paragraph to distinguish the two) (self-check Workflow, 2026-08-18, round 2
    # pressure-test: RUNTIME_BASE's backups/releases directories are never
    # pruned -- this file's own design, documented at their creation sites
    # -- and accumulate for the tool's entire lifetime, so an ordinary,
    # non-adversarial permission fault or an oversized stray file there -- a
    # stray quarantine flag, imperfect backup/restore tooling, a UID
    # mismatch after moving the external SSD between machines (this repo's
    # whole operating domain) -- becomes steadily more likely over time and,
    # if still fail-closed here, would permanently hard-block BOTH
    # uninstall() and recover_pending_install() (the tool's own
    # crash-recovery path) with no self-healing action available, since
    # RUNTIME_BASE content is never pruned by this tool.
    #
    # Round 12 narrowing (independent Claude opus5/max review, 2026-08-18,
    # round 11, R11-P1-B): this tolerance covers only genuine ABSENCE of
    # evidence -- we could not even determine whether the path is a regular
    # file, or could not even read its bytes. It does NOT extend to
    # _contains_owned_handler()'s own raise (its Stage 2: the raw bytes were
    # read successfully and structurally matched this tool's marker, but
    # could not be structurally verified) -- that is POSITIVE evidence of a
    # possible live handler, not an absence of information, and tolerating
    # it here was exactly R11-P1-B: a live, valid, plain-UTF-8 relocated
    # handler padded past the structural-detection size bound was silently
    # abandoned, the identical end-state this whole round exists to
    # prevent. To make that raise rare rather than routine for a genuine
    # relocated account, _contains_owned_handler() is called below with
    # MAX_MANAGED_FILE_BYTES (not the smaller _STRUCTURAL_DETECTION_MAX_BYTES
    # default) as its structural-detection bound specifically for candidates
    # under RUNTIME_BASE -- the largest size _read_for_detection() could ever
    # hand it at all -- so a genuine relocated handler of any size this tool
    # would ever accept gets a definitive, structurally-verified answer
    # instead of falling through to the ambiguous marker-only stage; see
    # _contains_owned_handler()'s own comment for the measured cost (tens of
    # milliseconds, and only ever paid for a file actually named
    # "hooks.json" under RUNTIME_BASE, which real bridge operation never
    # produces).
    #
    # Two things make the narrower "nothing found there" policy the right
    # trade-off specifically for RUNTIME_BASE, unlike every other subtree:
    # (1) every real file this tool ever writes there is deliberately never
    # named literally "hooks.json" (grep-confirmed against every
    # atomic_write() call site; atomic_write()'s own mkstemp()-based
    # temp-file naming means no transient bare "hooks.json" ever appears
    # mid-write either), so an unlistable/unsearchable subdirectory or an
    # unreadable file here can only ever be hiding non-candidate files --
    # never the one filename this scan is actually looking for; and (2)
    # RUNTIME_BASE and everything under it is created exclusively by
    # ensure_private_dir()/atomic_write() at mode 0o700/0o600 under this
    # process's own uid, so a THIRD PARTY managing to place unreadable
    # content here already requires a level of access (the same uid, or
    # root) that makes this scan's fail-closed protection close to
    # worthless as a defense against them anyway. Trading that narrow,
    # low-value blind spot for keeping the tool's own recovery path from
    # becoming permanently unusable is the better default. The equivalent
    # absence-of-evidence fault OUTSIDE RUNTIME_BASE, and a Stage-2 marker
    # hit anywhere (in or out of RUNTIME_BASE), are both deliberately left
    # fail-closed, with the offending path included in the error, so an
    # operator hitting either case can actually locate and act on it.
    try:
        root = resolve_ssd_path(LOCAL_HOMES_ROOT, must_exist=False)
    except InstallError:
        return []
    try:
        runtime_root = resolve_ssd_path(RUNTIME_BASE, must_exist=False)
    except InstallError:
        runtime_root = None

    def _is_under_runtime_root(path: Path) -> bool:
        return runtime_root is not None and (path == runtime_root or is_relative_to(path, runtime_root))

    def _raise_on_unlistable_directory(exc: OSError) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        if exc.filename and _is_under_runtime_root(Path(exc.filename)):
            return
        raise InstallError(f"cannot list {exc.filename}: {exc}") from exc

    untracked_owned: list[str] = []
    for dirpath, _dirnames, _filenames in os.walk(root, onerror=_raise_on_unlistable_directory, followlinks=False):
        current = Path(dirpath)
        candidate = current / "hooks.json"
        # _is_regular_file(candidate): absence-of-evidence tolerance too --
        # a directory that is listable but not searchable (missing the
        # execute bit) makes the stat() inside _is_regular_file() fail with
        # EACCES even though os.walk()'s own scandir() succeeded, so the
        # onerror callback above never sees it (independent Claude opus5/max
        # review, 2026-08-18, round 11, R11-P2-A: this call used to sit
        # outside every try block in this loop, so that specific shape hard-
        # blocked uninstall()/recover_pending_install()/install() even
        # though the round's own stated invariant is that every fault class
        # in RUNTIME_BASE degrades gracefully). Broad `except Exception`,
        # not `except InstallError`, here and on the _read_for_detection()
        # call below: both are absence-of-evidence calls (can we determine
        # this is a file; can we read it), so any failure of either --
        # enumerated or not -- means "no information", the same fail-open
        # verdict regardless of exactly which exception type surfaced it.
        try:
            is_candidate = _is_regular_file(candidate)
        except Exception as exc:
            if _is_under_runtime_root(current):
                continue
            raise InstallError(f"{exc} (at {candidate})") from exc
        if not is_candidate:
            continue
        try:
            resolved = resolve_ssd_path(candidate)
        except InstallError:
            continue
        candidate_str = os.fspath(resolved)
        if candidate_str in receipt_paths:
            continue
        # _read_for_detection(), not validate_owned_file(): this is
        # detecting whether a live handler exists, not deciding whether to
        # trust the file enough to rewrite it -- see that function's own
        # comment and R9-P1-B. None means "not a file we can identify"; a
        # candidate too large for _read_for_detection() to load whole is
        # handled separately below (it is readable, so it is not genuine
        # absence-of-evidence); every other failure (cannot open/read,
        # identity changed mid-read) is absence-of-evidence, same
        # tolerance/re-raise policy as _is_regular_file() above.
        try:
            candidate_raw = _read_for_detection(resolved)
        except _CandidateTooLargeForDetection as exc:
            if not _is_under_runtime_root(resolved):
                # Outside RUNTIME_BASE this stays exactly as fail-closed as
                # any other inspection failure there -- a genuinely large
                # third-party hooks.json is a real, if rare, possibility, and
                # this scan has no special reason to trust it the way it
                # trusts RUNTIME_BASE's own never-writes-hooks.json invariant.
                raise InstallError(f"{exc} (at {candidate_str})") from exc
            # Round 13 (closing R12-P1-A / P2-R12-A): unlike every other
            # _read_for_detection() failure, "too large" does not mean "no
            # information" -- the file is perfectly readable, just bigger
            # than MAX_MANAGED_FILE_BYTES, so a bounded streaming scan can
            # still cheaply establish whether the marker is present without
            # ever loading the whole file, closing the exact gap both
            # independent round-12 reviewers found: a genuine live relocated
            # handler merely padded past the size cap was silently abandoned
            # because this raise used to be blanket-tolerated the same way
            # EACCES/identity-changed are.
            try:
                marker_found = _stream_scan_oversized_for_bridge_marker(resolved, bridge_id=bridge_id)
            except Exception:
                # The scan itself failing (EACCES, EIO, the file vanishing or
                # being replaced mid-scan) IS genuine absence of evidence --
                # exactly the class this whole block otherwise tolerates
                # under RUNTIME_BASE, not the "we have positive evidence but
                # can't verify it" case the marker-hit branch below is for
                # (independent Claude opus5/max review, 2026-08-19, round 13,
                # R13-P2-A: a candidate that is BOTH oversized AND unreadable
                # -- e.g. a root-owned quarantine copy left by `sudo cp` under
                # RUNTIME_BASE/backups/, which is never pruned -- used to be
                # tolerated pre-round-13 via _read_for_detection()'s own
                # blanket except-Exception tolerance; the streaming scan's
                # own read failure was escalating unconditionally instead of
                # falling into that same tolerance, regressing the exact
                # invariant this function's own docstring and the comment
                # above _find_untracked_owned_configs()'s loop both still
                # promise): move on to the next candidate, same as every
                # other absence-of-evidence outcome in this loop.
                continue
            if marker_found:
                raise InstallError(
                    f"cannot rule out an owned hook handler: oversized content under RUNTIME_BASE "
                    f"matches the bridge marker (at {candidate_str})"
                ) from exc
            continue
        except Exception as exc:
            if _is_under_runtime_root(resolved):
                continue
            raise InstallError(f"{exc} (at {candidate_str})") from exc
        if candidate_raw is None:
            continue
        # _contains_owned_handler()'s own raise is POSITIVE evidence (its
        # Stage 2 marker hit), never absence-of-evidence -- always re-raise
        # with the path, regardless of RUNTIME_BASE (round 12, R11-P1-B; see
        # the long comment above this loop). The larger structural-detection
        # bound for RUNTIME_BASE candidates is what keeps this raise rare
        # for a genuine relocated account rather than routine.
        structural_max_bytes = MAX_MANAGED_FILE_BYTES if _is_under_runtime_root(resolved) else _STRUCTURAL_DETECTION_MAX_BYTES
        try:
            owned = _contains_owned_handler(
                candidate_raw, structural_max_bytes=structural_max_bytes, event=event, bridge_id=bridge_id
            )
        except Exception as exc:
            raise InstallError(f"{exc} (at {candidate_str})") from exc
        if owned:
            untracked_owned.append(candidate_str)
    return sorted(dict.fromkeys(untracked_owned))


def _untracked_owned_including_write_trigger(receipt: dict[str, Any], receipt_paths: set[str]) -> list[str]:
    # Shared by uninstall() and recover_pending_install() -- the only two callers of the
    # untracked-owned-handler safety scan -- so both run it under the base identity (BRIDGE_ID,
    # DEFAULT_HOOK_EVENT, this function's own defaults) AND under the write-trigger identity too
    # (event "SessionEnd" -- the same event every real write-trigger handler is actually registered
    # under; see install()'s own update_hook_config() call for that handler).
    #
    # Closes a real, previously-open defense-in-depth gap (P2-D fix, independent Claude opus5/max
    # review, 2026-08-21): before this, the untracked-owned-handler safety net -- the check that is
    # supposed to refuse an uninstall/recovery that would abandon a live, owned handler at a
    # receipt-untracked path -- never actually ran under the write-trigger identity anywhere in this
    # file, even though nothing in the scan's own design prevented it; only the base identity was
    # ever checked. Not yet exploitable when this was found (install() only ever writes the base and
    # write-trigger handlers together in one pass, and no call site passed remove=True for the
    # write-trigger identity alone), but a write-trigger handler relocated to an untracked path the
    # same way R8-P1-B already covers for the base handler would have been silently abandoned by
    # uninstall()/recover_pending_install() exactly like the base identity used to be. See
    # _find_untracked_owned_configs()'s own comment (the corrected version of the comment this fix
    # also fixes -- it used to describe this as a hypothetical future caller instead of a gap to
    # close now).
    #
    # P2-D's first version decided whether to run the write-trigger-identity scan at all by checking
    # `receipt.get("write_trigger_bridge_id") is not None` -- gating it on a field of the ONE receipt
    # this particular call happens to be looking at. Opus reproduced a real, second-most-realistic
    # operational sequence this breaks (P2-2 fix, independent Claude opus5/max review, 2026-08-21):
    # install WITH write_trigger (receipt now records write_trigger_bridge_id, safety net correctly
    # covers it) -> a LATER, entirely ordinary plain install() with no write_trigger= argument (e.g.
    # redeploying just a claude_memory_hook.py fix, independent of any write-trigger decision) writes
    # a NEW receipt whose write_trigger_receipt_fields is `{}` (install()'s own logic for a call with
    # no write_trigger=) -- even though nothing about that plain install touches the write-trigger's
    # SessionEnd handler at all (update_hook_config()'s per-event layering is additive, not a full
    # reset, so a handler a PRIOR install-write-trigger call registered is still genuinely live). The
    # pre-fix guard above then saw no write_trigger_bridge_id on this NEW receipt and silently stopped
    # checking that identity from that point on -- even though a write-trigger handler could still be
    # sitting, live, at a path this receipt does not track. The very next uninstall() would then
    # succeed and delete the receipt while that handler stayed orphaned -- silently reintroducing the
    # exact harm P2-D was supposed to close.
    #
    # Fixed by not deriving the decision from any one receipt's own field at all: this now
    # unconditionally ALSO scans under the well-known WRITE_TRIGGER_BRIDGE_ID identity -- the one
    # real value the one real producer (_load_write_trigger_config(), see its own comment) has ever
    # used, and (as of the P1 fix on _validate_write_trigger_argument()) the ONLY value make_release()
    # will ever accept -- checked directly against real, live hooks.json content via
    # _find_untracked_owned_configs() itself, exactly the same way the base identity is always
    # unconditionally checked. A write-trigger handler that is not actually present anywhere simply
    # yields an empty scan result, same as before; a genuinely orphaned one is now caught regardless
    # of what any specific call's own receipt happens to record, correct for every install-call
    # ordering (write-trigger-then-plain, plain-then-write-trigger, write-trigger-then-write-trigger-
    # again, ...), not just the one sequence the reviewer reproduced. `receipt`'s own
    # `write_trigger_bridge_id`, when present, is still ALSO scanned (defense in depth: a receipt
    # written by a hypothetical older/corrupted producer that ever recorded a different identity is
    # still covered, not silently dropped) -- simply no longer the sole basis for whether the
    # write-trigger identity is checked at all.
    #
    # Deliberately uses the hardcoded WRITE_TRIGGER_BRIDGE_ID constant here instead of
    # _import_write_candidate_capture()/`_wtc.MODULE_ID` (round-52 fix, converged independent Claude
    # opus/max + Codex gpt-5.6-sol/max review, 2026-08-21): this function is called from uninstall()'s
    # and recover_pending_install()'s own safety scans, which must both keep working even when
    # write_candidate_capture.py -- a currently-untracked file in this repo's own git history -- is
    # genuinely absent from disk (e.g. after `git checkout`/`git clean`/a fresh clone that does not
    # preserve untracked files). Before this fix, the unconditional import here meant a plain,
    # no-write-trigger uninstall()/recover_pending_install() call raised an uncaught
    # ModuleNotFoundError straight past main()'s own `except InstallError` handler -- a total
    # lockout, including of the emergency-recovery path itself. See WRITE_TRIGGER_BRIDGE_ID's own
    # comment for why this constant can never silently drift from the real module's MODULE_ID.
    #
    # Returns the combined, deduplicated list of untracked paths found under any identity: a single
    # path found under more than one identity is still just one path an operator needs to go
    # investigate, not a duplicated entry reporting the same fact twice.
    untracked = set(_find_untracked_owned_configs(receipt_paths))
    write_trigger_identities = {WRITE_TRIGGER_BRIDGE_ID}
    receipt_write_trigger_bridge_id = receipt.get("write_trigger_bridge_id")
    if receipt_write_trigger_bridge_id is not None:
        write_trigger_identities.add(receipt_write_trigger_bridge_id)
    for bridge_id in sorted(write_trigger_identities):
        untracked |= set(_find_untracked_owned_configs(receipt_paths, bridge_id=bridge_id, event="SessionEnd"))
    return sorted(untracked)


def make_handler(
    command: str, *, timeout: int | None = DEFAULT_HOOK_TIMEOUT, status_message: str = DEFAULT_STATUS_MESSAGE
) -> dict[str, Any]:
    # `timeout=None` omits the "timeout" key entirely rather than writing a null/0 -- required for
    # a SessionEnd handler (write_candidate_capture.py:2330-2336's own documented wiring): that
    # module's scan has unbounded latency by design (it is the entire reason SessionEnd, not
    # UserPromptSubmit, is the right event -- Codex's hook schema has no output contract for
    # SessionEnd, so nothing downstream reads or times out on this handler's return), and a
    # "timeout": 5 key would directly contradict that. The default (DEFAULT_HOOK_TIMEOUT, i.e. 5)
    # is unchanged for every existing/default call, so the live UserPromptSubmit handler's shape
    # stays byte-for-byte identical to before this parameter existed (independent Claude opus5/max
    # whole-candidate acceptance review, 2026-08-20).
    #
    # `timeout`'s two "no timeout key value" spellings are NOT the same and are easy to confuse:
    # `None` means "omit the key" (Codex's hook runner treats a handler with no "timeout" key as
    # having no limit at all -- this is what a SessionEnd handler needs, per the comment above).
    # `0` is a different, legal value that is NOT special-cased here -- it is written literally as
    # `"timeout": 0`, and a hook runner reading that would reasonably interpret it as "expire
    # immediately" (the opposite of "no limit"). Nothing in this file ever passes `timeout=0`
    # today; a future caller must not assume it means "no timeout" -- pass `None` for that.
    hook: dict[str, Any] = {
        "type": "command",
        "command": command,
        "statusMessage": status_message,
    }
    if timeout is not None:
        hook["timeout"] = timeout
    return {"hooks": [hook]}


def update_hook_config(
    raw: bytes,
    command: str,
    *,
    event: str = DEFAULT_HOOK_EVENT,
    bridge_id: str = BRIDGE_ID,
    timeout: int | None = DEFAULT_HOOK_TIMEOUT,
    status_message: str = DEFAULT_STATUS_MESSAGE,
    remove: bool = False,
) -> bytes:
    # Guard clauses for the two identity-shaped parameters added across the last two rounds
    # (independent Claude opus/max + Codex gpt-5.6-sol/max review, 2026-08-20): before this, a
    # `bridge_id` that was `None` (contradicting the `str` annotation), or an empty string, was
    # silently accepted and produced confusing downstream behavior -- registration not staying
    # idempotent, detection unconditionally returning False -- rather than a clear rejection at the
    # point the bad value entered this API. Delegated to the shared _validate_bridge_id()/
    # _validate_hook_event() (2026-08-21) so the same class of check also protects the other
    # effectively-public entry points that accept these identities -- owned_handler() and
    # _find_untracked_owned_configs() -- rather than only this function; see those helpers' own
    # comments and AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md section 19.3 item 4 / section 26.1.
    _validate_bridge_id(bridge_id)
    _validate_hook_event(event)
    payload = strict_json(raw)
    if not isinstance(payload, dict) or set(payload) != {"hooks"} or not isinstance(payload["hooks"], dict):
        raise InstallError("unexpected hooks.json structure")
    hooks = payload["hooks"]
    if event in hooks:
        # Present-but-wrong-shape is real corruption (or a foreign tool's incompatible use of
        # this same key) and must still fail closed exactly as before -- only a genuinely absent
        # key is treated as "nothing registered under this event yet" (below).
        event_handlers = hooks[event]
        if not isinstance(event_handlers, list):
            raise InstallError(f"malformed {event} hook list")
    elif event != DEFAULT_HOOK_EVENT:
        # A genuinely new, non-default event (e.g. "SessionEnd") legitimately has no key yet on
        # an otherwise-correct hooks.json -- initialize an empty list to register into, instead of
        # refusing to install under an event this file did not previously know about (AUTO-LEARN-
        # TRIGGER-DESIGN-2026-08-19.md section 4.2).
        event_handlers = []
    else:
        # DEFAULT_HOOK_EVENT ("UserPromptSubmit") missing entirely must still fail closed exactly
        # as before the SessionEnd capability above was added (independent dual review,
        # 2026-08-20, fail-closed regression). install()/plan() call this function with zero
        # non-default args, and _enumerate_hook_configs() includes every
        # codex-accounts/*/home/hooks.json unconditionally -- a freshly-discovered account config
        # can genuinely have no "UserPromptSubmit" key yet (e.g. only a SessionStart hook so far).
        # The `else` branch above's prior claim that a missing key was "unreachable... on any
        # already-bridged or freshly-discovered live config" was correct for every OTHER event but
        # wrong for exactly this one, which is the one every real install()/plan() call actually
        # uses; without this branch, that case silently created the key and proceeded instead of
        # raising as it always did before the SessionEnd change.
        raise InstallError(f"missing {event} hook list")
    # `bridge_id` (ownership detection/removal) and `timeout` (the handler this call installs)
    # default to BRIDGE_ID/DEFAULT_HOOK_TIMEOUT -- every call site in this file today passes
    # neither, so the live UserPromptSubmit path is unaffected. Threading `timeout` through here
    # (not just make_handler() itself) is what makes make_handler()'s `timeout=None` behavior
    # actually reachable through this, the only real config-writing entry point (independent
    # Claude opus5/max whole-candidate acceptance review, 2026-08-20: previously `timeout` was
    # exercisable only by calling make_handler() directly, never through update_hook_config()).
    retained = [handler for handler in event_handlers if not owned_handler(handler, bridge_id=bridge_id)]
    if not remove:
        retained.append(make_handler(command, timeout=timeout, status_message=status_message))
    hooks[event] = retained
    return canonical_json(payload)


def atomic_write(path: Path, raw: bytes, mode: int = 0o600) -> None:
    # Every OSError from this function used to propagate raw, uncaught, all
    # the way past main()'s `except InstallError` -- crashing into a Python
    # traceback instead of the clean {"ok": false, "error": ...} every other
    # failure mode in this file produces. Two real call sites hit this: the
    # PENDING_PATH write in install() (outside its own try block) and the
    # per-row rollback write in recover_pending_install() -- the latter is
    # the most dangerous, since it means an unrecoverable pending journal
    # would end in a bare traceback with no structured signal at all
    # (independent Claude opus5/max review, 2026-08-17, round 1, P2-1).
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    except OSError as exc:
        raise InstallError(f"cannot prepare write for {path}") from exc
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
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise InstallError(f"cannot write {path}") from exc
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


def _validate_write_trigger_argument(write_trigger: Any) -> None:
    # Full-shape validation of make_release()'s optional `write_trigger` argument -- see that
    # function's own comment for the argument's shape ({"bridge_id": str, "max_candidates_per_project":
    # int, "max_candidate_bytes": int}). The prior round (P2-C) only validated `bridge_id` here
    # (a bare _validate_bridge_id() call, accepting any non-empty string); the round after that
    # (P2-1) added full-shape validation for the two sibling limit fields, but two gaps remained
    # (round-52 dual review, independent Claude opus/max + Codex gpt-5.6-sol/max, 2026-08-21,
    # converging on the same root cause: receipt/argument-field-based identity tracking is fragile):
    #
    #   1. [P1] `bridge_id` was checked only for being a non-empty string, never for being the ONE
    #      real, well-known identity (WRITE_TRIGGER_BRIDGE_ID) every real caller has ever used. A
    #      caller-supplied custom bridge_id installed a real, permanently live SessionEnd handler
    #      that this file's own untracked-owned-handler safety scan and verify() -- both of which
    #      only ever look for the well-known identity -- could never detect: `ok: true` forever,
    #      zero real output forever (Codex, reproduced end to end). Fixed below by requiring exact
    #      equality against WRITE_TRIGGER_BRIDGE_ID (see that constant's own comment) instead of
    #      merely "any non-empty string". Since the only real producer of this argument
    #      (_load_write_trigger_config()) has always hardcoded exactly this value, and there is no
    #      supported way to register under any other identity through the real CLI, this closes the
    #      gap without narrowing any real, reachable use.
    #
    #   2. [P2, opus + Codex, same finding] The prior round's own "full dict shape validation" claim
    #      did not actually hold: the synthetic dict this function builds for
    #      _parse_write_trigger_block() below is assembled key-by-key from write_trigger, so any
    #      EXTRA/unexpected key on the caller's original dict was silently dropped before it ever
    #      reached that validator -- and the ORIGINAL dict (not the synthetic one) is what
    #      make_release() later feeds straight into canonical_json() for the release-key computation
    #      (see that function), so an extra key holding a non-JSON-serializable value (a `set`,
    #      `Path`, `bytes`, arbitrary object) or a non-string key still produced a bare, uncaught
    #      TypeError -- the exact failure class this validation exists to eliminate. Fixed below by
    #      requiring write_trigger's key set to be EXACTLY {"bridge_id", "max_candidates_per_project",
    #      "max_candidate_bytes"} -- no more, no fewer -- checked FIRST, before any field is consumed
    #      or reaches canonical_json() at all.
    #
    #      This also resolves the reviewers' related "`enabled` hardcoded True regardless of caller
    #      intent" observation, investigated rather than assumed: `enabled` was never part of this
    #      argument's own contract to begin with -- _load_write_trigger_config(), the only real
    #      producer, never includes it (its return statement's dict has exactly the 3 keys required
    #      here), and the whole reason a caller passes write_trigger to make_release()/install() at
    #      all is to mean "install this release WITH the write-trigger handler enabled" -- non-None
    #      write_trigger IS the enable signal at this call boundary, not a field inside it. With the
    #      exact-key-set check above, a caller who does pass `enabled` (True, False, or any other
    #      value) now gets a clean, immediate InstallError for an unexpected key, rather than that
    #      value being silently ignored -- there is no scenario left where a caller's `enabled` value
    #      is accepted and then not honored.
    #
    # Fixed by reusing write_candidate_capture._parse_write_trigger_block() itself -- the actual
    # runtime validator this data has to satisfy -- rather than reimplementing a second, potentially-
    # divergent validator here, the same reuse discipline _load_write_trigger_config() already
    # established for the on-disk policy.json shape (see that function's own comment, and
    # _import_write_candidate_capture()'s). The two schemas are siblings, not identical: the
    # *argument* to make_release() carries `bridge_id` (this release's own command-construction
    # value, validated separately below -- it is not part of the on-disk schema at all); the
    # *on-disk* policy["write_trigger"] block make_release() itself later writes (further down in
    # that function) carries `enabled` (always hardcoded True there) in bridge_id's place instead.
    # This function builds the synthetic, on-disk-shaped dict _parse_write_trigger_block() actually
    # expects out of write_trigger's own two limit fields, so the pre-flight check here and the real
    # runtime check can never silently diverge on what values those two fields may take -- exactly
    # the same divergence risk several earlier rounds already closed for bridge_id/event validation
    # (see _validate_bridge_id()'s own comment).
    #
    # Reuse was feasible here without any structural obstacle: _parse_write_trigger_block() is a
    # pure function of a plain dict (no I/O, no other module state), and this file already imports
    # write_candidate_capture lazily elsewhere for the identical reason
    # (_import_write_candidate_capture()), so there is no risk of a second, divergent validator ever
    # being needed.
    if not isinstance(write_trigger, dict):
        raise InstallError(f"write_trigger must be a dict, got {write_trigger!r}")
    expected_keys = {"bridge_id", "max_candidates_per_project", "max_candidate_bytes"}
    actual_keys = set(write_trigger)
    if actual_keys != expected_keys:
        raise InstallError(
            "write_trigger must have exactly these keys: "
            f"{sorted(expected_keys)}, got {sorted(repr(key) for key in actual_keys)}"
        )
    if write_trigger["bridge_id"] != WRITE_TRIGGER_BRIDGE_ID:
        raise InstallError(
            f"write_trigger['bridge_id'] must be {WRITE_TRIGGER_BRIDGE_ID!r} (the only identity "
            "the install-write-trigger action ever registers a handler under -- see "
            f"WRITE_TRIGGER_BRIDGE_ID's own comment), got {write_trigger['bridge_id']!r}"
        )
    # Guarded the same way as _load_write_trigger_config()'s and verify()'s own
    # _import_write_candidate_capture() calls (round-54 fix, independent Claude opus5/max GO +
    # Codex gpt-5.6-sol/max NO-GO dual review, 2026-08-21, issue 2): unguarded here, a genuinely
    # missing write_candidate_capture.py raised a raw, uncaught ModuleNotFoundError past main()'s
    # own `except InstallError` handler -- reachable via make_release(write_trigger=...) called
    # directly (this function's own docstring notes it has no leading underscore and is reachable
    # by any library caller), independently of whichever call site imported the module first.
    try:
        _wtc = _import_write_candidate_capture()
    except (ModuleNotFoundError, SyntaxError, ImportError) as exc:
        raise _wrap_write_candidate_capture_import_error(exc, "cannot validate write_trigger argument") from exc
    synthetic_policy_block = {
        "enabled": True,
        "max_candidates_per_project": write_trigger["max_candidates_per_project"],
        "max_candidate_bytes": write_trigger["max_candidate_bytes"],
    }
    try:
        _wtc._parse_write_trigger_block(synthetic_policy_block)
    except _wtc.WriteCaptureError as exc:
        raise InstallError(f"invalid write_trigger: {exc}") from exc


def make_release(*, write_trigger: dict[str, Any] | None = None) -> dict[str, Any]:
    # `write_trigger`, when given, is a plain dict {"bridge_id": str, "max_candidates_per_project":
    # int, "max_candidate_bytes": int} -- already validated and already confirmed `enabled: true` by
    # the ONE real CLI caller today (see _load_write_trigger_config(), the only real producer of
    # this shape via install_write_trigger()). That upstream validation is not a structural
    # guarantee this function itself can rely on, though: make_release() is a module-level function
    # with no leading underscore, reachable directly by any library caller of this module (via
    # make_release() itself or install_write_trigger()'s own Python API) with an arbitrary dict --
    # the CLI path hardcodes the correct MODULE_ID value and so isn't exposed to arbitrary input
    # today, but that is a property of the one caller, not of this function's own contract (P2-C
    # fix, independent Claude opus5/max review, 2026-08-21). This function's own top-level code stays
    # free of any write_candidate_capture import, exactly like the rest of this file's low-level
    # detection/rewrite chain (BRIDGE_ID's own comment, section 13.3): it only ever sees plain
    # string/int values pulled out of `write_trigger` here, the same way `command`/`event`/`bridge_id`
    # already work throughout this file. The one exception is the full-shape validation immediately
    # below, which deliberately DOES reach into write_candidate_capture (via
    # _validate_write_trigger_argument() -> _import_write_candidate_capture(), both lazy, function-
    # local imports) specifically to reuse its authoritative schema rather than reimplementing a
    # second copy of it here -- see _validate_write_trigger_argument()'s own comment (P2-1 fix,
    # independent Claude opus5/max + Codex review, 2026-08-21). `None` (every call site today except
    # install-write-trigger) reproduces this function's exact pre-existing behavior -- every line
    # below this comment's own `if write_trigger is not None:` blocks is new, additive code that a
    # default call never reaches.
    if write_trigger is not None:
        # Validated HERE, at the earliest point write_trigger is known to be present, before any
        # file I/O or other work below, and before ANY of its fields -- not just `bridge_id` -- are
        # consumed: _validate_write_trigger_argument() checks the full dict shape (bridge_id AND the
        # two limit fields, max_candidates_per_project/max_candidate_bytes) in one call. This matters
        # critically for `bridge_id` specifically because it is the first field to reach a consuming
        # call (_bridge_id_command_fragment()'s shlex.quote(), further down) -- this function runs
        # BEFORE update_hook_config() in both install()'s and plan()'s call sequences, so that
        # function's own _validate_bridge_id() call is not a guard this function can lean on. See
        # _validate_write_trigger_argument()'s own comment for the full reproduced failure-mode list
        # (P2-1 fix, independent Claude opus5/max + Codex review, 2026-08-21) -- it now covers every
        # field this function actually consumes below, not `bridge_id` alone (P2-C's original, now
        # incomplete, single-field fix).
        _validate_write_trigger_argument(write_trigger)
        # write_trigger carries no caller-supplied hook-event value (the write-trigger handler's
        # event is always the hardcoded "SessionEnd" literal at its own registration call site,
        # never taken from this dict -- see install_write_trigger()'s own comment), so there is no
        # analogous _validate_hook_event() call needed here.
    source_script = resolve_ssd_path(SOURCE_SCRIPT)
    script_raw = validate_owned_file(source_script)
    script_sha = sha256_bytes(script_raw)
    expected_uuid = volume_uuid()
    claude_projects = resolve_ssd_path(Path.home() / ".claude/projects")
    expected_claude_projects = resolve_ssd_path(LOCAL_HOMES_ROOT / ".claude/projects")
    if claude_projects != expected_claude_projects:
        raise InstallError("Claude projects do not resolve to the canonical SSD home")

    write_trigger_script_raw: bytes | None = None
    write_trigger_script_sha: str | None = None
    if write_trigger is not None:
        write_trigger_source = resolve_ssd_path(WRITE_TRIGGER_SOURCE_SCRIPT)
        write_trigger_script_raw = validate_owned_file(write_trigger_source)
        write_trigger_script_sha = sha256_bytes(write_trigger_script_raw)

    # release_id/release_dir are content-addressed: including write_trigger's own script hash and
    # config values here (rather than only when writing policy.json below) is what keeps a
    # write-trigger release from ever colliding with a base-only release, or with a
    # differently-configured write-trigger release, at the SAME release_dir path -- write_runtime()'s
    # immutable-collision check assumes any two releases sharing a release_id are byte-identical, and
    # policy.json's own bytes (below) vary with write_trigger, so the key must too.
    release_key_fields: dict[str, Any] = {
        "script_sha256": script_sha,
        "volume_uuid": expected_uuid,
        "source_root": os.fspath(claude_projects),
        "limits": DEFAULT_LIMITS,
    }
    if write_trigger is not None:
        release_key_fields["write_trigger_script_sha256"] = write_trigger_script_sha
        release_key_fields["write_trigger"] = write_trigger
    release_key = canonical_json(release_key_fields)
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
    if write_trigger is not None:
        # write_candidate_capture.load_policy_for_write_trigger() reads this same policy.json and
        # strips this one key before delegating base-field validation to hook.validate_policy() --
        # design doc section 2.2. Only `enabled`/the two numeric limits are ever written here;
        # `write_trigger["bridge_id"]` is this release's OWN command-construction value (below), not
        # part of the on-disk policy schema.
        policy["write_trigger"] = {
            "enabled": True,
            "max_candidates_per_project": write_trigger["max_candidates_per_project"],
            "max_candidate_bytes": write_trigger["max_candidate_bytes"],
        }
    policy_raw = canonical_json(policy)
    policy_sha = sha256_bytes(policy_raw)
    installed_script = release_dir / "claude_memory_hook.py"
    installed_policy = release_dir / "policy.json"
    # The `--bridge-id <value>` fragment is built via _bridge_id_command_fragment() -- the same
    # shared helper _bridge_id_marker_value_forms() (and so this file's own raw-bytes marker
    # scanners) draws from -- rather than shlex.quote()-ing BRIDGE_ID inline as just another list
    # element, so this command's actual quoting and what the marker scanners search for can never
    # silently drift apart (P2-3 fix, independent review 2026-08-21; see
    # _bridge_id_command_fragment()'s own comment).
    command = " ".join(
        [
            *(shlex.quote(part) for part in ("/usr/bin/python3", os.fspath(installed_script))),
            _bridge_id_command_fragment(BRIDGE_ID),
            *(
                shlex.quote(part)
                for part in (
                    "--policy",
                    os.fspath(installed_policy),
                    "--expected-policy-sha256",
                    policy_sha,
                    "--expected-script-sha256",
                    script_sha,
                )
            ),
        ]
    )
    result = {
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
    if write_trigger is not None:
        installed_write_trigger_script = release_dir / "write_candidate_capture.py"
        # Mirrors write_candidate_capture.py's own "NOT WIRED IN. Example only" wiring comment
        # (write_candidate_capture.py:2626-2654): `scan` subcommand, same four flags in the same
        # order as claude_memory_hook.py's own command above, `--bridge-id` set to this release's
        # write_trigger bridge id (write_candidate_capture.MODULE_ID in real use -- see
        # _load_write_trigger_config()), and pointed at the SAME installed_policy/policy_sha this
        # release already computed for claude_memory_hook.py -- one shared, hash-pinned policy.json
        # per release, not two.
        #
        # P0 fix: a 5th flag, `--write-candidates-root <resolved path>`, is now baked in here too.
        # The release directory this command actually runs from (release_dir, above) contains only
        # write_candidate_capture.py + policy.json -- never this file -- so
        # write_candidate_capture.default_write_candidates_root()'s lazy `import install_bridge`
        # (its only way to derive this path on its own) always raised ModuleNotFoundError when the
        # REGISTERED command ran for real, silently swallowed by that module's fail-closed
        # `except Exception: return None` in scan(): exit 0, no output, write-candidates/ never
        # created, forever, with nothing ever surfacing the failure (real end-to-end repro:
        # tests/test_install_bridge.py's InstallWriteTriggerRealCommandEndToEndTests). This process
        # -- running as the installer itself, not from inside that constrained release directory --
        # already has the one module-level constant default_write_candidates_root() derives from
        # (`RUNTIME_BASE`, install_bridge.py:92) directly in scope, so it resolves the same path
        # (`RUNTIME_BASE / "write-candidates"`) itself and passes it explicitly, exactly like
        # `--policy`/`--expected-policy-sha256`/`--expected-script-sha256`/`--bridge-id` already are
        # -- never re-derived lazily, at each invocation, from inside the release payload.
        # write_candidate_capture.py's own `_main_scan` (write_candidate_capture.py:2535) accepts
        # this as an optional 5th `--flag value` pair for exactly this reason.
        # Same shared _bridge_id_command_fragment() helper as the base command above -- see its
        # own comment (P2-3 fix).
        result["write_trigger_script_path"] = installed_write_trigger_script
        result["write_trigger_script_raw"] = write_trigger_script_raw
        result["write_trigger_script_sha256"] = write_trigger_script_sha
        result["write_trigger_bridge_id"] = write_trigger["bridge_id"]
        result["write_trigger_command"] = " ".join(
            [
                *(
                    shlex.quote(part)
                    for part in ("/usr/bin/python3", os.fspath(installed_write_trigger_script), "scan")
                ),
                _bridge_id_command_fragment(write_trigger["bridge_id"]),
                *(
                    shlex.quote(part)
                    for part in (
                        "--policy",
                        os.fspath(installed_policy),
                        "--expected-policy-sha256",
                        policy_sha,
                        "--expected-script-sha256",
                        write_trigger_script_sha,
                        "--write-candidates-root",
                        os.fspath(RUNTIME_BASE / "write-candidates"),
                    )
                ),
            ]
        )
    return result


def write_runtime(release: dict[str, Any]) -> None:
    resolve_ssd_path(LOCAL_HOMES_ROOT)
    release_dir: Path = release["release_dir"]
    ensure_private_dir(RUNTIME_BASE.parent)
    ensure_private_dir(RUNTIME_BASE)
    ensure_private_dir(RUNTIME_BASE / "releases")
    ensure_private_dir(release_dir)
    triples = [
        ("script_path", "script_raw", "script_sha256"),
        ("policy_path", "policy_raw", "policy_sha256"),
    ]
    if "write_trigger_script_path" in release:
        triples.append(("write_trigger_script_path", "write_trigger_script_raw", "write_trigger_script_sha256"))
    for path_key, raw_key, digest_key in triples:
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


def _validate_receipt_shape(receipt: Any) -> dict[str, Any]:
    # Shared strict-shape validation for a receipt dict, used by both
    # _receipt_rows() (which additionally touches disk/backups) and
    # read_receipt() (which previously only checked the top-level shape,
    # letting a corrupted-but-still-owned-and-private receipt file --
    # missing a "path" key, a non-string "backup", a mistyped mode -- raise
    # a raw KeyError/TypeError out of verify()/uninstall() instead of the
    # clean InstallError every other failure in this file produces
    # (independent Claude opus5/max review, 2026-08-17, round 1, P2-2).
    # round 2 added `release_id` to this check (independent Claude opus5/max
    # review, 2026-08-17, round 2, R2-P2-B: `verify()` reads
    # `receipt["release_id"]` unguarded, so a receipt missing only that
    # field passed every check here and still bare-KeyError'd in verify()).
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("bridge_id") != BRIDGE_ID
        or not isinstance(receipt.get("install_id"), str)
        or not receipt.get("install_id")
        or not isinstance(receipt.get("release_id"), str)
        or not receipt.get("release_id")
        or not isinstance(receipt.get("release_dir"), str)
        or not receipt.get("release_dir")
        or not isinstance(receipt.get("script_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt.get("script_sha256", "")) is None
        or not isinstance(receipt.get("policy_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt.get("policy_sha256", "")) is None
        or not isinstance(receipt.get("volume_uuid"), str)
        # `write_trigger_script_sha256`/`write_trigger_bridge_id` (install_write_trigger() only):
        # absent-on-both-keys is a normal base-only receipt, valid exactly as before this pair
        # existed. Present on only one, or present-but-malformed, is a receipt no real writer of
        # this file ever produces -- treated as corruption like every other malformed field above,
        # not silently tolerated (verify() unconditionally trusts both once this function returns).
        or (
            ("write_trigger_script_sha256" in receipt) != ("write_trigger_bridge_id" in receipt)
        )
        or (
            "write_trigger_script_sha256" in receipt
            and (
                not isinstance(receipt.get("write_trigger_script_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt.get("write_trigger_script_sha256", "")) is None
                or not isinstance(receipt.get("write_trigger_bridge_id"), str)
                or not receipt.get("write_trigger_bridge_id")
            )
        )
    ):
        raise InstallError("invalid receipt")
    raw_rows = receipt.get("configs")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise InstallError("invalid receipt: no config rows")
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            raise InstallError("invalid receipt config row")
        # `prev_*`/`after_backup` are new this round (see _receipt_rows() and
        # install()): `prev_sha256`/`prev_mode`/`prev_backup` record the
        # config's content at the *start* of the install transaction that
        # produced this row, distinct from `before_*` (the true pristine,
        # pre-bridge baseline, which round 1 made durable and inherited
        # across re-installs). `after_backup` durably stores this
        # transaction's target bytes so a fresh recovery process can finish
        # or roll back an interrupted install without having to re-derive
        # them (independent Claude opus5/max review, 2026-08-17, round 2,
        # R2-P1-A -- see recover_pending_install()'s "kind == install"
        # branch for why the round-1/round-2 single before_*/after_* pair
        # made every interrupted *upgrade* install permanently
        # unrecoverable).
        required_strings = (
            "path",
            "backup",
            "prev_backup",
            "after_backup",
            "before_sha256",
            "prev_sha256",
            "after_sha256",
        )
        if any(not isinstance(raw_row.get(key), str) or not raw_row[key] for key in required_strings):
            raise InstallError("invalid receipt config row")
        if any(
            re.fullmatch(r"[0-9a-f]{64}", raw_row[key]) is None
            for key in ("before_sha256", "prev_sha256", "after_sha256")
        ):
            raise InstallError("invalid receipt config digest")
        before_mode = raw_row.get("before_mode")
        prev_mode = raw_row.get("prev_mode")
        after_mode = raw_row.get("after_mode")
        if (
            not isinstance(before_mode, int)
            or not isinstance(prev_mode, int)
            or not isinstance(after_mode, int)
            or before_mode & ~0o777
            or prev_mode & ~0o777
            or after_mode != 0o600
        ):
            raise InstallError("invalid receipt config mode")
    return receipt


def _load_backup(backup_path: Path, expected_sha256: str) -> bytes:
    raw = validate_owned_file(backup_path, private=True)
    if sha256_bytes(raw) != expected_sha256:
        raise InstallError(f"transaction backup digest mismatch: {backup_path}")
    return raw


def _path_is_absent(path: Path) -> bool:
    # ENOENT (genuinely deleted/never existed) is the ONLY condition that
    # may be treated as "absent". Any other stat() failure -- EACCES from a
    # parent directory that lost +x, EIO from the external SSD this whole
    # installer is built around returning a transient read error -- means
    # "cannot determine whether this path exists", not "it doesn't", and
    # must fail closed instead of being silently treated as absent.
    # `Path.exists()`/`Path.is_symlink()` (round 4's original check) both
    # swallow every OSError, not just ENOENT, so a managed config that is
    # merely unreadable right now -- still on disk, still holding a live
    # bridge handler -- was misclassified as "absent": uninstall() then
    # reported ok:true, deleted latest-receipt.json, and permanently lost
    # the true baseline while the handler kept executing on every Codex
    # prompt (independent Claude opus5/max review, 2026-08-17, round 4,
    # R4-P1-A -- reproduced with nothing more than a single `chmod 0o000`
    # on an account's home directory; on Python interpreters where
    # Path.exists() swallows PermissionError, round 3's fail-closed
    # behavior on this exact input was one that this round had genuinely
    # regressed to fail-open).
    try:
        path.lstat()
        return False
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise InstallError(f"cannot determine whether {path} exists") from exc


def _receipt_rows(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    _validate_receipt_shape(receipt)
    rows: list[dict[str, Any]] = []
    for raw_row in receipt["configs"]:
        # must_exist=False: a row whose target no longer exists at all
        # (an account genuinely deleted, not just temporarily undiscovered
        # -- see install()'s comment on carried-forward rows) is not an
        # error at this layer; it is its own outcome, "absent", handled
        # below and tolerated by every caller of this function.
        path = resolve_ssd_path(Path(raw_row["path"]), must_exist=False)
        # backup (the true pristine baseline) is resolved but NOT read here
        # -- only whichever caller actually ends up needing to write from it
        # (uninstall()'s revert loop, recover_pending_install()'s
        # uninstall-direction revert loop) loads it lazily via
        # _load_backup(), exactly like prev_backup below. A carried-forward
        # row (see install()'s carried_forward_rows comment) is copied
        # verbatim into the new receipt without ever being re-copied into
        # the current transaction's own backup directory, so its `backup`
        # can keep pointing at an *older* transaction's directory
        # indefinitely; eagerly reading it here for every row -- including
        # rows nothing will ever write from, like an "absent" row -- made
        # that unreachable-but-irrelevant backup file break the whole
        # receipt instead of only the (nonexistent) operation that would
        # have needed it (independent Claude opus5/max review, 2026-08-17,
        # round 4, R4-P2-A). prev_backup/after_backup remain lazy for the
        # same reason established in round 3 (R3-P2-A).
        backup = resolve_ssd_path(Path(raw_row["backup"]), must_exist=False)
        prev_backup = resolve_ssd_path(Path(raw_row["prev_backup"]), must_exist=False)
        after_backup = resolve_ssd_path(Path(raw_row["after_backup"]), must_exist=False)

        if _path_is_absent(path):
            # Genuinely absent: not "drift" (nothing to compare against --
            # there is no content at all), not a modeling gap. uninstall()
            # and recover_pending_install() both treat this as "nothing to
            # do for this row" rather than refusing the whole transaction
            # (independent Claude opus5/max review, 2026-08-17, round 3,
            # R3-P1-A: round 3's original fix for Codex P1-R2-1 refused
            # install() outright the moment a managed path went
            # undiscovered, but uninstall()/verify() still unconditionally
            # required the path to exist -- so a *permanently* deleted
            # account, not just a temporarily renamed one, had no
            # supported way to ever be uninstalled or verified again: every
            # action refused forever except plan, the exact lockout
            # signature round 1 and round 2's own fixes each separately
            # introduced too).
            rows.append(
                {
                    **raw_row,
                    "path_obj": path,
                    "backup_obj": backup,
                    "prev_backup_obj": prev_backup,
                    "after_backup_obj": after_backup,
                    "state": "absent",
                    "install_state": "absent",
                }
            )
            continue

        current = validate_owned_file(path)
        current_mode = _mode_bits(path)
        current_sha = sha256_bytes(current)
        matches_before = current_sha == raw_row["before_sha256"] and current_mode == raw_row["before_mode"]
        matches_prev = current_sha == raw_row["prev_sha256"] and current_mode == raw_row["prev_mode"]
        matches_after = current_sha == raw_row["after_sha256"] and current_mode == raw_row["after_mode"]

        # `state` (before/prev/after/both/drift computed against BEFORE) is
        # used by uninstall()'s own revert-to-pristine logic -- unchanged in
        # meaning from round 1/2. `install_state` (computed against PREV
        # instead) is used by recover_pending_install()'s install-journal
        # branch below: whether an in-flight install can be rolled back to
        # its own transaction-start state, which for an upgrade is the
        # previous, fully-working install -- not pristine.
        if matches_before and matches_after:
            state = "both"
        elif matches_before:
            state = "before"
        elif matches_after:
            state = "after"
        else:
            state = "drift"

        if matches_prev and matches_after:
            install_state = "both"
        elif matches_prev:
            install_state = "prev"
        elif matches_after:
            install_state = "after"
        else:
            install_state = "drift"

        rows.append(
            {
                **raw_row,
                "path_obj": path,
                "backup_obj": backup,
                "prev_backup_obj": prev_backup,
                "after_backup_obj": after_backup,
                "state": state,
                "install_state": install_state,
            }
        )
    return rows


def recover_pending_install() -> dict[str, Any]:
    # Kind-aware since the P1-2 fix below (independent Claude opus5/max
    # review, 2026-08-17, round 1): install() and uninstall() now share this
    # same durable pending-journal mechanism, tagged with which direction is
    # in flight, because a real SIGKILL can end the process at any point --
    # a bare in-process try/except cannot run. "install" journals commit
    # toward the *after* (installed) state, exactly as before. "uninstall"
    # journals commit toward the *before* (pristine) state: regardless of
    # how far a previous attempt got, finishing means driving every row to
    # `before_*` and then clearing latest-receipt.json, never reverting back
    # toward "still installed" -- an interrupted uninstall must always
    # finish removing the bridge, not leave it silently active.
    if not PENDING_PATH.exists() and not PENDING_PATH.is_symlink():
        return {"ok": True, "state": "none"}
    pending_path = resolve_ssd_path(PENDING_PATH)
    pending_raw = validate_owned_file(pending_path, private=True)
    journal = strict_json(pending_raw)
    if (
        not isinstance(journal, dict)
        or set(journal) != {"schema", "kind", "receipt"}
        or journal.get("schema") != JOURNAL_SCHEMA
        or journal.get("kind") not in ("install", "uninstall")
    ):
        raise InstallError("invalid pending install journal")
    kind = journal["kind"]
    receipt: dict[str, Any] = journal["receipt"]
    _validate_receipt_shape(receipt)
    rows = _receipt_rows(receipt)

    # Run the untracked-owned-handler safety scan (see uninstall()'s own
    # comment) once here, before either direction's finishing logic, so
    # NEITHER branch can report success while abandoning a live, owned
    # handler at a path this journal's receipt does not track. Round 9
    # added this scan only to the "kind == uninstall" branch below; the
    # "kind == install" rollback branch is an equally real receipt/journal
    # commit -- for a first-ever install there is no latest-receipt.json
    # at all yet, so a relocated account whose row silently becomes
    # "absent" (skipped by that branch's own `install_state != "after"`
    # filter) is abandoned with no receipt ever having existed to reveal
    # it, which is strictly worse than the uninstall-side version of the
    # same bug (independent Claude opus5/max review, 2026-08-17, round 9,
    # R9-P1-A, reproduced with a real SIGKILL mid-install followed by a
    # single `mv` of an already-bridged account). Checking once here,
    # shared by both branches, is what round 8's own report called out as
    # the better fix over duplicating the scan per branch.
    receipt_paths = {row["path"] for row in receipt["configs"]}
    untracked_owned = _untracked_owned_including_write_trigger(receipt, receipt_paths)
    if untracked_owned:
        raise InstallError(
            "refusing to finish pending install/uninstall: found an owned hook handler at a path the "
            "current receipt does not track (a managed account directory may have moved -- restore it "
            "to its receipt-recorded path before retrying): " + ", ".join(untracked_owned)
        )

    latest_path = RUNTIME_BASE / "latest-receipt.json"
    latest_matches_this_receipt = False
    if latest_path.exists() or latest_path.is_symlink():
        latest_raw = validate_owned_file(resolve_ssd_path(latest_path), private=True)
        latest = strict_json(latest_raw)
        latest_matches_this_receipt = (
            isinstance(latest, dict)
            and latest.get("install_id") == receipt["install_id"]
            and latest_raw == canonical_json(receipt)
        )

    if kind == "install":
        if latest_matches_this_receipt:
            # "absent" (a carried-forward row for a currently-undiscoverable
            # path -- see install()'s comment) is an acceptable resting
            # state for a committed install: that row was never part of
            # this transaction's write set to begin with.
            drifted = [row for row in rows if row["install_state"] not in ("after", "both", "absent")]
            if drifted:
                raise InstallError("committed install journal has config drift")
            remove_file_durable(pending_path)
            return {"ok": True, "state": "committed", "install_id": receipt["install_id"]}
        # Roll back to PREV (this transaction's own starting point), not to
        # BEFORE (true pristine). For a first-ever install these are the
        # same value, so this behaves exactly like round 1's already
        # SIGKILL-verified rollback. They diverge on an upgrade: a config
        # not yet rewritten by this transaction still holds the *previous*
        # install's fully-working content, which is neither pristine nor
        # this transaction's target -- classifying that against BEFORE (as
        # round 1/round 2 did) misread ordinary, untouched mid-upgrade
        # state as "drift" and refused every action (recover/verify/
        # install/uninstall) forever, with `plan` the only one still
        # reporting ok:true (independent Claude opus5/max review,
        # 2026-08-17, round 2, R2-P1-A, reproduced with no crash, no race,
        # and no privilege -- an ordinary write failure on one config was
        # enough). Rolling back to PREV also fixes the secondary issue that
        # review flagged: the round-1/round-2 version, when it did manage
        # to complete a rollback after all configs were written, rolled all
        # the way back to pristine -- silently uninstalling the previously-
        # working install instead of just undoing the failed upgrade
        # attempt.
        drifted = [row for row in rows if row["install_state"] == "drift"]
        if drifted:
            raise InstallError("pending install cannot roll back because a config drifted")
        for row in reversed(rows):
            if row["install_state"] != "after":
                continue
            prev_backup_raw = _load_backup(row["prev_backup_obj"], row["prev_sha256"])
            atomic_write(row["path_obj"], prev_backup_raw, row["prev_mode"])
            restored = validate_owned_file(row["path_obj"])
            restored_mode = _mode_bits(row["path_obj"])
            if sha256_bytes(restored) != row["prev_sha256"] or restored_mode != row["prev_mode"]:
                raise InstallError(f"transaction rollback verification failed: {row['path_obj']}")
        remove_file_durable(pending_path)
        return {"ok": True, "state": "rolled_back", "install_id": receipt["install_id"]}

    # kind == "uninstall"
    # (The untracked-owned-handler safety scan guarding this finishing pass
    # -- and the "kind == install" one above -- now runs once, shared,
    # right after `rows = _receipt_rows(receipt)`; see that comment.)
    drifted = [row for row in rows if row["state"] == "drift"]
    if drifted:
        raise InstallError("pending uninstall cannot finish because a config drifted")
    # Load every backup this recovery pass will write from before writing
    # any of them -- the same ordering fix as uninstall()'s own loop (R5-P1-A):
    # by the time this branch runs the journal is already durable, so an
    # unloadable backup here cannot *create* a wedge, but discovering it
    # mid-loop would still leave some rows reverted and others not, and the
    # only way to try again is to re-run the identical loop. Failing before
    # any write means a re-run always starts from the same, fully-untouched
    # state instead of an unpredictable partial one.
    backup_bytes = {
        os.fspath(row["path_obj"]): _load_backup(row["backup_obj"], row["before_sha256"])
        for row in rows
        if row["state"] not in ("before", "both", "absent")
    }
    for row in rows:
        # "absent" (a carried-forward row whose path no longer exists at
        # all) has nothing to revert -- see uninstall()'s own comment for
        # why this must not abort the whole transaction.
        if row["state"] in ("before", "both", "absent"):
            continue
        backup_raw = backup_bytes[os.fspath(row["path_obj"])]
        atomic_write(row["path_obj"], backup_raw, row["before_mode"])
        restored = validate_owned_file(row["path_obj"])
        restored_mode = _mode_bits(row["path_obj"])
        if sha256_bytes(restored) != row["before_sha256"] or restored_mode != row["before_mode"]:
            raise InstallError(f"transaction rollback verification failed: {row['path_obj']}")
    if latest_matches_this_receipt:
        remove_file_durable(resolve_ssd_path(latest_path))
    remove_file_durable(pending_path)
    return {"ok": True, "state": "uninstalled", "install_id": receipt["install_id"]}


def install(*, write_trigger: dict[str, Any] | None = None) -> dict[str, Any]:
    # `write_trigger` (see make_release()'s own comment for its shape) is forwarded straight into
    # make_release() and, below, layered onto each config's SessionEnd handler list via a second
    # update_hook_config() call -- every other line in this function is completely unchanged from
    # before this parameter existed, and every real call site except install_write_trigger() still
    # passes nothing, reproducing this function's exact pre-existing behavior.
    recover_pending_install()
    release = make_release(write_trigger=write_trigger)
    configs: list[Path] = release["hook_configs"]

    # If this bridge was already installed (a latest-receipt.json exists),
    # the true pre-bridge baseline for any config that receipt already
    # covers is THAT receipt's own before_* fields/backup -- not whatever
    # is on disk right now, which already contains our handler. Capturing
    # current-disk-content as "before" on a second install (a routine,
    # designed-to-work operation: an idempotent re-install, or an upgrade
    # to a new release_id after claude_memory_hook.py changes) used to
    # poison the rollback baseline forever: uninstall would "successfully"
    # restore back to an already-installed state, every tool (verify,
    # uninstall, recover, plan) would report ok:true, and the bridge would
    # keep running on every Codex prompt with no error anywhere in the
    # chain (P1-1, independent Claude opus5/max review, 2026-08-17, round
    # 1, reproduced end-to-end with no crash/race/privilege required).
    previous_rows_by_path: dict[str, dict[str, Any]] = {}
    latest_path = RUNTIME_BASE / "latest-receipt.json"
    if latest_path.exists() or latest_path.is_symlink():
        previous_receipt = read_receipt()
        for row in previous_receipt["configs"]:
            previous_rows_by_path[row["path"]] = row

    # A path this machine's latest receipt already manages, but that
    # discover_hook_configs() does not find *this* time, must not be
    # silently dropped from the receipt: the next install to rediscover it
    # (nothing on disk changes for a config install() never touches) would
    # see no previous-receipt row for it and treat its current -- already
    # bridged -- content as a fresh pristine baseline, permanently losing
    # the real one. uninstall() would then leave that config's bridge
    # handler in place forever while reporting ok:true (independent Codex
    # sol/xhigh review, 2026-08-17, round 2, P1-R2-1, reproduced with a
    # plain rename-then-restore of one account's hooks.json between two
    # ordinary installs -- no crash, race, or privilege needed).
    #
    # The fix is to carry the row forward into the new receipt completely
    # unchanged (not to refuse the install outright, which round 2's
    # version of this fix did): a temporarily-undiscoverable path keeps
    # its real before_*/prev_*/after_* baseline waiting for it, and when
    # it reappears, the existing "does current content match the last
    # known installed state" check above runs exactly as it always does.
    # If the path never reappears -- a genuine deletion, not a temporary
    # rename -- the carried-forward row is what lets uninstall()/verify()
    # recognize and tolerate it as "absent" (see _receipt_rows()) rather
    # than requiring it to exist, which is what refusing here would have
    # forced them into needing anyway. Refusing the whole install instead
    # (round 3's first attempt at this fix) closed Codex P1-R2-1 but
    # reintroduced the identical "every action refuses forever except
    # plan" lockout for the much more common permanent case -- deleting or
    # re-provisioning a pooled Codex account, which the real target
    # machine's account tooling does under a fresh UUID indistinguishable
    # from deletion (independent Claude opus5/max review, 2026-08-17,
    # round 3, R3-P1-A). A path only *gaining* receipt coverage (a newly
    # discovered account) is unaffected and already handled correctly
    # below.
    discovered_paths = {os.fspath(path) for path in configs}
    carried_forward_rows = [
        previous_rows_by_path[missing_path]
        for missing_path in sorted(set(previous_rows_by_path) - discovered_paths)
    ]
    # Classify each carried-forward row's disk state now, before anything
    # this install does becomes durable -- not only at commit time, which
    # is what install()'s own finalizing recover_pending_install() call
    # used to do implicitly. discover_hook_configs() drops a path from
    # `configs` on ANY stat() failure it treats as "not found" (its own
    # instance of the class of bug R4-P1-A/R5-P1-A fixed elsewhere in this
    # file -- see R5-P3-A, deliberately deferred as its own P3), so an
    # account whose directory merely became unreadable between discovery
    # and now looks identical here to one that was genuinely deleted. If
    # the path is genuinely gone (ENOENT), _path_is_absent() returns True
    # and the row is carried forward exactly as before; if it is merely
    # indeterminate (EACCES, EIO, ...), it raises here -- so install()
    # refuses cleanly before writing a single live config or the receipt,
    # instead of writing everything successfully and then having the
    # commit-finalizing check discover the same problem afterward and
    # report "install failed" while every discovered config is, in fact,
    # already bridged (independent Claude opus5/max review, 2026-08-17,
    # round 5, R5-P2-A, reproduced with a single `chmod` on an account
    # directory between discovery and commit).
    for row in carried_forward_rows:
        _path_is_absent(resolve_ssd_path(Path(row["path"]), must_exist=False))

    originals: dict[Path, bytes] = {}
    updated: dict[Path, bytes] = {}
    original_modes: dict[Path, int] = {}
    baseline_raw: dict[Path, bytes] = {}
    baseline_sha: dict[Path, str] = {}
    baseline_mode: dict[Path, int] = {}
    # "prev" -- this transaction's own starting point, as opposed to
    # "baseline"/before, the permanent pristine one -- lets an interrupted
    # install be rolled back to the last known-*working* state instead of
    # all the way to pristine (see recover_pending_install()'s "kind ==
    # install" branch; independent Claude opus5/max review, 2026-08-17,
    # round 2, R2-P1-A). For a first-ever install these are identical.
    prev_raw: dict[Path, bytes] = {}
    prev_sha: dict[Path, str] = {}
    prev_mode: dict[Path, int] = {}
    prev_backup_source: dict[Path, Path | None] = {}
    for path in configs:
        raw = validate_owned_file(path)
        current_mode = _mode_bits(path)
        originals[path] = raw
        original_modes[path] = current_mode
        after = update_hook_config(raw, release["command"])
        if write_trigger is not None:
            # Layered onto the SAME payload the line above already produced, not a second
            # independent update_hook_config() call against `raw` -- both handlers must land in one
            # write to `updated[path]`, so uninstall()'s single before_sha256 backup/restore removes
            # both at once (see AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md's install-write-trigger
            # section for why this is what makes the existing uninstall/recover machinery a genuine
            # rollback path for this capability, not just for the base UserPromptSubmit handler).
            after = update_hook_config(
                after,
                release["write_trigger_command"],
                event="SessionEnd",
                bridge_id=write_trigger["bridge_id"],
                timeout=None,
                status_message="Scanning session for durable memory candidates",
            )
        updated[path] = after

        previous_row = previous_rows_by_path.get(os.fspath(path))
        if previous_row is None:
            # Never touched by a previous install *under this exact path*
            # -- but that is not the same claim as "its current content is
            # genuinely pristine". A managed directory renamed or moved
            # between installs (an ordinary way to relocate storage on an
            # external SSD -- a plain `mv`, no crash/race/privilege/mock)
            # resolves to a path string with no entry in
            # previous_rows_by_path, even though its *content* is still
            # whatever the previous install wrote. Trusting "no receipt
            # row for this path" as "pristine" adopted an already-bridged
            # config as its own baseline, so uninstall() -- comparing
            # against that self-referential baseline -- reported ok:true
            # while leaving the handler live, undetectable, and permanent
            # (latest-receipt.json is deleted in the same operation): P1-1
            # resurrected through a door install()'s own P1-1 fix never
            # covered (independent Claude opus5/max review, 2026-08-17,
            # round 6, R6-P1-A, reproduced on every prior revision of this
            # file with nothing more than a single `mv` of a pooled
            # account directory). Refuse instead of silently adopting
            # already-bridged content.
            #
            # The round-6 report's remediation claim -- "the operator
            # either restores the old path ... or runs uninstall first" --
            # was transcribed into this error message verbatim and is only
            # half true: uninstall() had no way to see a discovered config
            # that carries an owned handler but has no receipt row at all,
            # so "run uninstall first" reported ok:true, deleted the only
            # receipt, and left the handler live and now-untracked -- R6-
            # P1-A's exact harm, reached through uninstall() directly
            # instead of through this branch, and (because the receipt is
            # gone afterward) unrecoverable by any tool action: this
            # branch would keep refusing the same path forever, even after
            # renaming it back, since there is no longer a receipt for it
            # to match against (independent Claude opus5/max review,
            # 2026-08-17, round 7, R7-P1-A/R7-P1-B). uninstall() itself now
            # runs the same safety scan (see its own comment) and refuses
            # in this situation instead of completing, so "run uninstall
            # first" is safe again -- but the message no longer claims it
            # unconditionally, since restoring the original path is the
            # remediation that always works without depending on that scan.
            existing_payload = strict_json(raw)
            existing_handlers = (
                existing_payload.get("hooks", {}).get(DEFAULT_HOOK_EVENT, [])
                if isinstance(existing_payload, dict)
                else []
            )
            if any(owned_handler(handler) for handler in existing_handlers):
                raise InstallError(
                    "refusing to record an already-bridged config as a pristine baseline "
                    "(did this path move since the last install? restore it to its "
                    f"receipt-recorded path, or run uninstall to remove the bridge first): {path}"
                )
            # Never touched by a previous install this receipt covers --
            # its current content genuinely is the pristine baseline, and
            # (trivially) also this transaction's own starting point.
            baseline_raw[path] = raw
            baseline_sha[path] = sha256_bytes(raw)
            baseline_mode[path] = current_mode
            prev_raw[path] = raw
            prev_sha[path] = baseline_sha[path]
            prev_mode[path] = current_mode
            prev_backup_source[path] = None
        else:
            if sha256_bytes(raw) != previous_row["after_sha256"] or current_mode != previous_row["after_mode"]:
                raise InstallError(
                    f"hook config no longer matches the last known installed state "
                    f"(run verify or uninstall first): {path}"
                )
            prior_backup = resolve_ssd_path(Path(previous_row["backup"]))
            prior_backup_raw = validate_owned_file(prior_backup, private=True)
            if sha256_bytes(prior_backup_raw) != previous_row["before_sha256"]:
                raise InstallError(f"prior backup digest mismatch, refusing to trust baseline: {prior_backup}")
            baseline_raw[path] = prior_backup_raw
            baseline_sha[path] = previous_row["before_sha256"]
            baseline_mode[path] = previous_row["before_mode"]

            prior_after_backup = resolve_ssd_path(Path(previous_row["after_backup"]))
            prior_after_backup_raw = validate_owned_file(prior_after_backup, private=True)
            if sha256_bytes(prior_after_backup_raw) != previous_row["after_sha256"]:
                raise InstallError(
                    f"prior after-backup digest mismatch, refusing to trust baseline: {prior_after_backup}"
                )
            prev_raw[path] = prior_after_backup_raw
            prev_sha[path] = previous_row["after_sha256"]
            prev_mode[path] = previous_row["after_mode"]
            prev_backup_source[path] = prior_after_backup

    write_runtime(release)
    install_id = timestamp_id()
    backup_dir = RUNTIME_BASE / "backups" / install_id
    ensure_private_dir(RUNTIME_BASE / "backups")
    ensure_private_dir(backup_dir)
    backups: dict[Path, Path] = {}
    after_backups: dict[Path, Path] = {}
    prev_backups: dict[Path, Path] = {}
    for path in configs:
        digest = sha256_bytes(os.fspath(path).encode("utf-8"))
        backup_path = backup_dir / f"{digest}.json"
        atomic_write(backup_path, baseline_raw[path], 0o600)
        backups[path] = backup_path

        after_backup_path = backup_dir / f"{digest}-after.json"
        atomic_write(after_backup_path, updated[path], 0o600)
        after_backups[path] = after_backup_path

        # A first-ever path's "prev" backup is the same file as its
        # pristine one (they hold identical bytes); an already-managed
        # path's "prev" backup is simply a pointer to the previous
        # install's own after-backup file -- old backup directories are
        # never pruned (see plan()'s docs / opus's round-2 report), so it
        # is still there and does not need to be recopied.
        prev_backups[path] = (
            backup_path if prev_backup_source[path] is None else prev_backup_source[path]
        )
    # Additive, optional receipt fields -- absent entirely (not null) for every install that does
    # not pass write_trigger=, so _validate_receipt_shape() (which only checks specific keys it
    # knows about, never an exact key set) and every existing receipt-shape assertion in the test
    # suite are unaffected. verify() reads these to also hash-pin write_candidate_capture.py and
    # count its SessionEnd handler (see that function).
    write_trigger_receipt_fields: dict[str, Any] = (
        {
            "write_trigger_script_sha256": release["write_trigger_script_sha256"],
            "write_trigger_bridge_id": release["write_trigger_bridge_id"],
        }
        if write_trigger is not None
        else {}
    )
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
        **write_trigger_receipt_fields,
        "configs": [
            {
                "path": os.fspath(path),
                "backup": os.fspath(backups[path]),
                "before_sha256": baseline_sha[path],
                "before_mode": baseline_mode[path],
                "prev_backup": os.fspath(prev_backups[path]),
                "prev_sha256": prev_sha[path],
                "prev_mode": prev_mode[path],
                "after_backup": os.fspath(after_backups[path]),
                "after_sha256": sha256_bytes(updated[path]),
                "after_mode": 0o600,
            }
            for path in configs
        ]
        # Paths this install run could not discover are carried forward
        # verbatim from the previous receipt (see the comment above
        # carried_forward_rows's computation): this install never reads
        # or touches them, so their row -- including whichever backup
        # files it already points at -- is still exactly as valid as it
        # was in the receipt it came from.
        + carried_forward_rows,
    }
    receipt_raw = canonical_json(receipt)
    atomic_write(backup_dir / "receipt.json", receipt_raw, 0o600)
    atomic_write(
        PENDING_PATH,
        canonical_json({"schema": JOURNAL_SCHEMA, "kind": "install", "receipt": receipt}),
        0o600,
    )
    try:
        for path in configs:
            if updated[path] != originals[path] or original_modes[path] != 0o600:
                current = validate_owned_file(path)
                current_mode = _mode_bits(path)
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
    latest_path = RUNTIME_BASE / "latest-receipt.json"
    if not latest_path.exists() and not latest_path.is_symlink():
        # A clear, specific "not installed" signal rather than the generic
        # "path unavailable" resolve_ssd_path() would raise -- this is also
        # the state a successful uninstall() now leaves behind (P1-2 fix),
        # so this is the normal way verify()/uninstall() report "nothing to
        # do" after a real uninstall (independent Claude opus5/max review,
        # 2026-08-17, round 1, P2-4: previously latest-receipt.json was
        # never cleared by uninstall at all, so this state was unreachable
        # through the normal tool and its message never mattered).
        raise InstallError("not installed: no receipt found")
    path = resolve_ssd_path(latest_path)
    raw = validate_owned_file(path, private=True)
    receipt = strict_json(raw)
    _validate_receipt_shape(receipt)
    return receipt


# Round-4 (2026-08-28), BLOCKER 2. verify()'s per-config test used to be a byte-exact digest
# comparison against the receipt, and any mismatch raised `hook config drift: <path>` -- aborting
# the whole run, so no later account was checked either.
#
# That single line is the mechanism behind this project's own worst production incident. On
# 2026-08-21 a Codex app upgrade deleted `~/.codex/hooks.json` outright; the machine then ran the
# original, completely unbounded, pre-round-1 vulnerable `redact()` in production for about a week
# with nobody noticing. verify() was not silent during that week -- it was FAILING. It just failed
# the same way it fails for a harmless reformat, with the same word ("drift") and the same exit
# code, so the signal carried no information and stopped being read.
#
# It is not a hypothetical. Reproduced live on this machine while writing this fix: Orca had added
# six of its own hook events (PreToolUse/PostToolUse/Stop/SubagentStart/SubagentStop/
# PermissionRequest, all pointing at ~/.orca/agent-hooks/codex-hook.sh) to
# `/Volumes/Extreme SSD/Orca/local-homes/.codex/hooks.json`, and
#     $ ./install_bridge.py verify
#     {"ok": false, "error": "hook config drift: .../local-homes/.codex/hooks.json"}   rc=1
# while the redaction handler was present, correct and firing on all three managed configs
# (owned handler count 1/1/1, correct script path, correct digest).
#
# So the question this function asks changes from "are these bytes the bytes I wrote?" to "is the
# redaction hook actually wired correctly?", and byte drift becomes a separately reported, and
# separately CLASSIFIED, observation rather than a verdict. The classification is what makes the
# signal readable again:
#
#   reserialized   -- the parsed JSON is equal to what install() wrote; only formatting differs.
#                     Somebody else's writer rewrote the file. Cosmetic, by construction.
#   foreign_change -- the JSON differs, but every check below still passes: another tool added or
#                     changed ITS OWN handlers and left ours intact. Reported loudly, not fatal --
#                     making it fatal is what would put this machine back into permanent red, which
#                     is the failure this whole rewrite exists to undo.
#   (problems)     -- our handler is missing, duplicated, points somewhere else, or the script it
#                     points at does not hash to the expected release. THIS is "genuinely broken,
#                     hook not firing", and only this sets ok:false.
#
# `reserialized` is decided against the receipt's own `after_backup` (a copy of the exact bytes
# install() wrote, which every receipt this tool has ever written already records) rather than a new
# receipt field, so the distinction works for receipts that predate this change -- including the one
# live on this machine right now.
_HOOK_HEALTH_MISSING = "hook_missing"
_HOOK_HEALTH_DUPLICATE = "hook_duplicated"
_HOOK_HEALTH_MISPOINTED = "hook_points_elsewhere"
_HOOK_HEALTH_TAMPERED = "script_digest_mismatch"
_HOOK_HEALTH_UNPARSEABLE = "config_unparseable"


def _live_handler_arguments(command: str) -> dict[str, str]:
    """Pull the self-declared `--policy`/`--expected-*-sha256` pairs (and argv[1]) out of a command.

    Same shape make_release() always bakes in, and the same parse verify()'s write-trigger liveness
    check has used since round 52 -- shared here rather than copied so the base handler and the
    write-trigger handler cannot drift apart in how they read the identical command line.
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError as exc:
        raise InstallError("cannot parse a live hook command") from exc
    parsed: dict[str, str] = {}
    if len(tokens) > 1:
        parsed["script"] = tokens[1]
    for index, token in enumerate(tokens):
        if index + 1 >= len(tokens):
            continue
        if token in ("--policy", "--expected-policy-sha256", "--expected-script-sha256"):
            # `-`s normalized to `_` so the keys are the identifier-shaped names the callers use;
            # a bare `lstrip("-")` leaves "expected-script-sha256", which silently reads back as
            # None at every call site and reports a correctly-wired hook as mispointed.
            parsed[token[2:].replace("-", "_")] = tokens[index + 1]
    return parsed


def _config_hook_health(
    raw: bytes,
    *,
    expected_script: str,
    expected_policy: str,
    expected_script_sha256: str,
    expected_policy_sha256: str,
) -> dict[str, Any]:
    """Is the redaction hook correctly wired in THIS config's bytes? Semantics only, never bytes."""
    problems: list[dict[str, str]] = []
    try:
        payload = strict_json(raw)
    except InstallError as exc:
        return {
            "hook_present": False,
            "problems": [{"kind": _HOOK_HEALTH_UNPARSEABLE, "detail": str(exc)}],
            "payload": None,
        }
    handlers = payload.get("hooks", {}).get(DEFAULT_HOOK_EVENT, []) if isinstance(payload, dict) else []
    if not isinstance(handlers, list):
        handlers = []
    matches = [handler for handler in handlers if owned_handler(handler)]
    if not matches:
        # The 2026-08-21 incident's exact shape when the file survives but loses its entry, and the
        # one this whole rewrite must never again report with the same words as a reformat.
        problems.append(
            {
                "kind": _HOOK_HEALTH_MISSING,
                "detail": (
                    f"no handler owned by {BRIDGE_ID} under {DEFAULT_HOOK_EVENT}: the redaction "
                    "hook is NOT running for this account. Re-run `install`."
                ),
            }
        )
        return {"hook_present": False, "problems": problems, "payload": payload}
    if len(matches) > 1:
        problems.append(
            {
                "kind": _HOOK_HEALTH_DUPLICATE,
                "detail": f"{len(matches)} handlers owned by {BRIDGE_ID}; exactly one is expected",
            }
        )
    command = _owned_handler_command(matches[0])
    if command is None:
        problems.append({"kind": _HOOK_HEALTH_MISSING, "detail": "owned handler carries no command"})
        return {"hook_present": False, "problems": problems, "payload": payload}
    arguments = _live_handler_arguments(command)
    expected_arguments = {
        "script": expected_script,
        "policy": expected_policy,
        "expected_script_sha256": expected_script_sha256,
        "expected_policy_sha256": expected_policy_sha256,
    }
    for name, expected_value in expected_arguments.items():
        actual = arguments.get(name)
        if actual != expected_value:
            problems.append(
                {
                    "kind": _HOOK_HEALTH_MISPOINTED,
                    "detail": (
                        f"live handler's {name} is {actual!r}, expected {expected_value!r} -- the "
                        "hook is running, but not this release"
                    ),
                }
            )
    return {"hook_present": True, "problems": problems, "payload": payload, "command": command}


def _classify_config_drift(raw: bytes, row: dict[str, Any], payload: Any) -> dict[str, Any] | None:
    """None when the bytes still match the receipt; otherwise a classified drift record."""
    if sha256_bytes(raw) == row.get("after_sha256"):
        return None
    classification = "unclassified"
    detail = (
        "this config's bytes differ from what install() wrote, and the receipt's after-backup could "
        "not be read, so a reformat cannot be told apart from a foreign edit here"
    )
    after_backup = row.get("after_backup")
    if isinstance(after_backup, str):
        try:
            backup_path = resolve_ssd_path(Path(after_backup), must_exist=False)
            if not _path_is_absent(backup_path):
                installed = strict_json(validate_owned_file(backup_path, private=True))
                if installed == payload:
                    classification = "reserialized"
                    detail = (
                        "byte-for-byte different but semantically identical to what install() "
                        "wrote: another writer reformatted this file. Cosmetic."
                    )
                else:
                    classification = "foreign_change"
                    detail = (
                        "another tool changed this config's own content; the redaction hook itself "
                        "is unaffected (see hook_functional)"
                    )
        except InstallError:
            pass
    return {"classification": classification, "detail": detail}


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
    unreachable: list[str] = []
    # Round-4: `broken` carries the semantic verdicts that actually mean "the redaction hook is not
    # running"; `drift` carries the byte-level observations, each classified and each tagged with
    # whether the hook is still functional despite it. Keeping them in two lists is the whole point
    # of this change -- one list is what conflated a reformat with a deletion.
    broken: list[dict[str, Any]] = []
    drift: list[dict[str, Any]] = []
    # Fail-closed account coverage (P1, 2026-08-27). Everything below this line only ever looked at
    # receipt["configs"] -- the set install() discovered -- so verify() shared install()'s
    # enumeration blind spot instead of being an independent check of it: a real, live Codex account
    # that discovery never saw was simply absent from the output, and `ok: true` was reported over
    # a machine where an active account's hooks.json had no bridge handler at all. Re-enumerating
    # the LIVE registry here (see ORCA_ACCOUNTS_SUBPATH) is what makes this an actual check: any
    # registry account whose hooks.json this tool cannot locate, or can locate but has no receipt
    # coverage for, is now named explicitly in `unmanaged` and forces `ok: false`.
    #
    # Only REGISTRY accounts are held to this. A local-homes-only directory with no registry entry
    # is a leftover, not a live account Orca will ever run, and failing verify() over one would be
    # a false alarm.
    _, registry_accounts = _enumerate_accounts()
    receipt_config_paths = {
        row["path"] for row in receipt["configs"] if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    # Write-trigger liveness is now determined independently per config row, below, from real, live
    # hooks.json content under the well-known identity -- NOT from receipt.get("write_trigger_*")
    # (round-52 fix, converged independent Claude opus/max + Codex gpt-5.6-sol/max review,
    # 2026-08-21). The prior design gated the whole write-trigger check on those two receipt fields,
    # which an ordinary, unrelated LATER plain install() (no write_trigger= argument -- e.g.
    # redeploying just a claude_memory_hook.py fix) resets to entirely absent on the NEW receipt,
    # even though update_hook_config()'s per-event layering is additive, not a full reset, so a
    # SessionEnd handler a PRIOR install-write-trigger call registered is genuinely still live and
    # untouched. verify() then silently stopped checking BOTH the handler count and the installed
    # script's hash -- Codex proved this goes as far as not detecting a tampered/swapped script the
    # still-live handler points at, reporting `ok: true` regardless. Also, after such a plain
    # reinstall, receipt["release_dir"] is a DIFFERENT, newer release directory than the one the
    # live write-trigger handler was actually deployed into (release_id is content-addressed and a
    # plain install's release-key omits every write_trigger_* field -- see make_release()), so even
    # an unconditional version of the old release_dir-relative check would have looked in the wrong
    # place. The fix below derives everything it needs from the live handler's own command line
    # instead -- the same self-declared `--policy`/`--expected-policy-sha256`/`--expected-script-
    # sha256` arguments make_release() always bakes in -- which is correct regardless of which
    # release directory produced it or what the CURRENT receipt happens to say.
    write_trigger_commands_by_path: dict[str, str] = {}
    for row in receipt["configs"]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise InstallError("invalid receipt config row")
        # must_exist=False, then check explicitly via _path_is_absent(): a
        # permanently deleted or re-provisioned account's path is "absent",
        # not "drift" -- there is nothing left to compare content against,
        # so verify() must skip and report it rather than raising
        # (independent Claude opus5/max review, 2026-08-17, round 3,
        # R3-P1-A; see install()'s carried-forward comment and
        # _receipt_rows()'s "absent" branch, which this mirrors without
        # going through _receipt_rows() itself since verify() does not need
        # backup bytes). Using bare Path.exists()/is_symlink() here (round
        # 4's original check) swallowed EACCES/EIO the same way as
        # _receipt_rows()'s absent-check did, so this call site shares
        # R4-P1-A and gets the same errno-discriminating fix.
        path = resolve_ssd_path(Path(row["path"]), must_exist=False)
        if _path_is_absent(path):
            unreachable.append(os.fspath(path))
            continue
        raw = validate_owned_file(path, private=True)
        # Round-4 (see the block comment above `_config_hook_health`): SEMANTIC first, bytes second,
        # and neither raises -- every config is checked and reported, so one drifted account can no
        # longer hide the state of the others the way the old `raise` did.
        health = _config_hook_health(
            raw,
            expected_script=os.fspath(release_dir / "claude_memory_hook.py"),
            expected_policy=os.fspath(release_dir / "policy.json"),
            expected_script_sha256=receipt["script_sha256"],
            expected_policy_sha256=receipt["policy_sha256"],
        )
        if health["problems"]:
            broken.append({"config": os.fspath(path), "problems": health["problems"]})
        drift_record = _classify_config_drift(raw, row, health["payload"])
        if drift_record is not None:
            drift.append(
                {
                    "config": os.fspath(path),
                    "hook_functional": not health["problems"],
                    **drift_record,
                }
            )
        payload = health["payload"]
        # `SessionEnd` here is a PRE-EXISTING key this tool may not own or have ever written --
        # install() never inspects/normalizes it on a plain (non-write-trigger) install, only the
        # event actually being registered (DEFAULT_HOOK_EVENT, above) gets that treatment -- so a
        # user's own hooks.json is free to carry an arbitrary other tool's SessionEnd value verbatim,
        # legitimately, including a non-list JSON scalar (`null`, `0`, `true`, a bare string, ...).
        # This local isinstance guard exists BECAUSE SessionEnd carries no such guarantee here --
        # unlike this file's other two reads of a hooks.json event-array (round-54 comment fix,
        # independent Claude opus5/max GO + Codex gpt-5.6-sol/max NO-GO dual review, 2026-08-21: a
        # prior version of this comment claimed those two are "guarded the same way", which isn't
        # literally true -- neither has a local isinstance check either). Instead, each of those two
        # is protected upstream, by a DIFFERENT mechanism: the DEFAULT_HOOK_EVENT read at ~2608
        # (inside the `if previous_row is None:` branch) is safe because update_hook_config(raw, ...)
        # is called against this exact same `raw` moments earlier and already raises
        # `"malformed {event} hook list"` for anything but a list -- so this function never reaches
        # that read at all with a malformed shape. The DEFAULT_HOOK_EVENT read at ~2840 is safe
        # because it runs only after `sha256_bytes(raw) != row.get("after_sha256")` has already
        # confirmed `raw` is byte-identical to content update_hook_config() itself previously wrote
        # (always list-shaped). SessionEnd has neither guarantee -- it can be arbitrary content some
        # OTHER tool wrote -- so it needs a guard of its own (round-53 fix, independent Claude
        # opus/max review, 2026-08-21, P2-A): the unguarded version iterated `session_end_handlers`
        # directly, so a plain install (no write_trigger argument at all) against any such config
        # raised an uncaught TypeError from verify() -- a regression this round introduced, since the
        # pre-existing verify() returned ok=True in all these cases. A non-list value is treated the
        # same as "key absent": no write-trigger handlers present here.
        raw_session_end_handlers = payload.get("hooks", {}).get("SessionEnd", []) if isinstance(payload, dict) else []
        session_end_handlers = raw_session_end_handlers if isinstance(raw_session_end_handlers, list) else []
        write_trigger_matches = [
            handler for handler in session_end_handlers if owned_handler(handler, bridge_id=WRITE_TRIGGER_BRIDGE_ID)
        ]
        if len(write_trigger_matches) > 1:
            raise InstallError(f"owned write-trigger hook count mismatch: {path}")
        if write_trigger_matches:
            command = _owned_handler_command(write_trigger_matches[0], bridge_id=WRITE_TRIGGER_BRIDGE_ID)
            if command is None:
                raise InstallError(f"owned write-trigger hook has no command: {path}")
            write_trigger_commands_by_path[os.fspath(path)] = command
        checked.append(os.fspath(path))

    write_trigger_script_sha256: str | None = None
    if write_trigger_commands_by_path:
        # A write-trigger handler was found live on at least one reachable config -- require it
        # present on EVERY reachable config (matching the old design's equivalent strictness: it
        # required exactly one match per row whenever the receipt-based flag was set at all) and
        # require every one of them to carry the exact same command (this tool always writes the
        # identical command to every managed config in one release; a config carrying a DIFFERENT
        # write-trigger command than its siblings cannot be this tool's own doing).
        missing = [path for path in checked if path not in write_trigger_commands_by_path]
        if missing:
            # This state is real and worth hard-failing on (round-53 fix, independent Claude opus/max
            # review, 2026-08-21, P2-B) -- but it is also an EXPECTED consequence of a perfectly
            # ordinary sequence: install-write-trigger ran earlier against some configs, then a NEW
            # Codex account config appeared (this machine legitimately runs multiple pooled Codex
            # account homes), and an ordinary plain install() (no write_trigger argument) correctly
            # registered only the base UserPromptSubmit handler on it -- plain install must never
            # silently also add a SessionEnd handler a caller did not ask for. The message must say so:
            # a bare "hook is missing" with no further context left an operator with no idea whether
            # this was expected or how to fix it.
            raise InstallError(
                "owned write-trigger hook is missing from a config where the base hook is present: "
                + ", ".join(missing)
                + ". This config has the base UserPromptSubmit hook installed but not the "
                "write-trigger SessionEnd hook that this tool's other managed configs carry. This is "
                "expected after a plain `install` (no write-trigger) picks up a config -- e.g. a newly "
                "added Codex account home -- that an earlier `install-write-trigger` run never covered. "
                "To fix: re-run `install-write-trigger` (the install_write_trigger() action) with the "
                "same write-trigger policy so this config's write-trigger hook is brought back in sync "
                "with the others."
            )
        distinct_commands = set(write_trigger_commands_by_path.values())
        if len(distinct_commands) != 1:
            raise InstallError("owned write-trigger hook command differs across configs")
        command = next(iter(distinct_commands))
        try:
            tokens = shlex.split(command, comments=True)
        except ValueError as exc:
            raise InstallError("cannot parse the live write-trigger hook command") from exc
        script_path_str = tokens[1] if len(tokens) > 1 else None
        policy_path_str: str | None = None
        expected_script_sha256: str | None = None
        expected_policy_sha256: str | None = None
        for index, token in enumerate(tokens):
            if index + 1 >= len(tokens):
                continue
            if token == "--expected-script-sha256":
                expected_script_sha256 = tokens[index + 1]
            elif token == "--policy":
                policy_path_str = tokens[index + 1]
            elif token == "--expected-policy-sha256":
                expected_policy_sha256 = tokens[index + 1]
        if not script_path_str or not policy_path_str or not expected_script_sha256 or not expected_policy_sha256:
            raise InstallError("live write-trigger hook command is missing expected arguments")
        # Script hash: the actual deployed file at the path the live handler itself references must
        # match what that SAME command line asserts it should be -- this is what catches a
        # tampered/swapped script regardless of which release directory it lives under or whether
        # the current receipt still remembers this release at all.
        live_script_raw = validate_owned_file(resolve_ssd_path(Path(script_path_str)), private=True)
        if sha256_bytes(live_script_raw) != expected_script_sha256:
            raise InstallError("installed write-trigger script digest mismatch")
        # Policy: same self-consistency check for the policy.json the live handler points at, PLUS
        # (unlike the script check) re-validating its write_trigger block against the real,
        # authoritative schema -- reusing _parse_write_trigger_block() rather than a second,
        # potentially-divergent copy (see _validate_write_trigger_argument()'s own comment for the
        # same reuse discipline). This is the one part of this function that needs
        # write_candidate_capture importable -- unlike the untracked-owned-handler safety scan and the
        # base script/policy checks above -- but that reach is only exercised when a live write-trigger
        # handler was actually found (this whole branch is behind `if write_trigger_commands_by_path:`
        # above), so a plain, no-write-trigger verify() call never imports write_candidate_capture at
        # all and is unaffected either way.
        #
        # The write_candidate_capture import further below (past the policy digest re-check and
        # strict_json parse immediately following this comment) is explicitly guarded (round-53
        # fix, independent Claude opus/max review, 2026-08-21, P1): a prior version of this comment
        # claimed a missing write_candidate_capture.py here "surfaces as a clean InstallError from
        # verify() alone, not a lockout of install()/uninstall()/recover()" -- false, since the
        # import itself was unguarded. Reproduced end-to-end: install a live write-trigger handler,
        # then delete write_candidate_capture.py (genuinely untracked in this repo's own git
        # history) and call verify() -- it raised an uncaught ModuleNotFoundError straight past
        # main()'s own `except InstallError` handler: empty stdout, rc=1, and a raw Python traceback
        # on stderr, unlike every other failure mode in this function. Fixed by catching the import
        # error and re-raising it as an InstallError identifying the real cause, matching this
        # function's own error-reporting contract everywhere else.
        #
        # Round-54 fix (independent Claude opus5/max GO + Codex gpt-5.6-sol/max NO-GO dual review,
        # 2026-08-21): this guard was verify()-only -- _load_write_trigger_config() and
        # _validate_write_trigger_argument() made the identical unguarded _import_write_candidate_
        # capture() call, so `install-write-trigger` (dry-run and real) still raised a raw
        # ModuleNotFoundError traceback. Both now guard the same way. The except clause here also
        # widened from bare ModuleNotFoundError to (ModuleNotFoundError, SyntaxError, ImportError):
        # a corrupted/half-written write_candidate_capture.py raises SyntaxError at import time, not
        # ModuleNotFoundError, and was uncaught before. Message construction -- naming
        # write_candidate_capture itself vs. one of ITS OWN transitive imports as the actual missing
        # piece, or reporting a syntax error -- is now shared across all 3 guarded call sites by
        # _wrap_write_candidate_capture_import_error() instead of divergent copies.
        live_policy_raw = validate_owned_file(resolve_ssd_path(Path(policy_path_str)), private=True)
        if sha256_bytes(live_policy_raw) != expected_policy_sha256:
            raise InstallError("installed write-trigger policy digest mismatch")
        live_policy_payload = strict_json(live_policy_raw)
        try:
            _wtc = _import_write_candidate_capture()
        except (ModuleNotFoundError, SyntaxError, ImportError) as exc:
            raise _wrap_write_candidate_capture_import_error(exc, "cannot verify write-trigger liveness") from exc
        try:
            _wtc._parse_write_trigger_block(
                live_policy_payload.get("write_trigger") if isinstance(live_policy_payload, dict) else None
            )
        except _wtc.WriteCaptureError as exc:
            raise InstallError(f"live write-trigger policy is invalid: {exc}") from exc
        write_trigger_script_sha256 = expected_script_sha256

    covered = receipt_config_paths | set(checked) | set(unreachable)
    unmanaged: list[dict[str, str]] = []
    for account_id, located in sorted(registry_accounts.items()):
        if located is None:
            unmanaged.append(
                {
                    "account_id": account_id,
                    "registry_path": os.fspath(orca_accounts_root() / account_id),
                    "reason": (
                        "this Orca account's OWN home/hooks.json does not resolve onto the Extreme "
                        "SSD, so this tool cannot install, verify, or remove its hook; a same-id "
                        "directory under local-homes/codex-accounts, if one exists, is an unrelated "
                        "legacy snapshot and is not a substitute for it"
                    ),
                }
            )
            continue
        located_str = os.fspath(located)
        if located_str not in covered:
            unmanaged.append(
                {
                    "account_id": account_id,
                    "registry_path": os.fspath(orca_accounts_root() / account_id),
                    "config": located_str,
                    "reason": (
                        "this Orca account's hooks.json exists and is manageable but is not covered "
                        "by the install receipt, so nothing about it has been verified; re-run "
                        "`install` to bring it under management"
                    ),
                }
            )

    # Round-4: `hook_functional` is the answer to the only question that mattered during the
    # 2026-08-21 incident -- "is the redaction hook actually running on every account I manage?" --
    # and it is reported separately from `ok` so a caller can act on it without parsing anything
    # else. `ok` keeps its established meaning (nothing wrong AND nothing unverifiable), so a
    # scripted caller that only checks `ok`/rc is no more permissive than before.
    # `unreachable` is deliberately NOT part of either verdict. A receipt row whose path is gone is
    # a deleted or re-provisioned account -- this file has treated that as "skip and report, not a
    # failure" since round 3 (see the `_path_is_absent()` branch above and R3-P1-A), and two tests
    # pin exactly that (`test_permanently_retired_account_does_not_lock_out_other_accounts`,
    # `test_install_carries_forward_a_temporarily_undiscovered_config_without_losing_baseline`).
    # An account that no longer exists submits no prompts; the check that catches a LIVE account
    # going unprotected is the registry enumeration below, which reports it as `unmanaged`.
    hook_functional = not broken
    cosmetic = [record for record in drift if record["hook_functional"]]
    if broken:
        summary = (
            "BROKEN: the redaction hook is not correctly wired on "
            + ", ".join(record["config"] for record in broken)
            + ". Prompts submitted from these accounts are NOT being redacted. Re-run `install`."
        )
    elif unmanaged:
        summary = (
            "INCOMPLETE: the redaction hook is correctly wired on every config this tool manages, "
            "but " + str(len(unmanaged)) + " live Orca account(s) are outside its management and "
            "were not verified."
        )
    elif cosmetic:
        summary = (
            "HEALTHY: the redaction hook is correctly wired and functional on every managed config. "
            + str(len(cosmetic))
            + " config(s) have byte-level drift that does NOT affect it ("
            + ", ".join(sorted({record["classification"] for record in cosmetic}))
            + "); this is not a failure and needs no action."
        )
    else:
        summary = "HEALTHY: the redaction hook is correctly wired and functional on every managed config."
    result = {
        # NOT unconditionally True (P1, 2026-08-27): a verify() that cannot see an account is not
        # allowed to report success over it. See the `unmanaged` computation above.
        "ok": hook_functional and not unmanaged,
        "hook_functional": hook_functional,
        "summary": summary,
        "release_id": receipt["release_id"],
        "script_sha256": receipt["script_sha256"],
        "policy_sha256": receipt["policy_sha256"],
        "volume_uuid": receipt["volume_uuid"],
        "configs": checked,
        "broken": broken,
        "drift": drift,
        "unreachable": unreachable,
        "unmanaged": unmanaged,
    }
    if write_trigger_script_sha256 is not None:
        result["write_trigger_script_sha256"] = write_trigger_script_sha256
    return result


# Round-4 (2026-08-28), BLOCKER 2's liveness half. verify() answers "is what I installed still
# correctly installed?" and needs a valid receipt to answer anything at all -- `read_receipt()`
# raises before the first check whenever the receipt is missing, pending, or unparseable. That is
# the right contract for verify() and precisely the wrong one for detecting the 2026-08-21 incident,
# where the interesting states are the ones with nothing to read.
#
# doctor() therefore never raises. Every failure is a FINDING, including "there is no receipt", and
# the finding is loud rather than an exception a caller has to catch and interpret. It is also cheap
# and read-only, so it is safe to run on a timer.
#
# What is actually available on this machine to run it on a timer (investigated, not assumed):
#   * launchd user agents, and this repo already carries the pattern --
#     `orca-terminal-dispatch/com.local.orca-terminal-dispatch-reaper.plist.template` plus a
#     `doctor` subcommand, installed at ~/Library/LaunchAgents and loaded.
#   * That plist also carries the warning that matters here, learned the hard way on this exact
#     machine: "The Ego reaper this mirrors was silently dead for ~2 weeks because its script lived
#     under ~/.agents, which resolves onto /Volumes/Extreme SSD, and macOS TCC denies a launchd job
#     'Files on Removable Volumes' (Errno 1, Operation not permitted, 21,380 times)."
#     EVERY path this tool touches -- install_bridge.py itself, RUNTIME_BASE, the receipt, and every
#     managed hooks.json -- is on that volume BY DESIGN (resolve_ssd_path() enforces it). So a
#     launchd agent running this doctor is exactly the configuration that already failed silently
#     once here, and shipping one without saying so would be installing a health check that cannot
#     report its own death: the same class of bug as the one being fixed.
#   * The mechanism that demonstrably DOES reach the SSD is a hook running inside the user's own
#     session -- `~/.claude/settings.json`'s SessionStart hooks already run
#     `/usr/bin/python3 .../startup_context.py --knowledge-root '/Volumes/Extreme SSD/...'`
#     successfully today.
# `launchd_tcc_risk` below reports which of those a caller is in, so whichever runner is chosen, a
# silently-dead checker is itself a finding rather than silence. Nothing here installs a runner:
# that is a change to the user's own machine configuration and is theirs to make.
def doctor() -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    checked: list[str] = []

    def finding(kind: str, target: str, detail: str) -> None:
        findings.append({"kind": kind, "target": target, "detail": detail})

    receipt: dict[str, Any] | None = None
    try:
        receipt = read_receipt()
    except InstallError as exc:
        # "not installed", "pending journal", "unreadable receipt" are all real, actionable states
        # and none of them may be reported as silence.
        finding("receipt_unavailable", os.fspath(RUNTIME_BASE / "latest-receipt.json"), str(exc))

    expected_script = expected_policy = None
    if receipt is not None:
        release_dir = Path(receipt["release_dir"])
        expected_script = os.fspath(release_dir / "claude_memory_hook.py")
        expected_policy = os.fspath(release_dir / "policy.json")

    targets: list[str] = []
    if receipt is not None:
        targets.extend(
            row["path"] for row in receipt["configs"] if isinstance(row, dict) and isinstance(row.get("path"), str)
        )
    # Enumerated LIVE and independently of the receipt, in both halves: an account that appeared
    # after the last install is examined rather than being invisible the way it is to a purely
    # receipt-driven scan, and -- the case that matters most -- a machine whose receipt is gone or
    # unparseable still gets every discoverable config checked for the one question that does not
    # need a receipt to answer: is there a redaction handler here AT ALL? Without this, doctor()
    # degraded to "no receipt" and checked nothing in exactly the state most likely to be the
    # incident (caught by `test_doctor_still_answers_the_only_question_that_matters_without_a_
    # receipt`, which failed against the first version of this function).
    #
    # `_enumerate_accounts()` rather than `discover_hook_configs()` on purpose: the latter enforces
    # an "at least 2 configs" policy that belongs to INSTALLING, and a health check must keep
    # working when only one config is left -- the same reasoning uninstall()'s safety scan uses.
    try:
        discovered, registry_accounts = _enumerate_accounts()
    except InstallError as exc:
        discovered, registry_accounts = [], {}
        finding("registry_unreadable", os.fspath(orca_accounts_root()), str(exc))
    for config in discovered:
        if os.fspath(config) not in targets:
            targets.append(os.fspath(config))
    for account_id, located in sorted(registry_accounts.items()):
        if located is None:
            finding(
                "account_unmanageable",
                account_id,
                "this live Orca account's hooks.json does not resolve onto the Extreme SSD, so its "
                "redaction hook cannot be checked from here",
            )
            continue
        if os.fspath(located) not in targets:
            targets.append(os.fspath(located))

    for target in targets:
        checked.append(target)
        try:
            path = resolve_ssd_path(Path(target), must_exist=False)
        except InstallError as exc:
            finding("config_unreachable", target, str(exc))
            continue
        if _path_is_absent(path):
            # THE incident. A Codex app upgrade deleted ~/.codex/hooks.json on 2026-08-21 and the
            # machine ran an unredacted hook for about a week.
            finding(
                "config_missing",
                target,
                "hooks.json does not exist. If this account is live, NOTHING is redacting its "
                "prompts -- this is the exact state a Codex app upgrade left this machine in on "
                "2026-08-21. Re-run `install`.",
            )
            continue
        try:
            raw = validate_owned_file(path, private=True)
        except InstallError as exc:
            finding("config_unreadable", target, str(exc))
            continue
        if expected_script is None or expected_policy is None or receipt is None:
            # Without a receipt there is no expected release to compare against, but the single most
            # important question -- is there a redaction handler here AT ALL? -- still has an answer.
            try:
                payload = strict_json(raw)
            except InstallError as exc:
                finding("config_unparseable", target, str(exc))
                continue
            handlers = payload.get("hooks", {}).get(DEFAULT_HOOK_EVENT, []) if isinstance(payload, dict) else []
            handlers = handlers if isinstance(handlers, list) else []
            if not [handler for handler in handlers if owned_handler(handler)]:
                finding(
                    "hook_missing",
                    target,
                    f"no handler owned by {BRIDGE_ID}: this account's prompts are NOT being redacted",
                )
            continue
        health = _config_hook_health(
            raw,
            expected_script=expected_script,
            expected_policy=expected_policy,
            expected_script_sha256=receipt["script_sha256"],
            expected_policy_sha256=receipt["policy_sha256"],
        )
        for problem in health["problems"]:
            finding(problem["kind"], target, problem["detail"])
        if health["hook_present"] and not health["problems"]:
            # The handler self-declares the script it runs; confirm that file is really there and
            # really is the release, rather than trusting the command line's own assertion about it.
            try:
                script_raw = validate_owned_file(resolve_ssd_path(Path(expected_script)), private=True)
            except InstallError as exc:
                finding("script_unreadable", expected_script, str(exc))
                continue
            if sha256_bytes(script_raw) != receipt["script_sha256"]:
                finding(
                    _HOOK_HEALTH_TAMPERED,
                    expected_script,
                    "the script this account's hook runs does not hash to the installed release",
                )

    # Self-check for the way a periodic runner of THIS command would die silently. The hazard is
    # real and measured on this machine (`ego-reaper-launchd/README.md`, throwaway launchd job,
    # 2026-08-28) but it is narrower than "launchd cannot read /Volumes", which is what the first
    # version of this check asserted and is wrong:
    #
    #     /bin/cat                     reading a source on /Volumes/...   DENIED (Errno 1)
    #     /usr/bin/python3             running a source on /Volumes/...   DENIED (exit 2)
    #     /opt/homebrew/bin/python3    running a source on /Volumes/...   OK
    #
    # The denial applies to APPLE PLATFORM BINARIES in a launchd session, not to launchd generally,
    # so the configuration that fails silently is specifically "run from launchd, under
    # /usr/bin/python3 (or any /bin,/usr/bin tool), against paths on the external volume" -- and
    # every path this tool manages is on that volume by design (resolve_ssd_path() enforces it).
    # `com.local.orca-terminal-dispatch-reaper` already runs on this machine under
    # /opt/homebrew/bin/python3 for exactly this reason.
    #
    # This is reported rather than acted on: it is only a hazard for a launchd caller, an
    # interactive run inherits its terminal's removable-volume grant and is unaffected (which is
    # what made the Ego reaper's breakage look like it was working), and choosing/installing a
    # runner is a change to the user's own machine configuration.
    runtime_external = os.fspath(RUNTIME_BASE).startswith("/Volumes/")
    # Both forms are tested, and the Xcode/CommandLineTools prefixes are in the list, because
    # `/usr/bin/python3` is a STUB that re-execs `$(xcode-select -p)/usr/bin/python3` -- so
    # `sys.executable` under it reads `/Applications/Xcode.app/.../usr/bin/python3` and a check for
    # `/usr/bin/` alone silently returns "safe" for the single most likely interpreter a plist
    # author would reach for. (Observed while writing this: the first version of this check reported
    # launchd_tcc_risk=false when run under /usr/bin/python3, which is precisely the denied case.)
    _APPLE_PLATFORM_PREFIXES = (
        "/usr/bin/",
        "/bin/",
        "/System/",
        "/Applications/Xcode.app/",
        "/Library/Developer/CommandLineTools/",
    )
    interpreter_forms = {sys.executable or "", os.path.realpath(sys.executable) if sys.executable else ""}
    apple_platform_interpreter = any(
        form.startswith(_APPLE_PLATFORM_PREFIXES) for form in interpreter_forms if form
    )
    result: dict[str, Any] = {
        "ok": not findings,
        "checked": checked,
        "findings": findings,
        "runtime_base": os.fspath(RUNTIME_BASE),
        "interpreter": sys.executable,
        "launchd_tcc_risk": runtime_external and apple_platform_interpreter,
        "launchd_tcc_note": (
            "this run's interpreter is an Apple platform binary and the paths it checks are on an "
            "external volume. That exact combination is denied under launchd (measured on this "
            "machine) and would fail before producing any output -- it is how the Ego reaper stayed "
            "silently dead for two weeks. An interactive run like this one is unaffected. To run "
            "this on a timer, use /opt/homebrew/bin/python3, as "
            "com.local.orca-terminal-dispatch-reaper already does."
            if runtime_external and apple_platform_interpreter
            else "this interpreter/path combination is safe to run from launchd."
        ),
    }
    if findings:
        result["summary"] = (
            "ATTENTION: " + str(len(findings)) + " problem(s) found. "
            + "; ".join(f"[{item['kind']}] {item['target']}" for item in findings)
        )
    else:
        result["summary"] = (
            "HEALTHY: a redaction handler owned by this bridge is present, correctly pointed and "
            "backed by the expected script on all " + str(len(checked)) + " config(s) checked."
        )
    return result


def uninstall() -> dict[str, Any]:
    # Now durably journaled exactly like install() (P1-2 fix, independent
    # Claude opus5/max review, 2026-08-17, round 1): previously uninstall()
    # had no transaction log at all -- only an in-process try/except that a
    # real SIGKILL mid-loop skips entirely, leaving some configs reverted
    # and others still installed, a state recover_pending_install() could
    # not even see (it only knew about install-shaped journals), and the
    # only tool action that could make progress was install(), which (via
    # the P1-1 bug this same round also fixes) would re-poison the baseline
    # and leave the bridge permanently, silently active in whatever configs
    # never got reverted.
    recover_pending_install()
    receipt = read_receipt()
    # uninstall() otherwise operates entirely off the receipt's own path
    # list -- normally correct, since the receipt is the source of truth
    # for what this tool manages. But a receipt-managed account directory
    # renamed or moved since the last install leaves a live owned handler
    # at a path this receipt has NO row for at all: the old path is simply
    # "absent" (nothing to revert, handled below), and the new path is
    # invisible to a receipt-driven scan entirely. Before this fix,
    # uninstall() would revert whatever it could see, delete
    # latest-receipt.json as a normal successful commit, and report
    # ok:true -- while the relocated config kept executing the bridge with
    # no record left anywhere that it existed. Renaming it back afterward
    # did not help either: with the receipt gone, install()'s own R6-P1-A
    # guard (see its comment) refused to adopt the already-bridged content,
    # and no other action could recover it -- every path refused forever
    # except plan (independent Claude opus5/max review, 2026-08-17, round
    # 7, R7-P1-A/R7-P1-B; independently reproduced as R7-P1-A by Codex
    # sol/xhigh, same round). This is reachable by calling uninstall()
    # directly while a managed path is still renamed -- exactly what this
    # file's own R6-P1-A fix's error message used to recommend as the
    # first thing to try. Scan for any owned handler at a path the receipt
    # does not track (_untracked_owned_including_write_trigger(), wrapping
    # _find_untracked_owned_configs() -- a walk of the whole local-homes
    # tree, wider than the one-level-deep shape _enumerate_hook_configs()
    # understands, so a relocation is caught regardless of where under
    # local-homes it landed; see that function's own comment and R8-P1-B),
    # under the base identity AND, unconditionally, the write-trigger identity too (P2-D fix,
    # independent Claude opus5/max review, 2026-08-21; no longer gated on this one receipt's own
    # write_trigger_bridge_id field -- P2-2 fix, independent Claude opus5/max review, 2026-08-21 --
    # see _untracked_owned_including_write_trigger()'s own comment for why); if any is found under
    # either identity, refuse before
    # writing the pending journal or touching anything, so the receipt and
    # every config stay exactly as they were and the always-safe
    # remediation -- restore the path to where the receipt expects it --
    # remains available. recover_pending_install() runs the identical scan
    # before finishing an interrupted uninstall, since that commit path can
    # also delete the receipt and does not go through this function at all
    # (R8-P1-A).
    receipt_paths = {row["path"] for row in receipt["configs"]}
    untracked_owned = _untracked_owned_including_write_trigger(receipt, receipt_paths)
    if untracked_owned:
        raise InstallError(
            "refusing uninstall: found an owned hook handler at a path the current receipt does not "
            "track (a managed account directory may have moved -- restore it to its receipt-recorded "
            "path before uninstalling): " + ", ".join(untracked_owned)
        )
    rows = _receipt_rows(receipt)
    drifted = [row for row in rows if row["state"] == "drift"]
    if drifted:
        raise InstallError(
            "refusing uninstall because a config changed: "
            + ", ".join(os.fspath(row["path_obj"]) for row in drifted)
        )
    # A row whose target path is genuinely gone (e.g. a permanently deleted
    # or re-provisioned Codex account -- see install()'s carried-forward
    # comment and _receipt_rows()'s "absent" branch) has nothing to revert:
    # there is no file to restore pristine content into, and creating one
    # would resurrect a config for an account that no longer exists. Report
    # it separately as unreachable instead of writing to it or refusing the
    # whole uninstall over it (independent Claude opus5/max review,
    # 2026-08-17, round 3, R3-P1-A).
    unreachable = [os.fspath(row["path_obj"]) for row in rows if row["state"] == "absent"]
    # Load every backup this transaction will actually write from BEFORE the
    # durable journal exists, not lazily inside the write loop below. Round
    # 4 validated every needed backup eagerly inside _receipt_rows(), so an
    # unloadable backup was always a clean, nothing-happened refusal. Making
    # `backup` lazy per-row (R4-P2-A's fix) removed that implicit
    # precondition: the same unloadable-backup input moved from "refuse
    # before anything becomes durable" to "discover it after the journal is
    # written and after earlier rows have already been reverted" -- leaving
    # a half-uninstalled system (some configs reverted, one still bridged)
    # with a pending journal that recover_pending_install() itself cannot
    # clear, because it hits the identical unloadable backup. If the backup
    # is permanently gone, no tool action can ever finish or abandon the
    # uninstall (independent Claude opus5/max review, 2026-08-17, round 5,
    # R5-P1-A, reproduced end to end with a single `rm -rf` of a live
    # backup directory -- no crash, race, privilege, or carried-forward row
    # required). Loading here keeps R4-P2-A's fix intact: before/both/
    # absent rows are still skipped, so a carried-forward row's pruned
    # backup remains harmless.
    backup_bytes = {
        os.fspath(row["path_obj"]): _load_backup(row["backup_obj"], row["before_sha256"])
        for row in rows
        if row["state"] not in ("before", "both", "absent")
    }
    receipt_raw = canonical_json(receipt)
    atomic_write(
        PENDING_PATH,
        canonical_json({"schema": JOURNAL_SCHEMA, "kind": "uninstall", "receipt": receipt}),
        0o600,
    )
    try:
        for row in rows:
            if row["state"] in ("before", "both", "absent"):
                continue
            backup_raw = backup_bytes[os.fspath(row["path_obj"])]
            atomic_write(row["path_obj"], backup_raw, row["before_mode"])
            restored = validate_owned_file(row["path_obj"])
            restored_mode = _mode_bits(row["path_obj"])
            if sha256_bytes(restored) != row["before_sha256"] or restored_mode != row["before_mode"]:
                raise InstallError(f"transaction rollback verification failed: {row['path_obj']}")
        latest_path = resolve_ssd_path(RUNTIME_BASE / "latest-receipt.json")
        remove_file_durable(latest_path)
        outcome = recover_pending_install()
        if outcome.get("state") != "uninstalled":
            raise InstallError("uninstall commit journal did not finalize")
    except BaseException as exc:
        try:
            outcome = recover_pending_install()
        except BaseException as recovery_exc:
            raise InstallError(
                "uninstall failed and the durable recovery journal remains pending"
            ) from recovery_exc
        if outcome.get("state") == "uninstalled":
            return {
                "ok": True,
                "restored": [os.fspath(row["path_obj"]) for row in rows if row["state"] != "absent"],
                "unreachable": unreachable,
                "runtime_retained": True,
            }
        raise InstallError("uninstall failed; configs restored to their pre-uninstall state") from exc
    return {
        "ok": True,
        "restored": [os.fspath(row["path_obj"]) for row in rows if row["state"] != "absent"],
        "unreachable": unreachable,
        "runtime_retained": True,
    }


def plan(*, write_trigger: dict[str, Any] | None = None) -> dict[str, Any]:
    # `write_trigger`'s read-only twin of install()'s own second update_hook_config() call (see that
    # function's comment) -- this is install_write_trigger()'s --dry-run path, and (via `write_trigger
    # =None`, every real call site today) still the exact plan() the `plan` CLI action has always run.
    release = make_release(write_trigger=write_trigger)
    pending = PENDING_PATH.exists() or PENDING_PATH.is_symlink()
    configs = []
    for path in release["hook_configs"]:
        raw = validate_owned_file(path)
        after = update_hook_config(raw, release["command"])
        if write_trigger is not None:
            after = update_hook_config(
                after,
                release["write_trigger_command"],
                event="SessionEnd",
                bridge_id=write_trigger["bridge_id"],
                timeout=None,
                status_message="Scanning session for durable memory candidates",
            )
        configs.append(
            {
                "path": os.fspath(path),
                "before_sha256": sha256_bytes(raw),
                "after_sha256": sha256_bytes(after),
                "will_change": raw != after or _mode_bits(path) != 0o600,
            }
        )
    result = {
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
    if write_trigger is not None:
        result["write_trigger_script_sha256"] = release["write_trigger_script_sha256"]
    return result


def _load_write_trigger_config(policy_path: Path) -> dict[str, Any]:
    # Fail-closed boundary for install_write_trigger(): the supplied file must be a policy.json-
    # shaped JSON object whose "write_trigger" block (design doc section 2.2) has enabled: true.
    # Anything else -- unreadable file, malformed JSON, a missing/disabled/malformed write_trigger
    # block -- refuses here, before install()/plan() ever run, so this action can never silently
    # register a SessionEnd handler that scans nothing (write_candidate_capture.scan() itself is a
    # no-op whenever its own policy's write_trigger.enabled is false -- registering the hook without
    # enabling it would just waste a Codex SessionEnd cycle on every session for no effect).
    try:
        raw = Path(policy_path).read_bytes()
    except OSError as exc:
        raise InstallError(f"cannot read write-trigger policy: {policy_path}") from exc
    parsed = strict_json(raw)
    if not isinstance(parsed, dict):
        raise InstallError(f"write-trigger policy must be a JSON object: {policy_path}")
    # Uses _import_write_candidate_capture() (shared, as of the P2-1/P2-2 fix round, 2026-08-21, with
    # make_release()'s own write_trigger validation and _untracked_owned_including_write_trigger()'s
    # safety-net scan -- see that helper's own comment): install_write_trigger()'s own task explicitly
    # calls for importing write_candidate_capture.MODULE_ID directly rather than re-deriving it,
    # unlike the low-level detection/rewrite chain (owned_handler() and friends), which deliberately
    # stays free of this import -- see BRIDGE_ID's own comment / section 13.3/17.1 for why those
    # functions instead take bridge_id as a plain string. Reusing
    # write_candidate_capture._parse_write_trigger_block() directly (rather than re-implementing its
    # validation here) is also deliberate -- this file must not grow a second, less-reviewed
    # write_trigger schema validator that could drift out of sync with the real one.
    #
    # Guarded (round-54 fix, independent Claude opus5/max GO + Codex gpt-5.6-sol/max NO-GO dual
    # review, 2026-08-21, issue 2): unguarded here, this is install_write_trigger()'s FIRST call
    # (before install()/plan() run), so a genuinely missing write_candidate_capture.py made the
    # `install-write-trigger` CLI action -- dry-run and real alike -- raise a raw, uncaught
    # ModuleNotFoundError past main()'s own `except InstallError` handler: empty stdout, rc=1, a raw
    # traceback on stderr, the same failure shape round-53 fixed for verify() but never propagated
    # to this call site.
    try:
        _wtc = _import_write_candidate_capture()
    except (ModuleNotFoundError, SyntaxError, ImportError) as exc:
        raise _wrap_write_candidate_capture_import_error(exc, "cannot load write-trigger policy") from exc

    try:
        config = _wtc._parse_write_trigger_block(parsed.get("write_trigger"))
    except _wtc.WriteCaptureError as exc:
        raise InstallError(f"invalid write_trigger policy block: {exc}") from exc
    if not config.enabled:
        raise InstallError(
            "write_trigger.enabled must be true in the supplied policy to install the write-trigger hook"
        )
    return {
        "bridge_id": _wtc.MODULE_ID,
        "max_candidates_per_project": config.max_candidates_per_project,
        "max_candidate_bytes": config.max_candidate_bytes,
    }


def install_write_trigger(policy_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    # The `install-write-trigger` CLI action. A thin, additive wrapper: all it does beyond the
    # fail-closed policy validation above is call this file's own, already-transactional install()/
    # plan() with a validated `write_trigger` dict -- the same atomic-write/receipt/backup/pending-
    # journal/recover machinery, and the same uninstall()/recover_pending_install() rollback path,
    # every other action already reuses (AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md, install-write-
    # trigger section). Deliberately narrow, not a generic "register any command under any event"
    # action: event ("SessionEnd"), bridge_id (write_candidate_capture.MODULE_ID, from
    # _load_write_trigger_config()), and timeout (None, forced by install()'s own second
    # update_hook_config() call) are never caller-controlled here.
    write_trigger = _load_write_trigger_config(policy_path)
    if dry_run:
        return plan(write_trigger=write_trigger)
    return install(write_trigger=write_trigger)


def _acquire_exclusive_lock() -> int:
    # Concurrent invocations of this installer used to be able to interleave:
    # process B's recover_pending_install() (which both install() and
    # uninstall() call internally at their own start) could consume process
    # A's still-in-flight pending journal, producing a JSON report that says
    # the action failed while the disk state actually reflects success, or
    # vice versa (independent Claude opus5/max review, 2026-08-17, round 1,
    # P2-3, reproduced: a losing concurrent install() reported
    # {"ok": false, ...} while both hook configs and latest-receipt.json
    # were, in fact, fully and correctly installed). Acquired once here in
    # main(), around the whole dispatched action -- not inside
    # install()/uninstall() themselves, since those call
    # recover_pending_install() internally and flock() locks an *open file
    # description*, not the whole process; a second open+flock from the
    # same process on the same lock file would otherwise self-block.
    #
    # Round-2 regression fixed here (independent Claude opus5/max review,
    # 2026-08-17, round 2, R2-P1-B + R2-P2-A): the first version of this
    # function called `lock_path.parent.mkdir(mode=0o700, parents=True,
    # exist_ok=True)`. `pathlib.Path.mkdir(parents=True)` does NOT apply
    # `mode` to intermediate parent directories it creates along the way --
    # only to the final target -- so on a machine where `.shared-runtime`
    # does not exist yet (confirmed to be the real state of the actual
    # target machine at review time), that directory was created at the
    # default umask-derived mode (0o755, not 0o700), `ensure_private_dir()`
    # then correctly refused it as unsafe on every subsequent call, and
    # nothing ever chmods it back -- a fresh machine's very first `install`
    # failed permanently and non-self-healingly. Building the chain one
    # level at a time through the existing, already-proven
    # `ensure_private_dir()` (the same helper write_runtime() uses for this
    # exact directory) creates each level with an explicit, non-parents
    # `mkdir(mode=0o700)`, so this can never happen; also guards the open()
    # itself (it previously ran unguarded, reintroducing the class of bare
    # OSError the earlier atomic_write() fix exists to eliminate) and adds
    # O_NOFOLLOW, matching every other open in this file.
    ensure_private_dir(RUNTIME_BASE.parent)
    ensure_private_dir(RUNTIME_BASE)
    lock_path = RUNTIME_BASE / "installer.lock"
    try:
        descriptor = os.open(
            lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except OSError as exc:
        raise InstallError(f"cannot open lock file: {lock_path}") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise InstallError("another install_bridge.py invocation is already running") from exc
    return descriptor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("plan", "install", "verify", "doctor", "uninstall", "recover", "install-write-trigger"),
    )
    # Only meaningful for install-write-trigger; unused (and untouched-by-any-other-action) otherwise.
    parser.add_argument("--write-trigger-policy", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    lock_descriptor = -1
    try:
        # install-write-trigger writes through the exact same transactional machinery install()
        # already uses (see that function), so it needs the same exclusive lock -- but only when it
        # will actually write; --dry-run calls plan(), which (like the plain `plan` action) never
        # acquires this lock.
        writes = args.action in ("install", "uninstall", "recover") or (
            args.action == "install-write-trigger" and not args.dry_run
        )
        if writes:
            lock_descriptor = _acquire_exclusive_lock()
        if args.action == "plan":
            result = plan()
        elif args.action == "install":
            result = install()
        elif args.action == "verify":
            result = verify()
        elif args.action == "doctor":
            result = doctor()
        elif args.action == "uninstall":
            result = uninstall()
        elif args.action == "recover":
            result = recover_pending_install()
        else:
            if not args.write_trigger_policy:
                raise InstallError("install-write-trigger requires --write-trigger-policy")
            result = install_write_trigger(Path(args.write_trigger_policy), dry_run=args.dry_run)
    except InstallError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if lock_descriptor >= 0:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    # `verify` is the one action whose whole purpose is to answer "is this machine correctly
    # bridged?", so its own `ok: false` -- unmanaged Orca accounts (see verify()) -- must reach a
    # scripted caller through the exit status too, not only through JSON a caller may not parse.
    # Every other action keeps returning 0 on a structured result exactly as before (`plan` has
    # always reported `ok: false` for a pending transaction at rc 0).
    # `doctor` joins `verify` here for the same reason (round 4, 2026-08-28): it exists to be run
    # unattended, so its verdict has to reach a scripted caller through the exit status and not only
    # through JSON. A health check whose failure is invisible to `||` is the shape of the bug it is
    # meant to catch.
    if args.action in ("verify", "doctor") and not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
