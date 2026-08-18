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
            raise InstallError(f"cannot safely inspect {path}: too large")
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


def _enumerate_hook_configs() -> list[Path]:
    # The raw enumeration discover_hook_configs() is built on, split out so
    # a caller that only wants to know "what managed-shaped configs
    # currently exist" (uninstall()'s R7-P1-A safety scan, below) can reuse
    # it without also inheriting discover_hook_configs()'s own "at least 2"
    # policy -- that minimum is specific to *installing* (this bridge is
    # only meant to run against isolated multi-account setups) and has
    # nothing to do with what a safety scan needs, which must keep working
    # even when only one config remains discoverable.
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
        if _is_regular_file(candidate):
            configs.append(resolve_ssd_path(candidate))
    return list(dict.fromkeys(configs))


def discover_hook_configs() -> list[Path]:
    unique = _enumerate_hook_configs()
    if len(unique) < 2:
        raise InstallError("no isolated Codex account hook configs found")
    return unique


def owned_handler(handler: Any) -> bool:
    # Structural match against the exact `--bridge-id <BRIDGE_ID>` argv pair
    # make_release() generates, not a raw substring search over the whole
    # command string. A substring check treats BRIDGE_ID appearing *anywhere*
    # in an unrelated hook's command -- inside a comment, a log message, an
    # unrelated flag's value, or another bridge's command that merely mentions
    # this one -- as "owned by this installer", and update_hook_config()
    # deletes/replaces whatever owned_handler() returns True for. That would
    # silently destroy a hook this installer never created (found in the
    # full-audit Workflow, 2026-08-17, deferred at the time because
    # install_bridge.py had never been run; now in scope ahead of an actual
    # install). Parsing the command the way a shell would and requiring
    # BRIDGE_ID to be the exact token immediately following an exact
    # `--bridge-id` token closes that: BRIDGE_ID showing up as a substring of
    # some other token, or without the adjacent flag, no longer matches.
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
            if token == "--bridge-id" and index + 1 < len(tokens) and tokens[index + 1] == BRIDGE_ID:
                return True
    return False


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
# is a handful of UserPromptSubmit handler entries -- a few KB at most. Both of the expensive
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


def _owned_shape_match(payload: Any) -> bool | None:
    # The single decision point both parse layers funnel every successfully-parsed payload
    # through. None means `payload` is not recognizable as one of our hooks.json documents at
    # all (dict -> "hooks":dict -> "UserPromptSubmit":list) -- genuinely ambiguous, the caller
    # should keep looking under a different encoding/strategy. True or False means `payload`
    # unambiguously IS shaped like one of our configs, definitively containing (True) or not
    # containing (False) an owned handler -- final for that parse attempt.
    if not isinstance(payload, dict):
        return None
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return None
    handlers = hooks.get("UserPromptSubmit")
    if not isinstance(handlers, list):
        return None
    return any(owned_handler(handler) for handler in handlers)


def _raw_bytes_contain_bridge_marker(raw: bytes) -> bool:
    # Layer 2's final, genuine-ambiguity-only fallback: a pure byte-substring search for the
    # literal `--bridge-id <BRIDGE_ID>` marker under every byte width this file's own detection
    # encodings could plausibly render it in. ASCII/UTF-8/Latin-1 all encode this marker's
    # characters identically as single bytes, so one "utf-8" pattern covers all three; UTF-16 and
    # UTF-32, little- and big-endian, each need their own pattern. Deliberately NOT a decode of
    # the whole buffer under each codec -- `bytes.__contains__` is a fast, size-independent C-level
    # substring search, so this stays cheap even for a multi-megabyte file, unlike Layer 1's
    # structural parse. Only ever reached when no encoding produced a structurally recognizable
    # payload (or the file was too large to attempt one), so a hit here is ambiguous evidence, not
    # proof -- the caller fails closed on it rather than trusting it as a positive detection.
    marker_text = f"--bridge-id {BRIDGE_ID}"
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        if marker_text.encode(encoding) in raw:
            return True
    return False


def _attempt_structural_detection(raw: bytes, *, max_bytes: int) -> bool | None:
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
        return bool(_owned_shape_match(payload))
    for text in _lenient_decode_candidates(raw):
        payload = _lenient_parse_last_key_wins(text)
        if payload is _NOT_PARSED:
            continue
        match = _owned_shape_match(payload)
        if match is not None:
            return match
    return None


def _contains_owned_handler(raw: bytes, *, structural_max_bytes: int = _STRUCTURAL_DETECTION_MAX_BYTES) -> bool:
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
        result = _attempt_structural_detection(raw, max_bytes=structural_max_bytes)
    except Exception:
        result = None
    if result is not None:
        return result
    if _raw_bytes_contain_bridge_marker(raw):
        raise InstallError(
            "cannot rule out an owned hook handler: content matches the bridge marker but does "
            "not parse as a recognizable hooks.json under any supported encoding"
        )
    return False


def _find_untracked_owned_configs(receipt_paths: set[str]) -> list[str]:
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
    # (_is_regular_file()'s own raise), and an unreadable or oversized file
    # (_read_for_detection()'s own raises) all instead degrade to "nothing
    # found there", same as ENOENT (self-check Workflow, 2026-08-18, round 2
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
        # comment and R9-P1-B. None means "not a file we can identify"; any
        # other failure (cannot open/read, oversized, identity changed
        # mid-read) is absence-of-evidence, same tolerance/re-raise policy
        # as _is_regular_file() above.
        try:
            candidate_raw = _read_for_detection(resolved)
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
            owned = _contains_owned_handler(candidate_raw, structural_max_bytes=structural_max_bytes)
        except Exception as exc:
            raise InstallError(f"{exc} (at {candidate_str})") from exc
        if owned:
            untracked_owned.append(candidate_str)
    return sorted(dict.fromkeys(untracked_owned))


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
    untracked_owned = _find_untracked_owned_configs(receipt_paths)
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


def install() -> dict[str, Any]:
    recover_pending_install()
    release = make_release()
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
        updated[path] = update_hook_config(raw, release["command"])

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
                existing_payload.get("hooks", {}).get("UserPromptSubmit", [])
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
        "unreachable": unreachable,
    }


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
    # does not track (_find_untracked_owned_configs() -- a walk of the
    # whole local-homes tree, wider than the one-level-deep shape
    # _enumerate_hook_configs() understands, so a relocation is caught
    # regardless of where under local-homes it landed; see that function's
    # own comment and R8-P1-B); if any is found, refuse before writing the
    # pending journal or touching anything, so the receipt and every
    # config stay exactly as they were and the always-safe remediation --
    # restore the path to where the receipt expects it -- remains
    # available. recover_pending_install() runs the identical scan before
    # finishing an interrupted uninstall, since that commit path can also
    # delete the receipt and does not go through this function at all
    # (R8-P1-A).
    receipt_paths = {row["path"] for row in receipt["configs"]}
    untracked_owned = _find_untracked_owned_configs(receipt_paths)
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
                "will_change": raw != after or _mode_bits(path) != 0o600,
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
    parser.add_argument("action", choices=("plan", "install", "verify", "uninstall", "recover"))
    args = parser.parse_args()
    lock_descriptor = -1
    try:
        if args.action in ("install", "uninstall", "recover"):
            lock_descriptor = _acquire_exclusive_lock()
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
    finally:
        if lock_descriptor >= 0:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
