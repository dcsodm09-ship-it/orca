#!/usr/bin/env python3
"""Install a pinned Prime Agent release into an SSD-only managed prefix.

This deliberately does not use the upstream curl-to-shell installer. Four
official release tarballs, the release source lock, the upstream MIT license,
and the pinned Node.js toolchain are digest verified. Lifecycle scripts are disabled, direct
dependencies are exactified from the signed release commit, and the complete
generated production closure must match an independently reproduced normalized
lock digest before npm ci may run.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import gzip
import hashlib
import io
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


VERSION = "0.7.2"
TAG_COMMIT = "83a0f9f9566219551fcb6ffaf7f519a815749a58"
NODE_VERSION = "24.19.0"
NPM_VERSION = "11.17.0"
PRIME_AGENT_PROCESS_TITLE = "prime-agent"
NODE_ASSET = f"node-v{NODE_VERSION}-darwin-arm64.tar.gz"
NODE_ASSET_SHA256 = "8294b7aa9b03997481c06babf1e8b270c859358f27da57a11509afe537ac381d"
RECEIPT_SCHEMA = "orca.prime-agent-managed-install.v2"
JOURNAL_SCHEMA = "orca.prime-agent-managed-install-journal.v2"
SSD_ROOT = Path("/Volumes/Extreme SSD")
LOCAL_HOMES = SSD_ROOT / "Orca/local-homes"
TOOL_ROOT = LOCAL_HOMES / ".shared-tools/prime-agent"
RELEASE_DIR = TOOL_ROOT / "releases" / f"v{VERSION}"
STATE_DIR = TOOL_ROOT / "state"
PROBE_HOME = TOOL_ROOT / "probe-home"
USER_HOME = Path.home()
STATE_LINK = USER_HOME / ".prime"
BIN_LINK = USER_HOME / ".local/bin/prime-agent"
RECEIPT_PATH = TOOL_ROOT / "receipts" / f"v{VERSION}.json"
PENDING_PATH = TOOL_ROOT / "pending-install.json"
LOCK_URL = (
    "https://raw.githubusercontent.com/PrimeIntellect-ai/prime-agent/"
    f"{TAG_COMMIT}/package-lock.json"
)
LOCK_SHA256 = "7e708d473e01fe3f992e8e12e25c4f815282a1763038f1d0f65a8a0f6d7b37e7"
LICENSE_URL = (
    "https://raw.githubusercontent.com/PrimeIntellect-ai/prime-agent/"
    f"{TAG_COMMIT}/LICENSE"
)
LICENSE_SHA256 = "b288615fb31dc504623582fb790a28e6d86bc2f5c1396845af555e43386da5a0"
# Filled from independent darwin-arm64 replays using the pinned Node/npm toolchain.
# The normalized hash retains the complete registry dependency graph, URLs, and
# integrity digests while canonicalizing only path-dependent managed file assets.
# npm ci is not allowed to run until that exact closure is reproduced. Refreshed
# 2026-08-21 after two independent, real isolated replays found the 2026-08-19
# value stale (npm's transitive resolution had moved again, not this file's own
# pinned direct dependencies or release assets): 3 of 196 registry rows changed,
# all @smithy/* patch bumps published 2026-08-20 by the same official
# aws-sdk-js maintainers via GitHub Actions OIDC trusted publishing (verified
# against real Sigstore/SLSA attestations, not just registry metadata); row
# count is unchanged at 200. See README.md's "Pinned upstream evidence"
# section for the full re-verification record.
GENERATED_LOCK_SHA256 = "fe4402ae740cc0d2f326baf58f80543ecf8e9668e22f6434f0941bed43c732f5"
GENERATED_LOCK_PACKAGE_COUNT = 200
# The UTC calendar date GENERATED_LOCK_SHA256 above was last re-pinned (not
# when this file was edited for any other reason). Every review round of
# this file has independently rediscovered that npm's transitive resolution
# keeps drifting a few days after each pin -- round-53 QA (2026-08-22)
# recommended a visible revalidation cadence instead of relying on someone
# remembering to check; generated_lock_pin_age_days() reads this purely to
# report elapsed time, offline, in plan()'s evidence. Deliberately NOT a
# hard gate: a stale pin is a prompt to re-verify, not proof anything is
# actually wrong today, and turning wall-clock age into a hard failure
# would make every install eventually stop working on its own even when
# nothing upstream has changed.
#
# Round-54 dual review (2026-08-22, both Claude opus/max and Codex sol/max
# independently) caught this constant wrong on arrival: GENERATED_LOCK_SHA256
# was actually re-pinned by commit bafce01bf5, authored 2026-08-21T23:58:36
# +08:00 = 2026-08-21T15:58:36Z -- not 2026-08-22 (that was this constant's
# own author's LOCAL calendar date while writing the round-54 follow-up
# commit, a different thing). The wrong value made plan()'s
# generated_lock_pin_age_days report -1 on this file's own re-verification
# run, for the first several hours of every day this constant is ever
# updated on a UTC+ timezone machine -- see that function's own clamp for
# the other half of this fix.
GENERATED_LOCK_PINNED_AT = "2026-08-21"
ASSETS = {
    "prime-agent-0.7.2.tgz": "bc5471f2a626d727b88a45eb745fff93b10c554a3c4fc5912f25d8c64b987f5e",
    "prime-agent-ai-0.7.2.tgz": "0777108abbe12ffcd3efdbf063e1f321ff2a1b16c08a81867d9a6c0addcd1f8d",
    "prime-agent-core-0.7.2.tgz": "3d576b5edb4634be821c865c68af2afecaf06f5b6148ee9115f99e685704f4bf",
    "prime-agent-tui-0.7.2.tgz": "642285cd8f1bd06531cfa2f07c6a171a6efd4f54e8bc63fe9cede3838a0453e5",
}
WORKSPACE_PACKAGES = {
    "@earendil-works/pi-agent-core": {
        "source_name": "prime-agent-core",
        "official_asset": "prime-agent-core-0.7.2.tgz",
        "patched_asset": "prime-agent-core-0.7.2-orca-pinned.tgz",
    },
    "@earendil-works/pi-ai": {
        "source_name": "prime-agent-ai",
        "official_asset": "prime-agent-ai-0.7.2.tgz",
        "patched_asset": "prime-agent-ai-0.7.2-orca-pinned.tgz",
    },
    "@earendil-works/pi-tui": {
        "source_name": "prime-agent-tui",
        "official_asset": "prime-agent-tui-0.7.2.tgz",
        "patched_asset": "prime-agent-tui-0.7.2-orca-pinned.tgz",
    },
}
WORKSPACE_ASSETS = {
    name: str(package["patched_asset"]) for name, package in WORKSPACE_PACKAGES.items()
}
MAIN_PATCHED_ASSET = f"prime-agent-{VERSION}-orca-pinned.tgz"
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_NODE_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_TAR_MEMBERS = 20_000
MAX_TAR_EXPANDED_BYTES = 384 * 1024 * 1024
RENAME_EXCL = 0x00000004
# Whether this Python build can bind os.open()/os.mkdir()/os.stat()/
# os.symlink()/os.readlink()/os.unlink() to an already-open directory file
# descriptor (dir_fd=...) instead of re-resolving a path by name. Checked
# once, at import time, from Python's own platform introspection
# (os.supports_dir_fd) rather than assumed -- Darwin's dir_fd support is
# narrower than Linux's for some syscalls, and differs across the Python
# builds this installer can run under (verified True for both the Homebrew
# and Apple-system /usr/bin/python3 interpreters on this machine, but never
# assumed).
#
# ensure_local_link_parent(), verify_link_parent_descriptor(), and
# remove_exact_symlink() all require this to walk the ENTIRE ancestor chain
# from USER_HOME down to a managed link's parent purely via openat()-style
# dir_fd-relative lookups -- each component is opened relative to the
# descriptor of the previously opened and verified component, never by
# re-resolving an earlier component's name -- so that a same-UID racer
# renaming or resymlinking any ancestor, at any point during or after the
# walk, cannot redirect any step that already holds an open descriptor.
# atomic_symlink() then creates and re-verifies the link itself through that
# same descriptor, and remove_exact_symlink() inspects, quarantines, and
# deletes the link through it too (independent review round 3/4, 2026-08-18,
# P1 residual of P1-2: round 2's fix bound link creation and verification to
# this descriptor but left removal -- called from _uninstall_locked() and
# _enable_locked()'s rollback on BIN_LINK -- doing path.lstat(),
# os.readlink(path), and rename_noreplace(path, quarantine) entirely by
# lexical path, with no descriptor and no ancestor check). If a future
# platform cannot bind all six primitives to dir_fd, all three functions
# fail closed rather than silently falling back to a weaker,
# re-resolved-by-path walk (independent review round 1, 2026-08-18, P1-2
# residual: the previous fallback only covered atomic_symlink()'s own
# symlink-creation call and left the ancestor walk and all verification
# unconditionally path-based).
#
# open_verified_ancestor_chain() (the shared walk underneath all of the
# above) also takes an explicit `root`, so
# open_verified_generated_file_parent() reuses the IDENTICAL algorithm
# rooted at SSD_ROOT instead of USER_HOME to bind
# tighten_generated_private_file_mode() to a verified ancestor chain for
# RELEASE_DIR/package-lock.json, closing the analogous same-UID
# ancestor-directory-swap gap for that generated file (independent Codex
# sol/max round-19 review, 2026-08-19, P1).
LINK_DIR_FD_SUPPORTED = (
    os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.symlink in os.supports_dir_fd
    and os.readlink in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)

# The complete set of leading options Prime Agent v0.7.2 gives positional
# meaning to ahead of the real command token (currently just
# "--daemon-socket <value>" / "--daemon-socket=<value>", repeatable). This is
# the single source of truth for that set: managed_entrypoint_script()'s
# generated shell resolve_managed_command() function and
# managed_launch_guard_script()'s generated Python split_leading_options()
# function both derive their recognized-leading-option set from this one
# Python-level constant, rather than each hardcoding the option name a
# second time in its own generated script -- so a future leading option can
# be added here once and both generated parsers pick it up together, instead
# of independently drifting the way the ad hoc "$1 == '--daemon-socket' then
# $3'"/"arguments[0] == '--daemon-socket'" checks they replace already had
# (independent review round 5, 2026-08-18, P1: the prior ad hoc checks each
# recognized only a single leading "--daemon-socket <value>" pair in
# space-separated form, so the "=" form, a repeated/duplicate flag, or any
# future new leading option silently bypassed the update self-block, the
# agents/attach effective-project gate, and/or misplaced guard-flag
# insertion). Both resolve_managed_command() (shell) and
# split_leading_options() (Python) are themselves the ONE shared parsing
# function within their respective generated script -- every security
# decision that needs "the real command token" in that script uses their
# result, never a fixed argv position or form-specific pattern match again.
LEADING_COMMAND_OPTIONS: tuple[str, ...] = ("--daemon-socket",)


class PrimeInstallError(Exception):
    pass


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PrimeInstallError(f"cannot hash {path}") from exc
    return digest.hexdigest()


def sha256_file_verified(path: Path, *, max_bytes: int = MAX_TAR_EXPANDED_BYTES) -> str:
    """SHA-256 content digest of a regular file, computed through a SINGLE
    O_NOFOLLOW-open-then-fstat-identity-checked read -- the same discipline
    read_private_file() already applies to every PRIVATE-mode SSD file this
    installer reads -- but WITHOUT that function's `st_mode & 0o077 == 0`
    requirement: unlike this installer's own generated files, a file `npm
    ci` materializes from a tarball keeps the tarball's own stored mode
    bits (empirically 0o644/0o755 for a real prime-agent release; see
    tighten_generated_private_file_mode()'s and
    _install_locked_within_release_dir()'s own comments on that same real,
    non-mocked discovery), so requiring a private mode here would fail
    closed on every real, non-mocked install.

    sha256_file() -- this file's original, general-purpose digest helper --
    opens `path` by name via a plain Path.open("rb") call, entirely
    independent of whatever check (if any) a caller already performed on
    that same path. A caller that first inspects `path` via
    os.scandir()/os.stat() (to confirm it is a regular file, not a
    symlink) and only THEN calls sha256_file() leaves a same-UID racer the
    whole window between those two, independently-resolved filesystem
    accesses to swap the regular file for a symlink: the caller's own
    check already ran and passed, and sha256_file()'s plain path.open("rb")
    silently follows the newly-planted symlink, hashing whatever it points
    at instead of failing closed (independent Claude opus/max round-29
    review, 2026-08-19, P2-2, reproduced against
    assert_locally_patched_package_matches_pinned_digests()'s own
    os.scandir()-then-sha256_file() call pair: swapping a pinned regular
    file for a symlink to a file whose CURRENT content happens to match
    the pinned digest is accepted, and the symlink's target content -- or
    the symlink's own target -- can then be changed again afterward with
    nothing left to catch it, since that call was the only point this
    file's content was ever checked at all).

    This function instead performs the lstat, the O_NOFOLLOW-protected
    open, and the fstat-identity confirmation itself, as ONE operation --
    exactly mirroring read_private_file()'s own discipline -- so a caller
    never needs, and must never perform, any separate pre-check before
    calling this: a same-UID swap to a symlink at any point before this
    call is refused outright (O_NOFOLLOW on the final path component), and
    a swap of the regular file's own identity between the lstat and the
    open is refused by the (st_dev, st_ino) comparison -- exactly as
    read_private_file() already refuses both for private-mode files.
    Streams and hashes the content directly (never accumulates the whole
    file in memory), unlike read_private_file(), since callers of this
    function (the digest sweep, tree_digest()) may need to digest files
    materially larger than read_private_file()'s in-memory-copy use cases.
    """
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
        ):
            raise PrimeInstallError(f"unsafe file for digest: {path}")
        if before.st_size > max_bytes:
            raise PrimeInstallError(f"file too large to digest: {path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise PrimeInstallError(f"file identity changed before digest: {path}")
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise PrimeInstallError(f"cannot digest file: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise PrimeInstallError(f"file changed while computing digest: {path}")
    return digest.hexdigest()


def canonical_json(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _reject_unencodable_strings(value: Any) -> None:
    """Recursively reject any JSON string that cannot round-trip through a
    strict UTF-8 encode -- in practice, a lone (unpaired) UTF-16 surrogate
    code point (U+D800-U+DFFF) embedded in the source JSON via a `\\uXXXX`
    escape. RFC 8259 requires surrogates to appear only as a valid pair,
    but the stdlib decoder does not enforce that, so json.loads() accepts
    one without complaint and hands back a Python str containing the lone
    surrogate. canonical_json()'s later `.encode("utf-8")` call then raises
    an uncaught UnicodeEncodeError for exactly that content -- a same-UID
    actor who plants one into a recovery manifest (or any other file this
    installer reads via strict_json() and later re-serializes) could use
    that to permanently wedge every command that reaches the re-serialize
    path. Rejecting here, at parse time, closes it with this tool's own
    error type instead of leaving it to surface however far downstream
    canonical_json() happens to be called.

    Checked via an actual encode attempt -- not a hand-rolled surrogate
    range scan -- so this rejects precisely the strings that would break
    canonical_json() and nothing else: it cannot introduce a new false
    positive against legitimate, fully-encodable Unicode content (a valid
    surrogate PAIR is already combined by json.loads() into the single
    supplementary code point it denotes, which encodes to UTF-8 fine and is
    never rejected here).
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PrimeInstallError(
                "invalid JSON: string contains an unpaired UTF-16 surrogate"
            ) from exc
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_unencodable_strings(key)
            _reject_unencodable_strings(item)
    elif isinstance(value, list):
        for item in value:
            _reject_unencodable_strings(item)


def strict_json(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise PrimeInstallError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    def reject_constant(token: str) -> Any:
        # json.loads() calls parse_constant() specifically (and only) for
        # the three non-RFC-8259 tokens it otherwise accepts by default:
        # NaN, Infinity, -Infinity. A same-UID actor planting one of these
        # into a file this installer reads via strict_json() used to parse
        # through silently even though this tool has no legitimate use for
        # a non-finite JSON number anywhere. Wiring parse_constant to raise
        # closes that at parse time, with this tool's own error type,
        # rather than letting a non-RFC-strict token flow through into
        # later logic that assumes ordinary JSON values.
        raise PrimeInstallError(f"invalid JSON: non-finite numeric token {token!r}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrimeInstallError("invalid JSON") from exc
    except RecursionError as exc:
        # Sufficiently deep nesting can make json.loads() itself raise
        # RecursionError rather than json.JSONDecodeError (confirmed
        # empirically). Left uncaught, this escaped strict_json() -- and,
        # before the main() widening elsewhere in this file, the whole CLI
        # -- as a raw Python traceback instead of this tool's own
        # {"ok": false, "error": ...} contract.
        raise PrimeInstallError("invalid JSON: nesting too deep") from exc
    try:
        _reject_unencodable_strings(value)
    except RecursionError as exc:
        # Walking the already-parsed structure recurses to the same depth
        # json.loads() itself just accepted; guard it the same way in case
        # that walk is what actually exhausts the recursion limit first.
        raise PrimeInstallError("invalid JSON: nesting too deep") from exc
    return value


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def volume_uuid() -> str:
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", os.fspath(SSD_ROOT)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=3,
        )
        payload = plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as exc:
        raise PrimeInstallError("cannot verify Extreme SSD") from exc
    value = payload.get("VolumeUUID") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        value,
    ):
        raise PrimeInstallError("Extreme SSD UUID unavailable")
    return value.upper()


def resolve_ssd(path: Path, *, must_exist: bool = True) -> Path:
    try:
        root = SSD_ROOT.resolve(strict=True)
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise PrimeInstallError(f"path unavailable: {path}") from exc
    if not is_relative_to(resolved, root):
        raise PrimeInstallError(f"path escaped Extreme SSD: {path}")
    if must_exist and resolved.stat().st_dev != root.stat().st_dev:
        raise PrimeInstallError(f"path is on the wrong device: {path}")
    return resolved


def ensure_private_dir(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise PrimeInstallError(f"unsafe private directory: {path}")
        resolved = resolve_ssd(path)
    else:
        parent = resolve_ssd(path.parent)
        try:
            path.mkdir(mode=0o700, exist_ok=False)
        except OSError as exc:
            raise PrimeInstallError(f"cannot create {path}") from exc
        resolved = resolve_ssd(path)
        if resolved.parent != parent:
            raise PrimeInstallError(f"directory escaped parent: {path}")
    # Persist the directory name in its parent. Repeating this for an existing
    # directory also closes a prior interrupted creation whose parent sync did
    # not complete.
    fsync_directory(path.parent)
    return resolved


def create_fresh_private_dir(path: Path) -> Path:
    """Create a brand-new private 0700 directory; never accept a pre-existing one.

    ensure_private_dir() deliberately accepts a pre-existing same-UID
    directory so genuinely resumable, shared locations (TOOL_ROOT, the npm
    cache, the install home/tmp scratch dirs) survive being called more
    than once. That same acceptance is unsafe for release-scoped,
    single-use extraction destinations whose names are fully deterministic
    from constants any same-UID process can read (the per-package
    ".unpacked-<name>" directories and the Node.js "toolchain" directory)
    and which this installer only ever creates once, from empty, within a
    single install run -- quarantine_partial_release() already relocates
    any leftover RELEASE_DIR before a fresh install begins, so a live
    install never legitimately finds one of these already present.
    Accepting a pre-existing directory there let a same-UID attacker plant
    it -- with a symlinked sub-component already inside -- at any point
    during the long, multi-download window that precedes extraction, and
    have it silently trusted: safe_extract_main_asset() and
    extract_node_toolchain() both resolve nested extraction targets purely
    by path, so a pre-planted symlink ancestor (e.g. ".unpacked-<name>/
    package" or "toolchain/bin" pointing outside SSD_ROOT) was followed
    for every write, chmod, and (for the Node toolchain) later
    subprocess execution underneath it -- independent review round 2,
    2026-08-18, P1 residual of round-1 P1-1: that fix hardened only
    make_patched_asset's final tarball publish, not the extraction steps
    that run earlier in the same install. Failing closed here -- refusing
    ANY pre-existing occupant, symlink or not -- converts that deterministic,
    long-window pre-plant into, at worst, a microsecond in-process creation
    race between this call and the extraction loop's own first nested
    mkdir, which is the same residual class already accepted for other
    verify-then-reuse gaps in this file (e.g. atomic_create_private_file's
    parent race).
    """
    parent = resolve_ssd(path.parent)
    try:
        path.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise PrimeInstallError(f"cannot create {path}") from exc
    resolved = resolve_ssd(path)
    if resolved.parent != parent:
        raise PrimeInstallError(f"directory escaped parent: {path}")
    fsync_directory(path.parent)
    return resolved


def assert_tree_has_no_symlinks(root: Path) -> None:
    """Fail closed if any entry under root is a symlink.

    Path.rglob() does not follow symlinked directories, but it does yield
    the symlink entry itself, so this still finds a symlink planted at any
    depth -- including one whose ancestor directory was created moments
    earlier by this same process, during the narrow in-process race window
    create_fresh_private_dir() cannot itself close. Called after extraction
    so a same-UID racer that wins that narrow window is detected and
    refused instead of silently trusted (independent review round 2,
    2026-08-18: extract_node_toolchain() previously returned success with
    node/npm paths that resolved outside SSD_ROOT through exactly such a
    symlink).
    """
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise PrimeInstallError(f"unexpected symlink in managed extraction tree: {entry}")


def verify_private_ssd_dir(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PrimeInstallError(f"private directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise PrimeInstallError(f"unsafe private directory: {path}")
    resolved = resolve_ssd(path)
    if resolved != path.absolute():
        raise PrimeInstallError(f"private directory path contains a symlink: {path}")
    return resolved


def validate_empty_private_dir(path: Path, label: str) -> Path:
    root = verify_private_ssd_dir(path)
    try:
        if any(path.iterdir()):
            raise PrimeInstallError(f"{label} is not empty")
    except OSError as exc:
        raise PrimeInstallError(f"cannot inspect {label}: {path}") from exc
    return root


def atomic_write(path: Path, raw: bytes, mode: int = 0o600) -> None:
    """Atomically publish `raw` at `path`, mode `mode`, REPLACING any
    existing occupant (see atomic_create_private_file() for the sibling
    create-only, never-clobber discipline used elsewhere in this file).

    Fix (independent Codex sol/max review round 48, reproduced end to end;
    closed round 49): os.replace(temp_path, path) atomically publishes the
    new content -- POSIX rename() always replaces the destination's own
    last path component and never follows a trailing symlink there, so
    that step alone was always sound. But the function used to then
    re-resolve `path` BY NAME a second, independent time for
    os.chmod(path, mode), with no verification afterward that `path` still
    identified the file just published. A same-UID attacker who won the
    race between os.replace() returning and os.chmod() running (or at any
    later point, since nothing here ever re-checked) could swap `path` to
    a symlink; os.chmod() follows symlinks by default, so it silently
    chmod'd whatever the symlink pointed at while this function still
    returned success. Codex's real, isolated reproduction did exactly
    that; an end-to-end reproduction against quarantine_partial_release()
    (one of this function's real callers) went further, returning
    state=quarantined -- meant to signal a safely durable terminal state
    -- while the actual on-disk manifest was an attacker-controlled
    symlink containing {"schema":"attacker"}.

    Closed the same way atomic_create_private_file() already closes the
    equivalent gap for its own publish path: the write descriptor's
    (st_dev, st_ino) identity is captured BEFORE publishing (while the fd
    is still open), and after os.replace() the function opens `path` again
    with O_NOFOLLOW -- so a symlink swap makes this open fail closed with
    ELOOP instead of following it -- then re-fstats that descriptor and
    refuses to proceed unless the identity still matches what was
    published. os.fchmod() is then applied to that freshly opened,
    identity-verified descriptor -- never os.chmod() by path again -- so
    the mode change itself is immune to any later path-based swap too.

    Content is also re-read from that same verified descriptor and
    compared against `raw` byte-for-byte. Identity alone already catches a
    symlink swap (the common case Codex reproduced); a same-UID attacker
    who instead raced a write into the exact published inode (e.g. a
    lingering open fd to it, opened before this function ever ran) rather
    than swapping the path would pass an identity check while still
    corrupting the published bytes, so this content re-read closes that
    timing too -- both are real, reproduced-end-to-end timings this
    function now demonstrably catches (see
    test_atomic_write_fails_closed_on_symlink_swap_after_replace and
    test_atomic_write_fails_closed_on_same_inode_content_corruption).

    What the content re-read does NOT and cannot do -- corrected here in
    round 51 after two independent reviews (Claude opus/max and Codex
    sol/max, both with real reproductions) found the previous wording
    overclaimed this -- is rule out a same-UID attacker corrupting the
    published bytes altogether. There is a genuine, structural TOCTOU
    between this content verification completing and the os.fchmod() call
    a few lines below: a persistent-write attacker who writes to the same
    already-open, already-identity-verified descriptor (or otherwise
    mutates the underlying inode) in that exact window still gets
    corrupted content published while this function reports success. This
    is not a coding defect fixable by adding yet another verification
    step -- the same attacker can just as well act one instant after
    THAT step too, all the way up to and including one instant after this
    function has already returned, at which point the file is, from the
    filesystem's perspective, exactly as legitimate a target for the same
    same-UID adversary as it always was. Every check-then-act pattern has
    this limit against a persistent-write same-UID adversary; it is not
    something re-reading, re-hashing, or re-checking more can structurally
    close. See validate_exec_target()'s and reassert_exec_target_identity()'s
    docstrings for this file's other examples of documenting an
    acknowledged residual of this same shape honestly rather than
    overclaiming a full close.

    Round 51 also narrows (without closing) a smaller, related gap the
    same reviews raised: unlike read_private_file(), which re-fstats after
    reading and compares (st_dev, st_ino, st_size, st_mtime_ns) against
    the pre-read stat, the content re-read above used to only compare the
    bytes read against `raw` and the size seen at open time -- it never
    re-confirmed, AFTER finishing the read, that the file had not changed
    again in the meantime. A same-UID attacker APPENDING bytes past what
    the bounded read loop consumes (that loop stops the instant it has
    read len(raw) bytes) would go undetected by content/size comparisons
    alone. A second os.fstat(), taken immediately after the read loop and
    compared against the fstat taken right after opening, now catches
    that case too -- cheap, and it shrinks the window a little further --
    but it is still bounded by the exact same structural limit described
    above: it cannot see a write that lands after ITS OWN check completes
    either, including the same fchmod-timing gap.

    Unlike atomic_create_private_file(), this function does not attempt
    rollback on a failed post-publication check: its callers use it to
    REPLACE an existing occupant (recovery-manifest status transitions,
    package.json rewrites), not to create a new file, so there is no prior
    occupant left to restore -- the temp file that held it is already gone
    once os.replace() has run. Callers instead get a clear
    PrimeInstallError and must decide their own recovery; see
    quarantine_partial_release(), which already treats a failed
    status-transition write as a hard failure with no durable claim of
    success, and its own nested rollback-intent write, which already has
    its own explicit failure handling for exactly this case.
    """
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    published_identity: tuple[int, int] | None = None
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            info = os.fstat(handle.fileno())
            published_identity = (info.st_dev, info.st_ino)
        os.replace(temp_path, path)
        try:
            # Round 51 fix (independent review, P2-1 -- a real DoS
            # regression introduced by the O_NOFOLLOW reopen above):
            # lstat `path` and confirm it is still a regular file BEFORE
            # ever calling open() on it, exactly like read_private_file()
            # already does for its own analogous open. Without this, a
            # same-UID racer who swaps `path` for a FIFO (named pipe) in
            # this exact window makes the O_NOFOLLOW open() below BLOCK
            # INDEFINITELY -- confirmed empirically: opening a FIFO for
            # O_RDONLY blocks until some other process opens it for
            # writing, and this function's caller is still holding
            # exclusive_lifecycle_lock the whole time, wedging every
            # lifecycle operation, including recover() itself (SIGINT-
            # recoverable for an interactive human, but a real
            # denial-of-service surface for a non-interactive/automated
            # run). A FIFO -- or a socket, device, or directory -- could
            # never legitimately be the file atomic_write() just
            # published: os.replace() only ever lands a regular file
            # created by tempfile.mkstemp(). Refusing immediately, before
            # open(), is therefore not a weakening of the identity/content
            # guarantees below; it turns an indefinite hang into the same
            # fast, clean PrimeInstallError every other verification
            # failure in this function already produces.
            swap_check = os.lstat(path)
            if not stat.S_ISREG(swap_check.st_mode):
                raise PrimeInstallError(
                    f"managed file replaced with a non-regular file during publication: {path}"
                )
            # O_NOFOLLOW here is load-bearing, not defense in depth: if a
            # same-UID racer has swapped `path` to a symlink since
            # os.replace() returned, this open() must fail closed (ELOOP)
            # instead of transparently following it the way a plain
            # os.chmod(path, mode) call would.
            verified_descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                after = os.fstat(verified_descriptor)
                if (
                    published_identity is None
                    or (after.st_dev, after.st_ino) != published_identity
                ):
                    raise PrimeInstallError(
                        f"managed file identity changed during publication: {path}"
                    )
                if after.st_size != len(raw):
                    raise PrimeInstallError(
                        f"managed file size changed during publication: {path}"
                    )
                chunks: list[bytes] = []
                remaining = len(raw)
                while remaining:
                    chunk = os.read(verified_descriptor, min(65_536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if b"".join(chunks) != raw:
                    raise PrimeInstallError(
                        f"managed file content changed during publication: {path}"
                    )
                # Round 51 fix (independent review, related gap): re-fstat
                # immediately after finishing the read and compare against
                # the fstat taken right after opening -- mirrors
                # read_private_file()'s own post-read (st_dev, st_ino,
                # st_size, st_mtime_ns) comparison. Without this, a
                # same-UID attacker APPENDING bytes past what the bounded
                # read loop above consumes went undetected: that loop
                # stops the instant it has read len(raw) bytes and never
                # notices the file grew longer in the meantime. This
                # narrows the window further but does not close it -- see
                # this function's docstring for why no check-then-act step
                # here ever fully can.
                post_read = os.fstat(verified_descriptor)
                if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                    post_read.st_dev,
                    post_read.st_ino,
                    post_read.st_size,
                    post_read.st_mtime_ns,
                ):
                    raise PrimeInstallError(
                        f"managed file changed while re-reading during publication: {path}"
                    )
                # fchmod() on this already-open, already identity-verified
                # descriptor, NEVER os.chmod(path, mode) by name again --
                # this is the actual fix: the mode change itself can no
                # longer be redirected by a later path-based swap either.
                os.fchmod(verified_descriptor, mode)
            finally:
                os.close(verified_descriptor)
        except OSError as exc:
            raise PrimeInstallError(
                f"managed file could not be verified after publication: {path}"
            ) from exc
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


def read_private_file(path: Path, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
            or before.st_size > max_bytes
        ):
            raise PrimeInstallError(f"unsafe private file: {path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise PrimeInstallError(f"private file identity changed: {path}")
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
        raise PrimeInstallError(f"cannot read private file: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(raw) != after.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise PrimeInstallError(f"private file changed while reading: {path}")
    return raw


def read_private_ssd_file(path: Path, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    if path.is_symlink():
        raise PrimeInstallError(f"managed private file must not be a symlink: {path}")
    return read_private_file(resolve_ssd(path), max_bytes=max_bytes)


def open_verified_generated_file_parent(
    path: Path, *, expected_identity: tuple[int, int] | None = None
) -> int:
    """Return an open, verified directory descriptor for `path`'s immediate
    parent, resolved via the SAME dir_fd-chained, openat()-style ancestor
    walk open_verified_ancestor_chain() already uses for the managed
    command link (see ensure_local_link_parent() /
    verify_link_parent_descriptor()), except rooted at SSD_ROOT instead of
    USER_HOME: every component of `path`'s parent, all the way from
    SSD_ROOT down (e.g. TOOL_ROOT, "releases", "v<VERSION>"), is opened
    strictly relative to the previously opened and verified descriptor,
    never re-resolved by path string, so a same-UID racer who swaps ANY
    ancestor directory -- not only `path` itself -- at any point during or
    after the walk cannot redirect the descriptor this returns.

    Used by tighten_generated_private_file_mode() to close a gap
    Path.lstat()-then-os.open(path, O_NOFOLLOW) cannot: Path.lstat()
    follows every INTERMEDIATE symlink in a path, and O_NOFOLLOW on a
    plain os.open() only refuses the FINAL component being a symlink --
    neither protects an ancestor directory. A same-UID racer who swaps
    RELEASE_DIR itself (or any ancestor above it) for a symlink to a
    sibling directory, at any point during the long `npm install
    --package-lock-only` subprocess window that runs before
    tighten_generated_private_file_mode() is called, made the un-chained
    implementation chmod an unrelated file inside that sibling directory to
    0o600 -- reproduced deterministically by swapping RELEASE_DIR for a
    symlink to a sibling directory containing an unrelated
    package-lock.json (independent Codex sol/max round-19 review,
    2026-08-19, P1; round-19's Claude opus/max review tested only leaf-path
    symlink substitution and lstat-to-open inode swaps at the leaf, not
    this ancestor-directory-level gap).

    `expected_identity`, when given, is forwarded unchanged to
    open_verified_ancestor_chain(), which compares it against the returned
    descriptor's own (st_dev, st_ino) before returning it -- closing the
    NEXT gap round 19's symlink-only fix left open: a same-UID rename-swap
    that replaces `path`'s parent with a different, real, legitimately-
    owned, correctly-permissioned directory (not a symlink) passes every
    per-component check this walk performs, since those checks validate
    properties in the moment, not identity continuity with whatever this
    installer itself created before npm ever ran (independent Codex
    sol/max round-21 review, 2026-08-19, P1; see
    open_verified_ancestor_chain()'s own docstring for the full
    reasoning).
    """
    return open_verified_ancestor_chain(
        path, create_missing=False, root=SSD_ROOT, expected_identity=expected_identity
    )


def tighten_generated_private_file_mode(
    path: Path,
    mode: int = 0o600,
    *,
    expected_parent_identity: tuple[int, int],
    parent_dir_fd: int | None = None,
) -> tuple[int, int]:
    """Tighten an existing file's permission bits to `mode` in place, right
    after an external process this installer does not control (`npm`) has
    just generated it at whatever mode that process's own ambient umask
    happened to produce, and return the (st_dev, st_ino) identity it was
    tightened at.

    `expected_parent_identity` is required, not optional with a
    None-means-skip default: this is the ONE call site that mutates a
    file's mode through an ancestor directory resolved fresh, by name,
    strictly AFTER a long external `npm` subprocess this installer does not
    control has already run -- a caller that forgot to pass it would
    silently regress to round 21's gap (a same-UID rename-swap of
    RELEASE_DIR itself, using a different, real, legitimately-owned,
    correctly-permissioned directory, passing every per-component check
    open_verified_ancestor_chain() performs). Callers must pass the
    (st_dev, st_ino) identity captured for `path`'s intended parent BEFORE
    that subprocess ran -- for _install_locked(), that is RELEASE_DIR's own
    identity, captured immediately after ensure_private_dir(RELEASE_DIR)
    creates it and before either `npm` invocation that follows.

    Every OTHER private SSD state file this installer itself writes is
    published already at the intended private mode by atomic_write()/
    atomic_create_private_file(), which fchmod() the open descriptor before
    any content is ever visible at the final path -- so there is never a
    window where such a file exists on disk at a permissive mode. This
    installer cannot apply that same discipline to package-lock.json: it is
    not written by this installer at all, but generated in place by `npm
    install --package-lock-only` (that is the entire point of that step),
    so the mode can only be fixed strictly AFTER npm has already created it.

    A real `npm install --package-lock-only` writes package-lock.json at
    whatever mode the invoking process's ambient umask allows -- empirically
    0o644 under the common umask 0o022 -- npm has no notion of this
    installer's private-file discipline. Left untightened, the very next
    read of the file, verify_unchanged_private_ssd_file() (which reads it
    back via read_private_file()'s `st_mode & 0o077 == 0` requirement),
    fails closed on every real, non-mocked install (round 18, 2026-08-19,
    P1: the first real, non-mocked exercise of this npm step surfaced this;
    all 96 prior unit tests mocked lock-file generation in a way that
    bypassed real npm's actual default-umask output mode). package.json
    never hits this gap because atomic_create_private_file() publishes it
    at 0o600 before npm ever touches it. _install_locked() also now spawns
    that `npm install --package-lock-only` subprocess with an explicit
    child umask of 0o077 (see run_npm()'s `child_umask` parameter), so a
    real npm normally creates the file already private -- but that is
    defense in depth only (npm has no obligation to honor, or could ignore
    in some environment, an inherited umask), not a substitute for
    tightening the mode explicitly right here.

    Every step below -- the initial identity capture, the final
    O_NOFOLLOW-protected open, and the fchmod -- is bound to a single
    dir_fd obtained from open_verified_generated_file_parent()'s
    SSD_ROOT-rooted ancestor walk, rather than to any lexical path: a bare
    chmod-by-path (or a plain Path.lstat()-then-os.open(path, O_NOFOLLOW),
    this function's own previous implementation) follows every
    intermediate ancestor component by name, so a same-UID actor who
    swapped RELEASE_DIR -- or any ancestor above it -- for a symlink at any
    point during npm's long run would have made this call silently tighten
    (or fabricate a false sense of privacy for) some unrelated file in an
    attacker-chosen directory instead of failing closed on the
    substitution (independent Codex sol/max round-19 review, 2026-08-19,
    P1; see open_verified_generated_file_parent()). This is the same
    failure mode every other private-file operation in this file already
    refuses to allow, just applied one level higher: the leaf-symlink case
    (this path itself swapped for a symlink) was already refused by the
    O_NOFOLLOW-open-then-fstat-identity-check discipline read_private_file()
    established; only the ancestor-directory case was missed.

    round 19's fix still only refused a SYMLINKED ancestor, though: every
    per-component check open_verified_directory_component() performs
    (real directory, not a symlink, owned by this UID, safe mode)
    validates properties in the moment, not identity continuity with
    whatever RELEASE_DIR actually was before npm ran -- so a same-UID
    rename-swap that replaces RELEASE_DIR with a DIFFERENT, real,
    legitimately-owned, correctly-permissioned directory (not a symlink)
    passed every one of those checks and reached the fchmod below on
    whatever unrelated file happened to sit at the expected name inside
    it. `expected_parent_identity`, forwarded to
    open_verified_generated_file_parent() and compared there against the
    resolved parent's own (st_dev, st_ino) before its descriptor is ever
    returned, closes that gap: the replacement directory can only pass
    that comparison by actually BEING the one RELEASE_DIR identity was
    captured from (independent Codex sol/max round-21 review, 2026-08-19,
    P1; round 21's own Claude opus/max review tested only symlink swaps
    and swaps at other points in the walk, not a same-UID rename-swap to
    another real, well-formed directory).

    `parent_dir_fd`, when given, is a caller-held, already-open,
    already-verified directory descriptor for `path`'s parent (e.g.
    _install_locked()'s own release_dir_fd, opened on RELEASE_DIR
    immediately after creating it and held open for the entire install) --
    used directly via os.dup() instead of re-deriving a parent descriptor
    through open_verified_generated_file_parent()'s SSD_ROOT-rooted
    ancestor walk. This is strictly stronger than even the identity-pinned
    walk: the walk re-resolves each ancestor component by NAME and only
    then compares the terminal descriptor's inode against
    `expected_parent_identity`, so it can only detect a swap that has
    already happened by the time the walk runs; a caller-held descriptor
    was never re-resolved by name at all after the moment it was first
    opened, so there is no walk-time window left to narrow (independent
    Codex sol/max round-23 review, 2026-08-19, P1-1/P1-2; see
    assert_release_dir_fd_identity()). Defaults to None so every existing
    caller that only has a captured (st_dev, st_ino) VALUE for `path`'s
    parent -- not a live descriptor -- keeps using the ancestor-walk path
    unchanged.
    """
    if parent_dir_fd is not None:
        try:
            held = os.fstat(parent_dir_fd)
        except OSError as exc:
            raise PrimeInstallError(
                f"managed release directory descriptor is invalid: {path}"
            ) from exc
        if (held.st_dev, held.st_ino) != expected_parent_identity:
            raise PrimeInstallError(
                f"managed release directory identity changed before tightening: {path}"
            )
        try:
            parent_descriptor = os.dup(parent_dir_fd)
        except OSError as exc:
            raise PrimeInstallError(
                f"cannot duplicate managed release directory descriptor: {path}"
            ) from exc
    else:
        parent_descriptor = open_verified_generated_file_parent(
            path, expected_identity=expected_parent_identity
        )
    try:
        name = path.name
        try:
            before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise PrimeInstallError(f"cannot inspect generated file before tightening: {path}") from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
        ):
            raise PrimeInstallError(f"unsafe generated file: {path}")
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise PrimeInstallError(f"generated file identity changed before tightening: {path}")
            os.fchmod(descriptor, mode)
        except OSError as exc:
            raise PrimeInstallError(f"cannot tighten generated file mode: {path}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return (before.st_dev, before.st_ino)
    finally:
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def assert_release_dir_identity(expected_identity: tuple[int, int]) -> None:
    """Re-assert that RELEASE_DIR still resolves, right now, to the same
    (st_dev, st_ino) identity _install_locked() captured immediately after
    creating it, strictly before either external `npm` subprocess it does
    not control has ever run.

    tighten_generated_private_file_mode() (via
    open_verified_generated_file_parent()) already re-asserts this SAME
    captured identity immediately after `npm install --package-lock-only`
    -- the first of the two long subprocess windows -- but nothing
    previously re-asserted it after the SECOND: `npm ci`, which actually
    materializes node_modules, is immediately followed by
    _install_locked() moving node_modules to lib/node_modules, chmod'ing
    the entrypoint, and writing the launch guard and command wrapper
    scripts -- all of it resolving RELEASE_DIR by plain lexical path, with
    no identity check at all, right after that window closes. A same-UID
    racer who renames RELEASE_DIR aside (taking npm's real, just-installed
    output with it) and renames a different, legitimately-owned,
    correctly-permissioned real directory into its place at any point
    during `npm ci`'s run would otherwise have every one of those steps --
    including the two atomic_create_private_file() calls that publish the
    launch guard and the command wrapper npm's *own* future symlinked
    invocation permanently trusts -- silently operate on that replacement
    instead (independent Codex sol/max round-21 review, 2026-08-19, P1
    residual: round 21's own report was specific to
    tighten_generated_private_file_mode(), but the SAME root cause --
    trusting RELEASE_DIR's identity across an external subprocess window
    without pinning it -- reaches this second, unreviewed call site too).

    Deliberately the SAME plain path.lstat()-based idiom
    assert_lifecycle_lock_path_identity() already established for
    re-asserting a captured identity hasn't drifted (as opposed to the
    heavier dir_fd-chained walk open_verified_ancestor_chain() uses to
    resolve a descriptor it is about to MUTATE through): comparing the
    terminal (st_dev, st_ino) is sufficient here regardless of which
    ancestor was swapped, or whether the swap used a symlink or a rename,
    because the resulting directory can only share the original's inode by
    actually being it.
    """
    try:
        current = RELEASE_DIR.lstat()
    except OSError as exc:
        raise PrimeInstallError(
            "managed Prime Agent release directory became unavailable during install"
        ) from exc
    if (current.st_dev, current.st_ino) != expected_identity:
        raise PrimeInstallError(
            "managed Prime Agent release directory identity changed during "
            "install; a same-UID actor may have replaced it with a "
            "different directory"
        )


def assert_release_dir_fd_identity(release_dir_fd: int) -> None:
    """Re-assert, right now, that the lexical RELEASE_DIR path still
    resolves to the EXACT directory object `release_dir_fd` was opened on.

    assert_release_dir_identity() (above) already does this by comparing
    two plain (st_dev, st_ino) VALUE tuples -- one captured at open time,
    one re-read now. That is sufficient to detect a same-UID swap, but it
    is still a comparison between two independently-resolved snapshots:
    nothing stops the ORIGINAL directory from being deleted (dropping its
    link count to zero) and, in a sufficiently long-lived, sufficiently
    busy filesystem, a brand-new object eventually reusing the exact same
    (st_dev, st_ino) pair the kernel already recycled. This function
    instead holds the actual open file descriptor `release_dir_fd` --
    obtained via os.open(RELEASE_DIR, O_DIRECTORY | O_NOFOLLOW) at the
    moment _install_locked() itself created RELEASE_DIR, and kept open for
    the remainder of the install -- and compares the CURRENT lexical
    path's identity against os.fstat() of that live descriptor. As long as
    the descriptor stays open, the kernel guarantees the underlying inode
    it refers to cannot be reused for anything else, so this is not merely
    "the same value happened to be observed twice" but "this is
    provably the same directory object, continuously, for the entire
    install" -- the architectural fix independent Codex sol/max round-23
    review, 2026-08-19, recommended over repeatedly re-deriving and
    re-comparing point-in-time (st_dev, st_ino) snapshots: "Opening an
    O_DIRECTORY|O_NOFOLLOW fd at creation, fstat()-ing it once, and doing
    all release-relative work through dir_fd= would close all of these at
    once -- the machinery already exists in this file."

    Threaded through run_npm() (bracketing each of the two `npm`
    subprocess invocations _install_locked() makes -- immediately before
    AND immediately after each one) and called directly at every other
    point _install_locked() currently calls assert_release_dir_identity(),
    as an ADDITIONAL, strictly stronger check layered on top of -- not a
    replacement for -- the existing value-tuple comparison, so every
    existing caller and test of assert_release_dir_identity() keeps
    working unchanged.

    This still cannot detect a same-UID racer swapping the CONTENT of a
    file reachable through RELEASE_DIR without ever touching RELEASE_DIR's
    own directory entry (e.g. overwriting toolchain/bin/node or
    node_modules/prime-agent/dist/bundle/cli.js in place) -- that is a
    different residual this round closes separately, via per-file content
    digests captured at the earliest trustworthy moment and re-verified
    immediately before each subsequent use (see extract_node_toolchain()'s
    node_sha256/npm_cli_sha256 and capture_private_ssd_asset_digest(), used
    together with verify_unchanged_private_ssd_asset_digest()).
    """
    try:
        held = os.fstat(release_dir_fd)
    except OSError as exc:
        raise PrimeInstallError(
            "managed Prime Agent release directory descriptor became invalid during install"
        ) from exc
    try:
        current = RELEASE_DIR.lstat()
    except OSError as exc:
        raise PrimeInstallError(
            "managed Prime Agent release directory became unavailable during install"
        ) from exc
    if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
        raise PrimeInstallError(
            "managed Prime Agent release directory identity changed during "
            "install; a same-UID actor may have replaced it with a "
            "different directory"
        )


def capture_private_ssd_asset_digest(path: Path) -> tuple[str, tuple[int, int]]:
    """Capture a private SSD file's current content digest and (st_dev,
    st_ino) identity, through the SAME O_NOFOLLOW-open-then-fstat-identity-
    check discipline read_private_file() already applies to every other
    private-file read in this file (via read_private_ssd_file()), rather
    than a bare Path.read_bytes()/sha256_file() pair -- so the digest this
    returns is tied to one single, atomic, identity-verified read of the
    exact file this installer is about to trust as a future baseline, not
    two independent, independently-raceable filesystem accesses.

    Used to anchor a generated file's content to the EARLIEST point its
    final, trustworthy value can be known -- e.g. immediately after an
    external `npm ci` subprocess this installer does not control returns,
    strictly before any further installer-side operation (moving
    node_modules, chmod'ing the entrypoint, generating the launch guard)
    gives a same-UID racer more time to swap it in place. Downstream re-
    checks compare against the (digest, identity) pair this returns via
    verify_unchanged_private_ssd_asset_digest() (independent Codex sol/max
    round-23 review, 2026-08-19, P1-2: RELEASE_DIR's own identity check
    proves the DIRECTORY was not swapped, but says nothing about a same-UID
    racer overwriting node_modules/prime-agent/dist/bundle/cli.js's CONTENT
    in place between npm ci returning and this installer publishing it --
    tree_digest(), the only other content check in this file's path,
    RECORDS whatever is on disk at its own call time rather than COMPARING
    against a value captured before the vulnerable window, so it silently
    treats a swap that happened before its first call as the legitimate
    baseline).
    """
    raw = read_private_ssd_file(path, max_bytes=MAX_TAR_EXPANDED_BYTES)
    try:
        after = path.lstat()
    except OSError as exc:
        raise PrimeInstallError(f"cannot inspect generated asset: {path}") from exc
    return sha256_bytes(raw), (after.st_dev, after.st_ino)


def verify_unchanged_private_ssd_file(
    path: Path, expected_raw: bytes, expected_identity: tuple[int, int]
) -> None:
    """Re-verify, right now, that a private SSD file at `path` still has
    exactly the bytes and (st_dev, st_ino) inode identity captured at an
    earlier verification point -- immediately before a subsequent consumer
    this installer does not control (a spawned `npm` subprocess that
    re-reads the path from disk on its own) is allowed to use it.

    Closes the verify-then-use gap between an earlier read/hash/validate of
    a generated file (RELEASE_DIR/package.json, RELEASE_DIR/package-lock.json)
    and a later, independent re-read of the same path by that external
    process: without this, a same-UID actor has the whole window between
    those two reads to replace the file with different-but-still-parseable
    content, and it would be trusted silently because only the FIRST read
    was ever verified (independent review round 5, 2026-08-18, P2-3).
    Follows the same identity-plus-content re-check idiom
    remove_private_file_durable() already uses for its own verify-then-use
    window before quarantining a file.
    """
    read_limit = max(4 * 1024 * 1024, len(expected_raw))
    try:
        before = path.lstat()
    except OSError as exc:
        raise PrimeInstallError(f"cannot inspect managed file before use: {path}") from exc
    if (before.st_dev, before.st_ino) != expected_identity:
        raise PrimeInstallError(f"managed file identity changed before use: {path}")
    if read_private_ssd_file(path, max_bytes=read_limit) != expected_raw:
        raise PrimeInstallError(f"managed file changed before use: {path}")


def verify_unchanged_private_ssd_asset_digest(
    path: Path,
    expected_sha256: str,
    expected_identity: tuple[int, int],
    *,
    max_bytes: int = MAX_TAR_EXPANDED_BYTES,
) -> None:
    """Digest-based counterpart of verify_unchanged_private_ssd_file(), for
    managed assets too large to justify keeping a byte-exact in-memory copy
    alive for the entire remainder of an install merely to re-compare
    against later.

    Re-checks the well-known path's (st_dev, st_ino) identity AND
    recomputes its SHA-256 content digest RIGHT NOW, comparing both against
    values captured at an earlier verification point -- immediately before
    a subsequent consumer this installer does not control (a spawned `npm`
    subprocess that independently re-reads the path from disk on its own)
    is allowed to use it. This closes the exact same verify-then-use gap
    verify_unchanged_private_ssd_file() closes for package.json/
    package-lock.json, applied here to the locally patched tarballs
    make_patched_asset() produces: those assets can be materially larger
    than the few KB those two generated files are, so this compares a
    digest captured once at creation time instead of retaining the full
    tarball bytes in memory across the entire two-`npm`-invocation window.

    Independent review round 16, 2026-08-18, P1: make_patched_asset()'s
    output was the only downloaded-or-generated release asset this
    installer ever fed to npm without ANY re-verification between
    publication and consumption -- round 14 closed this identical gap for
    the two DOWNLOADED release assets (safe_extract_main_asset(),
    extract_node_toolchain()) via the same expected_sha256 +
    read_private_file() pattern this function applies to the locally BUILT
    patched tarball. See verify_patched_assets_unchanged() for the two call
    sites in _install_locked() (immediately before `npm install
    --package-lock-only` and immediately before `npm ci`).
    """
    try:
        before = path.lstat()
    except OSError as exc:
        raise PrimeInstallError(f"cannot inspect managed asset before use: {path}") from exc
    if (before.st_dev, before.st_ino) != expected_identity:
        raise PrimeInstallError(f"managed asset identity changed before use: {path}")
    raw = read_private_ssd_file(path, max_bytes=max_bytes)
    if sha256_bytes(raw) != expected_sha256:
        raise PrimeInstallError(f"managed asset changed before use: {path}")


def verify_patched_assets_unchanged(
    assets_dir: Path,
    patched_assets: dict[str, str],
    patched_asset_identities: dict[str, tuple[int, int]],
) -> None:
    """Re-verify every locally patched tarball make_patched_asset() produced
    is still exactly the content and inode identity captured right after
    its own publication, immediately before each of the two npm
    invocations in _install_locked() that independently re-read these
    paths from RELEASE_DIR/assets on their own (`npm install
    --package-lock-only`, which generates the lock, and `npm ci`, which
    actually installs from it). Without this, a same-UID actor who swaps
    one of these files after make_patched_asset() writes it but before
    either npm invocation consumes it gets their content silently used --
    the closure-hash check on the GENERATED lock cannot see the
    difference on its own, since it validates what npm reports having
    read, not what is on disk right now (independent review round 16,
    2026-08-18, P1; see validate_generated_lock()'s own content-digest
    check, added the same round, for the complementary fix that also binds
    the closure validation itself to the real on-disk content).
    """
    for name, digest in patched_assets.items():
        verify_unchanged_private_ssd_asset_digest(
            assets_dir / name, digest, patched_asset_identities[name]
        )


def rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename on Darwin while refusing to replace the destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = libc.renamex_np
    renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    renamex_np.restype = ctypes.c_int
    result = renamex_np(
        os.fsencode(source), os.fsencode(destination), ctypes.c_uint(RENAME_EXCL)
    )
    if result != 0:
        observed_errno = ctypes.get_errno()
        raise OSError(
            observed_errno,
            os.strerror(observed_errno),
            os.fspath(destination),
        )


def rename_noreplace_dir_fd(
    src_dir_fd: int,
    src_name: str,
    dst_dir_fd: int,
    dst_name: str,
    *,
    display_destination: Path,
) -> None:
    """dir_fd-relative counterpart of rename_noreplace(): atomically rename
    while refusing to replace an existing destination, resolving both
    endpoints relative to already-open, already-verified directory
    descriptors instead of by lexical path.

    Uses Darwin's renameatx_np(2) -- the openat()-style counterpart of the
    renamex_np(2) call rename_noreplace() already uses for every other
    no-clobber quarantine/publish rename in this file -- so a dir_fd-bound
    rename keeps the exact same no-clobber guarantee as every lexical-path
    quarantine/restore rename elsewhere, rather than silently downgrading to
    plain os.rename()'s replace-if-present semantics merely because the
    operation is now dir_fd-bound. (Plain os.rename() with src_dir_fd/
    dst_dir_fd IS supported on this platform and would be simpler, but it
    does not refuse to replace an existing destination -- unlike
    os.replace(), which is not usable here at all because it does not accept
    dir_fd on macOS. Silently trading away the no-clobber guarantee this
    file relies on elsewhere for quarantine/restore renames -- particularly
    the restore-on-failure direction, where the destination is the
    well-known original link name a legitimate racer could have
    re-occupied -- would be a regression, not a simplification.) Symbol
    presence is verified via ctypes on this exact Darwin/arm64 build; see
    remove_exact_symlink(), independent review round 3/4, 2026-08-18, P1
    residual of P1-2.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        ctypes.c_int(src_dir_fd),
        os.fsencode(src_name),
        ctypes.c_int(dst_dir_fd),
        os.fsencode(dst_name),
        ctypes.c_uint(RENAME_EXCL),
    )
    if result != 0:
        observed_errno = ctypes.get_errno()
        raise OSError(
            observed_errno,
            os.strerror(observed_errno),
            os.fspath(display_destination),
        )


def unique_quarantine_path(path: Path, operation: str) -> Path:
    for _ in range(16):
        candidate = path.with_name(
            f".{path.name}.{operation}-{os.getpid()}-{secrets.token_hex(8)}"
        )
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise PrimeInstallError(f"cannot allocate private quarantine name for {path}")


def restore_quarantined_path(quarantine: Path, original: Path) -> None:
    try:
        rename_noreplace(quarantine, original)
        fsync_directory(original.parent)
    except OSError as exc:
        raise PrimeInstallError(
            f"managed path changed during removal; occupant preserved at {quarantine}"
        ) from exc


def remove_private_file_durable(
    path: Path,
    expected_raw: bytes,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    read_limit = max(4 * 1024 * 1024, len(expected_raw))
    try:
        before = path.lstat()
    except OSError as exc:
        raise PrimeInstallError(f"cannot inspect managed file: {path}") from exc
    if expected_identity is not None and (
        before.st_dev,
        before.st_ino,
    ) != expected_identity:
        raise PrimeInstallError(f"managed file identity changed before removal: {path}")
    if read_private_ssd_file(path, max_bytes=read_limit) != expected_raw:
        raise PrimeInstallError(f"managed file changed before removal: {path}")
    quarantine = unique_quarantine_path(path, "remove")
    try:
        rename_noreplace(path, quarantine)
    except OSError as exc:
        raise PrimeInstallError(f"cannot remove managed file: {path}") from exc
    try:
        moved = quarantine.lstat()
        if (
            (moved.st_dev, moved.st_ino) != (before.st_dev, before.st_ino)
            or read_private_ssd_file(quarantine, max_bytes=read_limit) != expected_raw
        ):
            restore_quarantined_path(quarantine, path)
            raise PrimeInstallError(f"managed file identity changed during removal: {path}")
    except PrimeInstallError:
        raise
    except OSError as exc:
        raise PrimeInstallError(
            f"managed file removal is incomplete; inspect {quarantine}"
        ) from exc
    # rename_noreplace() above only proves the ORIGINAL lexical location was
    # vacated at the moment of the rename -- it says nothing about what that
    # now-empty directory entry holds an instant later. Every check to this
    # point only inspects the quarantine copy; a benign race with another
    # managed step, or a hostile same-UID actor, can recreate a file at the
    # exact same path the instant it is vacated, and this function used to
    # return success anyway (independent Codex sol/xhigh review, 2026-08-16,
    # P1-3). Any presence at `path` now is wrong: the vacated original inode
    # and the still-quarantined copy are the only two legitimate holders of
    # this content, and neither one is reachable at `path` anymore, so this
    # check is unconditional -- raised regardless of whether the re-occupant
    # happens to share an identity with either value already known to be
    # gone from this path. finalize_pending_install() relies on this raising
    # instead of returning success so a detected re-occupation is treated as
    # a removal failure, not a completed removal.
    try:
        reoccupied = path.lstat()
    except FileNotFoundError:
        reoccupied = None
    except OSError as exc:
        raise PrimeInstallError(
            f"managed file removal is incomplete; inspect {quarantine}"
        ) from exc
    if reoccupied is not None:
        raise PrimeInstallError(
            "managed file was removed but the original path was re-occupied "
            f"during removal: {path} (re-occupant identity "
            f"{(reoccupied.st_dev, reoccupied.st_ino)}, prior identities "
            f"before={(before.st_dev, before.st_ino)} "
            f"moved={(moved.st_dev, moved.st_ino)}; quarantine copy retained "
            f"for inspection at {quarantine})"
        )
    try:
        quarantine.unlink()
    except OSError as exc:
        raise PrimeInstallError(
            f"managed file removal is incomplete; inspect {quarantine}"
        ) from exc
    try:
        fsync_directory(path.parent)
    except (OSError, PrimeInstallError) as exc:
        raise PrimeInstallError(
            f"managed file was removed but parent-directory durability is unconfirmed: {path}"
        ) from exc


def atomic_create_private_file(path: Path, raw: bytes, mode: int = 0o600) -> None:
    """Publish a new private SSD file without ever replacing an occupant."""
    verify_private_ssd_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    published = False
    published_identity: tuple[int, int] | None = None
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            info = os.fstat(handle.fileno())
            published_identity = (info.st_dev, info.st_ino)
        rename_noreplace(temp_path, path)
        published = True
        fsync_directory(path.parent)
        after = path.lstat()
        if (
            published_identity is None
            or (after.st_dev, after.st_ino) != published_identity
            or read_private_ssd_file(
                path, max_bytes=max(4 * 1024 * 1024, len(raw))
            )
            != raw
        ):
            raise PrimeInstallError(
                f"new managed file changed during publication: {path}"
            )
    except (OSError, PrimeInstallError) as exc:
        if published:
            try:
                current = path.lstat()
            except OSError as inspect_exc:
                raise PrimeInstallError(
                    f"new managed file publication failed and the path is absent: {path}"
                ) from inspect_exc
            if published_identity is None or (
                current.st_dev,
                current.st_ino,
            ) != published_identity:
                raise PrimeInstallError(
                    f"new managed file publication failed after the path changed; "
                    f"occupant preserved: {path}"
                ) from exc
            try:
                remove_private_file_durable(
                    path, raw, expected_identity=published_identity
                )
            except PrimeInstallError as cleanup_exc:
                raise PrimeInstallError(
                    f"new managed file publication failed and rollback is incomplete: {path}"
                ) from cleanup_exc
            raise PrimeInstallError(
                f"new managed file publication was rolled back: {path}"
            ) from exc
        if isinstance(exc, OSError) and exc.errno == errno.EEXIST:
            raise PrimeInstallError(
                f"refusing to replace existing managed file: {path}"
            ) from exc
        raise PrimeInstallError(f"cannot create managed file: {path}") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temp_path.unlink()
        except OSError:
            pass


def _restore_quarantined_symlink(
    parent_descriptor: int, quarantine: Path, original_name: str
) -> None:
    """dir_fd-bound counterpart of restore_quarantined_path(), used only by
    remove_exact_symlink()'s own rollback so restoring the quarantined
    symlink to its original name stays bound to the SAME verified ancestor
    descriptor as every other step of the removal, instead of re-resolving
    the original path by lexical path string. Uses
    rename_noreplace_dir_fd() (not plain os.rename()) because the
    destination here is the well-known original link name, which a
    legitimate concurrent actor could have re-occupied since the quarantine
    rename -- exclusivity is load-bearing for this direction, not merely
    defense in depth.
    """
    try:
        rename_noreplace_dir_fd(
            parent_descriptor,
            quarantine.name,
            parent_descriptor,
            original_name,
            display_destination=quarantine,
        )
        fsync_open_directory(parent_descriptor)
    except OSError as exc:
        raise PrimeInstallError(
            f"managed path changed during removal; occupant preserved at {quarantine}"
        ) from exc


def remove_exact_symlink(path: Path, expected_target: Path) -> None:
    """Quarantine-then-remove `path`, refusing unless it is exactly the
    expected managed symlink.

    Bound to the SAME dir_fd-chained, non-symlinked ancestor descriptor that
    creation (atomic_symlink() -> ensure_local_link_parent()) and
    verification (verify_link() -> verify_link_parent_descriptor()) already
    use, rather than resolving `path`'s parent by lexical path -- so an
    ancestor an attacker has since replaced with a symlink cannot redirect
    the lstat/readlink/rename/unlink calls this function performs while
    removing the public command link (independent review round 3/4,
    2026-08-18, P1 residual of P1-2: creation and verification were bound to
    open_verified_ancestor_chain() in earlier rounds, but removal -- called
    from _uninstall_locked() and _enable_locked()'s rollback, both on
    BIN_LINK -- still resolved path.lstat(), os.readlink(path), and
    rename_noreplace(path, quarantine) purely by lexical path, with no
    descriptor and no ancestor check).

    The quarantine and restore-on-failure renames use
    rename_noreplace_dir_fd() -- the dir_fd-relative counterpart of the
    renamex_np(2) call rename_noreplace() already uses for every other
    no-clobber quarantine/publish rename in this file -- so this keeps the
    identical no-clobber guarantee, not a weaker plain os.rename(), while
    resolving both rename endpoints relative to the already-verified,
    already-open parent descriptor instead of by lexical path.

    open_verified_ancestor_chain() already refuses to run at all when this
    platform cannot bind directory operations (including rename and unlink)
    to dir_fd (see LINK_DIR_FD_SUPPORTED / require_link_dir_fd_support()),
    so every operation below is always dir_fd-bound -- there is no
    remaining path-based fallback to narrow.
    """
    parent_descriptor = open_verified_ancestor_chain(path, create_missing=False)
    try:
        name = path.name
        try:
            before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            target_text = os.readlink(name, dir_fd=parent_descriptor)
        except OSError as exc:
            raise PrimeInstallError(f"cannot inspect managed symlink: {path}") from exc
        if not stat.S_ISLNK(before.st_mode) or target_text != os.fspath(expected_target):
            raise PrimeInstallError(f"managed symlink changed before removal: {path}")
        quarantine = unique_quarantine_path(path, "remove")
        try:
            rename_noreplace_dir_fd(
                parent_descriptor,
                name,
                parent_descriptor,
                quarantine.name,
                display_destination=quarantine,
            )
        except OSError as exc:
            raise PrimeInstallError(f"cannot remove managed symlink: {path}") from exc
        try:
            moved = os.stat(
                quarantine.name, dir_fd=parent_descriptor, follow_symlinks=False
            )
            moved_target = os.readlink(quarantine.name, dir_fd=parent_descriptor)
        except OSError as exc:
            _restore_quarantined_symlink(parent_descriptor, quarantine, name)
            raise PrimeInstallError(
                f"managed symlink identity changed during removal: {path}"
            ) from exc
        if (
            (moved.st_dev, moved.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISLNK(moved.st_mode)
            or moved_target != target_text
        ):
            _restore_quarantined_symlink(parent_descriptor, quarantine, name)
            raise PrimeInstallError(f"managed symlink identity changed during removal: {path}")
        try:
            os.unlink(quarantine.name, dir_fd=parent_descriptor)
        except OSError as exc:
            raise PrimeInstallError(
                f"managed symlink removal is incomplete; inspect {quarantine}"
            ) from exc
        try:
            fsync_open_directory(parent_descriptor)
        except (OSError, PrimeInstallError) as exc:
            raise PrimeInstallError(
                f"managed symlink was removed but parent-directory durability is unconfirmed: {path}"
            ) from exc
    finally:
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PrimeInstallError(f"cannot sync managed directory: {path}") from exc


def fsync_regular_file(path: Path) -> None:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o022
        ):
            raise PrimeInstallError(f"unsafe runtime file before sync: {path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise PrimeInstallError(f"runtime file identity changed before sync: {path}")
        os.fsync(descriptor)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise PrimeInstallError(f"cannot sync managed runtime file: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        stat.S_IMODE(after.st_mode),
    ):
        raise PrimeInstallError(f"runtime file changed while syncing: {path}")


def sync_private_tree(root: Path) -> None:
    """Persist a complete managed tree before any commit journal can exist."""
    resolved_root = verify_private_ssd_dir(root)
    directories = [root]
    for path in sorted(
        root.rglob("*"), key=lambda item: os.fspath(item.relative_to(root))
    ):
        relative = os.fspath(path.relative_to(root))
        try:
            info = path.lstat()
        except OSError as exc:
            raise PrimeInstallError(
                f"cannot inspect runtime entry before sync: {relative}"
            ) from exc
        if info.st_uid != os.getuid() or (
            not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022
        ):
            raise PrimeInstallError(
                f"unsafe runtime ownership or mode before sync: {relative}"
            )
        if stat.S_ISREG(info.st_mode):
            fsync_regular_file(path)
        elif stat.S_ISDIR(info.st_mode):
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise PrimeInstallError(
                    f"runtime directory is unavailable before sync: {relative}"
                ) from exc
            if not is_relative_to(resolved, resolved_root):
                raise PrimeInstallError(
                    f"runtime directory escaped tree before sync: {relative}"
                )
            directories.append(path)
        elif stat.S_ISLNK(info.st_mode):
            target_text = os.readlink(path)
            try:
                target = (path.parent / target_text).resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise PrimeInstallError(
                    f"broken runtime symlink before sync: {relative}"
                ) from exc
            if Path(target_text).is_absolute() or not is_relative_to(
                target, resolved_root
            ):
                raise PrimeInstallError(
                    f"runtime symlink escaped tree before sync: {relative}"
                )
        else:
            raise PrimeInstallError(
                f"unexpected runtime file type before sync: {relative}"
            )
    for directory in sorted(
        directories,
        key=lambda item: len(item.relative_to(root).parts),
        reverse=True,
    ):
        fsync_directory(directory)
    # Persist the root directory entry itself. This is required for trees such
    # as releases/vX, state, and probe-home that were created during install.
    fsync_directory(root.parent)


def lifecycle_lock_path() -> Path:
    return TOOL_ROOT / "lifecycle.lock"


def managed_session_dir() -> Path:
    return TOOL_ROOT / "sessions"


def validate_lifecycle_lock_descriptor(path: Path, descriptor: int) -> None:
    try:
        lexical = path.lstat()
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise PrimeInstallError("managed lifecycle lock is unavailable") from exc
    if (
        not stat.S_ISREG(lexical.st_mode)
        or stat.S_ISLNK(lexical.st_mode)
        or lexical.st_uid != os.getuid()
        or lexical.st_mode & 0o077
        or (lexical.st_dev, lexical.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise PrimeInstallError("managed lifecycle lock is unsafe")
    resolved = resolve_ssd(path)
    if resolved != path.absolute():
        raise PrimeInstallError("managed lifecycle lock path contains a symlink")


def verify_lifecycle_lock_file() -> Path:
    path = lifecycle_lock_path()
    descriptor = -1
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        validate_lifecycle_lock_descriptor(path, descriptor)
    except OSError as exc:
        raise PrimeInstallError("managed lifecycle lock is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def assert_lifecycle_lock_path_identity(
    path: Path, expected_identity: tuple[int, int]
) -> None:
    """Re-assert that `path` (the lifecycle lock's well-known location)
    still resolves, right now, to the same (st_dev, st_ino) identity
    captured when this process's lock fd was opened, validated, and
    flock()'d.

    flock(2) binds exclusivity to the OPEN FILE DESCRIPTION -- and
    therefore to the inode held open at acquire time -- not to the path.
    Nothing about continuing to hold an already-acquired fd detects a
    same-UID actor renaming a brand-new file over that path afterwards: the
    new file has no relationship to this process's flock at all, so a
    second, independent actor opening the (now different) file at the same
    path can acquire its own, equally "exclusive" flock on it while this
    process still believes the well-known path is exclusively its own. This
    process must therefore periodically re-assert, at points where a stale
    lock's silent failure would matter most (see exclusive_lifecycle_lock()'s
    acquisition, and the finalize_pending_install()/verify() re-checks
    threaded through from it), that the path still names the inode it
    originally acquired -- treating a mismatch as lock-integrity-violated
    and failing closed rather than letting the caller silently proceed as
    if exclusivity still held (independent review round 5, 2026-08-18,
    P2-2).
    """
    try:
        current = path.lstat()
    except OSError as exc:
        raise PrimeInstallError(
            "managed lifecycle lock became unavailable while held"
        ) from exc
    if (current.st_dev, current.st_ino) != expected_identity:
        raise PrimeInstallError(
            "managed lifecycle lock identity changed while held; a same-UID "
            "actor may have replaced the lock file, so this process's "
            "exclusivity over it is no longer guaranteed"
        )


@contextmanager
def exclusive_lifecycle_lock(*, create: bool) -> Any:
    if create:
        ensure_private_dir(TOOL_ROOT.parent)
        ensure_private_dir(TOOL_ROOT)
    else:
        verify_private_ssd_dir(TOOL_ROOT)
    path = lifecycle_lock_path()
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    descriptor = -1
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        validate_lifecycle_lock_descriptor(path, descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PrimeInstallError(
                "managed lifecycle is busy; command and state were left unchanged"
            ) from exc
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        # Close the (small, but real) race between the pre-flock identity
        # check inside validate_lifecycle_lock_descriptor() above and
        # flock() actually taking effect: re-assert, with the lock now
        # held, that `path` still names the exact inode this fd is bound
        # to, before any caller is allowed to treat the lock as acquired.
        assert_lifecycle_lock_path_identity(path, identity)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise PrimeInstallError(
                "managed lifecycle is busy; command and state were left unchanged"
            ) from exc
        raise PrimeInstallError("cannot acquire managed lifecycle lock") from exc
    except PrimeInstallError:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        raise
    if identity is None:
        # Unreachable: every path above that could leave `identity` unset
        # raises out of this function first. Checked explicitly (not via a
        # bare `assert`, which `python -O`/`PYTHONOPTIMIZE=1` strip
        # entirely) so this stays a real, always-enforced invariant rather
        # than one this file's own PYTHONOPTIMIZE-awareness would flag as a
        # future risk (see sandbox_e2e.py's __debug__ guard for the same
        # concern applied to that script).
        raise PrimeInstallError("cannot acquire managed lifecycle lock")
    try:
        yield path, identity
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(descriptor)
            except OSError:
                pass


def safe_download(
    url: str,
    destination: Path,
    expected_sha256: str,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> None:
    if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise PrimeInstallError("unsafe download declaration")
    request = urllib.request.Request(url, headers={"User-Agent": "Orca-Prime-Agent-Installer/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.geturl().split(":", 1)[0].lower() != "https":
                raise PrimeInstallError("download redirected away from HTTPS")
            raw = response.read(max_bytes + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise PrimeInstallError(f"download failed: {destination.name}") from exc
    if len(raw) > max_bytes or sha256_bytes(raw) != expected_sha256:
        raise PrimeInstallError(f"download digest mismatch: {destination.name}")
    atomic_create_private_file(destination, raw, 0o600)


def verify_orca_support() -> dict[str, Any]:
    shared = Path("/Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/shared")
    declarations = {
        "agent_config": (
            shared / "tui-agent-config.js",
            ("'prime-agent':", "detectCmd: 'prime-agent'", "launchCmd: 'prime-agent'"),
        ),
        "process_identity": (
            shared / "agent-node-entrypoint-identities.js",
            ("node_modules\\/prime-agent\\/dist\\/bundle\\/cli\\.js", "agent: 'prime-agent'"),
        ),
        "resume": (
            shared / "agent-session-resume.js",
            ("case 'prime-agent':", "['prime-agent', '--resume', providerSession.transcriptPath]"),
        ),
        "agent_dir": (
            shared / "pi-agent-kind.js",
            ("'prime-agent': 'PRIME_AGENT_CODING_AGENT_DIR'", "return 'prime-agent'"),
        ),
    }
    evidence: dict[str, Any] = {}
    for name, (path, required) in declarations.items():
        try:
            if path.is_symlink():
                raise OSError("support file is a symlink")
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PrimeInstallError(f"installed Orca {name} support is unavailable") from exc
        if not all(marker in text for marker in required):
            raise PrimeInstallError(f"installed Orca {name} support is incomplete")
        evidence[name] = {"path": os.fspath(path), "sha256": sha256_bytes(raw)}
    return evidence


def preflight() -> dict[str, Any]:
    resolve_ssd(LOCAL_HOMES)
    if not re.fullmatch(r"[0-9a-f]{64}", GENERATED_LOCK_SHA256):
        raise PrimeInstallError("normalized production lock is not pinned")
    if platform.system() != "Darwin" or platform.machine().lower() != "arm64":
        raise PrimeInstallError("this pinned installer supports only macOS arm64")
    expected_uuid = volume_uuid()
    support = verify_orca_support()
    return {
        "volume_uuid": expected_uuid,
        "node_version": NODE_VERSION,
        "npm_version": NPM_VERSION,
        "orca_support": support,
    }


def package_name_from_lock_path(lock_path: str, row: dict[str, Any]) -> str | None:
    name = row.get("name")
    if isinstance(name, str) and name:
        return name
    if "node_modules/" not in lock_path:
        return None
    suffix = lock_path.rsplit("node_modules/", 1)[1].strip("/")
    parts = suffix.split("/")
    if suffix.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0] if parts and parts[0] else None


def exact_dependency_versions(manifest: dict[str, Any], upstream_lock: dict[str, Any]) -> None:
    packages = upstream_lock.get("packages")
    if not isinstance(packages, dict):
        raise PrimeInstallError("upstream lock has no packages map")
    assets_dir = RELEASE_DIR / "assets"
    for section in ("dependencies", "optionalDependencies"):
        dependencies = manifest.get(section)
        if dependencies is None:
            continue
        if not isinstance(dependencies, dict):
            raise PrimeInstallError(f"invalid {section}")
        for name in list(dependencies):
            if name in WORKSPACE_ASSETS:
                dependencies[name] = "file:" + os.fspath(assets_dir / WORKSPACE_ASSETS[name])
                continue
            direct = packages.get(f"node_modules/{name}")
            version = direct.get("version") if isinstance(direct, dict) else None
            if not isinstance(version, str):
                raise PrimeInstallError(f"release lock does not pin a direct version for {name}")
            dependencies[name] = version


def safe_extract_main_asset(
    asset: Path, destination: Path, expected_sha256: str
) -> tuple[Path, tuple[Path, ...], dict[Path, str], dict[Path, int]]:
    # Round 13/14, 2026-08-18 (independent review, P1-B): safe_download()
    # verifies downloaded bytes IN MEMORY against `expected_sha256` and then
    # writes them to `asset` on disk; without a re-check here, this function
    # used to re-open `asset` from disk by PATH with no digest re-check at
    # all, leaving the entire window between that earlier verified write and
    # this later open -- which can span the rest of preflight, every other
    # safe_download() call, and Node/npm version probing -- open for a
    # same-UID actor to swap the on-disk file for different-but-still-valid
    # tarball content that would then extract and (via the patched asset's
    # eventual `npm ci`) execute without ever being caught. read_private_file()
    # closes this the same way it closes every other verify-then-use gap in
    # this file: ONE atomic open-by-path, read the whole content through that
    # single fd, and (via its own before/after inode+size+mtime check) fail
    # closed if the file changed during the read itself -- so there is no
    # second, independent filesystem access for an attacker to win a race
    # against. The freshly re-read, re-hashed bytes -- not a second
    # tarfile.open(asset, ...) by path -- are what tarfile actually parses
    # below, eliminating the TOCTOU window entirely rather than merely
    # narrowing it.
    raw = read_private_file(asset, max_bytes=MAX_DOWNLOAD_BYTES)
    if sha256_bytes(raw) != expected_sha256:
        raise PrimeInstallError(f"Prime Agent tarball changed on disk before extraction: {asset.name}")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except tarfile.TarError as exc:
        raise PrimeInstallError("cannot open Prime Agent tarball") from exc
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_TAR_MEMBERS:
            raise PrimeInstallError("Prime Agent archive has too many members")
        regular_bytes = 0
        selected: list[tuple[tarfile.TarInfo, Path]] = []
        seen: set[Path] = set()
        for member in members:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or not pure.parts
                or pure.parts[0] != "package"
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or (not member.isdir() and not member.isreg())
            ):
                raise PrimeInstallError(f"unsafe tar member: {member.name}")
            relative = Path(*pure.parts)
            if relative == Path("package"):
                continue
            if relative in seen:
                raise PrimeInstallError(f"duplicate tar member: {member.name}")
            seen.add(relative)
            if member.isreg():
                regular_bytes += member.size
            selected.append((member, relative))
        if regular_bytes > MAX_TAR_EXPANDED_BYTES:
            raise PrimeInstallError("Prime Agent archive expands beyond the approved limit")
        # Content digest of each regular file's bytes, captured while they are
        # streamed from the digest-verified tarball to disk -- keyed by the
        # SAME package-relative path make_patched_asset() later iterates via
        # its `extracted_relative_paths` manifest. This is the only point in
        # the whole install where this installer has independent, tarball
        # -derived proof of what a given extracted file's content is SUPPOSED
        # to be; make_patched_asset() re-verifies each file against this
        # digest immediately before archiving it, closing the verify-then-use
        # gap a same-UID racer could otherwise exploit between this
        # extraction and the later archive.add() re-read (see
        # make_patched_asset(), independent dual review round 6, 2026-08-18,
        # P1).
        content_digests: dict[Path, str] = {}
        # Mode recorded for each directory member AT THE MOMENT this loop
        # itself creates it, keyed the same way as content_digests --
        # make_patched_asset() later publishes each directory's tar entry
        # from this walk-time record instead of re-resolving the path a
        # second time (see its own comment, independent review round 7/8,
        # 2026-08-18, P1). The value is always the literal 0o700 this call
        # itself just passed to mkdir() -- not a value read back from a
        # subsequent, independent lstat() -- so there is no second
        # path-based lookup anywhere in this directory's publish path.
        dir_modes: dict[Path, int] = {}
        for member, relative in sorted(
            selected, key=lambda item: (len(item[1].parts), os.fspath(item[1]))
        ):
            target = destination / relative
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=False)
                dir_modes[relative.relative_to("package")] = 0o700
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise PrimeInstallError(f"cannot read tar member: {member.name}")
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            temp_path = Path(temporary)
            try:
                mode = 0o700 if member.mode & 0o111 else 0o600
                os.fchmod(descriptor, mode)
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if remaining:
                    raise PrimeInstallError(f"short tar member: {member.name}")
                os.replace(temp_path, target)
                os.chmod(target, mode)
                content_digests[relative.relative_to("package")] = digest.hexdigest()
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
    package_dir = destination / "package"
    if not package_dir.is_dir() or package_dir.is_symlink():
        raise PrimeInstallError("Prime Agent package root missing")
    # A same-UID racer that wins the narrow in-process window between this
    # process creating `destination` and this point could still have planted
    # a symlink somewhere in the extracted tree (see create_fresh_private_dir
    # docstring); fail closed instead of returning a package_dir whose
    # descendants might resolve outside it.
    assert_tree_has_no_symlinks(package_dir)
    # The manifest of paths this call itself verified and wrote, relative to
    # package_dir -- NOT a fresh directory listing. Callers must build any
    # published artifact from this manifest rather than walking package_dir
    # again: destination may pre-date this call (or a same-UID racer may
    # write into it concurrently with this call), and anything already
    # sitting there that this call did not itself extract from the
    # digest-verified tar is never a legitimate part of the release
    # (independent review round 2, 2026-08-18, P1: make_patched_asset used
    # to archive whatever package_dir.rglob("*") found on disk, silently
    # publishing any file an attacker had planted alongside the real
    # extracted members).
    manifest = tuple(
        sorted(
            (relative.relative_to("package") for _, relative in selected),
            key=lambda item: (len(item.parts), os.fspath(item)),
        )
    )
    expected_digest_keys = {
        relative.relative_to("package") for member, relative in selected if member.isreg()
    }
    if set(content_digests) != expected_digest_keys:
        # Every regular-file member must have a captured digest, and vice
        # versa -- this is an internal self-consistency check on this
        # function's own bookkeeping, not a same-UID-attacker detection; a
        # mismatch here means this function's own logic is wrong, not that
        # the tree was tampered with.
        raise PrimeInstallError("Prime Agent extraction manifest/digest mismatch")
    expected_dir_keys = {
        relative.relative_to("package") for member, relative in selected if member.isdir()
    }
    if set(dir_modes) != expected_dir_keys:
        # Same self-consistency check as content_digests above, for the
        # directory-mode record instead of file digests.
        raise PrimeInstallError("Prime Agent extraction manifest/directory-mode mismatch")
    return package_dir, manifest, content_digests, dir_modes


def extract_node_toolchain(
    asset: Path, destination: Path, expected_sha256: str
) -> tuple[Path, Path, str, str, dict[Path, str]]:
    """Returns (node, npm_cli, node_sha256, npm_cli_sha256,
    toolchain_content_digests).

    node_sha256/npm_cli_sha256 are SHA-256 content digests of `node`/
    `npm_cli`'s own bytes, captured WHILE those exact bytes are streamed
    from the digest-verified tarball to disk below (the same idiom
    safe_extract_main_asset() already uses for its own content_digests) --
    not a separate, later re-read of the freshly-written file. This is the
    ONE point in the entire install where this installer has independent,
    tarball-derived proof of what the pinned Node/npm runtime's bytes are
    SUPPOSED to be, with no window between "trusted content is known" and
    "digest captured" for a same-UID racer to exploit.

    _install_locked() re-verifies both digests, via
    verify_unchanged_private_ssd_asset_digest(), immediately before EACH
    subsequent point either binary is exec'd (both exact_tool_version()
    version-probe calls, and both run_npm() invocations), and bakes them
    into the generated launch guard (managed_launch_guard_script()) so
    every FUTURE real invocation re-verifies them too. Independent Codex
    sol/max round-23 review, 2026-08-19, P1-1: node/npm were previously
    validated once, structurally, right here (regular file, not a symlink,
    owned by this UID -- no content digest at all) and then exec'd by bare
    lexical path at every one of those later points with no re-check; a
    same-UID racer who swapped RELEASE_DIR/toolchain/bin/node's CONTENT in
    place between the two `npm` subprocess invocations (measured real
    window: 3m34s) went undetected because round 22's RELEASE_DIR-identity
    guard only proves the DIRECTORY was not swapped, not that every file
    reachable through it still holds the content this function itself
    wrote.

    `toolchain_content_digests` is the SAME per-file digest map the write
    loop below already builds for every regular file it extracts (keyed by
    `destination`-relative Path, e.g. Path("bin/node")) -- round 34,
    2026-08-20 (independent Claude opus/max round-33 review, P1-2): before
    this round, this map was built in full but then discarded, with only
    its node_relative/npm_cli_relative entries ever pulled out (as
    node_sha256/npm_cli_sha256 below). `exact_tool_version()`'s npm-version
    probe execs `node lib/node_modules/npm/bin/npm-cli.js --version`, and
    real npm's own npm-cli.js is a two-line forwarding stub
    (`require('../lib/cli.js')`) -- the actual npm library code that
    subprocess loads and runs is lib/cli.js and everything it requires,
    none of which was ever pinned. Reproduced: tampering the unpinned
    lib/cli.js while leaving the pinned npm-cli.js stub untouched let the
    tampered code execute inside every exact_tool_version() npm-version
    call (at install time and, via verify()'s own re-probe, on every
    future invocation), with `node npm-cli.js --version` still reporting
    the correct version string throughout, since npm-cli.js itself was
    never touched. `_install_locked_within_release_dir()` now folds this
    entire map into `release_relative_pinned_digests` (keyed
    "toolchain/<relative>") the same way the four locally patched
    packages' own full file trees already are (round 27/28) -- so
    tree_digest()'s pinned-digest comparison, plus this round's P1-1
    completeness assertion, cover every regular file under toolchain/, not
    only the two entry-point files.
    """
    expected_root = f"node-v{NODE_VERSION}-darwin-arm64"
    # Must be freshly created, never a pre-existing directory: see
    # create_fresh_private_dir's docstring for why ensure_private_dir()'s
    # accept-existing behavior was unsafe here (independent review round 2,
    # 2026-08-18, P1).
    create_fresh_private_dir(destination)
    # Round 13/14, 2026-08-18 (independent review, P1-B): same TOCTOU gap as
    # safe_extract_main_asset() (see its comment for the full finding) --
    # safe_download() verified this exact asset in memory, but this function
    # used to re-open it from disk by PATH afterward with no digest re-check,
    # so a same-UID actor could swap the pinned Node.js toolchain tarball for
    # one that still passes every structural check below but runs attacker
    # code the moment `node`/`npm-cli.js` are executed. Re-read (one atomic
    # open-by-path via read_private_file(), which itself fails closed if the
    # file changes mid-read) and re-hash immediately before parsing, and feed
    # tarfile the freshly verified bytes directly -- never a second,
    # independently raceable tarfile.open(asset, ...) by path.
    raw = read_private_file(asset, max_bytes=MAX_NODE_DOWNLOAD_BYTES)
    if sha256_bytes(raw) != expected_sha256:
        raise PrimeInstallError(f"Node.js archive changed on disk before extraction: {asset.name}")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except tarfile.TarError as exc:
        raise PrimeInstallError("cannot open pinned Node.js archive") from exc
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_TAR_MEMBERS:
            raise PrimeInstallError("Node.js archive has too many members")
        regular_bytes = 0
        selected: list[tuple[tarfile.TarInfo, Path]] = []
        seen: set[Path] = set()
        for member in members:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or not pure.parts
                or pure.parts[0] != expected_root
                or member.isdev()
                or member.isfifo()
            ):
                raise PrimeInstallError(f"unsafe Node.js archive member: {member.name}")
            if member.issym() or member.islnk():
                continue
            if not member.isdir() and not member.isreg():
                raise PrimeInstallError(f"unsupported Node.js archive member: {member.name}")
            relative = Path(*pure.parts[1:])
            if not relative.parts:
                continue
            if relative in seen:
                raise PrimeInstallError(f"duplicate Node.js archive member: {member.name}")
            seen.add(relative)
            if member.isreg():
                regular_bytes += member.size
            selected.append((member, relative))
        if regular_bytes > MAX_TAR_EXPANDED_BYTES:
            raise PrimeInstallError("Node.js archive expands beyond the approved limit")
        # Content digest of each regular file's bytes, captured while they
        # are streamed from the digest-verified tarball to disk -- see this
        # function's own docstring for why node_sha256/npm_cli_sha256 (the
        # two entries this call site actually needs) are pulled from this
        # dict below rather than computed via a second, independent read.
        content_digests: dict[Path, str] = {}
        for member, relative in sorted(selected, key=lambda item: (len(item[1].parts), os.fspath(item[1]))):
            target = destination / relative
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(target, 0o700)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise PrimeInstallError(f"cannot read Node.js archive member: {member.name}")
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            temp_path = Path(temporary)
            try:
                mode = 0o700 if member.mode & 0o111 else 0o600
                os.fchmod(descriptor, mode)
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if remaining:
                    raise PrimeInstallError(f"short Node.js archive member: {member.name}")
                os.replace(temp_path, target)
                os.chmod(target, mode)
                content_digests[relative] = digest.hexdigest()
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
    # A same-UID racer that wins the narrow in-process window between
    # create_fresh_private_dir() creating `destination` and this point could
    # still have planted a symlinked ancestor (e.g. "bin" or
    # "lib/node_modules/npm/bin") somewhere in the extracted tree. The
    # per-path checks below only lstat() the two exact leaf paths, which
    # does not catch a symlinked ancestor -- os.lstat() still follows every
    # intermediate path component, only the final one is left unresolved
    # (independent review round 2, 2026-08-18, P1: this let the function
    # return success with `node`/`npm_cli` resolving outside SSD_ROOT, and
    # they were then executed and put first on npm's PATH). Fail closed on
    # any symlink anywhere in the tree before trusting anything under it.
    assert_tree_has_no_symlinks(destination)
    node = destination / "bin/node"
    npm_cli = destination / "lib/node_modules/npm/bin/npm-cli.js"
    node_relative = Path("bin/node")
    npm_cli_relative = Path("lib/node_modules/npm/bin/npm-cli.js")
    for name, path in (("node", node), ("npm", npm_cli)):
        try:
            info = path.lstat()
        except OSError as exc:
            raise PrimeInstallError(f"pinned {name} runtime is missing") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise PrimeInstallError(f"pinned {name} runtime is unsafe")
    try:
        node_sha256 = content_digests[node_relative]
        npm_cli_sha256 = content_digests[npm_cli_relative]
    except KeyError as exc:
        # Unreachable in practice: the lstat/regular-file checks just above
        # already require `node`/`npm_cli` to exist as regular files, and
        # the only way a path in `destination` becomes a regular file is
        # through the write loop above, which always records a digest
        # before advancing to the next member. A KeyError here would mean
        # this function's own bookkeeping is wrong, not that a same-UID
        # actor tampered with anything -- fail closed rather than silently
        # skip digest pinning for the pinned runtime.
        raise PrimeInstallError(
            "pinned Node.js runtime digest capture is incomplete"
        ) from exc
    # Round 34, 2026-08-20 (P1-2): return the COMPLETE per-file digest map
    # captured above -- not merely the two entries just pulled out for
    # node_sha256/npm_cli_sha256 -- so the caller can pin every regular
    # file under the extracted toolchain tree, not only its two entry
    # points. See this function's own docstring for why.
    return node, npm_cli, node_sha256, npm_cli_sha256, content_digests


def managed_npm_environment(
    node_path: str,
    cache: Path,
    install_home: Path,
    install_tmp: Path,
) -> dict[str, str]:
    for path in (cache, install_home, install_tmp):
        verify_private_ssd_dir(path)
    npmrc = install_home / "npmrc"
    global_npmrc = install_home / "global-npmrc"
    project_npmrc = install_home / ".npmrc"
    if project_npmrc.exists() or project_npmrc.is_symlink():
        raise PrimeInstallError("private npm probe cwd contains project configuration")
    managed_configs = (
        (npmrc, b"registry=https://registry.npmjs.org/\nalways-auth=false\n"),
        (global_npmrc, b""),
    )
    for config_path, expected in managed_configs:
        if config_path.exists() or config_path.is_symlink():
            if read_private_file(config_path) != expected:
                raise PrimeInstallError(
                    f"managed npm configuration drifted: {config_path.name}"
                )
        else:
            atomic_create_private_file(config_path, expected, 0o600)
    return {
        "HOME": os.fspath(install_home),
        "LC_ALL": "C",
        "NODE_DISABLE_COMPILE_CACHE": "1",
        "NO_UPDATE_NOTIFIER": "1",
        "PATH": os.pathsep.join((os.fspath(Path(node_path).parent), "/usr/bin", "/bin")),
        "TMPDIR": os.fspath(install_tmp),
        "npm_config_audit": "false",
        "npm_config_cache": os.fspath(cache),
        "npm_config_fund": "false",
        "npm_config_globalconfig": os.fspath(global_npmrc),
        "npm_config_ignore_scripts": "true",
        "npm_config_provenance": "false",
        "npm_config_registry": "https://registry.npmjs.org/",
        "npm_config_update_notifier": "false",
        "npm_config_userconfig": os.fspath(npmrc),
    }


def exact_tool_version(
    argv: list[str],
    expected: str,
    name: str,
    *,
    cache: Path,
    install_home: Path,
    install_tmp: Path,
) -> str:
    environment = managed_npm_environment(
        argv[0], cache, install_home, install_tmp
    )
    try:
        result = subprocess.run(
            argv,
            cwd=install_home,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrimeInstallError(f"cannot run pinned {name}") from exc
    observed = result.stdout.strip()
    normalized = observed[1:] if name == "Node.js" and observed.startswith("v") else observed
    if result.returncode != 0 or normalized != expected or result.stderr.strip():
        raise PrimeInstallError(f"pinned {name} version mismatch")
    return normalized


def make_patched_asset(
    original_asset: Path,
    original_sha256: str,
    upstream_lock: dict[str, Any],
    assets_dir: Path,
    *,
    expected_name: str,
    managed_name: str,
    output_name: str,
) -> tuple[Path, str, dict[str, Any], tuple[int, int], dict[Path, str]]:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", managed_name).strip("-")
    unpacked = RELEASE_DIR / f".unpacked-{safe_name}"
    # Must be freshly created, never a pre-existing directory: see
    # create_fresh_private_dir's docstring for why ensure_private_dir()'s
    # accept-existing behavior was unsafe here (independent review round 2,
    # 2026-08-18, P1).
    create_fresh_private_dir(unpacked)
    # original_sha256 is the SAME pinned digest safe_download() already
    # verified `original_asset`'s bytes against at download time (ASSETS[...]);
    # threading it through here lets safe_extract_main_asset() re-verify the
    # on-disk file immediately before parsing it, closing the round-13/14 P1-B
    # TOCTOU gap (see that function's own comment for the full finding).
    package_dir, extracted_relative_paths, content_digests, dir_modes = safe_extract_main_asset(
        original_asset, unpacked, original_sha256
    )
    manifest_path = package_dir / "package.json"
    manifest_relative = Path("package.json")
    # Verified read, not a plain manifest_path.read_bytes(): compare against
    # the digest safe_extract_main_asset() itself captured while streaming
    # this exact file's bytes from the digest-verified tarball, so a same-UID
    # swap between extraction and this parse is caught here instead of
    # silently feeding attacker-controlled JSON into the manifest this
    # function goes on to trust and republish.
    try:
        manifest_raw_initial = read_private_file(
            manifest_path, max_bytes=MAX_TAR_EXPANDED_BYTES
        )
    except PrimeInstallError as exc:
        raise PrimeInstallError("Prime Agent manifest is missing or unsafe") from exc
    if sha256_bytes(manifest_raw_initial) != content_digests.get(manifest_relative):
        raise PrimeInstallError("extracted Prime Agent manifest content changed before use")
    manifest = strict_json(manifest_raw_initial)
    if (
        not isinstance(manifest, dict)
        or manifest.get("name") != expected_name
        or manifest.get("version") != VERSION
    ):
        raise PrimeInstallError("unexpected Prime Agent manifest identity")
    manifest["name"] = managed_name
    exact_dependency_versions(manifest, upstream_lock)
    manifest_raw = canonical_json(manifest)
    atomic_write(manifest_path, manifest_raw, 0o600)
    # package.json's on-disk bytes were just intentionally rewritten above
    # (managed_name substitution, exact_dependency_versions()) -- the
    # extraction-time digest captured for this one path no longer describes
    # what should be archived below. Replace it with a digest of exactly the
    # bytes this function itself just published; every other path's digest
    # still describes the untouched, tarball-verified content
    # safe_extract_main_asset() wrote and this function never modifies.
    content_digests[manifest_relative] = sha256_bytes(manifest_raw)
    # Built entirely in memory, then published through atomic_create_private_file
    # -- the same private-temp-file-then-no-clobber-rename discipline every
    # other managed write in this file uses (see e.g. safe_download() and
    # extract_node_toolchain()) -- rather than the previous `patched.open("wb")`
    # on this known, predictable output path. That direct open() had no
    # O_NOFOLLOW, no create-only semantics, and no destination-identity check:
    # a same-UID process that replaced `patched` with a symlink to a file
    # outside SSD_ROOT between assets-dir creation and this write would have
    # had the write silently follow the symlink and clobber the victim file,
    # with the escape only possibly noticed much later, at tree_digest() time,
    # by which point the external damage was already done (independent Codex
    # sol/xhigh review, 2026-08-16, P1-1). atomic_create_private_file()
    # instead writes to a mkstemp() sibling in the same directory (so the
    # final rename is same-filesystem and atomic), fchmod()s it, and publishes
    # with rename_noreplace()'s RENAME_EXCL semantics, which fails loudly with
    # EEXIST -- never follows -- whether the occupant at `patched` is a
    # pre-existing regular file or a symlink.
    def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        info.pax_headers = {}
        return info

    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            # Iterate the manifest safe_extract_main_asset() itself verified
            # and wrote -- NOT a fresh package_dir.rglob("*") walk. Walking
            # the directory published whatever was physically sitting under
            # it, including files a same-UID attacker had planted there
            # (before extraction, during it, or in the window between
            # extraction finishing and this loop starting) that were never
            # part of the digest-verified original tarball (independent
            # review round 2, 2026-08-18, P1).
            for relative in extracted_relative_paths:
                path = package_dir / relative
                arcname = Path("package") / relative
                expected_digest = content_digests.get(relative)
                if expected_digest is not None:
                    # A regular file, per the tarball-verified extraction
                    # manifest (every regular-file member safe_extract_main_
                    # asset() wrote has an entry in content_digests; nothing
                    # else does). read_private_file() binds the lstat,
                    # O_NOFOLLOW open, fstat-identity check, bounded read,
                    # and post-read fstat-consistency check into ONE
                    # verify-then-use operation with no gap between
                    # verification and consumption; archive.addfile() below
                    # publishes exactly the bytes it returned -- there is no
                    # second, separate read of `path` from disk (unlike
                    # archive.add(path, ...), which performs its own later,
                    # independent open()+read() internally). This closes the
                    # window a same-UID racer previously had to swap this
                    # file's content between an early lstat-only check and
                    # that later, separate tarfile-internal read (independent
                    # dual review round 6, 2026-08-18, P1: reviewer
                    # reproduced the swap in that window in 4/4 trials, with
                    # the measured window ranging from ~1.9s to ~27s).
                    try:
                        raw = read_private_file(path, max_bytes=MAX_TAR_EXPANDED_BYTES)
                    except PrimeInstallError as exc:
                        raise PrimeInstallError(
                            f"extracted member vanished or is unsafe before publish: {relative}"
                        ) from exc
                    if sha256_bytes(raw) != expected_digest:
                        raise PrimeInstallError(
                            f"extracted member content changed before publish: {relative}"
                        )
                    try:
                        entry_info = path.lstat()
                    except OSError as exc:
                        raise PrimeInstallError(
                            f"extracted member vanished before publish: {relative}"
                        ) from exc
                    tarinfo = tarfile.TarInfo(name=os.fspath(arcname))
                    tarinfo.size = len(raw)
                    tarinfo.mode = stat.S_IMODE(entry_info.st_mode)
                    tarinfo.type = tarfile.REGTYPE
                    archive.addfile(normalize(tarinfo), io.BytesIO(raw))
                    continue
                # A directory, per that same tarball-verified extraction
                # manifest. Directories carry no content bytes for a
                # same-UID racer to swap, but archive.add() performs its own
                # SECOND, entirely independent path-based lstat() internally
                # when it builds the TarInfo it publishes -- no lstat()
                # performed here beforehand closes that gap, because
                # archive.add() re-resolves and re-inspects `path` itself,
                # regardless of what any earlier check here found. A
                # same-UID racer that swapped this path for a symlink to an
                # arbitrary, possibly out-of-tree target in the window
                # between any check here and that later, internal re-stat
                # would have the published archive silently contain a
                # symlink member in place of the intended directory entry --
                # from a link-free, digest-verified source archive
                # (independent review round 7/8, 2026-08-18, P1). Closed the
                # same way read_private_file() closes the analogous gap for
                # file content: never touch `path` again here at all. This
                # directory's mode is the literal 0o700 value
                # safe_extract_main_asset() itself passed to mkdir() when it
                # originally created this exact path -- not a value read
                # back from any lstat, here or there -- so archive.addfile()
                # below publishes a fixed DIRTYPE member built entirely from
                # that walk-time record, with no second path-based lookup
                # and no archive.add() call anywhere in this branch.
                mode = dir_modes.get(relative)
                if mode is None:
                    # Cannot happen given safe_extract_main_asset()'s own
                    # manifest/dir_modes self-consistency check, which
                    # already guarantees every directory in
                    # extracted_relative_paths has a recorded mode; this is
                    # defense-in-depth against this function's own logic
                    # drifting from that invariant, not a same-UID-attacker
                    # detection.
                    raise PrimeInstallError(
                        f"extracted directory missing from walk-time record: {relative}"
                    )
                tarinfo = tarfile.TarInfo(name=os.fspath(arcname))
                tarinfo.type = tarfile.DIRTYPE
                tarinfo.mode = mode
                tarinfo.size = 0
                archive.addfile(normalize(tarinfo))
    patched_raw = buffer.getvalue()
    patched = assets_dir / output_name
    atomic_create_private_file(patched, patched_raw, 0o600)
    # Captured immediately after atomic_create_private_file() itself already
    # re-verified this exact identity as part of publication -- before
    # shutil.rmtree(unpacked) below, and before returning control to the
    # caller -- so the window between "this file is known-good" and "the
    # caller has a value it can re-check later" is as narrow as this
    # in-process call sequence allows. _install_locked() threads this
    # identity, together with the digest returned below, through to
    # verify_patched_assets_unchanged(), which re-verifies both immediately
    # before each of the two npm invocations that independently re-read
    # this path from disk (round 16, 2026-08-18, P1; see
    # verify_unchanged_private_ssd_asset_digest()).
    published_stat = patched.lstat()
    published_identity = (published_stat.st_dev, published_stat.st_ino)
    shutil.rmtree(unpacked)
    # content_digests: the ORIGINAL tarball's own per-file digest map,
    # captured by safe_extract_main_asset() directly from the digest-
    # verified tarball's bytes as they were streamed to disk -- strictly
    # BEFORE `npm ci` (or anything else) ever ran. Every entry describes
    # exactly what safe_extract_main_asset() itself wrote to `package_dir`;
    # every path this loop above archived into `patched_raw` was verified
    # against this same digest immediately before its archive.addfile()
    # call, except `manifest_relative` (package.json), whose entry was
    # deliberately reassigned above to describe the bytes this function
    # itself just published in its place. Returned so a caller that later
    # compares some INDEPENDENTLY produced copy of one of these same
    # relative paths (e.g. `npm ci`'s own materialized
    # node_modules/prime-agent/dist/bundle/cli.js) can use THIS map as an
    # untamperable baseline -- fixed before npm ever ran, and therefore
    # never influenced by a same-UID swap that happens during OR after
    # npm's own subprocess window (round 25, 2026-08-19, P1-A; see
    # _install_locked_within_release_dir()'s entrypoint-digest capture).
    # Round 27, 2026-08-19, P1: that entrypoint-digest capture was, until
    # this round, the ONLY consumer of this return value -- pinning just
    # ONE entry (dist/bundle/cli.js) out of the complete per-file map this
    # function already builds for every one of the four locally patched
    # packages. _install_locked_within_release_dir() now also threads the
    # COMPLETE map returned here, for every one of those four calls, into
    # assert_locally_patched_package_matches_pinned_digests() -- which
    # verifies every file `npm ci` materializes for that package, not only
    # its entrypoint -- so this map has two independent consumers today,
    # not one.
    return patched, sha256_bytes(patched_raw), manifest, published_identity, content_digests


def resolved_local_asset(resolved: str) -> Path:
    if not resolved.startswith("file:"):
        raise PrimeInstallError("managed dependency did not resolve from a local file")
    parsed = urllib.parse.urlparse(resolved)
    if parsed.netloc or parsed.query or parsed.fragment:
        raise PrimeInstallError("managed dependency used an unsafe file URL")
    raw_path = urllib.parse.unquote(resolved[5:])
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = RELEASE_DIR / candidate
    return resolve_ssd(candidate)


def normalized_production_lock(generated: dict[str, Any]) -> bytes:
    """Produce the canonical bytes GENERATED_LOCK_SHA256 is pinned against:
    the exact generated package-lock.json npm produced, with only the
    parts that are legitimately environment-dependent replaced by a fixed
    placeholder, so the SAME closure reproduced on any darwin-arm64 machine
    with the pinned Node/npm toolchain hashes to the SAME pinned constant.

    Two things are normalized for the four locally patched assets
    (`prime-agent` + the three `@earendil-works/pi-*` workspace packages):
    "resolved" (a `file:` URL that literally embeds RELEASE_DIR's absolute
    path) is genuinely path-dependent and must be normalized for the hash
    to be portable at all. "integrity" is NOT path-dependent -- it is
    npm's own content-derived digest of the local tarball -- but was
    normalized to a fixed placeholder anyway, which made GENERATED_LOCK_SHA256
    -- and therefore validate_generated_lock()'s top-level hash check --
    unable to distinguish two DIFFERENT patched-tarball contents as long as
    both had the same declared name/version (independent review round 16,
    2026-08-18, P1). This function's normalization is left unchanged here
    (recomputing GENERATED_LOCK_SHA256 against real content would require
    an independent real-npm replay this installer's own test/review
    environment cannot perform); instead validate_generated_lock() now
    performs its own separate, real content-digest check for these same
    four rows against the actual on-disk assets, using the SAME
    freshly-verified digests _install_locked() already captures from
    make_patched_asset() and re-verifies via verify_patched_assets_unchanged()
    -- closing the gap this function's own placeholder leaves open without
    touching what this function hashes or what GENERATED_LOCK_SHA256 is
    pinned to.
    """
    release_text = os.fspath(RELEASE_DIR)
    encoded_release = urllib.parse.quote(release_text, safe="/")

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return value.replace(release_text, "$RELEASE_DIR").replace(
                encoded_release, "$RELEASE_DIR"
            )
        return value

    normalized = normalize(generated)
    packages = normalized.get("packages") if isinstance(normalized, dict) else None
    if not isinstance(packages, dict):
        raise PrimeInstallError("generated lock has no packages map")
    local_assets = {
        "prime-agent": MAIN_PATCHED_ASSET,
        **WORKSPACE_ASSETS,
    }
    for lock_path, row in packages.items():
        if not isinstance(lock_path, str) or not isinstance(row, dict) or not lock_path:
            continue
        name = package_name_from_lock_path(lock_path, row)
        asset_name = local_assets.get(name or "")
        if asset_name is None:
            continue
        row["resolved"] = f"file:$RELEASE_DIR/assets/{asset_name}"
        if "integrity" in row:
            row["integrity"] = f"managed-local:{name}@{VERSION}"
    return canonical_json(normalized)


def generated_lock_pin_age_days(now: datetime | None = None) -> int:
    """Purely offline: elapsed whole days since GENERATED_LOCK_PINNED_AT.
    Informational only (see that constant's own comment for why this is
    deliberately never a hard gate) -- read by plan() so a human deciding
    whether to re-verify has the number in front of them without needing
    to check README.md or git blame by hand first.

    Clamped to 0 rather than returning a negative value: round-54 dual
    review (2026-08-22) reproduced GENERATED_LOCK_PINNED_AT itself being
    momentarily wrong by one day (see that constant's own comment) and
    this then silently reporting -1, defeating a staleness indicator by
    making it read as "not stale" in the one case that most needs a human
    to notice something is off. A clock genuinely running behind the
    pinned date (not just the one historical mistake above) hits the same
    clamp and is equally not this function's business to diagnose --
    "somehow not stale yet" is the safe direction to fail toward for a
    purely informational field.
    """
    pinned_at = datetime.strptime(GENERATED_LOCK_PINNED_AT, "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    )
    current = now if now is not None else datetime.now(timezone.utc)
    return max(0, (current - pinned_at).days)


def _registry_resolved_versions_by_name(packages: dict[str, Any]) -> dict[str, set[str]]:
    """Round-54 dual review (2026-08-22, both Claude opus/max and Codex
    sol/max independently) reproduced this originally requiring an
    explicit `resolved` field starting with the registry URL prefix,
    which silently dropped 122 of 184 comparable names from the real
    pinned upstream source lock -- a real npm lockfile-v3 property, not a
    malformed input: 242 of its 463 rows are genuine registry packages
    recorded with only `version` (no `resolved`/`integrity` at all).
    Verified empirically against that exact lock which of the three real
    row shapes each marker distinguishes: a `link: true` row (npm
    workspace member, e.g. `@earendil-works/pi-ai`) always pairs with a
    `resolved` value that is a plain relative path (`packages/...`), never
    absent and never a registry URL; every other row either carries a
    real `https://registry.npmjs.org/...` `resolved` value, a local
    `file:...` value (the four locally patched packages' own generated-
    lock rows), or nothing at all -- there is no observed case of a
    `link: true` row with a missing `resolved`. So a row is accepted here
    when `resolved` is a registry URL OR entirely absent, and rejected
    only when `resolved` is present and is something else (`file:`, a
    relative workspace path, or `link: true` as an explicit second,
    defense-in-depth check in case some other npm version ever omits
    `resolved` on a link row).
    """
    by_name: dict[str, set[str]] = {}
    for lock_path, row in packages.items():
        if not isinstance(lock_path, str) or not isinstance(row, dict) or not lock_path:
            continue
        if row.get("link") is True:
            continue
        resolved = row.get("resolved")
        if isinstance(resolved, str) and not resolved.startswith(
            "https://registry.npmjs.org/"
        ):
            continue
        if resolved is not None and not isinstance(resolved, str):
            continue
        version = row.get("version")
        if not isinstance(version, str) or not version:
            continue
        name = package_name_from_lock_path(lock_path, row)
        if not name:
            continue
        by_name.setdefault(name, set()).add(version)
    return by_name


def compute_lock_drift(
    generated_packages: dict[str, Any], upstream_packages: dict[str, Any]
) -> list[dict[str, Any]]:
    """Pure, offline comparison between this install's freshly generated
    production lock and the pinned upstream source lock (LOCK_SHA256),
    both already fully parsed by the time _install_locked_within_release_
    dir() calls this. Every review round of this file has had to
    reconstruct a version of this exact tuple-level diff by hand, from
    scratch, via an ad-hoc standalone replica script (see e.g. the
    round-53 QA report, 2026-08-22) -- this makes it a durable, automatic
    part of every real install instead, closing the specific gap that
    round's own recommendation #1 asked for ("persist generated-lock
    evidence and full tuple diffs").

    Matches registry-resolved rows only, by bare package NAME (not lock
    path -- the same package legitimately sits at different nesting
    depths in either lock, and the two trees have entirely different
    shapes: this install's own closure is one production package's
    narrow transitive runtime tree, while the upstream source lock spans
    the whole upstream monorepo -- every workspace member, every dev
    dependency). A `file:`-resolved row on either side (the four locally
    patched packages) is skipped: those are pinned and verified through
    an entirely separate, stronger, already-existing mechanism (see
    normalized_production_lock() and make_patched_asset()), not this one.

    Deliberately reports version MISMATCHES only, never bare presence
    differences (a package this install resolves that the source lock
    never mentions, or vice versa) -- the shape mismatch between the two
    trees described above would make a presence diff dominated by exactly
    that kind of noise rather than by anything resembling real drift.
    This is real, useful signal, not a proof: it cannot see whether a
    same-named, same-VERSION row's resolved URL or integrity value
    silently changed underneath an unmoved version number (a stronger,
    heavier check the round-53 QA report also names and this round
    deliberately did not attempt to automate).
    """
    generated_by_name = _registry_resolved_versions_by_name(generated_packages)
    upstream_by_name = _registry_resolved_versions_by_name(upstream_packages)
    drift: list[dict[str, Any]] = []
    for name, generated_versions in generated_by_name.items():
        upstream_versions = upstream_by_name.get(name)
        if not upstream_versions:
            continue
        for generated_version in sorted(generated_versions - upstream_versions):
            drift.append(
                {
                    "name": name,
                    "generated_version": generated_version,
                    "upstream_versions": sorted(upstream_versions),
                }
            )
    drift.sort(key=lambda entry: (entry["name"], entry["generated_version"]))
    return drift


def validate_generated_lock(
    raw: bytes,
    generated: dict[str, Any],
    patched_asset_sha256: dict[str, str],
) -> dict[str, Any]:
    """`patched_asset_sha256` maps each locally patched asset's FILENAME
    (assets_dir-relative, e.g. MAIN_PATCHED_ASSET) to the real SHA-256
    digest _install_locked() captured from make_patched_asset() and just
    re-verified, immediately before this call, via
    verify_patched_assets_unchanged(). Required (no default) so a caller
    can never silently skip binding this check to real content -- the same
    fail-closed-by-construction discipline every other mandatory
    verify-then-use parameter in this file already uses.

    normalized_production_lock()'s closure-hash comparison below treats
    every locally patched asset's "integrity" field as a fixed placeholder
    (see that function's own docstring for why), which makes it, on its
    own, unable to distinguish two DIFFERENT patched-tarball contents that
    declare the same name/version/resolved path. The per-row check inside
    the local-asset branch below is this function's OWN, separate defense
    against exactly that: it independently re-reads the actual on-disk
    asset right now and requires its SHA-256 match `patched_asset_sha256`
    -- unconditionally, regardless of what (if anything) npm's own
    generated lock says -- so a same-UID content swap is caught here even
    though the closure hash above cannot see it (independent review round
    16, 2026-08-18, P1). If npm's row DOES include an "integrity" value
    (mirroring the registry-package branch's own, unconditional
    requirement), it must be well-formed, but its presence is not itself
    required: this installer's review/test environment cannot verify
    whether real npm always populates "integrity" for a LOCAL tarball
    `file:` dependency the way it always does for a registry dependency,
    and the digest re-check above does not depend on that assumption
    either way.
    """
    del raw
    observed_sha256 = sha256_bytes(normalized_production_lock(generated))
    if observed_sha256 != GENERATED_LOCK_SHA256:
        raise PrimeInstallError(
            "generated production lock hash mismatch: "
            f"observed {observed_sha256}, expected {GENERATED_LOCK_SHA256}"
        )
    if generated.get("lockfileVersion") != 3:
        raise PrimeInstallError("generated lockfile version drifted")
    packages = generated.get("packages")
    if not isinstance(packages, dict):
        raise PrimeInstallError("generated lock has no packages map")
    root = packages.get("")
    expected_root_dependency = f"file:assets/{MAIN_PATCHED_ASSET}"
    if (
        not isinstance(root, dict)
        or root.get("name") != "orca-managed-prime-agent"
        or root.get("version") != VERSION
        or root.get("dependencies") != {"prime-agent": expected_root_dependency}
    ):
        raise PrimeInstallError("generated lock root identity drifted")
    local_assets = {
        "prime-agent": RELEASE_DIR / "assets" / MAIN_PATCHED_ASSET,
        **{
            name: RELEASE_DIR / "assets" / asset_name
            for name, asset_name in WORKSPACE_ASSETS.items()
        },
    }
    expected_asset_filenames = {path.name for path in local_assets.values()}
    if set(patched_asset_sha256) != expected_asset_filenames:
        raise PrimeInstallError(
            "patched asset digest map does not match the managed local assets"
        )
    checked = 0
    registry_checked = 0
    observed_local: set[str] = set()
    for lock_path, row in packages.items():
        if not isinstance(lock_path, str) or not isinstance(row, dict) or not lock_path:
            continue
        if row.get("dev") is True or row.get("link") is True:
            raise PrimeInstallError(f"generated production lock contains a dev/link row: {lock_path}")
        name = package_name_from_lock_path(lock_path, row)
        version = row.get("version")
        if name in local_assets:
            resolved = row.get("resolved")
            # Whether npm's own generated lock includes a real "integrity"
            # value for a LOCAL tarball `file:` dependency (as opposed to a
            # registry dependency, which always has one) is not something
            # this installer's own review/test environment can verify
            # against a real npm run -- unlike the mandatory content-digest
            # re-check a few lines below, which does not depend on that
            # assumption at all. So presence is NOT required here (an
            # incorrect assumption would fail every real install), but if
            # npm DID include one, it must be well-formed -- mirroring the
            # registry branch's own requirement below as defense in depth,
            # never as the actual security boundary for this branch.
            integrity = row.get("integrity")
            if integrity is not None and (
                not isinstance(integrity, str)
                or not re.fullmatch(r"sha512-[A-Za-z0-9+/=]+", integrity)
            ):
                raise PrimeInstallError(f"generated managed asset identity drift: {name}")
            if version != VERSION or not isinstance(resolved, str):
                raise PrimeInstallError(f"generated managed asset identity drift: {name}")
            actual_asset = resolved_local_asset(resolved)
            expected_asset = resolve_ssd(local_assets[name])
            if actual_asset != expected_asset:
                raise PrimeInstallError(f"generated managed asset path drift: {name}")
            # Real content-digest re-check, independent of the closure hash
            # above (which cannot see this, since normalized_production_lock()
            # normalizes this same field to a fixed placeholder before
            # hashing -- see that function's docstring). Reads the actual
            # on-disk bytes right now rather than trusting anything captured
            # earlier, closing the gap where two different patched-tarball
            # contents with the same declared name/version/resolved path
            # would otherwise pass identically (independent review round
            # 16, 2026-08-18, P1).
            expected_digest = patched_asset_sha256[expected_asset.name]
            actual_raw = read_private_ssd_file(actual_asset, max_bytes=MAX_TAR_EXPANDED_BYTES)
            if sha256_bytes(actual_raw) != expected_digest:
                raise PrimeInstallError(f"generated managed asset content drift: {name}")
            observed_local.add(name)
            checked += 1
            continue
        if not name or not isinstance(version, str):
            raise PrimeInstallError(f"generated dependency identity drift: {name or lock_path}@{version}")
        resolved = row.get("resolved")
        integrity = row.get("integrity")
        if not isinstance(resolved, str) or not isinstance(integrity, str):
            raise PrimeInstallError(f"generated registry dependency is unpinned: {name}@{version}")
        parsed = urllib.parse.urlparse(resolved)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "registry.npmjs.org"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"sha512-[A-Za-z0-9+/=]+", integrity)
        ):
            raise PrimeInstallError(f"unsafe generated dependency artifact: {name}@{version}")
        checked += 1
        registry_checked += 1
    if checked != GENERATED_LOCK_PACKAGE_COUNT:
        raise PrimeInstallError(
            f"generated production closure size drifted: {checked} != {GENERATED_LOCK_PACKAGE_COUNT}"
        )
    if observed_local != set(local_assets):
        missing = sorted(set(local_assets) - observed_local)
        raise PrimeInstallError(f"generated production lock omitted managed assets: {missing}")
    return {
        "lock_sha256": observed_sha256,
        "packages_checked": checked,
        "registry_packages_checked": registry_checked,
    }


def declared_top_level_node_modules_packages(packages: dict[str, Any]) -> frozenset[str]:
    """The set of DEPTH-1 node_modules-relative package directory paths
    `packages` (a validated generated package-lock.json's own "packages"
    map, as produced by `npm install --package-lock-only` and already
    confirmed well-formed by validate_generated_lock()) declares -- e.g.
    "node_modules/prime-agent" or "node_modules/@earendil-works/pi-ai" for
    a direct top-level dependency, EXCLUDING any further-nested
    peer-dependency entry (a lock_path containing more than one
    "node_modules/" segment, e.g.
    "node_modules/parent/node_modules/child"), which real npm's own
    hoisting/`--omit=dev` behavior may or may not actually materialize at
    the top level and this function does not attempt to reason about (see
    assert_materialized_node_modules_matches_lock()'s docstring for what
    that residual means in practice).

    Each entry's NAME is resolved via package_name_from_lock_path() -- the
    SAME resolution validate_generated_lock() already uses to establish
    package identity elsewhere in this file -- rather than parsed
    positionally out of `lock_path` alone: real npm always names a
    top-level lock_path after the exact directory it materializes
    ("node_modules/<name>"), so the two coincide for any real generated
    lock, but going through the shared resolver keeps this function
    consistent with the rest of this file's notion of "what package does
    this lock row identify" in the one case they could diverge (a row
    whose own declared "name" differs from its lock_path's trailing
    segment).

    Used as the authoritative "what npm ci is SUPPOSED to have installed
    at the top level of node_modules/" baseline for
    assert_materialized_node_modules_matches_lock() -- built entirely from
    the SAME lock file content validate_generated_lock() already pinned
    the whole install to (via GENERATED_LOCK_SHA256), captured before
    `npm ci` ever runs, not from anything read after.
    """
    declared: set[str] = set()
    for lock_path, row in packages.items():
        if not isinstance(lock_path, str) or not lock_path.startswith("node_modules/"):
            continue
        if not isinstance(row, dict):
            continue
        suffix = lock_path[len("node_modules/"):]
        if not suffix or "/node_modules/" in lock_path:
            continue
        name = package_name_from_lock_path(lock_path, row)
        if name:
            declared.add(f"node_modules/{name}")
    return frozenset(declared)


def declared_nested_node_modules_packages(
    packages: dict[str, Any]
) -> dict[str, frozenset[str]]:
    """Every node_modules/ CONTAINER this closure declares at any nesting
    depth BEYOND the top level -- e.g. "node_modules/proxy-agent/node_modules"
    or "node_modules/@aws-sdk/credential-provider-sso/node_modules" -- mapped
    to the SET of bare package directory names (scoped packages spelled
    "@scope/name") that container declares directly beneath it, derived from
    the SAME validated generated package-lock.json "packages" map
    declared_top_level_node_modules_packages() already reads.

    A row's container is everything up to and including its OWN LAST
    "node_modules/" segment (`lock_path.rfind("node_modules/")`), so this
    groups correctly at ANY depth the lock format allows -- not only the one
    level of nesting this closure's pinned lock happens to have today (12
    declared nested rows across 9 distinct nested containers, empirically
    confirmed via a real darwin-arm64 `npm ci` replay of this exact pinned
    lock, round 27, 2026-08-19) -- without any change to this function if a
    future lock refresh introduces a doubly-nested row.

    Used, together with declared_top_level_node_modules_packages(), as the
    complete "what npm ci is SUPPOSED to have materialized" baseline for
    assert_materialized_node_modules_matches_lock() (round 27, 2026-08-19,
    P2-2 fix; see that function's own docstring for why the round-25/26
    version of this check, scoped to the top level only, missed this
    entirely).
    """
    grouped: dict[str, set[str]] = {}
    for lock_path, row in packages.items():
        if not isinstance(lock_path, str) or not lock_path.startswith("node_modules/"):
            continue
        if not isinstance(row, dict):
            continue
        last = lock_path.rfind("node_modules/")
        if last == 0:
            continue  # Top-level row -- declared_top_level_node_modules_packages()'s concern, not this function's.
        container = lock_path[:last] + "node_modules"
        name = package_name_from_lock_path(lock_path, row)
        if name:
            grouped.setdefault(container, set()).add(name)
    return {container: frozenset(names) for container, names in grouped.items()}


def declared_registry_package_lock_rows(
    packages: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
    P1-1): every node_modules-relative row this closure's validated
    generated package-lock.json declares for a REGISTRY dependency --
    keyed by its own full `lock_path` string (e.g. "node_modules/undici",
    or "node_modules/proxy-agent/node_modules/socks" for a row nested one
    level deep) -- EXCLUDING the four locally patched packages/workspace
    assets (prime-agent and the three `@earendil-works/pi-*` packages),
    which already get their own complete, pre-npm-ci pinned digest map via
    make_patched_asset() and never need this function's own, separate
    npm-cache-derived mechanism (see verified_registry_package_tarball()).

    `lock_path` is directly usable as a release-root-relative filesystem
    path with NO further translation: npm's own lockfileVersion 3
    "packages" map keys are always exactly the directory path npm
    materializes a package at -- the same property
    declared_top_level_node_modules_packages()/
    declared_nested_node_modules_packages() already rely on to derive
    materialized directory names from this same string.

    Classifies rows IDENTICALLY to validate_generated_lock()'s own
    registry-vs-local-asset split (same `package_name_from_lock_path()`
    resolution, same `name in {"prime-agent", *WORKSPACE_PACKAGES}` test)
    so this function's notion of "a registry row" never diverges from what
    that function already validated every such row's own "resolved"/
    "integrity" fields to be: a real `https://registry.npmjs.org/...` URL
    and a well-formed `sha512-...` SRI digest. Callers of this function
    only ever run AFTER validate_generated_lock() has already succeeded
    for the exact same `packages` map, so every row this function returns
    is guaranteed to have both fields in that validated shape.
    """
    local_names = frozenset({"prime-agent", *WORKSPACE_PACKAGES})
    declared: dict[str, dict[str, Any]] = {}
    for lock_path, row in packages.items():
        if not isinstance(lock_path, str) or not lock_path.startswith("node_modules/"):
            continue
        if not isinstance(row, dict):
            continue
        name = package_name_from_lock_path(lock_path, row)
        if name in local_names:
            continue
        declared[lock_path] = row
    return declared


def verified_registry_package_tarball(
    cache: Path,
    integrity: str,
    package_label: str,
    *,
    max_bytes: int = MAX_TAR_EXPANDED_BYTES,
) -> bytes:
    """Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
    P1-1): read the exact tarball bytes npm's own local package cache
    retained for a registry dependency during THIS install's `npm ci` run
    -- from `cache`, the SAME private, this-installer-owned directory
    managed_npm_environment() already points every `run_npm()` call's own
    `npm_config_cache` at -- and independently re-verify, via a fresh
    SHA-512 computed over the SAME bytes this call is about to return, that
    they equal `integrity` (a "sha512-<base64>" SRI string, in the exact
    format validate_generated_lock() already requires every registry row
    in this pinned closure to declare).

    Concretely feasible, not merely assumed: empirically confirmed (round
    36, 2026-08-20, against a real `npm ci` run of a real registry package
    with the pinned npm 11.17.0/Node 24.19.0 toolchain) that npm's local
    cache is managed by its own `cacache` library, which stores every
    tarball it downloads at a path FULLY DETERMINED by that tarball's own
    content digest and NOTHING else -- `<cache>/_cacache/content-v2/sha512/
    <first-2-hex-chars>/<next-2-hex-chars>/<remaining-hex-chars>`, where the
    hex string is the digest already decoded from that exact package's own
    package-lock.json "integrity" field. This means the tarball for any
    registry row in the validated, pinned generated lock can be located
    with NO dependency on cacache's own separate index/bookkeeping (never
    read here at all) -- purely by decoding `integrity`, exactly as this
    function does.

    Does NOT trust cacache's own path-implies-hash addressing on its own:
    a same-UID actor with write access to this SAME private cache
    directory (same-UID threat model, unchanged from the rest of this
    file) could, in principle, plant ARBITRARY bytes at the exact path
    this function's own path computation names. The SHA-512 re-check
    below, performed over the SAME bytes this call reads and returns (not
    a second, independent re-read), is what actually defeats that -- not
    the path convention. Winning that race without also producing a
    SHA-512 SECOND PREIMAGE (bytes that genuinely hash to the exact
    `integrity` value the validated, pinned generated lock this whole
    install is already bound to declares for this row) is computationally
    infeasible. This is the SAME "verify, using the bytes already in hand,
    rather than trust-then-read-again" discipline sha256_file_verified()/
    read_private_file() already apply to every other file this installer
    reads, applied here to a tarball this installer did not itself
    download -- npm did, into a cache directory this installer created and
    privately owns -- rather than one safe_download() fetched directly.

    Deliberately mirrors safe_extract_main_asset()'s own tar-safety
    posture in its caller, registry_package_content_digests(), rather than
    inventing a second one: the same member-type/path-traversal/size
    bounds apply.

    Fails closed (never silently returns a partial or substitute result)
    if the cache path is missing (npm never downloaded this tarball for
    this platform -- see this function's own caller for why that is
    tolerated as "nothing to verify" rather than reached as an error at
    all), if what is at that path is not a private, current-uid-owned
    regular file, if it exceeds `max_bytes`, or if the freshly computed
    SHA-512 over its content does not equal `integrity`.
    """
    if not re.fullmatch(r"sha512-[A-Za-z0-9+/=]+", integrity):
        raise PrimeInstallError(
            f"{package_label} has an unsafe pinned integrity value"
        )
    try:
        expected_raw_digest = base64.b64decode(
            integrity[len("sha512-"):], validate=True
        )
    except ValueError as exc:
        raise PrimeInstallError(
            f"{package_label} has a malformed pinned integrity value"
        ) from exc
    if len(expected_raw_digest) != hashlib.sha512().digest_size:
        raise PrimeInstallError(
            f"{package_label} pinned integrity value has an unexpected digest size"
        )
    hex_digest = expected_raw_digest.hex()
    path = (
        cache
        / "_cacache"
        / "content-v2"
        / "sha512"
        / hex_digest[0:2]
        / hex_digest[2:4]
        / hex_digest[4:]
    )
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PrimeInstallError(
                f"{package_label} cached tarball is unsafe: {path}"
            )
        if info.st_size > max_bytes:
            raise PrimeInstallError(
                f"{package_label} cached tarball exceeds the approved size: {path}"
            )
        chunks: list[bytes] = []
        digest = hashlib.sha512()
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            chunks.append(chunk)
            remaining -= len(chunk)
    except FileNotFoundError as exc:
        raise PrimeInstallError(
            f"{package_label} tarball is not present in the managed npm cache "
            f"after npm ci: {path}"
        ) from exc
    except OSError as exc:
        raise PrimeInstallError(
            f"cannot read cached tarball for {package_label}: {path}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if digest.hexdigest() != hex_digest:
        raise PrimeInstallError(
            f"{package_label} cached tarball content does not match its own "
            "pinned SRI integrity"
        )
    return b"".join(chunks)


def registry_package_content_digests(raw: bytes, package_label: str) -> dict[Path, str]:
    """Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
    P1-1): derive a per-file SHA-256 content digest map from `raw` -- the
    SAME digest-verified tarball bytes verified_registry_package_tarball()
    just returned -- for every regular file it contains, keyed by its
    package-relative path (e.g. Path("dist/index.js")), mirroring
    safe_extract_main_asset()'s own posture (symlink/hardlink/device/fifo
    refusal, path-traversal and member-count/expanded-size bounds) except
    this never writes anything to disk: registry packages are already
    materialized on disk by `npm ci` itself, so this only needs the
    digests to compare that existing tree against, not a second
    extraction of it.

    Does NOT hardcode "package" as the required top-level tar directory
    name the way safe_extract_main_asset() does. That hardcoding is
    correct THERE -- this installer's own make_patched_asset() controls
    the four locally patched packages' tarballs, and confirmed, across
    many rounds, that they use it -- but it is not a general npm
    requirement: real npm's own extractor strips whichever single
    top-level path component every member happens to share, regardless of
    its name. Found by this round's OWN real, non-mocked
    tests/sandbox_e2e.py replay against the real closure, not by any
    mocked unit test: at least one real registry package in this exact
    pinned closure does not use "package" -- @types/mime-types's real
    published tarball (fetched via `npm pack @types/mime-types` and
    inspected directly with `tar tvzf`, round 36, 2026-08-20) uses
    "mime-types/", the DefinitelyTyped `types-publisher` tooling's own
    convention (the bare, unscoped package name), not plain `npm
    publish`'s default. This function instead derives the top-level
    component from the FIRST member in the archive, then requires EVERY
    member to share that exact same component -- failing closed on a
    tarball with more than one top-level directory, or a member with none
    at all, which is exactly as suspicious for a registry package as it
    would be for one of the four locally patched ones.

    Does NOT fail closed merely because two raw tar member NAMES resolve
    to the same normalized destination path (e.g. a redundant "." path
    component -- PurePosixPath collapses "package/./dist/index.js" and
    "package/dist/index.js" to the identical Path("dist/index.js") key,
    the same way real npm/tar extraction target resolution does): real,
    published npm tarballs can genuinely contain this -- reproduced for
    real (round 36, 2026-08-20): agent-base@7.1.0 through 7.1.4's real
    published tarballs, fetched via `npm pack` and inspected directly with
    Python's own tarfile module, each contain BOTH spellings, byte-for-
    byte identical. What DOES fail closed is two members resolving to the
    same destination with DIFFERENT content -- a genuine ambiguity about
    which one real extraction would keep, which this installer has no
    independent way to resolve. See the loop's own comment, at the digest
    comparison this paragraph describes, for the precise logic.
    """
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except tarfile.TarError as exc:
        raise PrimeInstallError(
            f"cannot open cached tarball for {package_label}"
        ) from exc
    content_digests: dict[Path, str] = {}
    with archive:
        members = archive.getmembers()
        if not members:
            raise PrimeInstallError(f"cached tarball for {package_label} is empty")
        if len(members) > MAX_TAR_MEMBERS:
            raise PrimeInstallError(
                f"cached tarball for {package_label} has too many members"
            )
        first_name = PurePosixPath(members[0].name)
        if first_name.is_absolute() or ".." in first_name.parts or not first_name.parts:
            raise PrimeInstallError(
                f"unsafe cached tar member for {package_label}: {members[0].name}"
            )
        top = first_name.parts[0]
        regular_bytes = 0
        for member in members:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or not pure.parts
                or pure.parts[0] != top
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or (not member.isdir() and not member.isreg())
            ):
                raise PrimeInstallError(
                    f"unsafe cached tar member for {package_label}: {member.name}"
                )
            relative = Path(*pure.parts)
            if relative == Path(top) or not member.isreg():
                continue
            key = relative.relative_to(top)
            regular_bytes += member.size
            if regular_bytes > MAX_TAR_EXPANDED_BYTES:
                raise PrimeInstallError(
                    f"cached tarball for {package_label} expands beyond the "
                    "approved limit"
                )
            source = archive.extractfile(member)
            if source is None:
                raise PrimeInstallError(
                    f"cannot read cached tar member for {package_label}: {member.name}"
                )
            digest = hashlib.sha256()
            remaining = member.size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if remaining:
                raise PrimeInstallError(
                    f"short cached tar member for {package_label}: {member.name}"
                )
            digest_hex = digest.hexdigest()
            if key in content_digests:
                if content_digests[key] != digest_hex:
                    raise PrimeInstallError(
                        f"conflicting cached tar members for {package_label} "
                        f"both resolve to {key}: {member.name}"
                    )
                # Round 36, 2026-08-20 (independent Claude opus/max
                # round-35 review, P1-1 -- found by this round's OWN real,
                # non-mocked sandbox_e2e.py replay against the real
                # closure): TWO raw tar member names that normalize (via
                # PurePosixPath, which collapses a redundant "." path
                # component the same way real npm's own extraction target
                # resolution does) to the SAME destination path are not
                # automatically an attack or a corrupt tarball -- real
                # published npm tarballs can genuinely contain this
                # (reproduced for real: agent-base@7.1.0 through 7.1.4's
                # real published tarballs, fetched and inspected directly
                # with Python's own tarfile module, each contain BOTH
                # "package/./dist/index.js" and "package/dist/index.js",
                # byte-identical, evidently an artifact of whatever tool
                # built those specific tarballs). Real npm/tar extraction
                # simply writes the same destination twice in this case
                # (last member wins; content is unchanged either way, so
                # which one "wins" does not matter). Failing closed here
                # ONLY when the two members' CONTENT actually differs
                # (checked immediately above) -- not merely when the same
                # logical destination path is named twice -- avoids
                # rejecting this real, benign package while still catching
                # a genuine ambiguity: two DIFFERENT byte sequences both
                # claiming the same destination, where the observed
                # digest could depend on npm's own extraction order this
                # installer does not control.
                continue
            content_digests[key] = digest_hex
    return content_digests


# Round 27, 2026-08-19 (independent Claude opus/max round-27 review, P2-1):
# the ONE non-directory entry a real `npm ci` -- run against this exact
# pinned lock's closure, on a real darwin-arm64 install -- writes directly at
# the top level of node_modules/: npm's own hidden installation bookkeeping
# marker, never a package. Verified empirically (not assumed): a real
# replay's node_modules/ top level, the inside of every "@scope/" directory
# it materializes, and every nested node_modules/ container it materializes,
# together contain exactly one non-directory entry in the whole tree, and it
# is this file. Any OTHER non-directory entry found anywhere a declared
# package name is expected is therefore refused outright rather than
# silently ignored, the way this file's own round-25 version of this check
# used to (`if not entry.is_dir(): continue`).
KNOWN_NON_DIRECTORY_NODE_MODULES_ENTRIES: frozenset[str] = frozenset({".package-lock.json"})

# Depth sanity bound for _walk_nested_node_modules_containers()'s iterative
# descent through materialized node_modules/ containers -- not a real
# security boundary (a same-UID attacker with write access for npm ci's
# entire runtime already has far cheaper ways to waste this installer's
# time), but cheap insurance against a pathologically deep, symlink-free
# directory chain turning a should-be-instant structural check into an
# unbounded one. Real npm output for this closure nests at most one level
# deep (empirically confirmed, round 27, 2026-08-19); 64 is generous
# headroom for any plausible future lock refresh while still being a hard
# stop well short of anything that would make this check slow.
MAX_NODE_MODULES_NESTING_DEPTH = 64


def _materialized_node_modules_directory_names(directory: Path, label: str) -> frozenset[str]:
    """The set of package directory NAMES (bare, e.g. "lru-cache", or
    "@scope/name" for a scoped package) directly materialized one level
    inside a node_modules/ directory (`directory`) -- the top-level
    RELEASE_DIR/node_modules itself, or a package's own nested node_modules/
    -- computed identically either way, so
    assert_materialized_node_modules_matches_lock()'s top-level and nested
    checks can never independently drift in what counts as a legitimate
    entry.

    Fails closed -- rather than silently skipping, as this file's own
    round-25 version of this logic did for the top level -- on:
      * a symlink anywhere a package name, ".bin", or the npm bookkeeping
        marker is expected;
      * a non-directory entry where a declared package name is expected,
        UNLESS its name is in KNOWN_NON_DIRECTORY_NODE_MODULES_ENTRIES
        (round 27, 2026-08-19, P2-1: the round-25 version's
        `if not entry.is_dir(): continue` silently dropped a planted
        top-level FILE from the "materialized" set entirely, so it could
        never appear in the unexpected-entries diff -- a same-named ".js"
        file placed beside a real package directory, e.g.
        node_modules/some-package.js, went completely uncaught, and Node's
        own CJS LOAD_AS_FILE(DIR/X) resolution precedes
        LOAD_AS_DIRECTORY(DIR/X), so such a file actually shadows the real
        package for any package lacking an "exports" field -- true for the
        majority of this closure's declared packages, confirmed against the
        pinned Node build actually used here);
      * a non-directory entry, or a further symlink, inside an "@scope"
        directory.
    """
    names: set[str] = set()
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise PrimeInstallError(f"cannot inspect materialized {label}") from exc
    for entry in entries:
        name = entry.name
        if entry.is_symlink():
            raise PrimeInstallError(f"materialized {label} entry is a symlink: {name}")
        if not entry.is_dir():
            if name in KNOWN_NON_DIRECTORY_NODE_MODULES_ENTRIES:
                continue
            raise PrimeInstallError(
                f"materialized {label} contains an unexpected non-directory entry "
                f"where a declared package name was expected: {name}"
            )
        if name == ".bin":
            continue
        if name.startswith("@"):
            try:
                scoped_entries = sorted(entry.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise PrimeInstallError(f"cannot inspect materialized {label}") from exc
            for scoped_entry in scoped_entries:
                scoped_name = scoped_entry.name
                if scoped_entry.is_symlink():
                    raise PrimeInstallError(
                        f"materialized {label} entry is a symlink: {name}/{scoped_name}"
                    )
                if not scoped_entry.is_dir():
                    raise PrimeInstallError(
                        f"materialized {label} contains an unexpected non-directory "
                        f"entry inside a scope where a declared package name was "
                        f"expected: {name}/{scoped_name}"
                    )
                names.add(f"{name}/{scoped_name}")
            continue
        names.add(name)
    return frozenset(names)


def _find_nested_node_modules_containers(
    package_dir: Path, *, depth_budget: int
) -> list[tuple[str, Path]]:
    """Find every directory literally named "node_modules" ANYWHERE under
    `package_dir` -- not only directly at `package_dir`'s own root -- by
    walking its full subtree, without descending PAST a node_modules
    directory once found (that container's own contents are the caller's
    concern, via its own queue entry, not this function's) and without
    following any symlinked directory while searching. Returns
    (relative_posix_path, resolved_path) pairs; `relative_posix_path` is
    `package_dir`-relative and always ends in "node_modules" (e.g.
    "node_modules" for a direct child, "lib/node_modules" for one nested
    under a subdirectory of `package_dir`).

    Round 29, 2026-08-19 (independent Claude opus/max round-29 review,
    P2-1): the previous implementation (inlined in
    _walk_nested_node_modules_containers(), before this function existed)
    checked only `package_dir / "node_modules"` -- a package's own
    ROOT-level nested container -- which is the only shape a real
    package-lock.json can ever DECLARE (npm always nests a further
    node_modules/ directly under a dependency's own package root, never
    under an arbitrary subdirectory of it), but is NOT the only shape a
    same-UID attacker can PLANT: a node_modules/ directory placed at
    `<package>/<subdir>/node_modules` was invisible to a root-only check,
    yet real Node's own CJS module resolution (verified against the pinned
    Node build this installer uses) walks up starting from the REQUIRING
    file's own directory, so a subdirectory-anchored node_modules/ is
    actually resolved BEFORE any hoisted copy higher up -- a real
    resolution-order gap, not merely a completeness nitpick. Because a
    real package-lock.json can never legitimately declare a container at
    this shape, declared_nested_node_modules_packages() will never have an
    entry for one, so extending the walk to find it produces zero false
    positives against any real, legitimate install (empirically confirmed:
    the real materialized tree for this closure has zero such
    containers -- any package materialized inside one is therefore
    necessarily undeclared and fails closed).
    """
    found: list[tuple[str, Path]] = []
    stack: list[tuple[Path, str, int]] = [(package_dir, "", 0)]
    while stack:
        directory, prefix, depth = stack.pop()
        if depth > depth_budget:
            raise PrimeInstallError(
                "materialized package tree nests deeper than this installer "
                f"will inspect: {package_dir}"
            )
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise PrimeInstallError(
                f"cannot inspect materialized package tree: {directory}"
            ) from exc
        for entry in entries:
            if entry.is_symlink():
                if entry.name == "node_modules":
                    raise PrimeInstallError(
                        "materialized node_modules entry is a symlink: "
                        f"{prefix}{entry.name}"
                    )
                # Not followed, and not itself named "node_modules" --
                # out of scope for this function (which only looks for
                # node_modules/ containers), same as
                # _materialized_node_modules_directory_names() leaving
                # arbitrary non-node_modules-related symlinks outside a
                # package's own declared entries unexamined.
                continue
            if not entry.is_dir():
                continue
            relative = f"{prefix}{entry.name}"
            if entry.name == "node_modules":
                found.append((relative, entry))
                continue
            stack.append((entry, f"{relative}/", depth + 1))
    return found


def _walk_nested_node_modules_containers(
    node_modules_root: Path,
) -> list[tuple[str, frozenset[str]]]:
    """Discover every node_modules/ CONTAINER reachable by descending
    through materialized package directories starting at `node_modules_root`
    -- the top level itself, plus every package's own nested node_modules/
    at ANY depth AND any subdirectory anchoring within a package's own
    tree (round 29, 2026-08-19, P2-1 fix; see
    _find_nested_node_modules_containers()) -- and return one
    (container_path, materialized_names) pair per container found,
    INCLUDING the top level as the first entry. `container_path` uses the
    SAME lock-relative string convention declared_nested_node_modules_packages()
    derives from package-lock.json for a ROOT-anchored container (e.g.
    "node_modules" for the top level,
    "node_modules/proxy-agent/node_modules" for a container nested one level
    inside proxy-agent's own package directory, directly at its root), so a
    caller can compare each entry directly against that function's return
    value with no further translation -- and, for a subdirectory-anchored
    container a real lock could never declare (e.g.
    "node_modules/proxy-agent/lib/node_modules"), the same lookup simply
    finds nothing declared, which is exactly the fail-closed behavior a
    same-UID plant at that shape needs.

    Iterative (an explicit work queue, not recursion) and depth-bounded via
    MAX_NODE_MODULES_NESTING_DEPTH, so a pathologically deep, symlink-free
    directory chain fails closed with a clear error instead of an unbounded
    walk or a Python RecursionError (round 27, 2026-08-19, P2-2 fix;
    see that constant's own comment). The per-package subtree search
    _find_nested_node_modules_containers() performs is bounded the same
    way, independently, via its own `depth_budget`.
    """
    results: list[tuple[str, frozenset[str]]] = []
    queue: list[tuple[Path, str, int]] = [(node_modules_root, "node_modules", 0)]
    while queue:
        directory, container_path, depth = queue.pop()
        if depth > MAX_NODE_MODULES_NESTING_DEPTH:
            raise PrimeInstallError(
                "materialized node_modules/ nests deeper than this installer "
                f"will inspect: {container_path}"
            )
        names = _materialized_node_modules_directory_names(directory, container_path)
        results.append((container_path, names))
        for name in names:
            package_dir = directory / name
            for relative, nested_path in _find_nested_node_modules_containers(
                package_dir, depth_budget=MAX_NODE_MODULES_NESTING_DEPTH
            ):
                queue.append(
                    (nested_path, f"{container_path}/{name}/{relative}", depth + 1)
                )
    return results


# npm's own lockfileVersion 3 "packages" rows spell the platform/architecture
# an "os"/"cpu" constraint restricts a row to using Node's own
# process.platform / process.arch strings -- "darwin" / "arm64" for this
# installer's only supported target (preflight() already refuses to run at
# all on anything else). Named constants, not inlined string literals in
# _lock_row_platform_excludes_current_target() below, so both this file's
# own single supported-platform check (preflight()) and the lock-row
# constraint check stay obviously in sync if a future round ever needs to
# read them side by side.
NPM_LOCK_CURRENT_OS = "darwin"
NPM_LOCK_CURRENT_CPU = "arm64"


def _npm_lock_platform_list_excludes(values: Any, current: str) -> bool:
    """Whether a single "os" or "cpu" array from a package-lock.json row
    (npm's own convention: bare entries are an allow-list, "!"-prefixed
    entries are a block-list, and the two forms are not mixed in a single
    well-formed array) excludes `current` -- mirrors npm's own
    checkPlatform()/checkCpu() semantics closely enough for this
    installer's one purpose (justifying an otherwise-unexplained absence
    from the materialized tree; see
    assert_materialized_node_modules_matches_lock()'s own round-38
    paragraph), without needing to reproduce npm's full validation of the
    array's own well-formedness -- a malformed array here simply excludes
    nothing, which fails this installer CLOSED (the row's absence stays
    unjustified) rather than open.
    """
    if not isinstance(values, list) or not values:
        return False
    allowed = [
        value for value in values if isinstance(value, str) and value and value[0] != "!"
    ]
    blocked = [
        value[1:]
        for value in values
        if isinstance(value, str) and len(value) > 1 and value[0] == "!"
    ]
    if allowed and current not in allowed:
        return True
    return current in blocked


def lock_row_platform_excludes_current_target(row: Any) -> bool:
    """Whether `row` (a package-lock.json "packages" map entry) declares an
    "os" or "cpu" constraint that excludes this installer's one supported
    target (darwin/arm64) -- i.e. whether `npm ci` was SUPPOSED to skip
    materializing this row here, as opposed to silently failing to install
    something it should have. Used by
    assert_materialized_node_modules_matches_lock()'s round-38
    reverse-direction check (below) to distinguish a legitimately
    platform-skipped declared row from a genuinely missing one; see that
    function's own docstring for the full finding this closes.
    """
    if not isinstance(row, dict):
        return False
    return _npm_lock_platform_list_excludes(
        row.get("os"), NPM_LOCK_CURRENT_OS
    ) or _npm_lock_platform_list_excludes(row.get("cpu"), NPM_LOCK_CURRENT_CPU)


def assert_materialized_node_modules_matches_lock(
    release_dir: Path,
    declared_top_level: frozenset[str],
    declared_nested: dict[str, frozenset[str]],
    *,
    packages: dict[str, Any] | None = None,
) -> None:
    """Round 25, 2026-08-19 (independent Claude opus/max round-25 review,
    P1-B): `npm ci`'s own runtime is an external subprocess window this
    installer does not control -- measured in a real install at several
    minutes -- and a same-UID local attacker with filesystem write access
    for that entire duration can plant an ENTIRELY NEW, undeclared package
    directory directly under RELEASE_DIR/node_modules/ (a "sibling
    module") without ever touching RELEASE_DIR's own directory entry, so
    none of assert_release_dir_identity()/assert_release_dir_fd_identity()'s
    several call sites in _install_locked_within_release_dir() raise, and
    the install would otherwise complete with the planted package
    published as if it were part of the verified closure.

    This is a deliberately NARROWER mitigation than a full, independent
    re-verification of every installed file's content against
    package-lock.json's own SRI "integrity" hashes: those hashes are
    per-TARBALL, not per-file, and are not practically re-derivable from
    an already-extracted directory tree without re-creating
    registry-identical tar bytes (ordering, metadata, and compression all
    matter to the hash), which this installer cannot do without
    duplicating -- and re-trusting -- the same network fetch npm's own
    `--ci` integrity checking already performs. That fuller approach was
    judged out of scope for this round; what this function does instead:
    compare the SET of directory NAMES actually materialized under
    node_modules/ -- at the top level (including top-level-scoped
    directories) AND inside every nested node_modules/ container reachable
    by descending through those directories, at any depth
    (_walk_nested_node_modules_containers()) -- against `declared_top_level`
    and `declared_nested`, the sets declared_top_level_node_modules_packages()
    and declared_nested_node_modules_packages() derived from the SAME lock
    file content this entire install is already pinned to, and fail closed
    if npm materialized anything, at any level, not in the corresponding
    declared set. This directly catches the "planted sibling module" repro
    above, because a new package directory necessarily has a name absent
    from the verified lock -- and, as of round 27 (2026-08-19, P2-1/P2-2),
    catches the SAME attack one directory deeper (inside a package's own
    node_modules/) or disguised as a non-directory entry at a position a
    package name is expected, both of which the round-25/26 version of this
    check missed entirely (see _materialized_node_modules_directory_names()
    and _walk_nested_node_modules_containers() for exactly how).

    Deliberately NOT checked, and an ACCEPTED RESIDUAL of the same-UID
    threat model (documented here rather than silently left uncovered, per
    this file's existing convention for accepted residuals -- see e.g.
    run_npm()'s own docstring for the analogous exec-time TOCTOU on
    NODE/CLI paths, or managed_launch_guard_script()'s validate_exec_target()
    docstring for the same class of residual applied to future invocations):
    a same-UID attacker who instead overwrites the CONTENT of a package
    directory that IS already declared in the lock (rather than adding a
    new one) is NOT caught by this structural, directory-presence-only
    check. As of round 27 (2026-08-19, P1 fix), that residual is CLOSED for
    all four locally patched packages (prime-agent and the three
    `@earendil-works/pi-*` workspace assets) -- every file of every one of
    those four packages is verified, immediately after this check runs, by
    assert_locally_patched_package_matches_pinned_digests() against the
    original, pre-npm-ci, tarball-derived digest map make_patched_asset()
    already computed for each. Through round 34, it was NOT closed for the
    content of any of this closure's ~196 third-party REGISTRY dependency
    packages' own files -- see the round-36 paragraph below for why that is
    no longer the complete picture, and exactly what changed.

    Round 29, 2026-08-19 (independent Claude opus/max round-29 review,
    documentation finding): an earlier revision of this paragraph cited
    `npm ci`'s own registry-integrity/SRI checking as covering that
    residual -- that MISATTRIBUTES the protection. npm's SRI check
    operates at download/extract time, over the downloaded tarball's own
    bytes, before anything is ever written into this installer's private
    tree; it provides NO protection whatsoever against a same-UID local
    attacker tampering a file's content AFTER extraction, on disk, under
    this installer's own UID -- which is this file's entire stated threat
    model everywhere else (see e.g. run_npm()'s own docstring, or
    capture_private_ssd_asset_digest()'s). At the time this paragraph was
    written, there was nothing in this installer, and nothing in npm's own
    SRI checking, that mitigated same-UID post-extraction content tampering
    of these ~196 packages' files -- an accepted, unmitigated gap given
    this project's practical scope constraints at the time, not a covered
    case. Be precise about scope in any future review, matching the lesson
    of rounds 25, 27, and 29's own findings: an inaccurate "covered" claim
    here is itself the kind of bug that lets real gaps go undetected --
    when in doubt, understate coverage rather than overstate it.

    Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
    P1-1 and P1-2b): TWO separate fixes, both applied here:

    P1-2b: this function used to be called exactly ONCE, before the
    node_modules move (see _install_locked_within_release_dir()'s own
    comments at each call site) -- despite the paragraph above already
    claiming "a same-UID attacker who instead overwrites the CONTENT..."
    as the only residual, which implicitly (and, for this specific case,
    incorrectly) implied the STRUCTURAL check itself covered the whole
    remaining window. A same-UID racer who plants an undeclared sibling
    package directly under lib/node_modules/ AFTER the move -- during the
    real window this function spans (the move itself, "bin" mkdir, the
    entrypoint chmod, launch guard/wrapper generation) -- went completely
    undetected: this function was never called again to look at the
    POST-MOVE tree at all. Reproduced end-to-end through the real
    install(): planting a sibling package the instant the launch guard is
    published (after the move, well after this function's one and only
    pre-fix call) let install() complete and verify() report ok:true. Fixed
    by calling this function a SECOND time, against the post-move tree
    (release_dir = RELEASE_DIR/"lib", so release_dir/"node_modules" is the
    real global_root), at the same point _install_locked_within_release_dir()
    already re-runs the per-package content digest sweep a second time
    post-move (the round-29/30 pattern for the four locally patched
    packages, applied here to this function too).

    P1-1: the residual described above -- registry dependency packages'
    own file CONTENT was completely unverified -- is now substantially
    closed, not merely re-documented. `_install_locked_within_release_dir()`
    derives a complete, independently-trustworthy per-file digest map for
    every registry package `npm ci` actually materializes on this
    platform, from npm's OWN local package cache (the same
    `npm_config_cache` directory managed_npm_environment() already points
    every `run_npm()` call at) -- see verified_registry_package_tarball()'s
    own docstring for exactly how that cache is read and independently
    re-verified (never merely trusted because of where it sits), and
    registry_package_content_digests() for how a per-file digest map is
    derived from it the same way make_patched_asset() already derives one
    for the four locally patched packages' own tarballs. Those maps are
    then verified against the actual on-disk tree via the SAME
    assert_locally_patched_package_matches_pinned_digests() sweep the four
    locally patched packages already get (pre-move AND post-move), and
    folded into tree_digest()'s `pinned_relative_digests` so the
    zero-install-time-window guarantee that mechanism already provides
    extends to these packages too. What remains an accepted, documented
    residual after this round -- see
    _install_locked_within_release_dir()'s own comment where
    `release_relative_pinned_digests` is assembled for the complete,
    precise statement -- is narrower than "the whole ~196-package registry
    closure is unmitigated": it is now limited to the handful of declared
    registry rows this platform's `npm ci` never downloads at all (a
    platform-specific optional dependency skipped for darwin-arm64, which
    therefore has no content on disk to tamper in the first place) and the
    same microsecond-scale, in-process open/lstat gap already accepted for
    every OTHER pinned path in this file.

    Round 38, 2026-08-20 (independent Claude opus/max round-37 review,
    part of the P1-1 fix): the paragraph above says a declared registry
    row this platform's `npm ci` never downloads "has no content on disk
    to tamper in the first place" -- true only as long as nothing else
    ever plants content at that exact declared-but-never-materialized
    path. Round 37 found that untrue in practice: the walk above only
    ever checks the MATERIALIZED-but-undeclared direction (a directory
    present on disk with no matching declared row); it never checked the
    converse (a declared row absent from the materialized tree), so a
    same-UID racer planting a directory at one of those never-materialized
    declared paths went completely unnoticed by this function -- and,
    before this round's separate tree_digest() deny-unknown fix, by
    anything else either. `packages`, when given, enables that converse
    check: for every declared top-level or nested entry NOT found in the
    materialized tree, look up its row in `packages` by lock_path and, if
    found, require its OWN "os"/"cpu" constraints (see
    lock_row_platform_excludes_current_target()) to justify the absence as
    a genuine, expected platform skip -- an absence with no such
    justification fails this call closed instead of being silently
    tolerated the way every prior round tolerated it. `packages` is NOT
    the full generated package-lock.json "packages" map -- callers must
    scope it to REGISTRY rows only (declared_registry_package_lock_rows()'s
    return value; both real call sites in
    `_install_locked_within_release_dir()`, pre- and post-move, pass
    exactly that), excluding the four locally patched/workspace packages
    on purpose: those are never platform-conditional, are already governed
    by their own separate, unconditional presence checks elsewhere in this
    file, and -- for a synthetic test lock -- may legitimately use a
    lock_path that does not match this function's own name-based
    resolution (see package_name_from_lock_path()'s own docstring for the
    one real-world case they can genuinely diverge in). A lock_path this
    function looks up that is absent from `packages` (a local/workspace
    asset, or any row the caller did not choose to scope this check to) is
    therefore treated as nothing to verify here, not as an unjustified
    absence. `packages` defaults to None so this check is opt-in entirely:
    every existing caller/test that does not supply it is unaffected. This
    is independent, narrower defense-in-depth alongside tree_digest()'s
    own round-38 deny-unknown
    fix (which structurally closes the same exploitation path a different
    way -- see that function's own docstring): either mechanism alone
    already refuses the same-UID plant this paragraph describes.

    Round 41, 2026-08-20 (independent Codex sol/max round-39 review, P1-C):
    the round-38 reverse-direction nested check above used to treat "this
    container's own `container_path` is absent from `materialized_by_container`"
    as ONE case -- the container's parent package directory was never
    materialized -- and skip it unconditionally, deferring to the
    top-level (or an ancestor nested) check to justify the parent's own
    absence. That conflated two genuinely different situations:
    `_walk_nested_node_modules_containers()` never queues a nested
    container's `container_path` at all when its own parent package
    directory was never materialized (nothing to look inside), but it
    ALSO never queues one when the parent package directory IS
    materialized yet simply has no "node_modules" subdirectory of its own
    -- `_find_nested_node_modules_containers()` finds nothing under it, so
    zero queue entries are added either way. Both are therefore
    indistinguishable from `containers`' own shape alone. Reproduced
    exactly as Codex described: a full installer-orchestration fixture
    with node_modules/prime-agent materialized and verified, its declared
    node_modules/prime-agent/node_modules/<child> row present and NOT
    platform-excluded, but no node_modules/prime-agent/node_modules
    directory on disk at all -- both this function's structural calls
    skipped the gap entirely, the pin builder marked the child skipped,
    the post-move check found no directory to complain about, and
    tree_digest()'s deny-unknown had no materialized path to even see, so
    the install reached finalize() with a genuinely missing, declared,
    non-platform-excluded dependency and nothing anywhere caught it.

    Fixed by disambiguating the two cases below using
    `materialized_package_lock_paths` -- built from the SAME `containers`
    list this function already collected via the one real filesystem walk
    above (no second walk, no new filesystem access): a package is a
    member of that set if and only if its own containing container was
    actually walked, i.e. it is genuinely present and already
    structurally verified by the diff loop earlier in this function. When
    `container_path` is absent from `materialized_by_container`, its own
    parent package's lock_path (`container_path` with the trailing
    "/node_modules" removed -- always well-formed, since
    declared_nested_node_modules_packages() only ever produces a
    container_path of the form "<parent_lock_path>/node_modules") is
    looked up in `materialized_package_lock_paths`: absent means the
    parent itself was never materialized (the pre-round-41 skip still
    applies, unchanged, for exactly the same reason given in the comment
    at that `continue`); present means the parent IS materialized but its
    own nested node_modules/ container is not, in which case every one of
    that container's declared children is treated as missing and, unless
    individually justified by its own row's os/cpu platform exclusion
    (the identical `lock_row_platform_excludes_current_target()` mechanism
    the top-level and already-walked-nested cases use), fails this call
    closed. Because `materialized_package_lock_paths` is assembled from
    EVERY entry of `containers` (the top level and every container
    actually reached, at any depth `_walk_nested_node_modules_containers()`
    walked), this resolves correctly at arbitrary nesting depth -- a
    doubly-nested child's own container being absent is diagnosed the same
    way, one level deeper, with no special-casing for exactly one level of
    nesting.
    """
    node_modules_root = release_dir / "node_modules"
    if node_modules_root.is_symlink() or not node_modules_root.is_dir():
        raise PrimeInstallError("npm did not materialize a private node_modules directory")
    containers = _walk_nested_node_modules_containers(node_modules_root)
    _, top_names = containers[0]
    materialized_top_level = {f"node_modules/{name}" for name in top_names}
    unexpected_top = sorted(materialized_top_level - declared_top_level)
    if unexpected_top:
        raise PrimeInstallError(
            "npm materialized undeclared node_modules package(s) not present "
            f"in the verified lock: {unexpected_top}"
        )
    for container_path, names in containers[1:]:
        declared_names = declared_nested.get(container_path, frozenset())
        unexpected_nested = sorted(names - declared_names)
        if unexpected_nested:
            raise PrimeInstallError(
                "npm materialized undeclared nested node_modules package(s) not "
                f"present in the verified lock inside {container_path}: {unexpected_nested}"
            )
    if packages is None:
        return
    # Round 38, 2026-08-20 (P1-1, reverse direction): every declared entry
    # in `packages` not found in the materialized set above must be
    # justified by its own lock row's os/cpu platform constraints -- see
    # this function's own round-38 docstring paragraph. `packages` is
    # deliberately scoped by the caller to REGISTRY rows only (see that
    # paragraph for why) -- `row = packages.get(lock_path)` returning None
    # therefore means "not a row this check tracks" (a local/workspace
    # asset, governed by its own separate, unconditional presence checks
    # elsewhere in this file, or a row this function's own name resolution
    # cannot map back to `packages`'s literal keys) and is treated as
    # nothing to verify here, NOT as an unjustified absence -- only a row
    # this dict actually contains, that is both absent from the
    # materialized tree AND not platform-excluded, fails closed.
    missing_top = sorted(declared_top_level - materialized_top_level)
    for lock_path in missing_top:
        row = packages.get(lock_path)
        if row is not None and not lock_row_platform_excludes_current_target(row):
            raise PrimeInstallError(
                "declared node_modules package is missing from the materialized "
                "tree and is not justified by an os/cpu platform constraint on "
                f"its own lock row: {lock_path}"
            )
    materialized_by_container = {
        container_path: names for container_path, names in containers[1:]
    }
    # Round 41, 2026-08-20 (P1-C): every package genuinely materialized at
    # ANY depth this walk actually reached, keyed the same lock_path-style
    # string convention as `declared_top_level`/`declared_nested`'s own
    # keys -- built purely from `containers` (already collected above via
    # the one real filesystem walk this function performs; no second walk
    # here). Used below to tell "this container's own parent package was
    # never materialized at all" apart from "the parent IS materialized
    # but its own nested node_modules/ container is absent" -- see this
    # function's own round-41 docstring paragraph for why `containers`'
    # shape alone cannot distinguish the two.
    materialized_package_lock_paths = {
        f"{container_path}/{name}"
        for container_path, names in containers
        for name in names
    }
    for container_path, declared_names in declared_nested.items():
        if container_path not in materialized_by_container:
            # This container's own parent package's lock_path -- always
            # well-formed here, since declared_nested_node_modules_packages()
            # only ever produces a container_path of the form
            # "<parent_lock_path>/node_modules".
            parent_lock_path = container_path[: -len("/node_modules")]
            if parent_lock_path not in materialized_package_lock_paths:
                # The parent package directory itself was never
                # materialized -- if that parent's own absence is
                # unjustified, the top-level (or an ancestor nested) check
                # above already catches IT; nothing here can independently
                # justify a specific child of a container whose own parent
                # does not exist, and treating "parent legitimately
                # platform-skipped" as implying "every declared child of
                # its nested node_modules is ALSO individually
                # platform-excluded" would not generally be true, so this
                # is deliberately skipped rather than guessed at.
                continue
            # Round 41 (P1-C): the parent package IS materialized (present
            # on disk, already verified by the diff loop above) -- its own
            # nested node_modules/ container is simply absent, even though
            # the lock declares children for it. None of those declared
            # children can possibly be materialized (the container holding
            # them does not exist at all), so every one is treated as
            # missing here, subject to the identical os/cpu platform-
            # exclusion justification the already-walked-container branch
            # below applies.
            for name in sorted(declared_names):
                lock_path = f"{container_path}/{name}"
                row = packages.get(lock_path)
                if row is not None and not lock_row_platform_excludes_current_target(row):
                    raise PrimeInstallError(
                        "declared nested node_modules package's own parent "
                        "container is absent from the materialized tree "
                        "even though its parent package is present, and is "
                        "not justified by an os/cpu platform constraint on "
                        f"its own lock row: {lock_path}"
                    )
            continue
        materialized_names = materialized_by_container[container_path]
        missing_nested = sorted(declared_names - materialized_names)
        for name in missing_nested:
            lock_path = f"{container_path}/{name}"
            row = packages.get(lock_path)
            if row is not None and not lock_row_platform_excludes_current_target(row):
                raise PrimeInstallError(
                    "declared nested node_modules package is missing from the "
                    "materialized tree and is not justified by an os/cpu "
                    f"platform constraint on its own lock row: {lock_path}"
                )


def assert_locally_patched_package_matches_pinned_digests(
    package_dir: Path, pinned_digests: dict[Path, str], package_label: str
) -> None:
    """Round 27, 2026-08-19 (independent Claude opus/max round-27 review,
    P1): verify EVERY file `npm ci` materialized under one of the four
    locally patched packages' own installed directory -- not merely its
    entrypoint -- against `pinned_digests`, the SAME per-file digest map
    make_patched_asset() (via safe_extract_main_asset()) already captured
    directly from the digest-verified ORIGINAL tarball's bytes as they were
    streamed to disk, strictly BEFORE `npm ci` (or anything else) ever ran.

    Round 25/26 only ever pinned ONE entry out of this map --
    dist/bundle/cli.js, prime-agent's own ESM forwarding stub -- even though
    the installer already computed a complete, per-file digest for every one
    of this closure's 1,739 files across all four locally patched packages
    (prime-agent, @earendil-works/pi-ai, @earendil-works/pi-tui,
    @earendil-works/pi-agent-core). cli.js is 0.013% of prime-agent's own
    dist/bundle/ directory (39 files, 13,804,347 bytes) and both statically
    and dynamically imports the REST of that directory's content
    unconditionally on every invocation -- none of which round 25/26
    covered: assert_materialized_node_modules_matches_lock() only ever
    checked directory PRESENCE, never a declared package's own contents, and
    receipt['release_tree_sha256'] is recorded AFTER this point in the
    install, so it cannot detect tampering that already happened before it
    runs. Reproduced: tampering with a sibling chunk file next to a genuine,
    correctly-pinned cli.js, during npm ci's own window, previously
    completed install with a clean launch guard and a clean receipt --
    permanent RCE on every future managed invocation.

    Fails closed on:
      * content mismatch for any pinned file (a same-UID swap of ANY file
        in ANY of the four locally patched packages, not only the
        entrypoint);
      * a pinned file missing from disk after `npm ci` (npm silently
        dropped, or never wrote, something the original, digest-verified
        tarball declared);
      * an extra, undeclared file present anywhere in the package's own
        directory tree that is not in `pinned_digests` -- caught even if it
        sits deeper than this package's own top level, since the walk below
        recurses through every subdirectory EXCEPT one named "node_modules"
        (round 36, 2026-08-20, P1-1 fix -- see that round's comment at the
        walk's own node_modules-skip below for why: a nested node_modules/
        belongs to a DIFFERENT declared package with its OWN separate call
        to this function, not to `package_dir`'s own pinned map, and an
        UNDECLARED one is still caught, unconditionally, by
        assert_materialized_node_modules_matches_lock()'s own nested check,
        run both before and after every call to this function);
      * a symlink, or any other non-regular-file entry, anywhere in the
        tree (the original tarball this digest map was built from contains
        no symlinks at all -- safe_extract_main_asset() itself refuses any
        tar member that is a symlink, hardlink, device, or fifo -- so a
        symlink materialized here can only be something `npm ci` or a
        same-UID racer added, never something this map ever pinned).

    Called once per locally patched package, immediately after `npm ci`
    returns and after assert_materialized_node_modules_matches_lock()'s own
    structural check, for all four packages -- including the three
    `@earendil-works/pi-*` workspace assets this file's own prior
    documentation incorrectly implied were out of scope. Called AGAIN,
    against each package's post-move path (inside lib/node_modules/),
    immediately before write_pending_install() -- round 29, 2026-08-19,
    P1-1 fix; see that call site's own comment for why this narrows,
    though does not by itself zero out, the window between this content
    check and write_pending_install()'s own tree_digest() call, which
    additionally compares every one of these same files' content directly
    against this SAME pinned baseline as part of computing the recorded
    release-tree digest (see tree_digest()'s `pinned_relative_digests`
    parameter). Through round 34, what remained an accepted, UNMITIGATED
    residual was third-party REGISTRY dependency packages' own file
    content -- as of round 36 that is substantially closed too (this
    function is now called for those packages as well, pre- and post-move,
    the identical way); see assert_materialized_node_modules_matches_lock()'s
    own docstring for the complete, precise, currently-accurate statement
    of scope, and why "npm's own SRI checking covers it" (an earlier
    revision of this paragraph's own claim) was itself an inaccurate
    over-claim, not merely an incomplete one.

    Content digests here are computed via sha256_file_verified(), not the
    plain sha256_file() this function used through round 28: sha256_file()
    opens `entry_path` by a SECOND, independent, symlink-following
    filesystem resolution after the os.scandir()-based checks just above
    already ran -- a same-UID racer who swaps a pinned regular file for a
    symlink in that exact window has the swap silently followed and,
    if the symlink's target happens to match the pinned digest at that
    moment, accepted; the target (or the symlink itself) can then be
    changed again afterward with nothing left to catch it (independent
    Claude opus/max round-29 review, 2026-08-19, P2-2). sha256_file_verified()
    performs its own O_NOFOLLOW-protected open and fstat-identity check as
    part of the SAME call, so this class of swap is refused outright
    regardless of what the scandir-based checks above already observed;
    those checks are kept for their clearer, entry-type-specific error
    messages, not because they are still what makes this safe.

    The existing entrypoint-specific digest capture and re-verification in
    _install_locked_within_release_dir() (round 24/25's
    capture_private_ssd_asset_digest()/verify_unchanged_private_ssd_asset_digest()
    calls on dist/bundle/cli.js) is intentionally left in place rather than
    removed now that this function subsumes its content check: it also
    captures the (st_dev, st_ino) IDENTITY this installer needs to re-verify
    cli.js specifically through the node_modules move and into the launch
    guard's permanent baked-in trust anchor, which is a distinct concern
    this function does not address. Its own content-digest comparison is
    therefore now redundant with this function's broader sweep -- but
    harmlessly so, not a second, divergent mechanism checking the same thing
    two different ways: both compare the same on-disk bytes against entries
    of the exact same pinned `main_content_digests` map.

    `pinned_digests` may legitimately be empty -- ONLY as a test double's
    stand-in for a locally patched package a specific test does not
    exercise; a REAL make_patched_asset() call always returns at least one
    entry (package.json, at minimum, per safe_extract_main_asset()'s own
    manifest/digest self-consistency invariant), so an empty map can never
    happen for a genuine install. An empty map means "nothing pinned for
    this package by this caller" and is treated as nothing to verify, rather
    than requiring `package_dir` to exist -- there is no real content this
    call could compare against either way.

    Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
    P1-1): despite this function's own name, it is, and always was, generic
    over ANY package tree plus ANY independently-derived per-file digest
    map for it -- nothing below is specific to the four locally patched
    packages. As of this round it is ALSO called, unchanged, for every
    registry dependency package `npm ci` materializes on this platform,
    with `pinned_digests` derived from npm's own local cache instead of a
    directly downloaded tarball -- see
    verified_registry_package_tarball()/registry_package_content_digests()
    and _install_locked_within_release_dir()'s own call sites. The function
    was deliberately NOT renamed: every fail-closed property documented
    above (content mismatch, missing pinned file, undeclared extra file,
    any symlink/non-regular entry) applies identically regardless of which
    kind of package tree `package_dir` names, and renaming risked
    introducing a transcription error across this function's own
    extensive, cross-referenced history above for no behavioral benefit.
    """
    if not pinned_digests:
        return
    if package_dir.is_symlink() or not package_dir.is_dir():
        raise PrimeInstallError(f"{package_label} package directory is missing after npm ci")
    observed: set[Path] = set()
    stack: list[Path] = [package_dir]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError as exc:
            raise PrimeInstallError(
                f"cannot inspect {package_label} package tree after npm ci"
            ) from exc
        for entry in entries:
            entry_path = Path(entry.path)
            relative = entry_path.relative_to(package_dir)
            if entry.is_symlink():
                raise PrimeInstallError(
                    f"{package_label} materialized an unexpected symlink: {relative}"
                )
            if entry.is_dir(follow_symlinks=False):
                if entry.name == "node_modules":
                    # Round 36, 2026-08-20 (independent Claude opus/max
                    # round-35 review, P1-1 -- found by this round's OWN
                    # real, non-mocked sandbox_e2e.py replay against the
                    # real closure, not by any mocked unit test): a nested
                    # node_modules/ directory belongs to a DIFFERENT
                    # declared package (or several) -- real npm legitimately
                    # nests a private copy of a transitive dependency under
                    # a package's own node_modules/ whenever a hoisting
                    # conflict requires it (reproduced for real:
                    # node_modules/@aws-sdk/credential-provider-sso/
                    # node_modules/@aws-sdk/token-providers, in a real
                    # darwin-arm64 npm ci replay of this exact pinned
                    # lock). `pinned_digests` here is `package_dir`'s OWN
                    # tarball-declared members only -- it was never
                    # supposed to, and structurally cannot, also cover a
                    # DIFFERENT package's files. Each such nested package is
                    # its own separate row in the lock (see
                    # declared_registry_package_lock_rows(), whose
                    # `lock_path` keys already include nested paths like the
                    # one above) and gets its OWN separate call to this
                    # exact function, against its OWN pinned digest map --
                    # so skipping descent here loses no coverage, it avoids
                    # a double (and wrongly-scoped) scan of an area another
                    # call already verifies correctly. An UNDECLARED nested
                    # container, or a stray non-directory entry planted
                    # directly inside one, is still caught regardless of
                    # this skip: assert_materialized_node_modules_matches_lock()
                    # walks every "node_modules"-named directory at any
                    # depth (subdirectory-anchored or not --
                    # _find_nested_node_modules_containers()) and is run,
                    # unconditionally, both immediately before and
                    # immediately after every call to this function (pre-
                    # and post-move). This assumes -- as
                    # safe_extract_main_asset()'s own "package/"-top-level
                    # requirement already does -- that no REAL, published
                    # npm tarball ships a literal "node_modules" directory
                    # as its own content: `npm publish`/`npm pack`
                    # unconditionally exclude node_modules/ from what gets
                    # packed, so this is a documented convention this
                    # installer already relies on elsewhere, not a new
                    # assumption introduced here.
                    continue
                stack.append(entry_path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise PrimeInstallError(
                    f"{package_label} materialized an unexpected non-regular file: {relative}"
                )
            expected_digest = pinned_digests.get(relative)
            if expected_digest is None:
                raise PrimeInstallError(
                    f"{package_label} materialized an undeclared file not present "
                    f"in the pinned pre-npm-ci digest map: {relative}"
                )
            observed.add(relative)
            if sha256_file_verified(entry_path) != expected_digest:
                raise PrimeInstallError(
                    f"{package_label} materialized file content does not match the "
                    f"digest-verified original tarball: {relative}"
                )
    missing = sorted(os.fspath(path) for path in (set(pinned_digests) - observed))
    if missing:
        raise PrimeInstallError(
            f"{package_label} is missing pinned file(s) after npm ci: {missing}"
        )


def run_npm(
    npm_path: str,
    node_path: str,
    args: list[str],
    cwd: Path,
    cache: Path,
    install_home: Path,
    install_tmp: Path,
    *,
    timeout: int = 300,
    child_umask: int | None = None,
    release_dir_fd: int | None = None,
) -> None:
    """`child_umask`, when given, is applied to the CHILD process only, via
    `preexec_fn` running after fork() and before exec() -- the parent
    installer process's own umask is never touched. Only the `npm install
    --package-lock-only` call site (_install_locked()) passes 0o077: that
    is the one invocation that GENERATES a brand-new file
    (package-lock.json) an external process controls the initial mode of,
    so narrowing its ambient umask makes npm create it already-private from
    the moment it exists, shrinking (but not eliminating -- npm has no
    obligation to honor an inherited umask in every environment, and could
    still be interrupted mid-write) the window it sits at a permissive
    mode before tighten_generated_private_file_mode() explicitly fixes it.
    This is deliberately NOT the default for every run_npm() call: `npm ci`
    materializes an entire node_modules tree whose file/directory modes
    this installer re-validates itself afterward (tree_digest(),
    extract_node_toolchain()-style checks) under the existing, looser
    0o022-tolerant gates -- scoping the tighter umask to only this one
    call avoids any risk of an unrelated, unreviewed behavior change to
    npm's other output across the rest of this file.

    `release_dir_fd`, when given, is a caller-held, already-open,
    already-verified O_DIRECTORY|O_NOFOLLOW descriptor for `cwd` (RELEASE_DIR
    for both real call sites), re-checked via assert_release_dir_fd_identity()
    both immediately BEFORE constructing the subprocess call and immediately
    AFTER it returns. subprocess.Popen/run has no dir_fd-based way to set a
    child's cwd -- only a lexical path -- so `cwd` itself is still handed to
    it by name; this cannot make npm's OWN internal path resolution
    dir_fd-bound. What it does do is narrow the specific gap independent
    Codex sol/max round-23 review, 2026-08-19 (P1-1), measured at a real
    3m34s: a same-UID racer who renames RELEASE_DIR aside and renames a
    different, real, legitimately-owned, correctly-permissioned directory
    into its place DURING this exact subprocess's run is now caught the
    instant it returns, before this installer trusts anything the process
    produced -- rather than being caught only much later (or never, for the
    node/npm binaries themselves -- see extract_node_toolchain()'s digest
    pinning for that companion fix) by a check placed far downstream of the
    actual window. The pre-call check additionally catches a swap that
    happened in the gap between the previous checkpoint and this call.
    Documented residual: the subprocess's own execution window -- between
    this function handing `cwd` to subprocess.run() and npm's own first
    internal path resolution -- is narrowed but not eliminated, because
    execve()/chdir() semantics for an external, unmodified `npm` process are
    inherently lexical-path-based; a full fix would require running npm
    inside a fchdir(release_dir_fd)'d child, which is a larger change than
    this round scopes (see this file's `validate_exec_target()` docstring in
    managed_launch_guard_script() for the same class of documented,
    intentionally-not-hidden residual). Defaults to None so every existing
    caller/test without a held descriptor is unaffected.
    """
    if release_dir_fd is not None:
        assert_release_dir_fd_identity(release_dir_fd)
    environment = managed_npm_environment(
        node_path, cache, install_home, install_tmp
    )
    preexec_fn = (
        (lambda: os.umask(child_umask)) if child_umask is not None else None
    )
    try:
        result = subprocess.run(
            [node_path, npm_path, *args],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            preexec_fn=preexec_fn,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrimeInstallError("npm execution failed") from exc
    if release_dir_fd is not None:
        assert_release_dir_fd_identity(release_dir_fd)
    if result.returncode != 0:
        tail = " ".join(result.stdout.splitlines()[-5:])[:1_000]
        raise PrimeInstallError(f"npm failed with exit {result.returncode}: {tail}")


def allowed_unpinned_release_files() -> frozenset[str]:
    """The complete set of RELEASE_DIR-relative regular-file paths that a
    genuine install legitimately leaves OUTSIDE `release_relative_pinned_
    digests` (see _install_locked_within_release_dir()'s construction of
    that map) -- files tree_digest()'s round-38 deny-unknown check (below)
    must NOT reject even though they have no pinned entry.

    Round 38, 2026-08-20 (independent Claude opus/max round-37 review,
    P1-1/P1-2): every regular file under RELEASE_DIR that is NOT a key of
    `pinned_relative_digests` and NOT named here is now refused outright by
    tree_digest() -- this is the "deny-unknown" half of the completeness
    invariant round 34 only ever enforced one direction of (see that
    function's own docstring). Getting this exemption set exactly right
    matters as much as the check itself: too narrow, and a genuine clean
    install fails closed on its own legitimate files; too broad, and it
    silently reopens the exact hole this round closes. So every entry here
    is derived from an EXISTING constant, never hardcoded as a bare
    literal, so this set self-corrects if any of those constants ever
    change:

      * Five bookkeeping files this installer downloads, generates, or
        copies at the top of RELEASE_DIR / inside lib/node_modules/, whose
        content is either independently digest-verified at the moment it
        is written (LICENSE, upstream-package-lock.json -- see
        safe_download() and, as of this same round, the re-verified read
        in _install_locked_within_release_dir()) or re-derivable/re-
        validated by other means (package.json is a literal this
        installer's own root_manifest constructs and immediately re-reads
        via verify_unchanged_private_ssd_file(); package-lock.json is
        validated in full by validate_generated_lock() against
        GENERATED_LOCK_SHA256 before `npm ci` ever runs;
        lib/node_modules/.package-lock.json is `npm ci`'s own hidden
        top-level bookkeeping marker -- see
        KNOWN_NON_DIRECTORY_NODE_MODULES_ENTRIES's own comment for the
        empirical confirmation that this is the ONE non-package entry a
        real install ever materializes there) -- none of these four is
        individually pinned (an accepted, long-documented residual; see
        the comment above `release_relative_pinned_digests`'s own
        construction), and none is ever read or executed again by this
        installer or its generated scripts after install.
      * Every file this installer downloads into RELEASE_DIR/assets/ --
        the four official release tarballs (ASSETS), the Node.js tarball
        (NODE_ASSET), and the four locally-patched tarballs this installer
        itself builds from them (MAIN_PATCHED_ASSET, WORKSPACE_ASSETS) --
        each already digest-verified at download/build time (safe_download()'s
        own in-memory check, make_patched_asset()'s own published-then-
        reread comparison) and never the source of anything this installer
        or its generated scripts execute directly (only their EXTRACTED,
        individually-pinned contents under lib/node_modules/ and
        toolchain/ are ever executed).

    Deliberately a plain function, not a module-level constant computed at
    import time: keeping it a function makes the "derived from existing
    constants, not hardcoded" property directly testable (a test can call
    it and assert its value tracks ASSETS/WORKSPACE_ASSETS/
    MAIN_PATCHED_ASSET/NODE_ASSET if any of them are ever mutated in a
    future round) without relying on import-order side effects.
    """
    literal_bookkeeping_files = frozenset(
        {
            "LICENSE",
            "package.json",
            "package-lock.json",
            "upstream-package-lock.json",
            "lib/node_modules/.package-lock.json",
        }
    )
    asset_file_names = frozenset(
        {*ASSETS, NODE_ASSET, MAIN_PATCHED_ASSET, *WORKSPACE_ASSETS.values()}
    )
    return literal_bookkeeping_files | frozenset(
        f"assets/{name}" for name in asset_file_names
    )


def tree_digest(
    root: Path, *, pinned_relative_digests: dict[str, str] | None = None
) -> tuple[str, int]:
    """Structural + content digest of everything under `root`, used both to
    RECORD a release tree's permanent trust baseline
    (receipt['release_tree_sha256']/['release_tree_entries'], via
    write_pending_install()) and, on every later verify() call, to detect
    drift against that recorded baseline.

    `pinned_relative_digests`, when given, maps a SUBSET of `root`-relative
    POSIX path strings (e.g. "toolchain/bin/node",
    "lib/node_modules/prime-agent/dist/bundle/cli.js") to a SHA-256 digest
    already known, independently, to be correct -- captured strictly
    BEFORE any external `npm` subprocess this installer does not control
    ever ran (see extract_node_toolchain()'s node_sha256/npm_cli_sha256/
    toolchain_content_digests and make_patched_asset()'s per-file
    content_digests maps). For any regular file whose `root`-relative path
    is a key in this map, the content digest this walk computes below is
    compared against the pinned value RIGHT HERE, in the SAME pass that is
    about to fold that digest into the tree's overall hash -- and this
    function fails closed on a mismatch -- INSTEAD OF unconditionally
    recording whatever happens to be on disk as the permanently-trusted
    baseline with no comparison against anything. EVERY key present in
    `pinned_relative_digests` is additionally required to have been
    observed and matched as a regular file during this same walk -- see
    the round-34 paragraph below for why, and for what this closes. As of
    round 38, the CONVERSE is also enforced: every regular file this walk
    observes that is NEITHER a key of `pinned_relative_digests` NOR a
    member of allowed_unpinned_release_files() also fails this call
    closed -- see the round-38 paragraph below.

    Round 40, 2026-08-20 (independent Claude opus/max round-39 review,
    P1-1): round 38's deny-unknown completeness check, directly below,
    populated `observed_regular_relative_paths` -- and therefore only ever
    compared anything -- inside the `elif stat.S_ISREG(...)` branch. The
    `if stat.S_ISLNK(...)` branch above it never contributed to any
    completeness set at all, so a symlink at ANY relative path, pointing
    anywhere `is_relative_to(resolved_root)` already permits, was accepted
    into the permanent trust baseline with ZERO comparison against
    anything -- structurally unable to ever be caught by the round-38 fix,
    which only ever looked at regular files. Reproduced with real Node:
    planting a symlink at a node_modules-package-shaped path (mimicking a
    package the pinned bundle unconditionally `require()`s, e.g.
    "lib/node_modules/bufferutil") pointing at RELEASE_DIR/LICENSE -- one
    of the 14 allowed_unpinned_release_files() exemptions, since its
    content is verified once at download time but never individually
    pinned into `pinned_relative_digests` the way every node_modules
    package member is -- was accepted here, and Node's own
    extensionless-symlink-target loader fallback then loaded LICENSE's
    text and executed it as JavaScript on every future managed invocation.
    The identical blind spot independently defeated a second, separate
    piece of round-38's own new code: `_install_locked_within_release_dir()`'s
    post-move assertion that a previously-skipped registry package had not
    since materialized used to check `is_dir() and not is_symlink()`,
    which evaluates False -- i.e. "not materialized, still fine" -- for a
    symlink-to-directory; fixed there, the same round, to fail closed on
    ANY post-move presence (file, symlink, or directory), not only a
    directory (see that assertion's own comment for detail).

    Fixed the same structural way as round 38 itself: `observed_symlink_
    relative_paths`, tracked alongside `observed_regular_relative_paths`
    below (same `pinned_relative_digests is not None` gate), records every
    symlink's relative path together with its already-resolved,
    already-containment-checked target's OWN relative path. After the
    walk, any observed symlink that is NOT both (a) directly inside a
    `.bin/` directory and (b) resolved to a target that is itself a key of
    `pinned_relative_digests` (i.e. a digest-verified, individually pinned
    regular file) fails this call closed. `.bin/` symlinks are the ONLY
    symlinks a genuine install ever produces under RELEASE_DIR: every
    OTHER source this tree's content can come from -- the four release
    tarballs (safe_extract_main_asset()) and the Node.js tarball
    (extract_node_toolchain()) -- already refuses any symlink outright via
    assert_tree_has_no_symlinks() before that content is ever published
    here, so `npm ci`'s own node_modules/.bin/ generation (one symlink per
    package with a declared "bin" entry, pointing at that same package's
    own, already-pinned script file) is the only legitimate source left.
    Requiring the target to be a pinned key -- rather than merely
    re-running the existing containment check -- is what directly closes
    the LICENSE reproduction above: LICENSE is deliberately NOT a key of
    `pinned_relative_digests` (see allowed_unpinned_release_files()), so a
    symlink pointing at it is refused regardless of where the symlink
    itself sits.

    Round 38, 2026-08-20 (independent Claude opus/max round-37 review,
    P1-1/P1-2): through round 37, this function enforced only ONE
    direction of what is actually a completeness invariant with two
    halves: "every pinned key must be observed as a regular file" (round
    34, below). The other half -- "every regular file observed during the
    walk must be pinned (or an accepted, explicitly-named exemption)" --
    was never implemented, so ANY regular file anywhere under `root` that
    was not one of `pinned_relative_digests`'s own keys was unconditionally
    folded into the recorded baseline with zero comparison, exactly as if
    `pinned_relative_digests` had never been passed at all. Round 36
    responded to round 35's "no deny-unknown half" finding by growing the
    allow-list (`release_relative_pinned_digests` went from ~1,700 to a
    measured 24,060 entries -- covering the toolchain, both locally
    patched and registry-derived node_modules content, the launch guard,
    and the command wrapper) rather than inverting the invariant, leaving
    every OTHER location under RELEASE_DIR -- most concretely, any
    declared registry package row this platform's `npm ci` happens not to
    materialize (see declared_registry_package_lock_rows()'s own docstring
    for exactly which rows those are) -- open for a same-UID racer to
    plant an entirely new, never-pinned file that becomes part of the
    permanently-trusted baseline. Reproduced two ways against pre-fix HEAD
    36a4008c08: (a) hide a genuinely-materialized registry package during
    the pre-move presence probe below (see the P1-1 comment at
    `_install_locked_within_release_dir()`'s registry-pinning loop), then
    restore a tampered copy after the probe passed it by; (b) a zero-race
    variant needing no timing at all -- several of this exact closure's
    declared registry rows are unconditionally skipped by `npm ci` on this
    platform (a real, always-true platform/architecture mismatch, not a
    race), so a same-UID actor can plant content at one of those
    declared-but-never-materialized paths at ANY point during the install
    with nothing to out-race.

    Fixed the same way round 34 fixed the missing-pinned-key half: track
    every regular file's `root`-relative path observed during the walk
    (`observed_regular_relative_paths`, alongside the existing
    `consumed_pinned_relative_paths`), and after the walk, when
    `pinned_relative_digests is not None`, additionally refuse if
    `observed_regular_relative_paths - set(pinned_relative_digests) -
    allowed_unpinned_release_files()` is non-empty. This closes the
    exploitation path structurally -- independent of whether any given
    caller (`_install_locked_within_release_dir()`'s registry-pinning
    loop, `assert_materialized_node_modules_matches_lock()`) also happens
    to catch a given same-UID plant through its own, narrower mechanism --
    because it is enforced in the SAME pass that establishes the
    permanently-trusted `release_tree_sha256` baseline, the same way the
    round-34/round-29 fixes already are. See allowed_unpinned_release_files()
    for the complete, derived-from-existing-constants exemption set this
    requires (measured, on a real install, at exactly 14 files), and
    _install_locked_within_release_dir()'s registry-pinning loop and
    assert_materialized_node_modules_matches_lock() for the independent,
    narrower defense-in-depth checks added the same round.

    Round 34, 2026-08-20 (independent Claude opus/max round-33 review,
    P1-1): the content comparison above only ever fired inside this
    function's `elif stat.S_ISREG(...)` branch -- the `if stat.S_ISLNK(...)`
    branch and the `elif stat.S_ISDIR(...)` branch below never consulted
    `pinned_relative_digests` at all, and nothing anywhere in this function
    checked that every key of `pinned_relative_digests` was actually
    observed during the walk. A same-UID racer who, in the same install-time
    window this parameter already exists to close, replaced a pinned
    path's regular file with a SYMLINK, replaced it with a DIRECTORY, or
    simply DELETED it outright had that substitution (or omission) fold
    into the recorded baseline with zero comparison -- whatever the racer
    left in its place (or didn't leave at all) was accepted unconditionally,
    exactly as if `pinned_relative_digests` had never been passed for that
    one path. Reproduced end-to-end through the real install(): symlink-
    substituting a file that IS a key of `release_relative_pinned_digests`
    (built in _install_locked_within_release_dir()) but is NOT one of
    verify()'s five receipt-checked exec targets let install() complete and
    verify() report ok:true, while the attacker's symlink target executed on
    every managed invocation.

    Fixed with a completeness invariant, not a narrow symlink-only patch
    (round-33 review explicitly found deletion and directory-substitution
    equally exploitable, and asked for one mechanism that closes all
    three): `consumed_pinned_relative_paths`, tracked below, records every
    `pinned_relative_digests` key that was actually matched against a
    regular file (i.e. reached the `elif stat.S_ISREG(...)` branch AND had
    a pinned entry AND passed the content comparison). After the walk
    completes, ANY key of `pinned_relative_digests` that is not also in
    `consumed_pinned_relative_paths` -- because the walk observed it as a
    symlink, observed it as a directory, or never observed it at all
    (deleted, or never created) -- fails this call closed. This is
    structurally the same missing-file bookkeeping
    assert_locally_patched_package_matches_pinned_digests() already
    performs for the four locally patched packages' own trees (its own
    `observed`/`missing` pair, just above this function); this walk now
    enforces the identical invariant for every `pinned_relative_digests`
    key, regardless of which caller supplied the map.

    Round 29, 2026-08-19 (independent Claude opus/max round-29 review,
    P1-1): before this parameter existed, tree_digest() was purely a
    RECORDING pass -- every prior content check in this file's install path
    (the per-package digest sweep, the toolchain re-verification calls) ran
    and passed BEFORE this function's first call in write_pending_install(),
    but nothing carried their result forward as something THIS call
    compares against; a same-UID racer who tampered a pinned file's content
    in the real, externally-observable window between those checks
    completing and this walk reaching that same path (measured real
    window: ~6.8 seconds -- 1.3s from the sweep completing to
    os.mkdir("lib", ...), itself a public, pollable signal, plus up to 5.5s
    for this function's own sorted walk to reach a tampered path later in
    sort order) had the tampered content silently adopted as the
    permanent baseline: write_pending_install() recorded it, verify()
    later confirmed the release tree still matched what was recorded (true
    -- nothing changed AFTER that point), and the launch guard executed
    it. Passing the SAME pinned digests this walk is about to fold into
    the baseline as `pinned_relative_digests` collapses that window, for
    every file the caller has an independent pin for, to whatever this
    function's own single O_NOFOLLOW-open-then-fstat-identity-checked read
    (sha256_file_verified()) cannot itself close -- there is no LATER,
    separate read of that file before its digest becomes part of the
    trusted baseline, because this read and that comparison are the same
    operation. See _install_locked_within_release_dir()'s own comment,
    where `pinned_relative_digests` is assembled, for the real measured
    residual after this fix and the complementary re-sweep it also adds.
    """
    resolved_root = verify_private_ssd_dir(root)
    root_info = root.lstat()
    digest = hashlib.sha256()
    digest.update(b"R\0" + str(stat.S_IMODE(root_info.st_mode)).encode() + b"\n")
    count = 1
    # Round 34, 2026-08-20 (P1-1): every `pinned_relative_digests` key that
    # this walk actually matched against a regular file whose content
    # equalled the pinned value -- see the completeness assertion after the
    # loop, and this function's own docstring for what this closes.
    consumed_pinned_relative_paths: set[str] = set()
    # Round 38, 2026-08-20 (P1-1/P1-2): every regular file's `root`-relative
    # path observed during this walk, regardless of whether it has a pinned
    # entry -- the deny-unknown completeness check after the loop below
    # uses this to refuse any regular file that is neither pinned nor an
    # accepted exemption. Only populated (via the same `pinned_relative_
    # digests is not None` guard already used for the content comparison
    # below) when a caller actually supplied a pinned map to check against;
    # left empty and unused for the two callers (finalize_pending_install(),
    # verify()) that intentionally call this function without one, to
    # compare against an already-recorded baseline -- see this function's
    # own docstring for why that is correct, not an oversight.
    observed_regular_relative_paths: set[str] = set()
    # Round 40, 2026-08-20 (P1-1): the symlink counterpart of `observed_
    # regular_relative_paths` just above -- maps every observed symlink's
    # `root`-relative path to its own already-resolved, already-containment-
    # checked target's `root`-relative path. Same population gate
    # (`pinned_relative_digests is not None`) and same purpose: the deny-
    # unknown completeness check after the loop uses this to refuse any
    # symlink that is not a genuine npm `.bin/` entry pointing at a pinned,
    # digest-verified regular file -- see this function's own docstring for
    # the full finding this closes.
    observed_symlink_relative_paths: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: os.fspath(item.relative_to(root))):
        relative = os.fspath(path.relative_to(root))
        info = path.lstat()
        if info.st_uid != os.getuid() or (
            not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022
        ):
            raise PrimeInstallError(f"unsafe runtime ownership or mode: {relative}")
        if stat.S_ISLNK(info.st_mode):
            target_text = os.readlink(path)
            try:
                target = (path.parent / target_text).resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise PrimeInstallError(f"broken runtime symlink: {relative}") from exc
            if Path(target_text).is_absolute() or not is_relative_to(target, resolved_root):
                raise PrimeInstallError(f"runtime symlink escaped release: {relative}")
            if pinned_relative_digests is not None:
                observed_symlink_relative_paths[relative] = os.fspath(
                    target.relative_to(resolved_root)
                )
            payload = b"L\0" + relative.encode() + b"\0" + target_text.encode()
        elif stat.S_ISREG(info.st_mode):
            content_sha256 = sha256_file_verified(path)
            if pinned_relative_digests is not None:
                observed_regular_relative_paths.add(relative)
                expected = pinned_relative_digests.get(relative)
                if expected is not None:
                    if content_sha256 != expected:
                        raise PrimeInstallError(
                            "release tree content does not match its digest-verified "
                            f"pre-npm-ci pinned baseline: {relative}"
                        )
                    consumed_pinned_relative_paths.add(relative)
            payload = (
                b"F\0"
                + relative.encode()
                + b"\0"
                + str(stat.S_IMODE(info.st_mode)).encode()
                + b"\0"
                + content_sha256.encode()
            )
        elif stat.S_ISDIR(info.st_mode):
            payload = (
                b"D\0"
                + relative.encode()
                + b"\0"
                + str(stat.S_IMODE(info.st_mode)).encode()
            )
        else:
            raise PrimeInstallError(f"unexpected runtime file type: {relative}")
        digest.update(payload + b"\n")
        count += 1
    if pinned_relative_digests is not None:
        # Round 34, 2026-08-20 (P1-1): fail closed if ANY pinned path was
        # not observed and matched as a regular file above -- covers a
        # pinned path replaced with a symlink (observed, but never added to
        # `consumed_pinned_relative_paths` because only the S_ISREG branch
        # ever adds to it), replaced with a directory (same reason), or
        # deleted outright (never observed by the walk at all) with the
        # exact same check, rather than three separate narrow patches.
        unconsumed_pinned_relative_paths = sorted(
            set(pinned_relative_digests) - consumed_pinned_relative_paths
        )
        if unconsumed_pinned_relative_paths:
            raise PrimeInstallError(
                "release tree is missing pinned path(s), or a pinned path is no "
                "longer a plain regular file (replaced with a symlink, replaced "
                f"with a directory, or deleted): {unconsumed_pinned_relative_paths}"
            )
        # Round 38, 2026-08-20 (independent Claude opus/max round-37 review,
        # P1-1/P1-2): the converse of the completeness check just above --
        # fail closed if this walk observed any regular file that is
        # neither a key of `pinned_relative_digests` NOR one of the
        # explicitly-named, derived-from-constants exemptions
        # allowed_unpinned_release_files() returns. See this function's own
        # docstring for the full finding this closes: without this half of
        # the invariant, ANY regular file under `root` with no pinned entry
        # -- most concretely, a declared registry package row this
        # platform's `npm ci` never materializes, or content planted while
        # a legitimately-pinned package directory was transiently absent --
        # was unconditionally folded into the permanently-trusted baseline
        # with zero comparison against anything.
        unknown_regular_relative_paths = sorted(
            observed_regular_relative_paths
            - set(pinned_relative_digests)
            - allowed_unpinned_release_files()
        )
        if unknown_regular_relative_paths:
            raise PrimeInstallError(
                "release tree contains regular file(s) that are neither pinned "
                "nor an accepted unpinned exemption -- refusing to adopt into "
                f"the trusted baseline: {unknown_regular_relative_paths}"
            )
        # Round 40, 2026-08-20 (independent Claude opus/max round-39 review,
        # P1-1): the symlink counterpart of the regular-file deny-unknown
        # check just above -- see this function's own docstring for the full
        # finding this closes. A symlink is accepted into the trusted
        # baseline only when BOTH (a) it sits directly inside a `.bin/`
        # directory -- the only shape a genuine `npm ci` ever produces under
        # RELEASE_DIR, since every other content source is already required
        # to be symlink-free by assert_tree_has_no_symlinks() before
        # publication -- AND (b) its already-containment-checked target is
        # itself a key of `pinned_relative_digests`, i.e. a digest-verified,
        # individually pinned regular file, not merely something structurally
        # contained within RELEASE_DIR. Condition (b) is what directly closes
        # the LICENSE reproduction: LICENSE is a deliberate
        # allowed_unpinned_release_files() exemption, never a key of
        # `pinned_relative_digests`, so a symlink pointing at it is refused
        # regardless of where the symlink itself is placed.
        unknown_symlink_relative_paths = sorted(
            relative
            for relative, target_relative in observed_symlink_relative_paths.items()
            if PurePosixPath(relative).parent.name != ".bin"
            or target_relative not in pinned_relative_digests
        )
        if unknown_symlink_relative_paths:
            raise PrimeInstallError(
                "release tree contains symlink(s) that are neither a package's "
                "own .bin/ entry nor resolved to a pinned, digest-verified "
                "regular file -- refusing to adopt into the trusted baseline: "
                f"{unknown_symlink_relative_paths}"
            )
    return digest.hexdigest(), count


def assert_no_unexpected_ancestor_node_modules(release_dir: Path) -> None:
    """Real Node's own CommonJS module resolution (`Module._nodeModulePaths`)
    walks from wherever it is resolving a bare specifier upward through
    EVERY ancestor directory, appending "node_modules" at each level, all
    the way to the real filesystem root -- confirmed empirically against
    this installer's exact pinned Node version (real
    `Module._nodeModulePaths(...)` output, real ancestor walk, real
    termination at "/"). Every location that walk reaches STRICTLY ABOVE
    `release_dir` is something this installer never legitimately creates --
    every write it performs during a real install stays within
    `release_dir` itself, or within sibling, dot-prefixed per-tool home
    directories under LOCAL_HOMES that are never named "node_modules" -- and
    was, through round 39, not considered by anything in this file's trust
    model at all (independent Claude opus/max round-39 review, P1-2).

    Round 40, 2026-08-20: this is verify()'s counterpart to
    managed_launch_guard_script()'s own, independently generated
    `unexpected_ancestor_node_modules()` check (the two cannot literally
    share code -- the launch guard is a standalone script with no import of
    this module -- but enforce the identical invariant). Mirrors this file's
    existing pattern of defense-in-depth checks that verify() performs even
    though verify() itself never execs NODE/CLI directly (see the
    node_sha256/npm_cli_sha256/entrypoint_sha256/launch_guard_sha256/
    command_wrapper_sha256 loop in verify() for the established precedent).
    Refuses on ANY presence at all (file, symlink -- dangling or not -- or
    directory) rather than comparing against a recorded baseline, for the
    same reason `unexpected_ancestor_node_modules()` does: none of these
    locations should ever exist, so there is nothing to legitimately
    distinguish between "existed at install time" and "appeared later" --
    both are equally wrong.

    Round 43, 2026-08-20 (independent Claude opus/max round-42 review,
    P1-B): the ancestor walk above models `Module._nodeModulePaths` only.
    Real Node's actual resolution (`Module._resolveLookupPaths`) is that
    walk PLUS `Module.globalPaths` -- confirmed empirically against this
    installer's exact pinned Node binary (real
    `require('module').globalPaths` output, `env -i` with/without HOME
    set): `$HOME/.node_modules`, `$HOME/.node_libraries` (only when HOME
    is a non-empty string -- real Node's own `if (homeDir)` check treats
    unset and empty identically, mirrored by `node_global_folder_paths()`
    below), and a third path derived from the Node binary's own install
    location (`path.resolve(process.execPath, '..', '..')` + "lib/node"),
    which on this installer's fixed toolchain layout
    (`RELEASE_DIR/toolchain/bin/node`) is `RELEASE_DIR/toolchain/lib/node`
    -- itself INSIDE `release_dir`, not an ancestor, so the walk above
    never reaches it. Confirmed against the real pinned Node darwin-arm64
    tarball's own contents that extraction never creates a "lib/node"
    directory there (only "lib/node_modules") -- the same "must never
    legitimately exist" property the ancestor walk itself already relies
    on, so this refuses on ANY presence exactly the same way. NODE_PATH-
    derived `globalPaths` entries are deliberately not modeled here:
    NODE_PATH is unconditionally removed by the generated launch guard's
    `scrubbed_node_environment()` before the one real exec that ever
    matters, so real Node will see no NODE_PATH for that invocation and
    contributes no such entries to its own `globalPaths`.
    """
    current = os.fspath(release_dir)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        candidate = os.path.join(current, "node_modules")
        if os.path.lexists(candidate):
            raise PrimeInstallError(
                "unexpected node_modules directory on Node's module "
                f"resolution ancestor chain outside the managed release: {candidate}"
            )
    for candidate in node_global_folder_paths(release_dir):
        if os.path.lexists(candidate):
            raise PrimeInstallError(
                "unexpected item on Node's own global module-resolution "
                f"path outside the managed release: {candidate}"
            )


def node_global_folder_paths(release_dir: Path) -> list[str]:
    """The three locations `Module.globalPaths` adds to real Node's module
    resolution beyond the plain ancestor `node_modules` walk -- see
    `assert_no_unexpected_ancestor_node_modules()`'s round-43 docstring
    paragraph just above for how each was empirically confirmed. Uses
    `os.environ.get("HOME")` -- the SAME value the generated launch
    guard's `scrubbed_node_environment()` passes through unscrubbed to the
    one real Node invocation that matters -- rather than any other notion
    of "home directory", so this checks exactly what that future real
    launch will actually see. `release_dir/"toolchain/lib/node"` mirrors
    this installer's own fixed, hardcoded toolchain layout (matching
    `RELEASE_DIR / "toolchain/bin/node"` used elsewhere, e.g. this
    installer's own `node_target` receipt field) rather than re-deriving
    it from a live NODE path, since this function -- unlike the generated
    launch guard's own independent copy -- has no validated, symlink-free
    NODE path available to it (verify() calls this before it resolves
    `node` from the receipt at all).

    Round 45, 2026-08-20 (independent Claude opus/max round-44 review):
    the two HOME-relative entries are wrapped in `os.path.abspath()`
    (pure lexical normalization, no filesystem access) rather than left
    as a bare `os.path.join()`. Real Node builds these with
    `path.resolve(homeDir, '.node_modules')`, which normalizes `..`/`.`/
    duplicate separators PURELY LEXICALLY. A bare `os.path.join()` does
    not normalize at all, so a HOME containing a `..` segment that routes
    through a path component that does not exist on disk previously made
    the un-normalized candidate string fail the `os.path.lexists()` check
    below with ENOENT even though real Node's own lexical resolution
    landed on a real, existing, attacker-controlled directory -- letting
    real Node load and execute code from a location this check believed
    was empty. `os.path.abspath()` normalizes the same way `path.resolve()`
    does without requiring any intermediate component to exist, closing
    that gap. Verified empirically against the real pinned Node binary
    across `..` through a nonexistent component, `.` segments, duplicate
    separators, a bare `~` (neither side tilde-expands), a relative HOME,
    and a leading `//` HOME. Only the last diverges textually: Python's
    `os.path` specially preserves exactly two leading slashes (a POSIX-
    permitted convention) while Node's `path.resolve()` collapses them to
    one. Confirmed benign on this platform -- Darwin's (and Linux's)
    kernel path resolution does not implement that POSIX allowance, so
    `//x` and `/x` name the identical inode and `os.path.lexists()` sees
    the same file either way; see
    test_node_global_folder_paths_leading_double_slash_diverges_textually_but_same_inode.
    """
    paths: list[str] = []
    home = os.environ.get("HOME")
    if home:
        paths.append(os.path.abspath(os.path.join(home, ".node_modules")))
        paths.append(os.path.abspath(os.path.join(home, ".node_libraries")))
    paths.append(os.path.join(os.fspath(release_dir), "toolchain", "lib", "node"))
    return paths


def write_pending_install(
    receipt: dict[str, Any],
    *,
    pinned_relative_digests: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Cross the durable-tree barrier, then and only then publish the journal.

    `pinned_relative_digests`, when given, is forwarded unchanged to both
    tree_digest() calls below -- see that function's own docstring, and
    _install_locked_within_release_dir()'s construction of this map -- so
    the FIRST call, whose result becomes the permanently-trusted
    receipt['release_tree_sha256']/['release_tree_entries'] baseline, is a
    COMPARISON against every pinned file's already-known-correct digest
    rather than an unconditional recording of whatever tree_digest()'s own
    walk happens to observe (round 29, 2026-08-19, P1-1 fix). The SECOND
    call (after the durability sync barrier) receives the same map for
    consistency -- by then nothing legitimate should have changed the
    content of any pinned file, so this is a harmless, redundant
    re-confirmation, not a second, divergent check.
    """
    validate_pristine_managed_home(STATE_DIR, "Prime Agent pending state")
    validate_pristine_managed_home(PROBE_HOME, "Prime Agent pending probe home")
    if read_private_ssd_file(STATE_DIR / "agent/settings.json") != expected_prime_settings():
        raise PrimeInstallError("Prime Agent pending telemetry settings drifted")
    if read_private_ssd_file(PROBE_HOME / "agent/settings.json") != expected_prime_settings():
        raise PrimeInstallError("Prime Agent pending probe settings drifted")
    validate_empty_private_dir(
        managed_session_dir(), "Prime Agent pending session directory"
    )
    release_identity = tree_digest(
        RELEASE_DIR, pinned_relative_digests=pinned_relative_digests
    )
    for root in (RELEASE_DIR, STATE_DIR, PROBE_HOME, managed_session_dir()):
        sync_private_tree(root)
    if (
        tree_digest(RELEASE_DIR, pinned_relative_digests=pinned_relative_digests)
        != release_identity
    ):
        raise PrimeInstallError("Prime Agent release tree changed across durability barrier")
    validate_pristine_managed_home(STATE_DIR, "Prime Agent pending state")
    validate_pristine_managed_home(PROBE_HOME, "Prime Agent pending probe home")
    durable_receipt = dict(receipt)
    durable_receipt["release_tree_sha256"] = release_identity[0]
    durable_receipt["release_tree_entries"] = release_identity[1]
    atomic_create_private_file(
        PENDING_PATH,
        canonical_json({"schema": JOURNAL_SCHEMA, "receipt": durable_receipt}),
        0o600,
    )
    return durable_receipt


def managed_entrypoint_script(
    node: Path, cli: Path, launch_guard: Path | None = None
) -> bytes:
    del node, cli
    launch_guard = launch_guard or (RELEASE_DIR / "bin/prime-agent-launch-guard.py")
    # "config", "package", and unconditional "help" used to be classified
    # session_start=0 here (skipping BOTH the $PWD/.prime/agent/settings.json
    # gate and ORCA_PRIME_AGENT_RESOURCE_GUARD below) alongside genuinely
    # session-free commands, but upstream's own handlers for two of them can
    # still touch the launch project:
    #   * handleConfigCommand()/handlePackageCommand() (shipped bundle,
    #     dist/bundle/chunk-CAY2X72A.js) both call
    #     SettingsManager.create(process.cwd(), agentDir), and "config"
    #     additionally calls packageManager.resolve() with no onMissing
    #     guard -- so a project-declared package source in a hostile $PWD/
    #     .prime/agent/settings.json gets auto-installed and executed via
    #     the project's own configured npm command, with neither protection
    #     ever applying. Independent review reproduced this end-to-end
    #     (real generated wrapper -> real generated launch guard -> real
    #     pinned Node -> real prime-agent 0.7.2 bundle, offline): a hostile
    #     .prime/agent/settings.json's configured command ran via plain
    #     `prime-agent config` (independent review round 7/8, 2026-08-18,
    #     P1 -- the highest-severity finding across every round so far).
    #   * "help" is only genuinely safe when upstream's own
    #     isHelpCommandRequest() (dist/bundle/chunk-PMFPRFOT.js) says so:
    #     true unconditionally for BARE "help" with no further argument at
    #     all (path.length === 0 short-circuits before any matching runs,
    #     confirmed by reading that function directly, not assumed), but for
    #     "help <anything>" it depends on an edit-distance fuzzy match
    #     against known subcommand names -- any realistic miss (e.g.
    #     "help tools", "help auth", "help mcp", a typo) falls through to
    #     upstream's own continueWith(args): a full, unprotected session
    #     start in $PWD. Reimplementing that exact fuzzy-match algorithm in
    #     shell here would drift from upstream again -- the same
    #     hand-maintained-list fragility this bug class keeps stemming from
    #     -- so this wrapper does not attempt it. Only the exact,
    #     upstream-confirmed-always-safe bare-"help" case keeps the
    #     exemption, via a separate, argument-COUNT-based (not fuzzy
    #     name-based) check just after the case statement below; "help"
    #     with any argument now gets full session-start protection like an
    #     ordinary command.
    # Both now get the same $PWD/.prime/agent/settings.json gate and
    # resource-guard treatment as an ordinary session start, by default;
    # ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS=1 opts back into the previous,
    # unprotected behavior for them, exactly like every other session-start
    # command already requires for project settings review.
    #
    # Round 10, 2026-08-18 (independent review, P1 x2 -- round 9 found round
    # 8's fix above incomplete): "session" was still classified
    # session_start=0 here, and "help <argument>" -- despite already being
    # session_start=1 via the block above -- was still exempted from
    # RESOURCE_GUARDS entirely by managed_launch_guard_script()'s
    # RUNTIME_NO_GUARD_COMMANDS. Neither needs a hostile settings.json to be
    # exploitable, unlike round 8's config/package finding: a project's own
    # .prime/agent/extensions/*.js loads whenever extension discovery is not
    # explicitly disabled, and RESOURCE_GUARDS (--no-extensions/--no-skills/
    # --no-prompt-templates) is what disables it, gate or no gate. Two
    # concrete, reproduced triggers, both requiring zero opt-in and no
    # settings.json at all:
    #   * `prime-agent help <anything upstream's fuzzy matcher misses>`
    #     (e.g. "help zzzzzzzzzzzz", "help tols", "help mcp", "help auth",
    #     "help -- zzzzzzzzzzzz") -- isHelpCommandRequest() (dist/cli/
    #     command-registry.js) returns false, runPublicCommand() (dist/cli/
    #     public-command.js) falls through its switch's default case to
    #     continueWith(args), and the untouched args (still literally
    #     starting with "help") reach parseArgs() as a real session start
    #     with "help"/the argument(s) becoming ordinary positional chat
    #     messages -- WITH extension discovery still enabled, because
    #     RUNTIME_NO_GUARD_COMMANDS suppressed RESOURCE_GUARDS for every
    #     "help" invocation, match or miss alike.
    #   * `prime-agent session export ""` -- upstream's rewriteNestedCommand()
    #     unconditionally sets result.export = args[++i] for "--export"
    #     (the internal flag "session export" rewrites to), and an empty
    #     string is falsy at main.js's later `if (parsed.export)` gate, so
    #     control falls through past the early-exit export branch into the
    #     same full, unprotected session start -- a completely realistic
    #     trigger via plain shell expansion of an unset variable
    #     ("session export "$UNSET_VAR""), no adversarial argv needed. This
    #     one WAS session_start=0 here (in public_commands, directly above),
    #     so it skipped the settings gate too, not just RESOURCE_GUARDS.
    # "session" is removed from public_commands below so it gets the same
    # settings-gate + RESOURCE_GUARD_ENV treatment as an ordinary
    # session-start command (mirroring config/package's round-8 fix); the
    # matching RESOURCE_GUARDS placement fix for both "session" and
    # "help <argument>" lives in managed_launch_guard_script()'s
    # guarded_arguments() (RUNTIME_PUBLIC_COMMANDS now includes "session",
    # and "help" gets bespoke MATCH/MISS-aware placement -- see that
    # function's comments for why a naive placement identical to
    # RUNTIME_PUBLIC_COMMANDS's would silently corrupt a legitimate
    # `help <realtopic>` instead of just protecting a miss).
    public_commands = "|".join(
        (
            "doctor",
            "list",
            "rename",
            "schedule",
            "send",
            "shutdown",
            "status",
            "stop",
        )
    )
    leading_option_names = "|".join(LEADING_COMMAND_OPTIONS)
    leading_option_value_forms = "|".join(
        f"{name}=*" for name in LEADING_COMMAND_OPTIONS
    )
    lines = [
        "#!/bin/sh",
        "set -eu",
        "unset ORCA_PRIME_AGENT_RESOURCE_GUARD",
        # A real "skip every recognized leading option, take the first
        # non-option token" parse, not ad hoc pattern matching on a fixed
        # argv position/form. Defined as a shell function (not inline) so
        # `shift` operates on the function's own copy of the positional
        # parameters -- leaving the caller's "$@" untouched -- and every
        # security-relevant decision in this script that needs "the real
        # command token" (the update self-block below, the session_start
        # public-command case, and the agents/attach effective-project deny
        # gate) reads the SAME managed_command this one function computed,
        # instead of each re-deriving it from a fixed position (independent
        # review round 5, 2026-08-18, FIX 1: the prior ad hoc
        # `[ "${1-}" = "update" ]` / `[ "$managed_command" = "--daemon-socket" ]
        # && [ "$#" -ge 3 ]` checks each recognized only bare "update" at $1
        # and a single space-separated "--daemon-socket <value>" pair at
        # $1/$2, so `--daemon-socket <sock> update` bypassed the self-update
        # block entirely, and the "=" form or a repeated/duplicate
        # "--daemon-socket" flag bypassed the agents/attach gate and
        # miscomputed session_start for every other public command).
        "managed_command=\"\"",
        "resolve_managed_command() {",
        "  while [ \"$#\" -gt 0 ]; do",
        "    case \"$1\" in",
        f"      {leading_option_names})",
        "        if [ \"$#\" -ge 2 ]; then",
        "          shift 2",
        "        else",
        '          managed_command="$1"',
        "          return",
        "        fi",
        "        ;;",
        f"      {leading_option_value_forms})",
        "        shift",
        "        ;;",
        "      *)",
        '        managed_command="$1"',
        "        return",
        "        ;;",
        "    esac",
        "  done",
        '  managed_command=""',
        "}",
        'resolve_managed_command "$@"',
        'if [ "$managed_command" = "update" ]; then',
        '  printf "%s\\n" "prime-agent: self-update is disabled for the Orca-managed pinned release" >&2',
        "  exit 64",
        "fi",
        # A SEPARATE, deliberately much narrower resolver, used ONLY to
        # decide session_start -- reproducing upstream's own
        # normalizeLeadingDaemonSocketOption() (dist/cli/public-command.js)
        # EXACTLY rather than sharing resolve_managed_command()'s more
        # thorough parse. Upstream only ever remaps a SINGLE bare,
        # space-separated "--daemon-socket <value>" pair, and only when the
        # token immediately after it is exactly "stop" or "rename"; the "="
        # form is never recognized at all (regardless of what follows), a
        # repeated/second occurrence is never recognized, and a bare pair
        # followed by any other token (including a real public command name
        # like "status") is also left alone. Every one of those forms upstream
        # does NOT remap falls through to upstream's own default action: an
        # ordinary interactive/background SESSION START in $PWD -- not the
        # public command a more thorough parse would suggest.
        # resolve_managed_command() above is intentionally MORE thorough than
        # that (it also strips repeated occurrences and the "=" form) which
        # is safe for the self-update block and the agents/attach gate above
        # and below: over-matching there only ever DENIES an invocation
        # upstream would actually have started as a session anyway, and that
        # denial is itself one of the protections session_start=1 exists to
        # apply. Session_start is the opposite question -- marking it 0 SKIPS
        # the settings-block gate and ORCA_PRIME_AGENT_RESOURCE_GUARD=1 below
        # -- so being more thorough than upstream there is exactly backwards:
        # it means treating an invocation upstream will actually run as a
        # real session start as though it were an already-resolved, harmless
        # public command, and skipping the protections that session start
        # needs. resolve_public_command() defaults to session_start=1
        # (protected) for every form resolve_managed_command() would
        # over-match, and only reports a resolved public command for exactly
        # the forms upstream itself recognizes (independent dual review round
        # 6, 2026-08-18, P1, found independently by a Claude opus/max review
        # and a separate Codex QA pass: round 5's single shared resolver made
        # session_start=0 -- skipping both protections -- whenever the
        # shared, over-thorough parser found e.g. "status" after
        # "--daemon-socket=<v>", or after "--daemon-socket <v>" followed by
        # any command other than stop/rename, even though upstream's real
        # parser leaves those forms alone and actually starts a full
        # session).
        "managed_public_command=\"\"",
        "resolve_public_command() {",
        # "--daemon-socket" is intentionally a literal here, NOT derived from
        # LEADING_COMMAND_OPTIONS/leading_option_names like
        # resolve_managed_command() above: this function models one
        # SPECIFIC, bespoke upstream function's exact behavior (upstream's
        # normalizeLeadingDaemonSocketOption() hardcodes "--daemon-socket"
        # and stop/rename itself; it is not a generic leading-option
        # mechanism). A future second entry in LEADING_COMMAND_OPTIONS would
        # need its own upstream-behavior verification before this function
        # could safely be generalized to loop over it -- silently doing so
        # would reintroduce exactly the "assumed thorough parsing without
        # verifying upstream's real behavior" bug class this fix corrects.
        '  if [ "$#" -ge 3 ] && [ "$1" = "--daemon-socket" ]; then',
        '    case "$3" in',
        "      stop|rename)",
        '        managed_public_command="$3"',
        "        return",
        "        ;;",
        "    esac",
        '    managed_public_command=""',
        "    return",
        "  fi",
        '  managed_public_command="${1-}"',
        "}",
        'resolve_public_command "$@"',
        "session_start=1",
        'case "$managed_public_command" in',
        f"  {public_commands}|-h|--help|-v|--version) session_start=0 ;;",
        "esac",
        # Bare `help` with NO further argument at all is the one form
        # upstream's own isHelpCommandRequest() treats as real help
        # unconditionally (path.length === 0 returns true immediately,
        # before any fuzzy/candidate matching runs -- confirmed by reading
        # dist/bundle/chunk-PMFPRFOT.js directly). "$#" here is the
        # wrapper's OWN, still-unshifted positional parameter count --
        # resolve_managed_command()/resolve_public_command() each shift only
        # their own function-local copy of "$@" (see their own comments
        # above), never the caller's -- so this checks exactly "the whole
        # invocation was the single token help, nothing else", not merely
        # "the first token was help". Every other help form (one or more
        # further arguments, of any content) intentionally falls through to
        # full session_start=1 protection instead of attempting to
        # reproduce upstream's fuzzy match here (see the comment on
        # public_commands above for why).
        'if [ "$managed_public_command" = "help" ] && [ "$#" -eq 1 ]; then',
        "  session_start=0",
        "fi",
        # FIX 2, round 10, 2026-08-18 (independent review, P2): upstream's
        # own maybeStartDaemonEarly()/shouldStartDaemonEarly() (dist/cli/
        # daemon-launch.js) runs BEFORE any subcommand-specific dispatch --
        # before runPublicCommand(), before parseArgs(), before this
        # wrapper's own public/no-guard classification could possibly
        # matter -- and shouldStartDaemonEarly() spawns a detached daemon
        # whenever `args.includes("--print") || args.includes("-p")` is
        # true, full stop, checked with a literal, POSITION- and
        # "--"-AGNOSTIC array-membership test (confirmed by reading that
        # function directly): reproduced with "status -p", "list -p",
        # "doctor --print", "stop -p abc", "send bot -p",
        # "shutdown --force -p", and "schedule add ... -- -p run" all
        # spawning a daemon even though bare "status" etc. do not, and even
        # though "--" precedes "-p" in the schedule case. The spawned
        # daemon's cwd defaults to process.cwd() (this wrapper's own launch
        # cwd) and its own argv is hardcoded by upstream to just
        # "--mode daemon --daemon-socket <path>" -- none of this wrapper's
        # RESOURCE_GUARDS or the invoking command's own flags are ever
        # forwarded to it, by upstream's own design (the daemon is a
        # long-lived, shared background service, not scoped to any single
        # invocation). That means RESOURCE_GUARDS genuinely CANNOT reach
        # the daemon process itself no matter what this wrapper does here --
        # a documented, accepted residual gap (P2, not P1: no escalation to
        # code execution from the daemon itself has been demonstrated,
        # unlike FIX 1 above). What this wrapper CAN still do, and does
        # here, is make sure the $PWD/.prime/agent/settings.json gate below
        # still fires for any invocation upstream would treat this way --
        # closing the "hostile settings.json short-circuits the daemon
        # early-spawn before this wrapper's own public-command
        # classification would otherwise have gated it" vector, the same
        # way round 8's config/package fix and FIX 1 above do for their own
        # commands. Deliberately simpler than upstream's exact predicate
        # (which also exempts --mode daemon and a handful of
        # EARLY_LAUNCH_EXCLUDED_FLAGS like --help/--version/--list-models/
        # --export from early-spawn): this only ever makes session_start
        # MORE protective than upstream's own daemon-early-spawn decision
        # would strictly require, never less, and every one of those
        # exempted combinations is already an unrealistic, non-adversarial
        # edge case not worth the added shell complexity to special-case
        # here. Deliberately does NOT `break` on "--" the way the
        # --session-dir/--resume/--cwd loops below do -- upstream's own
        # check does not respect "--" either, so neither can this one
        # without reopening exactly the gap it closes.
        'for managed_arg in "$@"; do',
        '  case "$managed_arg" in',
        "    -p|--print)",
        "      session_start=1",
        "      ;;",
        "  esac",
        "done",
        'for managed_arg in "$@"; do',
        '  [ "$managed_arg" = "--" ] && break',
        '  case "$managed_arg" in',
        '    --session-dir|--session-dir=*)',
        '      printf "%s\\n" "prime-agent: --session-dir is fixed to managed Extreme SSD storage" >&2',
        "      exit 78",
        "      ;;",
        "  esac",
        "done",
        'if [ "${ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS-}" != "1" ]; then',
        '  case "$managed_command" in',
        '    agents|attach)',
        '      printf "%s\\n" "prime-agent: agents/attach require explicit effective-project settings review; set ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS=1 to opt in" >&2',
        "      exit 78",
        "      ;;",
        "  esac",
        '  for managed_arg in "$@"; do',
        '    [ "$managed_arg" = "--" ] && break',
        '    case "$managed_arg" in',
        '      --resume|--resume=*|-r|-r?*)',
        '        printf "%s\\n" "prime-agent: resume requires explicit effective-project settings review; set ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS=1 to opt in" >&2',
        "        exit 78",
        "        ;;",
        '      --cwd|--cwd=*)',
        '        printf "%s\\n" "prime-agent: --cwd requires explicit project settings review; set ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS=1 to opt in" >&2',
        "        exit 78",
        "        ;;",
        "    esac",
        "  done",
        "fi",
        'if [ "$session_start" -eq 1 ] && [ -e "$PWD/.prime/agent/settings.json" ] && '
        '   [ "${ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS-}" != "1" ]; then',
        '  printf "%s\\n" "prime-agent: project .prime/agent/settings.json requires explicit review; set ORCA_PRIME_AGENT_ALLOW_PROJECT_SETTINGS=1 to opt in" >&2',
        "  exit 78",
        "fi",
        f"export PRIME_AGENT_CODING_AGENT_DIR={shlex.quote(os.fspath(STATE_DIR / 'agent'))}",
        f"export PRIME_AGENT_LAUNCHER_PATH={shlex.quote(os.fspath(RELEASE_DIR / 'bin/prime-agent'))}",
        f"export PRIME_AGENT_SESSION_DIR={shlex.quote(os.fspath(managed_session_dir()))}",
        f"export PRIME_AGENT_CODING_AGENT_SESSION_DIR={shlex.quote(os.fspath(managed_session_dir()))}",
        "export NODE_DISABLE_COMPILE_CACHE=1",
        "export DO_NOT_TRACK=1",
        "export PI_SKIP_VERSION_CHECK=1",
        "export PRIME_AGENT_TELEMETRY=0",
        "export PYTHONDONTWRITEBYTECODE=1",
        'if [ "$session_start" -eq 1 ] && [ "${ORCA_PRIME_AGENT_ALLOW_PROJECT_RESOURCES-}" != "1" ]; then',
        "  export ORCA_PRIME_AGENT_RESOURCE_GUARD=1",
        "fi",
        # Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
        # P1-2a): `-B` alone leaves sys.path[0] (the launch guard's own
        # directory, bin/) prepended to the interpreter's module search
        # path, and CPython inserts that entry BEFORE this exec even reaches
        # this script's own `import` statements -- so a same-UID racer who
        # plants bin/hashlib.py (or bin/subprocess.py, bin/fcntl.py, ... any
        # name this launch guard imports from the stdlib) during the install
        # window has it silently imported and EXECUTED inside the launch
        # guard's own process, before validate_exec_target() -- the very
        # mechanism meant to verify NODE/CLI's identity and content -- ever
        # runs, letting the racer forge that verification from inside the
        # process that is supposed to be enforcing it. `-I` (isolated mode)
        # closes this: since Python 3.4, `-I` has always excluded "the
        # script's directory" from sys.path (this is `-I`'s own, original
        # behavior, not something `-P`, added later in 3.11, split out of
        # it) -- empirically confirmed this round, on BOTH interpreters this
        # generated script can run under here (Xcode's /usr/bin/python3
        # 3.9.6, and Homebrew's python3), that `-B -I` alone leaves the real
        # stdlib hashlib imported (not a same-directory shadow) with
        # sys.path containing no script-relative entry at all.
        #
        # `-P` (a narrower flag introduced in Python 3.11 that isolates
        # sys.path without `-I`'s other side effects, e.g. ignoring
        # PYTHON*-prefixed environment variables) is deliberately NOT added
        # alongside it despite an earlier draft of this fix, and the review
        # finding that prompted it, suggesting both: this exact machine's
        # own `/usr/bin/python3` -- the literal, hardcoded interpreter path
        # this line execs, not whatever `python3` a PATH lookup would find
        # -- is Xcode Command Line Tools' Python 3.9.6, which does not
        # implement `-P` at all (`Unknown option: -P`, exit 2) and would
        # refuse to start entirely, breaking EVERY managed `prime-agent`
        # invocation outright -- a strictly worse regression than the
        # shadowing vector this fix closes. Verified empirically, not
        # assumed: `/usr/bin/python3 -B -I -P <script>` fails to start on
        # this machine's real interpreter; `/usr/bin/python3 -B -I <script>`
        # succeeds and already defeats the shadow, on both interpreters.
        # `-I` alone is therefore both necessary and sufficient here; `-P`
        # would be a redundant no-op on a 3.11+ interpreter (already implied
        # by `-I` there) and a hard failure on this one -- pure downside,
        # no additional coverage, on the actual target platform. The launch
        # guard imports only fcntl/hashlib/os/stat/subprocess/sys (all
        # stdlib, resolved from the interpreter's own compiled-in search
        # path, never from a script-relative directory), so `-I` removes
        # nothing this script actually needs -- confirmed by this round's
        # own regression test, which plants a shadowing bin/hashlib.py next
        # to a real copy of this generated script and invokes it exactly
        # this way, under both interpreters.
        f'exec /usr/bin/python3 -B -I {shlex.quote(os.fspath(launch_guard))} "$@"',
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def managed_launch_guard_script(
    node: Path, cli: Path, node_sha256: str, cli_sha256: str
) -> bytes:
    """`node_sha256`/`cli_sha256` are SHA-256 content digests -- for NODE,
    captured at extraction time by extract_node_toolchain() (before any
    `npm` subprocess this installer does not control ever ran); for CLI,
    PINNED to the original, digest-verified prime-agent tarball's own
    per-file digest for dist/bundle/cli.js (via make_patched_asset()'s
    content_digests return value), and only confirmed -- never merely
    read and trusted -- against `npm ci`'s actual materialized output
    immediately after it returns (see
    _install_locked_within_release_dir()). Round 25, 2026-08-19
    (independent Claude opus/max round-25 review, P1-A): an earlier
    version of this docstring claimed the post-`npm ci` disk read itself
    was "the earliest point [CLI's] final, trustworthy value can be
    known" -- that was false, since a same-UID racer who overwrites CLI's
    path DURING `npm ci`'s own runtime (not merely after it returns) had
    their bytes read and trusted as this permanent baseline. Anchoring to
    the pre-npm, tarball-derived digest instead means the baseline can
    never be attacker-influenced regardless of when the swap happens --
    baked into the generated script as
    NODE_SHA256/CLI_SHA256 and checked by validate_exec_target() before
    EVERY future real invocation of the managed command, not merely during
    this install. Round 22's launch guard validated NODE/CLI structurally
    (no symlink, inside SSD_ROOT, regular file, owned by this UID, private
    mode) but never their CONTENT, so a same-UID racer who won the
    install-time race this round's other fixes now close, or who swaps
    either file's content at any point AFTER a legitimate install
    completes, previously exec'd undetected forever after (independent
    Codex sol/max round-23 review, 2026-08-19, P1-1/P1-2: "...launch
    guard's baked-in NODE= points at the attacker binary, verify()
    passes").

    Round 40, 2026-08-20 (independent Claude opus/max round-39 review,
    P1-2): everything above -- and everything validate_exec_target() below
    checks -- verifies NODE and CLI themselves. Nothing here, through round
    39, considered two entirely different things real Node itself does once
    it actually starts running CLI: (1) it reads NODE_OPTIONS/NODE_PATH/
    NODE_PRESERVE_SYMLINKS(_MAIN) from whatever process environment this
    script happens to inherit -- there was no `env=` argument to the
    `subprocess.run([NODE, CLI, ...])` call below at all before this round,
    so the FULL parent environment passed through unscrubbed, and
    NODE_OPTIONS in particular can inject `--require`/`--loader`/`--import`
    and run attacker code before CLI's own first line ever executes,
    independent of anything this script's own path/content checks cover;
    (2) its own CommonJS module resolution (`Module._nodeModulePaths`)
    walks from CLI's directory upward through EVERY ancestor directory,
    appending "node_modules" at each level, all the way to the real
    filesystem root -- confirmed empirically against this exact pinned
    Node version (real `Module._nodeModulePaths(...)` output, real
    ancestor walk, real termination at "/") -- reaching well outside
    RELEASE_DIR, which is the only tree tree_digest() (and therefore this
    installer's entire verification model) ever looks at. On this actual
    machine the walk crosses five same-UID-writable ancestor locations
    before reaching SSD_ROOT itself, which this round separately confirmed
    (real `stat`/`diskutil info`) is root:wheel-owned with no other-write
    bit on a real, Owners-enabled APFS volume -- so the practical exposure
    is bounded, not unbounded, but those five locations are real and were
    entirely unconsidered by anything in this file. `scrubbed_node_
    environment()` and `unexpected_ancestor_node_modules()` below close (1)
    and mitigate (2) respectively -- see each function's own docstring, and
    this installer's round-40 dispatch notes, for the exact scope closed
    and the residual this leaves (the RELEASE_DIR-internal ancestor
    locations, items already covered at install time by tree_digest()'s own
    completeness invariant but not re-checked on every single launch, is a
    separate, pre-existing, already-documented residual -- see
    validate_exec_target()'s own docstring below for that one -- not new to
    this round).
    """
    lock = lifecycle_lock_path()
    ssd_root = SSD_ROOT
    release_dir = RELEASE_DIR
    source = f'''#!/usr/bin/python3
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import subprocess
import sys

LOCK = {os.fspath(lock)!r}
SSD_ROOT = {os.fspath(ssd_root)!r}
RELEASE_DIR = {os.fspath(release_dir)!r}
NODE = {os.fspath(node)!r}
CLI = {os.fspath(cli)!r}
NODE_SHA256 = {node_sha256!r}
CLI_SHA256 = {cli_sha256!r}
MAX_EXEC_TARGET_BYTES = {MAX_TAR_EXPANDED_BYTES!r}
RESOURCE_GUARD_ENV = "ORCA_PRIME_AGENT_RESOURCE_GUARD"
RESOURCE_GUARDS = ("--no-extensions", "--no-skills", "--no-prompt-templates")
# Single source of truth shared with managed_entrypoint_script()'s generated
# shell resolve_managed_command() -- both derive from install_prime_agent.py's
# module-level LEADING_COMMAND_OPTIONS constant (see its docstring there) so
# a future leading option is recognized by both generated parsers together.
LEADING_COMMAND_OPTIONS = {LEADING_COMMAND_OPTIONS!r}
RUNTIME_PUBLIC_COMMANDS = frozenset(("agents", "attach", "model", "session"))
# Historically mirrored the shell entrypoint's own "public_commands" set
# exactly; as of round 8, 2026-08-18 it deliberately no longer does. The two
# lists now answer two DIFFERENT questions and have intentionally diverged:
#   * managed_entrypoint_script()'s public_commands decides session_start --
#     whether the $PWD/.prime/agent/settings.json gate and
#     ORCA_PRIME_AGENT_RESOURCE_GUARD export apply at all. "config" and
#     "package" were removed from that list in round 8 (P1: upstream's own
#     handlers for both can still touch the launch project -- see
#     managed_entrypoint_script()'s public_commands comment for the full
#     finding) and now get full session-start protection; "session" was
#     removed in round 10 for the same reason (see that comment's round-10
#     paragraph -- "session export """ falls through to a real,
#     unprotected session start).
#   * RUNTIME_PUBLIC_COMMANDS here decides a narrower, DIFFERENT question:
#     given that ORCA_PRIME_AGENT_RESOURCE_GUARD=1 WAS exported (a genuine
#     session start), WHERE should RESOURCE_GUARDS actually be spliced into
#     this command's own argv so upstream's real parser still accepts it?
#     "agents"/"attach"/"model" answer this the same way: upstream's own
#     dispatcher (runPublicCommand() in dist/cli/public-command.js) requires
#     each command's own name to be literally remaining[0] (module the
#     narrow daemon-socket-prefix cases resolve_upstream_public_command()
#     itself models), so RESOURCE_GUARDS must land somewhere AFTER
#     remaining[0], never before it or spliced between it and a fixed
#     literal token upstream itself requires immediately after it (e.g.
#     "list" for "model", "export" for "session", the agent name for
#     "attach"). "session" joined this set in round 10, 2026-08-18
#     (independent review, P1): verified directly against upstream's
#     rewriteNestedCommand()/splitOperandsAndOptions() (dist/cli/
#     public-command.js) that "session"/"export" token adjacency is
#     preserved by the current placement.
#
#     Round 13/14, 2026-08-18 (independent review, P1-A -- the sixth round
#     to touch this general area, after 3, 4, 6, 8, and 10): the ACTUAL
#     placement used to be append-after-everything (right before a trailing
#     "--" if present, else at the very end), on the theory that
#     parseArgs() "recognizes RESOURCE_GUARDS by exact string equality
#     scanned across the whole argv, not by position". That theory is true
#     but incomplete -- it ignores that upstream's real parseArgs()
#     (dist/cli/args.js) unconditionally consumes the token immediately
#     AFTER 17 different value-taking options as that option's OWN value
#     (`args[++i]`, no check on what the next token looks like), so
#     whenever the user's own last real token was one of those 17 options
#     with a missing/empty value (trivially reachable via plain shell
#     expansion of an unset variable, e.g. `model list --model
#     $UNSET_VAR`), the first appended guard flag was consumed as THAT
#     option's value instead of ever being scanned as its own token --
#     RESOURCE_GUARDS silently did nothing, and a hostile project's
#     extensions loaded (60 confirmed reproductions, real generated wrapper
#     -> real generated launch guard -> real pinned Node -> the real
#     prime-agent-0.7.2.tgz bundle). The fix, in
#     insert_resource_guards_before_first_flag() just below (see its own
#     docstring for the full per-command verification): splice
#     RESOURCE_GUARDS in right before the FIRST token (scanning from
#     remaining[1] onward) that starts with "-", instead of at the very
#     end. The token immediately preceding that insertion point is, by
#     construction, never itself a flag -- so it can never treat a guard as
#     its own value -- and this placement still lands after every fixed
#     literal token ("list"/"export"/the agent name) each command requires
#     immediately following remaining[0], so none of those adjacency
#     requirements are disturbed either. "help" deliberately did NOT join
#     this set -- see is_help_command_request() and guarded_arguments()
#     below for why ANY guard insertion (not just the old placement) is
#     UNSAFE for a help MATCH specifically (it silently turns a legitimate
#     `help <realtopic>` into an "Unknown command" error) and what this
#     script does instead.
#   * RUNTIME_NO_GUARD_COMMANDS just below answers the same "where" question
#     for the remaining public commands, differently: NOWHERE. "config" and
#     "package" stay in this set because inserting RESOURCE_GUARDS at the
#     position this script's fallback branch would use for them has never
#     been verified against upstream's real argv acceptance for those two
#     specific commands, and getting that placement wrong is exactly the
#     round-3/4 argv-corruption bug class below; the settings-file gate
#     (applying to both by default since round 8) is the actual fix for the
#     round-8 finding, and leaving these two out of guard-flag insertion
#     remains an intentional, separate, defense-in-depth choice, not an
#     oversight. The rest (doctor/list/rename/schedule/send/shutdown/status/
#     stop) genuinely never read $PWD/.prime/agent/settings.json or load
#     extensions at all -- see managed_entrypoint_script()'s public_commands
#     for the daemon-socket commands they dispatch to instead -- so
#     RESOURCE_GUARDS would be a no-op for them even if inserted, and this
#     set just documents that instead of guessing a placement for flags
#     that would never do anything anyway.
# Checked here as a hard safety net, not merely relying on the entrypoint's
# own session_start invariant continuing to hold: if RESOURCE_GUARD_ENV ever
# reached this script as "1" for one of these anyway, the prior fallback
# branch below would have inserted RESOURCE_GUARDS between a leading
# "--daemon-socket <value>" pair and the command token itself -- corrupting
# the command line for every one of these commands except stop/rename
# (independent review round 3/4, 2026-08-18, P1 companion: guarded_arguments()
# only recognized agents/attach/model as "the command is at remaining[0]";
# every other public command fell through to the plain-flags fallback that
# assumes remaining has no command token at all).
RUNTIME_NO_GUARD_COMMANDS = frozenset(
    (
        "config",
        "doctor",
        "list",
        "package",
        "rename",
        "schedule",
        "send",
        "shutdown",
        "status",
        "stop",
    )
)
# Round 40, 2026-08-20 (independent Claude opus/max round-39 review, P1-2
# item 1): NODE_OPTIONS/NODE_PATH/NODE_PRESERVE_SYMLINKS(_MAIN)/
# NODE_REPL_EXTERNAL_MODULE each directly influence, or (NODE_OPTIONS) can
# inject arbitrary code into, Node's own startup and module resolution --
# independent of anything validate_exec_target() below checks (that only
# ever inspects NODE/CLI themselves, never the environment they run in).
#
# Round 43, 2026-08-20 (independent Claude opus/max round-42 review,
# P1-A): round 40's approach above was a DENYLIST -- start from the full
# environment, remove five named keys. Node/OpenSSL/npm's own
# environment-variable surface is large and not fully enumerable by this
# installer, so a denylist can only ever be as complete as whatever this
# installer's authors already happened to know about. Reproduced for real
# against this installer's exact pinned Node binary (v24.19.0
# darwin-arm64): with a real self-signed TLS certificate and a real
# loopback TLS server, NODE_TLS_REJECT_UNAUTHORIZED=0 and
# NODE_EXTRA_CA_CERTS -- real, documented Node environment variables
# (https://nodejs.org/api/cli.html#node_tls_reject_unauthorizedvalue,
# https://nodejs.org/api/cli.html#node_extra_ca_certsfile) -- each
# silently changed a real TLS trust decision when inherited unscrubbed,
# exactly like the NODE_OPTIONS finding round 40 already fixed; and
# NODE_COMPILE_CACHE (https://nodejs.org/api/module.html#module-compile-
# cache) -- only ever indirectly blocked via this installer's own,
# separate NODE_DISABLE_COMPILE_CACHE=1 export, never itself in round 40's
# denylist -- really did write real V8 compile-cache files to an
# attacker-chosen directory when set (all three reproduced with the real
# pinned Node binary; see this round's regression tests). None of round
# 40's five denied keys covered any of these.
#
# NODE_ENV_ALLOWED_NAMES/NODE_ENV_ALLOWED_PREFIXES below replace the
# denylist with an ALLOWLIST: scrubbed_node_environment() now starts from
# nothing and copies across only a name this installer has positively
# reviewed and can justify, instead of starting full and trying to
# enumerate everything dangerous. This is deliberately NOT a minimal,
# wholesale-replaced environment the way managed_npm_environment() builds
# for install-time npm invocations (see that function's own docstring) --
# this exec is the user's real interactive session, and the real,
# materialized upstream CLI bundle (dist/bundle/cli.js and its own
# dependencies, inspected directly for this round) genuinely reads a
# wide, but bounded and reviewed, set of session/locale/terminal/
# credential variables to function as an interactive, TUI, multi-provider
# AI coding agent. What is explicitly, deliberately excluded regardless:
# every Node/V8/OpenSSL/native-addon-loading environment variable this
# round found documented (or, for OPENSSL_CONF, previously reported) to
# influence code loading, TLS trust, or module resolution -- round 40's
# original five, this round's three newly reproduced above, plus
# OPENSSL_CONF, OPENSSL_MODULES, OPENSSL_ENGINES, SSL_CERT_FILE,
# SSL_CERT_DIR, NODE_ICU_DATA, NODE_REDIRECT_WARNINGS, NODE_V8_COVERAGE,
# NODE_REPL_HISTORY, and NAPI_RS_NATIVE_LIBRARY_PATH (a native-addon
# dlopen() path override the real upstream bundle's own dependency tree
# reads -- the same "attacker-controlled path gets dlopen()'d" shape as
# the Node/OpenSSL items, just for a native addon instead of a Node
# module or OpenSSL provider).
#
# OPENSSL_CONF specifically: this round built a real, compiled, harmless
# marker .dylib and a real OpenSSL 3.x `[provider_sect]` config activating
# it, and confirmed the *mechanism* is real -- the real system `openssl`
# CLI (a dynamically-linked OpenSSL 3.6.3 build) genuinely dlopen()'d it
# and ran its constructor. Against THIS installer's exact pinned Node
# binary specifically, though, that same config/exec chain did not
# reproduce (confirmed: `process.config.variables.openssl_is_fips` is
# false, and this Node build's statically-linked OpenSSL did not visibly
# consult OPENSSL_CONF for provider auto-activation for a plain script or
# for `--openssl-config=<file>` in real testing) -- Node's own docs hedge
# this exact variable's effect as being "among other uses... to enable
# FIPS-compliant crypto if Node.js is built with ./configure
# --openssl-fips", consistent with what was observed. OPENSSL_CONF is
# excluded from the allowlist regardless: it costs nothing to exclude, it
# is Node's own documented environment variable for exactly this
# influence-code-loading category, and this installer does not control
# what a future Node version, build configuration, or FIPS mode does with
# it -- only that this round's own regression tests could not reproduce
# live exploitation against the one pinned binary this installer actually
# ships is recorded here as an honest, narrower finding than initially
# suspected, not a reason to allow it.
NODE_ENV_ALLOWED_PREFIXES = ("LC_",)

NODE_ENV_ALLOWED_NAMES = frozenset(
    (
        # Session/OS basics.
        "HOME", "PATH", "PWD", "OLDPWD", "SHELL", "USER", "LOGNAME",
        "TMPDIR", "TEMP", "TMP", "EDITOR", "VISUAL",
        "XDG_DATA_HOME", "XDG_CACHE_HOME",
        # Locale (LC_* itself is covered by NODE_ENV_ALLOWED_PREFIXES).
        "LANG", "LANGUAGE",
        # Terminal/TUI detection -- prime-agent is a TUI application (see
        # README.md's own "TUI release" wording); the real bundle reads
        # every one of these to detect terminal capabilities correctly.
        "TERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "COLUMNS", "LINES",
        "COLORTERM", "COLORFGBG", "TMUX", "TMUX_SESSION_ID",
        "ITERM_SESSION_ID", "WEZTERM_PANE", "KITTY_WINDOW_ID",
        "GHOSTTY_RESOURCES_DIR", "TERMUX_VERSION", "OSTYPE",
        "SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY", "CI",
        # Network -- corporate/proxied network access to a model provider
        # is a real, legitimate use case; these are data (destination
        # host), not code-loading or trust-relevant.
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
        # Node's own vars confirmed NOT to influence code loading, TLS
        # trust, or module resolution: NODE_ENV is not even a Node-core
        # var (an app-level convention only); NODE_DEBUG only toggles
        # extra diagnostic printing for named core modules
        # (https://nodejs.org/api/all.html#node_debugmodule);
        # NODE_DISABLE_COMPILE_CACHE only ever *disables* the compile
        # cache this installer already wants disabled (the opposite
        # direction from the excluded NODE_COMPILE_CACHE above), and is
        # exported by managed_entrypoint_script() itself.
        "NODE_ENV", "NODE_DEBUG", "NODE_DISABLE_COMPILE_CACHE",
        # This installer's own env, generated by managed_entrypoint_script()
        # and required for the managed wrapper's own documented contract
        # (README.md "For session starts it also:").
        "PRIME_AGENT_CODING_AGENT_DIR", "PRIME_AGENT_LAUNCHER_PATH",
        "PRIME_AGENT_SESSION_DIR", "PRIME_AGENT_CODING_AGENT_SESSION_DIR",
        "PRIME_AGENT_TELEMETRY", "DO_NOT_TRACK", "PI_SKIP_VERSION_CHECK",
        "PI_OFFLINE", "PYTHONDONTWRITEBYTECODE",
        "ORCA_PRIME_AGENT_RESOURCE_GUARD",
        # Model-provider credentials/endpoints -- read directly by the
        # real, materialized upstream dist/bundle/cli.js and its own
        # dependencies (confirmed by inspecting the real extracted
        # v0.7.2 release tree for this round); without these the CLI
        # cannot call any model at all. Pure data (keys, IDs, URLs, file
        # paths to credential/token files) -- none of these load code,
        # change TLS trust, or affect Node's own module resolution.
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "ANTHROPIC_LOG",
        "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID", "OPENAI_API_VERSION", "OPENAI_BASE_URL",
        "OPENAI_WEBHOOK_SECRET", "OPENAI_LOG",
        "AZURE_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_BASE_URL",
        "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT_NAME_MAP",
        "AZURE_OPENAI_RESOURCE_NAME", "AZURE_OPENAI_API_VERSION",
        "AZURE_ENDPOINT",
        "GEMINI_API_KEY", "GEMINI_NEXT_GEN_API_BASE_URL",
        "GEMINI_NEXT_GEN_API_LOG",
        "MISTRAL_API_KEY", "MISTRAL_BASE_URL",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION", "AWS_DEFAULT_REGION",
        "AWS_PROFILE", "AWS_BEDROCK_BASE_URL",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_CLOUD_PROJECT", "GOOGLE_PROJECT_ID", "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_API_KEY", "GCLOUD_PROJECT", "CLOUDSDK_CONFIG",
        # Debug/diagnostic toggles that are pure data (no path or
        # code-loading implication).
        "DEBUG", "DEBUG_AUTH", "LOG_TOKENS",
    )
)


def scrubbed_node_environment() -> dict[str, str]:
    """An ALLOWLIST-filtered copy of this process's real environment --
    only a key in NODE_ENV_ALLOWED_NAMES, or starting with a prefix in
    NODE_ENV_ALLOWED_PREFIXES, survives -- for the SOLE subprocess this
    script ever execs: `NODE CLI ...`. Round 40, 2026-08-20 (independent
    Claude opus/max round-39 review, P1-2 item 1): before that round,
    `subprocess.run([NODE, CLI, ...])` in main() below passed no `env=`
    argument at all, so the child inherited this process's FULL
    environment completely unfiltered -- a same-UID actor who set
    NODE_OPTIONS (e.g. to "--require=/path/to/attacker.js") anywhere
    upstream of this exec (the calling shell's own exported environment,
    a wrapper script, an inherited launchd/tmux/orchestration
    environment) had that flag consumed by Node BEFORE CLI's own first
    line ever ran, and every check this script performs above (LOCK
    identity, NODE/CLI structural and content validation) reports success
    regardless, because none of them inspects the process environment at
    all. Round 43, 2026-08-20 (independent Claude opus/max round-42
    review, P1-A): round 40's fix was a five-key denylist, which missed
    OPENSSL_CONF/NODE_TLS_REJECT_UNAUTHORIZED/NODE_EXTRA_CA_CERTS/
    NODE_COMPILE_CACHE and could not, by construction, ever be complete --
    see NODE_ENV_ALLOWED_NAMES's own comment just above for the full
    reasoning and the real-Node reproductions behind this round's switch
    to an allowlist. Deliberately avoids a dict/set-literal comprehension
    here (a plain loop plus `dict()`, with no curly-brace literal at all)
    purely to keep this source text simple to review -- functionally
    identical to a filtering comprehension.
    """
    allowed: dict[str, str] = dict()
    for key, value in os.environ.items():
        if key in NODE_ENV_ALLOWED_NAMES or any(
            key.startswith(prefix) for prefix in NODE_ENV_ALLOWED_PREFIXES
        ):
            allowed[key] = value
    return allowed


def unexpected_ancestor_node_modules() -> str | None:
    """Round 40, 2026-08-20 (independent Claude opus/max round-39 review,
    P1-2 item 4): real Node's own CommonJS module resolution
    (`Module._nodeModulePaths`) walks from CLI's directory upward through
    EVERY ancestor directory, appending "node_modules" at each level, all
    the way to the real filesystem root -- confirmed empirically against
    this exact pinned Node version, independent of anything RELEASE_DIR-
    scoped this installer's own tree_digest() walk ever considers. Every
    ancestor node_modules location STRICTLY ABOVE RELEASE_DIR is something
    this installer never legitimately creates -- every write this
    installer performs during a real install stays within RELEASE_DIR
    itself, or within sibling, dot-prefixed per-tool home directories
    under LOCAL_HOMES that are never named "node_modules" (empirically
    confirmed absent on this real managed prefix's real ancestor chain at
    the time this round was implemented) -- so this refuses outright on
    ANY presence at all (file, symlink, or directory; `os.path.lexists`
    does not follow the final symlink component, so a dangling symlink is
    still caught) rather than trying to compare against some previously
    recorded baseline, which would need a place to durably store that
    baseline and a mechanism to keep it from itself becoming stale. Walks
    to the real filesystem root ("/") rather than stopping at SSD_ROOT: on
    this actual machine SSD_ROOT itself is root:wheel-owned with no
    other-write bit (confirmed via `stat`/`diskutil info` on a real,
    Owners-enabled APFS volume, so genuinely not same-UID-writable here),
    but this script does not assume that permission state holds forever
    (a remount, a different machine, a future ownership change) -- the
    extra ~2-3 stat calls this costs are negligible, and checking
    unconditionally is strictly more robust than trusting an assumption
    about current filesystem permissions. Returns the first offending
    "<ancestor>/node_modules" path found, or None if the whole chain is
    clean.

    Round 43, 2026-08-20 (independent Claude opus/max round-42 review,
    P1-B): the ancestor walk above models `Module._nodeModulePaths` only.
    Real Node's actual resolution (`Module._resolveLookupPaths`) is that
    walk PLUS `Module.globalPaths` -- see node_global_folder_paths()'s own
    docstring just below for how each of its three locations was
    empirically confirmed against this exact pinned Node binary. Checked
    here too, after the ancestor walk finds nothing, with the identical
    refuse-on-any-presence logic -- none of the three should ever
    legitimately exist either.
    """
    current = RELEASE_DIR
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        candidate = os.path.join(current, "node_modules")
        if os.path.lexists(candidate):
            return candidate
    for candidate in node_global_folder_paths():
        if os.path.lexists(candidate):
            return candidate
    return None


def node_global_folder_paths() -> list[str]:
    """The three locations `Module.globalPaths` adds to real Node's own
    module resolution beyond the plain ancestor `node_modules` walk --
    empirically confirmed against this installer's exact pinned Node
    binary (v24.19.0 darwin-arm64): real `require('module').globalPaths`
    output, captured under `env -i` both with and without HOME/NODE_PATH
    set, rather than assumed from memory, since this is version- and
    platform-dependent. `$HOME/.node_modules` and `$HOME/.node_libraries`
    only when HOME is a non-empty string -- real Node's own `if
    (homeDir)` check treats an unset HOME and an empty one identically
    (both confirmed empirically), so `os.environ.get("HOME")` -- exactly
    the value scrubbed_node_environment() passes through unscrubbed to
    the one real Node invocation that matters -- is used the same way
    here. The third is derived from the Node binary's own install
    location, exactly the way real Node itself derives it
    (`path.resolve(process.execPath, '..', '..')` + "lib/node") --
    `os.path.dirname(os.path.dirname(NODE))` here, safe to use directly
    (rather than needing `os.path.realpath(NODE)` first) because NODE has
    already been confirmed symlink-free by validate_exec_target() before
    this is ever called from main() below, so NODE already equals its own
    realpath. On this installer's fixed toolchain layout
    (RELEASE_DIR/toolchain/bin/node) this resolves to
    RELEASE_DIR/toolchain/lib/node -- itself INSIDE RELEASE_DIR, not an
    ancestor, so the ancestor walk above never reaches it; confirmed
    against the real pinned Node darwin-arm64 tarball's own contents that
    extraction never creates a "lib/node" directory there (only
    "lib/node_modules"), so this is exactly as safe to refuse-on-presence
    as every other location this script checks. NODE_PATH-derived
    globalPaths entries are deliberately not modeled here: NODE_PATH is
    unconditionally removed by scrubbed_node_environment() before this
    exact exec, so real Node will see no NODE_PATH at all for this
    invocation and contributes no such entries to its own globalPaths.

    Round 45, 2026-08-20 (independent Claude opus/max round-44 review):
    the two HOME-relative entries are wrapped in os.path.abspath() (pure
    lexical normalization, no filesystem access) instead of a bare
    os.path.join(), matching real Node's path.resolve(homeDir, name)
    which normalizes .. / . / duplicate separators purely lexically. A
    bare os.path.join() left a HOME containing ".." through a nonexistent
    path component un-normalized, so the lexists() check below hit ENOENT
    on the un-normalized string and refused to flag a directory real Node
    itself resolves to and actually loads code from -- see
    node_global_folder_paths()'s verify()-side sibling docstring, just
    above managed_launch_guard_script() in this same file, for the full
    empirical verification (including the one confirmed-benign textual
    divergence: a leading double-slash HOME, where os.path.abspath()
    keeps two leading slashes but real Node collapses to one -- harmless
    here because this platform's own filesystem resolution treats both
    forms as the identical inode).
    """
    paths: list[str] = []
    home = os.environ.get("HOME")
    if home:
        paths.append(os.path.abspath(os.path.join(home, ".node_modules")))
        paths.append(os.path.abspath(os.path.join(home, ".node_libraries")))
    paths.append(os.path.join(os.path.dirname(os.path.dirname(NODE)), "lib", "node"))
    return paths


def fail(message: str) -> int:
    print(f"prime-agent: {{message}}", file=sys.stderr)
    return 78


def split_leading_options(arguments: list[str]) -> tuple[list[str], list[str]]:
    """Skip every recognized leading option, in whatever form it appears
    (bare "--opt value" or "--opt=value", repeated any number of times),
    and return (leading_prefix, remaining_arguments) so every caller that
    needs "the real command token" reads remaining[0] -- never a fixed
    argv position or a single hardcoded form -- exactly mirroring the
    shell entrypoint's own resolve_managed_command() (independent review
    round 5, 2026-08-18, FIX 1: the prior one-shot
    "arguments[0] == '--daemon-socket'" check missed the "=" form and any
    repeated/duplicate occurrence, corrupting guard-flag placement for
    those forms).
    """
    prefix: list[str] = []
    remaining = list(arguments)
    while remaining:
        head = remaining[0]
        if head in LEADING_COMMAND_OPTIONS and len(remaining) >= 2:
            prefix.extend(remaining[:2])
            remaining = remaining[2:]
            continue
        if any(head.startswith(name + "=") for name in LEADING_COMMAND_OPTIONS):
            prefix.append(head)
            remaining = remaining[1:]
            continue
        break
    return prefix, remaining


def resolve_upstream_public_command(arguments: list[str]) -> str | None:
    """Reproduce upstream's own normalizeLeadingDaemonSocketOption()
    (dist/cli/public-command.js) EXACTLY -- return the single command token
    upstream will actually treat as an already-resolved public command for
    this exact argv, or None when upstream will actually fall through to a
    real session start instead.

    Deliberately narrower than split_leading_options(): upstream only ever
    remaps a SINGLE bare, space-separated "--daemon-socket <value>" pair --
    literally arguments[0] == "--daemon-socket", never the "=" form -- and
    only when the token immediately after that pair is exactly "stop" or
    "rename"; every other form (a repeated/second occurrence, the "="
    form, or a bare pair followed by any other token) is left completely
    alone by upstream and actually starts a real session. "--daemon-socket"
    is intentionally a literal here, not derived from
    LEADING_COMMAND_OPTIONS, for the same reason the shell entrypoint's
    resolve_public_command() hardcodes it: this models one specific,
    bespoke upstream function's exact behavior, not a generic mechanism.

    split_leading_options() is intentionally MORE thorough than this for
    OUR OWN wrapper policy questions that only ever need to become MORE
    restrictive when they over-match. guarded_arguments()'s "does this
    invocation need RESOURCE_GUARDS" decision is the opposite: it must
    default to inserting guards (protected) for every form upstream would
    actually treat as a session start, so it asks this stricter question
    instead (independent dual review round 6, 2026-08-18, P1 companion to
    managed_entrypoint_script()'s session_start fix: the shared, more
    thorough split_leading_options() previously made guarded_arguments()
    skip inserting RESOURCE_GUARDS for the exact same forms upstream
    actually treats as a session start, e.g. "--daemon-socket=<v> status"
    or "--daemon-socket <v> status").
    """
    if (
        len(arguments) >= 3
        and arguments[0] == "--daemon-socket"
        and arguments[2] in ("stop", "rename")
    ):
        return arguments[2]
    if (
        arguments
        and arguments[0] not in LEADING_COMMAND_OPTIONS
        and not any(arguments[0].startswith(name + "=") for name in LEADING_COMMAND_OPTIONS)
    ):
        return arguments[0]
    return None


# Round 10, 2026-08-18 (independent review, P1), REVISED round 12,
# 2026-08-18 (independent review, P1): round 10 originally faithfully
# transcribed the pinned v0.7.2 upstream's own isHelpCommandRequest() /
# findCommandSuggestion() / editDistance() FUZZY matcher
# (dist/cli/command-registry.js, read unminified) into Python. Round 11's
# independent review found a real, exploitable divergence in that
# transcription: upstream measures string length in JS UTF-16 code units,
# the Python port used code-point length (plain len() on a str) -- these
# disagree for any character outside the Basic Multilingual Plane (most
# emoji), so e.g. "help status\U0001F600\U0001F600" computed a fuzzy-MATCH
# in Python (guards skipped) but a MISS in the real upstream Node CLI (a
# real, unprotected session start loading $PWD/.prime/agent/extensions/*.js)
# -- reproduced end-to-end, zero opt-in, no settings.json required. This is
# the second real bug the hand-reimplementation approach produced in as many
# rounds (round 10 built it, round 11 found it wrong), so round 12
# deliberately ABANDONS fuzzy/edit-distance matching entirely:
# is_help_command_request() below now does EXACT, case-sensitive membership
# checks only, against the same pinned COMMAND_SPECS / REMOVED_COMMAND_NAMES
# set -- a finite string-set membership test has no length semantics, no
# distance calculation, and therefore no room for this class of bug.
# Traded away deliberately: upstream's "did you mean" fuzzy suggestion for a
# genuine typo (e.g. "help satus" meaning "status") no longer passes
# through unguarded the way upstream's own dispatcher would handle it --
# it now gets RESOURCE_GUARDS applied like any other miss. This is safe:
# the guards disable extension/skill/prompt-template loading, so a hostile
# project's extensions never load for this case either. It is NOT low-cost
# in the way an earlier version of this comment claimed, though -- verified
# directly (independent review round 13, 2026-08-18): once RESOURCE_GUARDS
# are appended, upstream's own argv parser no longer recognizes the
# resulting argv shape as a help lookup at all (the real unguarded output
# for "help satus" is "Error: Unknown command: satus" + a real "Did you
# mean" suggestion; guarded, upstream instead proceeds into a real,
# protected session start/TUI). So a genuine near-miss like "help satus"
# unexpectedly starts a guarded session instead of showing help text --
# still safe (no extension code runs), just not upstream's actual help UX.
# Documented here rather than "fixed" because reproducing upstream's exact
# UX for this specific miss case would mean going back to some form of
# fuzzy/near-match logic, which is exactly the risk this round removed.
#
# Unlike every other guard-placement decision in this script, "help"'s own
# upstream dispatcher (runPublicCommand() in dist/cli/public-command.js)
# treats `args[0] === "help" && isHelpCommandRequest(args.slice(1))` as a
# single, all-or-nothing choice between two mutually exclusive, INCOMPATIBLE
# argv shapes:
#   * a MATCH (isHelpCommandRequest() true) short-circuits into
#     printRequestedHelp(args.slice(1)), which walks args.slice(1) itself as
#     a literal command PATH -- formatCommandHelp()/getCommandSpec() there
#     require an EXACT path-LENGTH match, so appending anything after the
#     real topic (RESOURCE_GUARDS included) turns a legitimate
#     "help package" into "Unknown command: package --no-extensions
#     --no-skills --no-prompt-templates" instead of printing package's help
#     text (confirmed by tracing formatCommandHelp/getCommandSpec directly,
#     not assumed). printRequestedHelp() always returns HANDLED without
#     ever reaching SettingsManager or extension loading, so this case
#     needs NO guards at all -- a pure passthrough, like
#     RUNTIME_NO_GUARD_COMMANDS, is both safe and the only way to keep the
#     real help text intact.
#   * a MISS (isHelpCommandRequest() false) falls through runPublicCommand's
#     switch default to continueWith(args) -- the FULL, unmodified args
#     (still literally starting with the word "help") reach parseArgs() as
#     a real, unprotected session start, with "help" and its argument(s)
#     becoming ordinary positional chat messages. Before this round,
#     RUNTIME_NO_GUARD_COMMANDS treated this identically to the match case
#     (no guards, ever) -- so any argument upstream's fuzzy matcher misses
#     (e.g. "help zzzzzzzzzzzz", "help tols", "help mcp", "help auth")
#     reached a real session start with extension discovery fully enabled,
#     in any project, no settings.json required at all.
# guarded_arguments() below computes is_help_command_request() on the
# ORIGINAL, unmodified topic (remaining[1:], before any guard insertion) and
# only ever inserts guards for a confirmed miss, using the same
# first-flag-boundary placement RUNTIME_PUBLIC_COMMANDS uses (round 13/14,
# 2026-08-18: replaced the previous append-near-the-end / before-a-trailing-
# "--" placement -- see insert_resource_guards_before_first_flag()'s
# docstring for why that placement let a trailing value-hungry flag on the
# miss argument, e.g. "help --model", swallow the first guard token as its
# own value). That placement is safe for the miss case specifically because
# remaining[0] is always "help" here and everything after it reaches the
# same order-agnostic general parseArgs() as "agents" does -- and because
# the token immediately before the insertion point is, by construction,
# never itself a flag that could consume a guard as its value.
#
# This is pinned to v0.7.2's real COMMAND_SPECS; an upstream version bump
# that adds/renames/removes a command or subcommand needs this table
# re-verified against the new dist/cli/command-registry.js, exactly like
# every other version-pinned assumption in this file.
HELP_COMMAND_PATHS = frozenset(
    (
        ("agents",),
        ("attach",),
        ("config",),
        ("doctor",),
        ("help",),
        ("list",),
        ("model",),
        ("model", "list"),
        ("package",),
        ("package", "install"),
        ("package", "list"),
        ("package", "remove"),
        ("package", "update"),
        ("rename",),
        ("schedule",),
        ("schedule", "add"),
        ("schedule", "cancel"),
        ("schedule", "list"),
        ("send",),
        ("session",),
        ("session", "export"),
        ("shutdown",),
        ("status",),
        ("stop",),
        ("update",),
    )
)
HELP_REMOVED_COMMAND_NAMES = frozenset(
    ("app", "daemon", "install", "manage", "remove", "uninstall")
)


def help_command_spec_exists(path: tuple[str, ...]) -> bool:
    return path in HELP_COMMAND_PATHS


def is_help_command_request(path: list[str]) -> bool:
    # Round 12, 2026-08-18: EXACT membership checks only -- no fuzzy/
    # edit-distance matching (see the module comment above HELP_COMMAND_PATHS
    # for why that approach was abandoned after round 11 found a real bug in
    # round 10's fuzzy-matcher transcription). Any path not covered by one of
    # these three exact checks is treated as a miss, full stop.
    path_tuple = tuple(path)
    if len(path_tuple) == 0 or help_command_spec_exists(path_tuple):
        return True
    if path_tuple[0] in HELP_REMOVED_COMMAND_NAMES:
        return True
    if help_command_spec_exists(path_tuple[:1]):
        return True
    return False


def insert_resource_guards_before_first_flag(
    prefix: list[str], remaining: list[str]
) -> list[str]:
    """Splice RESOURCE_GUARDS into `remaining` immediately before the first
    token, scanning from remaining[1] onward, that starts with "-" -- a real
    flag or a literal "--" end-of-options separator alike -- or at the very
    end of `remaining` if no such token exists. remaining[0] is always the
    already-resolved command token itself (see resolve_upstream_public_
    command()) and is never scanned or touched.

    Round 13/14, 2026-08-18 (independent review, P1-A): this REPLACES the
    previous append-near-the-end / before-a-trailing-"--" placement, which
    had a real, structural bug predating even that placement's own
    introduction. Verified directly against the real pinned v0.7.2
    dist/cli/args.js: parseArgs() is a positional scanner that
    UNCONDITIONALLY consumes the very next argv token as a value for 17
    different value-taking options (exhaustively verified against the real
    source, not assumed): --mode, --daemon-socket, --provider, --model,
    --api-key, --cwd, --system-prompt, --append-system-prompt, --fork,
    --session-dir, --models, --tools/-t, --thinking, --extension/-e,
    --skill, --prompt-template, --theme (a further handful,
    e.g. --resume/-r, --print/-p, --autonomous-gate*, --goal*, either check
    the next token's own leading "-" first or use upstream's own
    hasRequiredOptionValue() to refuse a "--"-prefixed next token as a
    value, so they were never actually vulnerable to this). Appending
    RESOURCE_GUARDS near the end of the user's own argv meant that whenever
    the user's own last real token was one of those 17 options with a
    missing/empty value (the realistic, non-adversarial trigger: a shell
    expanding an unset variable, e.g. `model list --model $UNSET_VAR`
    leaving a literal trailing "--model"), the first appended guard flag
    ("--no-extensions") was silently consumed AS THAT OPTION'S VALUE
    instead of ever being reached as its own token -- the remaining two
    guards then landed as harmless stray tokens, and --no-extensions never
    took effect: a hostile project's $PWD/.prime/agent/extensions loaded
    with zero opt-in (60 confirmed reproductions across all 17 flag names,
    real generated wrapper -> real generated launch guard -> real pinned
    Node v24.19.0 -> the real prime-agent-0.7.2.tgz bundle). The module
    comments this replaces asserted parseArgs() "recognizes RESOURCE_GUARDS
    by exact string equality scanned across the whole argv, never by
    position" -- that specific claim is true (confirmed directly against
    args.js) but incomplete: it says nothing about a guard token never
    being *reached* as its own `arg` in the scan at all, which is exactly
    what happens when a preceding value-taking option's `args[++i]`
    consumes it first. Placement, not recognition, was always the gap.

    Placing RESOURCE_GUARDS immediately before the first flag-looking token
    instead is safe because the token immediately preceding the insertion
    point (whenever the insertion point isn't remaining[1] itself) is, by
    construction of this left-to-right scan, never itself a token starting
    with "-" -- i.e. never a flag, value-taking or otherwise -- so it can
    never treat the first inserted guard as its own consumed value. None of
    the three guard flags (--no-extensions/--no-skills/--no-prompt-
    templates) themselves consume a following token either (confirmed
    against args.js: all three are plain boolean sets, no `args[++i]`), so
    inserting them anywhere among a command's own flags never perturbs
    THEIR adjacency either. This holds for every one of
    RUNTIME_PUBLIC_COMMANDS's own structural requirements, each verified
    directly against the real dist/cli/public-command.js:
      * "agents": args.slice(1) (everything after "agents") reaches the
        general parseArgs() with no operand/option ordering requirement at
        all -- parseArgs() is a pure left-to-right scan indifferent to
        whether a flag or a positional message comes first -- so any
        placement within args.slice(1) that isn't itself swallowed is safe,
        this one included.
      * "attach": upstream requires rest[0] (remaining[1] here) to be the
        literal agent name -- never itself a flag in any invocation that
        was going to succeed anyway, since upstream independently rejects
        agent.startsWith("-") regardless of guard placement -- and requires
        every token after it (`options`) to be flags only
        (hasPositionalArguments(options) rejects any operand there).
        Scanning from remaining[1] onward finds the agent name first, skips
        it (never dash-prefixed), and lands the guards as the FIRST element
        of `options` -- before any of the user's own attach flags, so they
        can never be swallowed by one.
      * "model"/"session": rewriteNestedCommand() requires remaining[1] to
        be the literal subcommand token ("list"/"export") and
        splitOperandsAndOptions() requires every operand (0-1 for model,
        1-2 for session) to precede every option, split on the first
        dash-prefixed token. Scanning from remaining[1] onward skips the
        literal subcommand (never dash-prefixed) and any real operand
        (never dash-prefixed, by the operand/option split's own
        definition), landing guards exactly at the operand/option boundary
        upstream itself computes -- preserving the true operand count
        (never turning a legitimate `model list <search>` into an "Unknown
        model command" usage error the way naively prepending right after
        "list" would) and never appearing after a trailing value-hungry
        flag the way the old append-at-the-end placement did.
    The identical reasoning covers the "help" MISS call site below:
    remaining[0] is always "help" there too, and a miss falls through to
    the same order-agnostic general parseArgs() as "agents".

    A literal "--" is itself a "-"-prefixed token, so this scan naturally
    stops there too whenever no other flag precedes it -- subsuming the
    previous separator-respecting behavior as the case where no other flag
    exists ahead of the separator, while fixing the case where one does.
    """
    for index in range(1, len(remaining)):
        if remaining[index].startswith("-"):
            return [*prefix, *remaining[:index], *RESOURCE_GUARDS, *remaining[index:]]
    return [*prefix, *remaining, *RESOURCE_GUARDS]


def guarded_arguments(arguments: list[str]) -> list[str]:
    if os.environ.pop(RESOURCE_GUARD_ENV, None) != "1":
        return arguments
    resolved_command = resolve_upstream_public_command(arguments)
    prefix, remaining = split_leading_options(arguments)
    if resolved_command == "help":
        # remaining[0] is always the literal "help" token whenever
        # resolved_command is "help": resolve_upstream_public_command()
        # only ever resolves to "help" via its second branch (arguments[0]
        # itself -- its daemon-socket branch only ever resolves to "stop"
        # or "rename"), and that branch's own guard condition
        # ("arguments[0] not in LEADING_COMMAND_OPTIONS and ...") is exactly
        # the condition under which split_leading_options()'s loop leaves
        # remaining unchanged (breaks on its very first iteration), so
        # remaining[0] == arguments[0] == "help" here always.
        if is_help_command_request(remaining[1:]):
            # MATCH: printRequestedHelp() will short-circuit before ever
            # reaching extension loading, and inserting anything here would
            # corrupt the specific help text upstream prints -- passthrough.
            return [*prefix, *remaining]
        # MISS: falls through to a real, unprotected session start upstream
        # -- insert guards using the same first-flag-boundary placement as
        # RUNTIME_PUBLIC_COMMANDS below (safe here because remaining[0] is
        # always "help" and everything after it reaches the same
        # order-agnostic general parseArgs(); see
        # insert_resource_guards_before_first_flag()'s docstring for why
        # this placement -- unlike the old append-near-the-end one -- can
        # never be swallowed as some other flag's value).
        return insert_resource_guards_before_first_flag(prefix, remaining)
    if resolved_command is not None and resolved_command in RUNTIME_NO_GUARD_COMMANDS:
        # A non-session public command never receives resource-guard flags,
        # regardless of how it was invoked -- matches the entrypoint's own
        # session_start=0 treatment for these exactly (see
        # RUNTIME_NO_GUARD_COMMANDS above). resolved_command, not
        # remaining[0], gates this: only a form upstream itself would
        # actually resolve to this command may skip guards.
        return [*prefix, *remaining]
    if resolved_command is not None and resolved_command in RUNTIME_PUBLIC_COMMANDS:
        return insert_resource_guards_before_first_flag(prefix, remaining)
    return [*prefix, *RESOURCE_GUARDS, *remaining]


def validate_exec_target(path: str, root: str, expected_sha256: str) -> tuple[str | None, int]:
    # Round 13/14, 2026-08-18 (independent review, P2): NODE and CLI are the
    # two paths that matter MOST in this whole script -- they are what
    # actually gets exec'd -- yet, unlike LOCK just above (~15 lines of
    # symlink/containment/ownership/mode validation), they used to be handed
    # to subprocess.run() completely unvalidated. A same-UID actor who
    # replaced either path (symlink, or a swapped regular file) at any point
    # between this script being generated and this exact invocation running
    # would have had that replacement silently exec'd. Applies the identical
    # checks LOCK already gets: no symlink anywhere in the resolved path,
    # resolves inside SSD_ROOT, and (once resolved) is a regular,
    # current-uid-owned file with no group/other permission bits -- matching
    # the exact mode extract_node_toolchain()/safe_extract_main_asset()
    # themselves always write (0o700 for the executable NODE, 0o600 or
    # 0o700 for CLI/its own dependencies). Returns (error_message, fd) --
    # fd is -1 whenever error_message is not None, mirroring this script's
    # own fail()-based idiom (a plain return value, not an exception type
    # this standalone generated script never defines) rather than
    # introducing a new error-handling convention.
    #
    # Round 23, 2026-08-19 (independent Codex sol/max review, P1-1/P1-2):
    # these structural checks alone never verified CONTENT -- only that
    # whatever currently sits at `path` is a private, non-symlinked regular
    # file this UID owns. A same-UID racer who swapped the file's bytes in
    # place (same inode or not) while leaving every structural property
    # intact went undetected forever, both during install (see run_npm()'s
    # release_dir_fd bracketing and extract_node_toolchain()'s digest
    # capture for the install-time half of this fix) and for every real
    # invocation of the managed command after a legitimate install
    # completed. `expected_sha256` -- NODE_SHA256/CLI_SHA256, baked in by
    # managed_launch_guard_script() from a digest captured at the earliest
    # trustworthy moment (extraction time for NODE; immediately after `npm
    # ci` returns for CLI) -- closes that: this function now also reads
    # `path`'s full content, through the SAME O_NOFOLLOW-opened descriptor
    # used to re-confirm the leaf identity (not a second, independent
    # lexical open), and refuses unless its digest matches exactly.
    #
    # Round 47, 2026-08-20 (independent Codex sol/terra round-46 review,
    # P1): this function used to close its own descriptor before
    # returning, so `main()` validated NODE/CLI's identity+content through
    # one open()/read()/close() cycle and then handed the SAME path
    # strings to `subprocess.run([NODE, CLI, ...])`, which re-resolves
    # both by pathname a SECOND, entirely independent time to actually
    # exec them -- a same-UID racer who won the (previously unbounded)
    # window between this function returning and that later, separate
    # exec could still swap the target underneath it, with the hash check
    # having validated content that was never what actually ran.
    #
    # The intended fix (this round's original brief) was to bind the real
    # exec target to THIS SAME already-open, already-validated descriptor
    # via Darwin's /dev/fd/<n> instead of ever re-resolving by path again
    # -- so this function now returns the open descriptor on success
    # instead of closing it, letting the caller decide how to use it.
    #
    # That full fd-binding turned out to be empirically infeasible for
    # BOTH NODE and CLI on this exact platform/toolchain -- confirmed by
    # real, non-mocked tests (subprocess.run AND a raw os.fork()+
    # os.execve() that bypasses Python's subprocess machinery entirely),
    # not assumed from documentation, before writing any of the code
    # below:
    #
    #   NODE (the OS-level exec target itself): Darwin's /dev/fd entries
    #   report permission bits derived from the ORIGINAL open()'s access
    #   mode, not the underlying file's real mode -- os.fstat() on the raw
    #   descriptor shows the true 0700, but os.stat("/dev/fd/<n>") on that
    #   SAME descriptor shows an access-mode-filtered 0444 for an
    #   O_RDONLY open, or 0111 for an O_EXEC open -- and os.O_RDONLY and
    #   the BSD os.O_EXEC access modes are mutually exclusive per open
    #   file description on this platform: an O_EXEC descriptor cannot be
    #   read() (confirmed: Errno 9, EBADF) and an O_RDONLY descriptor
    #   cannot be exec'd via /dev/fd (confirmed: Errno 13, EPERM, both via
    #   subprocess.run(executable="/dev/fd/<n>", pass_fds=(...)) AND via a
    #   raw os.fork()+os.execve("/dev/fd/<n>", ...)) -- a real macOS
    #   restriction on executing through the fdesc pseudo-filesystem, not
    #   a Python/subprocess limitation, and not fixable by combining open
    #   flags: os.O_RDONLY | os.O_EXEC behaves exactly like plain
    #   os.O_EXEC (exec-shaped, unreadable). Darwin has no
    #   fexecve(2)-equivalent syscall reachable from Python at all.
    #
    #   CLI (the script argument NODE itself loads): reading CLI's
    #   content via /dev/fd/<n> DOES work -- a real Node process loads and
    #   runs a script referenced this way correctly, confirmed end to
    #   end. But doing so sets __dirname/__filename to
    #   "/dev/fd"/"/dev/fd/<n>" instead of CLI's real directory, confirmed
    #   with a real Node process running a script that does
    #   require(path.join(__dirname, 'sibling.js')): it throws "Cannot
    #   find module '/dev/fd/sibling.js'" instead of loading the sibling.
    #   The real, pinned dist/bundle/cli.js this installer ships is
    #   already documented elsewhere in this file (see the
    #   patched_content_digests comment in
    #   _install_locked_within_release_dir()) as statically AND
    #   dynamically importing the rest of prime-agent's own dist/bundle/
    #   directory unconditionally -- this is not a hypothetical edge case,
    #   it is confirmed to be exactly the shape of the real shipped
    #   entrypoint, so launching it via /dev/fd/<n> would silently break
    #   those imports at runtime rather than merely failing an abstract
    #   test script.
    #
    # Given both halves of the intended fd-binding fix are genuinely
    # blocked -- not skipped for convenience -- the actual mitigation
    # applied instead is reassert_exec_target_identity() below: main()
    # keeps BOTH validated descriptors this function returns open, purely
    # as an immutable identity ANCHOR (once opened, an fd's own (st_dev,
    # st_ino) can never be changed by anything done to the path
    # afterward), and re-checks, as literally the last two statements
    # before subprocess.run(), that NODE/CLI's paths still resolve to
    # those exact anchored inodes. This shrinks the exploitable window
    # from "the entire remainder of main() after this function first
    # returns" (lock reassertion, the sibling validate_exec_target() call,
    # the ancestor-node_modules walk, argument/environment construction)
    # down to "the gap between that final cheap lstat-based check and
    # subprocess.run()'s own internal, unavoidable path-based exec" -- the
    # smallest window expressible without OS support this platform does
    # not have. It does NOT reach zero: subprocess.run() still ultimately
    # re-resolves NODE/CLI by path string to actually exec/open them, so a
    # racer who wins that last, now much narrower gap (or who can mutate
    # the SAME already-validated inode's bytes in place, which no
    # content-hash-then-later-use scheme fully closes against a same-UID
    # adversary who already has write access to begin with) is still not
    # caught. Documented here with the same honesty this comment used to
    # describe the residual with before this round, rather than silently
    # claiming a full close that real testing disproved.
    if os.path.realpath(path) != os.path.abspath(path):
        return f"managed exec target contains a symlink: {{path}}", -1
    if os.path.commonpath((root, os.path.realpath(path))) != root:
        return f"managed exec target escaped the Extreme SSD: {{path}}", -1
    try:
        info = os.lstat(path)
    except OSError:
        return f"managed exec target is missing: {{path}}", -1
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        return f"managed exec target is unsafe: {{path}}", -1
    if info.st_size > MAX_EXEC_TARGET_BYTES:
        return f"managed exec target exceeds the approved size: {{path}}", -1
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            os.close(descriptor)
            return f"managed exec target identity changed before verification: {{path}}", -1
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        return f"managed exec target became unreadable: {{path}}", -1
    if digest.hexdigest() != expected_sha256:
        os.close(descriptor)
        return f"managed exec target content changed: {{path}}", -1
    return None, descriptor


def reassert_exec_target_identity(path: str, anchor_descriptor: int) -> str | None:
    # Round 47, 2026-08-20 (independent Codex sol/terra round-46 review,
    # P1): the best achievable mitigation for the gap validate_exec_
    # target()'s own docstring documents in full -- see it for why full
    # descriptor-bound exec was empirically infeasible on this platform
    # for both NODE and CLI. Re-checks, via a fresh os.lstat() of `path`
    # (a NEW path-based lookup, deliberately -- this is exactly the
    # lookup subprocess.run() is about to perform a moment later to
    # actually exec/open the target) that the path STILL resolves to the
    # exact same inode `anchor_descriptor` was opened against and fully
    # content-validated by validate_exec_target(). `anchor_descriptor`
    # stays open the whole time specifically so this comparison is
    # against an immutable reference (an already-open file descriptor's
    # own identity can never be changed by anything done to the path
    # afterward) rather than against a second, equally racy, path-based
    # lstat() -- mirrors main()'s own pre-existing post-flock LOCK
    # identity reassertion above, applied here to NODE/CLI. Called as the
    # LAST statement before subprocess.run() so the remaining window is
    # as small as this platform allows -- see validate_exec_target() for
    # exactly what that window still leaves open.
    try:
        anchor = os.fstat(anchor_descriptor)
    except OSError:
        return f"managed exec target descriptor became invalid: {{path}}"
    try:
        current = os.lstat(path)
    except OSError:
        return f"managed exec target is missing: {{path}}"
    if (current.st_dev, current.st_ino) != (anchor.st_dev, anchor.st_ino):
        return f"managed exec target identity changed immediately before exec: {{path}}"
    return None


def main() -> int:
    descriptor = -1
    node_descriptor = -1
    cli_descriptor = -1
    try:
        if os.path.realpath(LOCK) != os.path.abspath(LOCK):
            return fail("managed lifecycle lock path contains a symlink")
        root = os.path.realpath(SSD_ROOT)
        if os.path.commonpath((root, os.path.realpath(LOCK))) != root:
            return fail("managed lifecycle lock escaped the Extreme SSD")
        before = os.lstat(LOCK)
        descriptor = os.open(LOCK, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return fail("managed lifecycle lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        # Round 13/14, 2026-08-18 (independent review, P2): re-assert, right
        # now that the lock is actually held, that LOCK still names the
        # exact inode this fd's flock() bound to -- mirroring the
        # installer side's own assert_lifecycle_lock_path_identity(), which
        # exists for exactly this purpose (flock(2) binds exclusivity to the
        # OPEN FILE DESCRIPTION, not to the path; nothing about already
        # holding the fd detects a same-UID actor renaming a brand-new file
        # over LOCK afterward, which would let a second, independent actor
        # acquire its own "exclusive" flock on that new file while this
        # process still believes the well-known path is exclusively its
        # own). Closes the same small-but-real race
        # exclusive_lifecycle_lock() itself already closes on the installer
        # side, between the pre-flock identity check above and flock()
        # actually taking effect.
        after_flock = os.lstat(LOCK)
        if (after_flock.st_dev, after_flock.st_ino) != (opened.st_dev, opened.st_ino):
            return fail("managed lifecycle lock identity changed while held")
        node_error, node_descriptor = validate_exec_target(NODE, root, NODE_SHA256)
        if node_error is not None:
            return fail(node_error)
        cli_error, cli_descriptor = validate_exec_target(CLI, root, CLI_SHA256)
        if cli_error is not None:
            return fail(cli_error)
        # Round 40, 2026-08-20 (independent Claude opus/max round-39
        # review, P1-2 item 4; extended round 43, P1-B): see
        # unexpected_ancestor_node_modules()'s own docstring -- refuses to
        # exec at all if Node's own module resolution (the ancestor
        # node_modules walk, which reaches well outside RELEASE_DIR and
        # everything the two validate_exec_target() calls above cover,
        # PLUS the three Module.globalPaths locations node_global_folder_
        # paths() adds) contains a location this installer never creates.
        resolution_hit = unexpected_ancestor_node_modules()
        if resolution_hit is not None:
            return fail(
                "unexpected item on Node's own module resolution path "
                f"outside the managed release: {{resolution_hit}}"
            )
        arguments = guarded_arguments(sys.argv[1:])
        # Round 40, 2026-08-20 (P1-2 item 1): see
        # scrubbed_node_environment()'s own docstring.
        environment = scrubbed_node_environment()
        # Round 47, 2026-08-20 (independent Codex sol/terra round-46
        # review, P1): reassert NODE/CLI identity through the SAME
        # already-open descriptors validate_exec_target() returned above,
        # as literally the last two statements before the actual exec --
        # see reassert_exec_target_identity()'s own docstring, and
        # validate_exec_target()'s, for the full reasoning and the
        # empirically-confirmed platform constraints that make this a
        # window-shrink rather than a full close.
        node_reassert_error = reassert_exec_target_identity(NODE, node_descriptor)
        if node_reassert_error is not None:
            return fail(node_reassert_error)
        cli_reassert_error = reassert_exec_target_identity(CLI, cli_descriptor)
        if cli_reassert_error is not None:
            return fail(cli_reassert_error)
        completed = subprocess.run(
            [NODE, CLI, *arguments],
            check=False,
            env=environment,
        )
        return completed.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        return fail(f"managed launch guard failed: {{type(exc).__name__}}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if node_descriptor >= 0:
            os.close(node_descriptor)
        if cli_descriptor >= 0:
            os.close(cli_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
'''
    return source.encode("utf-8")


def expected_prime_settings() -> bytes:
    return canonical_json(
        {
            "sessionDir": os.fspath(managed_session_dir()),
            "telemetry": {"enabled": False, "noticeShown": True},
        }
    )


def validate_partial_managed_home(path: Path, label: str) -> Path:
    root = verify_private_ssd_dir(path)
    allowed = {Path("agent"), Path("agent/settings.json")}
    observed = {entry.relative_to(path) for entry in path.rglob("*")}
    unexpected = sorted(os.fspath(entry) for entry in observed - allowed)
    if unexpected:
        raise PrimeInstallError(f"{label} contains unrecognized partial state: {unexpected}")
    agent_dir = path / "agent"
    if agent_dir.exists() or agent_dir.is_symlink():
        verify_private_ssd_dir(agent_dir)
    settings = agent_dir / "settings.json"
    if settings.exists() or settings.is_symlink():
        if read_private_ssd_file(settings) != expected_prime_settings():
            raise PrimeInstallError(f"{label} settings require explicit review")
    return root


def validate_pristine_managed_home(path: Path, label: str) -> Path:
    root = validate_partial_managed_home(path, label)
    expected_entries = {Path("agent"), Path("agent/settings.json")}
    observed = {entry.relative_to(path) for entry in path.rglob("*")}
    if observed != expected_entries:
        raise PrimeInstallError(f"{label} is incomplete")
    return root


def validate_runtime_state(path: Path) -> Path:
    root = verify_private_ssd_dir(path)
    agent = verify_private_ssd_dir(path / "agent")
    settings = strict_json(read_private_ssd_file(agent / "settings.json"))
    telemetry = settings.get("telemetry") if isinstance(settings, dict) else None
    if not isinstance(telemetry, dict) or telemetry.get("enabled") is not False:
        raise PrimeInstallError("Prime Agent runtime telemetry is not disabled")
    if settings.get("sessionDir") != os.fspath(managed_session_dir()):
        raise PrimeInstallError("Prime Agent runtime session directory drifted")
    verify_private_ssd_dir(managed_session_dir())
    return root


def initialize_prime_state() -> Path:
    ensure_private_dir(STATE_DIR)
    agent_dir = ensure_private_dir(STATE_DIR / "agent")
    settings = agent_dir / "settings.json"
    expected = expected_prime_settings()
    if settings.exists() or settings.is_symlink():
        current = read_private_ssd_file(settings)
        if current != expected:
            raise PrimeInstallError("existing Prime Agent settings require explicit review")
    else:
        atomic_create_private_file(settings, expected, 0o600)
    return settings


def initialize_probe_home() -> Path:
    ensure_private_dir(PROBE_HOME)
    agent_dir = ensure_private_dir(PROBE_HOME / "agent")
    settings = agent_dir / "settings.json"
    expected = expected_prime_settings()
    if settings.exists() or settings.is_symlink():
        if read_private_ssd_file(settings) != expected:
            raise PrimeInstallError("Prime Agent probe settings drifted")
    else:
        atomic_create_private_file(settings, expected, 0o600)
    return agent_dir


def require_link_dir_fd_support() -> None:
    if not LINK_DIR_FD_SUPPORTED:
        raise PrimeInstallError(
            "this Python build cannot bind directory operations to an open "
            "file descriptor (dir_fd); refusing to verify or create the "
            "managed command link without that race protection"
        )


class _AncestorComponentAbsent(Exception):
    """Internal signal used only within open_verified_directory_component()/
    open_verified_ancestor_chain_or_absent(): the named component simply
    does not exist. Kept distinct from PrimeInstallError so a walk that
    tolerates absence (see open_verified_ancestor_chain_or_absent()) can
    tell mere absence -- the ordinary "nothing has ever been installed
    here" state -- apart from every OTHER rejection this same walk enforces
    (a symlinked ancestor, wrong owner/mode, a non-directory component,
    ...), which must still fail closed rather than being folded into
    "absent" (independent review round 5, 2026-08-18, P1-3)."""


def open_verified_directory_component(
    parent_descriptor: int,
    name: str,
    display_path: Path,
    *,
    create_missing: bool,
    missing_ok: bool = False,
) -> int:
    """Open path component `name` strictly relative to the already-open,
    already-verified directory descriptor `parent_descriptor` (an
    openat()-style dir_fd=parent_descriptor lookup), never by re-resolving
    any ancestor through a path string. O_NOFOLLOW refuses a symlinked
    component outright; O_DIRECTORY refuses a non-directory outright. The
    returned descriptor is bound to the component's inode, not to its name,
    so a same-UID racer renaming or resymlinking `display_path` or any of
    its ancestors at any later point cannot redirect this call or anything
    chained from its result.

    (independent review round 1, 2026-08-18, P1-2 residual fix: replaces the
    previous per-ancestor lexical lstat()/os.open(full_path) walk, which
    pinned only the FINAL directory in the chain via O_NOFOLLOW|O_DIRECTORY
    but still opened it by re-resolving every ancestor from a path string --
    so a same-UID racer swapping an EARLIER ancestor for a symlink during
    that walk could still redirect the final open into an attacker
    directory. Chaining dir_fd-relative opens the whole way from USER_HOME
    closes that gap for every ancestor, not only the last one.)

    `missing_ok` (only meaningful together with create_missing=False) makes
    a genuinely missing component raise the internal _AncestorComponentAbsent
    signal instead of PrimeInstallError, for open_verified_ancestor_chain_or_absent()
    to translate into "this link legitimately does not exist yet" -- every
    other failure mode below is unaffected and still raises PrimeInstallError
    regardless of `missing_ok`.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError as exc:
        if not create_missing and missing_ok:
            raise _AncestorComponentAbsent(display_path) from exc
        if not create_missing:
            raise PrimeInstallError(f"link parent is missing: {display_path}") from exc
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        except OSError as mkdir_exc:
            raise PrimeInstallError(
                f"cannot create link parent: {display_path}"
            ) from mkdir_exc
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as reopen_exc:
            raise PrimeInstallError(
                f"cannot inspect link parent: {display_path}"
            ) from reopen_exc
    except OSError as exc:
        raise PrimeInstallError(f"cannot inspect link parent: {display_path}") from exc
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise PrimeInstallError(f"cannot inspect link parent: {display_path}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        os.close(descriptor)
        raise PrimeInstallError(f"unsafe link parent: {display_path}")
    if create_missing:
        # Durably sync the directory that contains this component's entry,
        # exactly as the pre-chained implementation did for every ancestor
        # on every call (not only when a fresh mkdir just happened) -- closes
        # an interrupted creation whose directory entry may not yet have
        # reached disk, on retry as well as on first creation. Uses the
        # patchable, path-based fsync_directory() (not a raw dir_fd fsync)
        # so this stays a plain durability best-effort, not a security
        # check: display_path is only ever used for this and for error
        # messages, never to open or verify anything.
        try:
            fsync_directory(display_path.parent)
        except BaseException:
            os.close(descriptor)
            raise
    return descriptor


def open_verified_ancestor_chain(
    link: Path,
    *,
    create_missing: bool,
    root: Path | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> int:
    """Walk from `root` (USER_HOME by default) to link's parent entirely via
    dir_fd-chained, openat()-style lookups (see
    open_verified_directory_component()) and return an open, verified
    descriptor for the immediate parent. Used by both
    ensure_local_link_parent() (create_missing=True, for link creation) and
    verify_link_parent_descriptor() (create_missing=False, for read-only
    verification), so creation and verification are bound to the identical
    ancestor-resolution algorithm rather than verification trusting a plain
    lexical path that could resolve through a since-interposed symlink
    ancestor.

    `root` defaults to None (meaning USER_HOME, read fresh from the module
    global on every call -- not captured as a mutable default argument --
    so tests that `mock.patch.object(installer, "USER_HOME", ...)` keep
    working) but is also reused, with root=SSD_ROOT, by
    open_verified_generated_file_parent() to bind
    tighten_generated_private_file_mode() to the identical dir_fd-chained
    ancestor walk instead of USER_HOME -- the SAME algorithm, just rooted
    at a different pre-existing, non-swappable authority, so a same-UID
    racer who swaps RELEASE_DIR (or any ancestor above it, e.g. TOOL_ROOT
    or "releases") for a symlink cannot redirect this walk either
    (independent Codex sol/max round-19 review, 2026-08-19, P1).

    `expected_identity`, when given, is compared -- via os.fstat(), on the
    already-open, already-verified descriptor this walk is about to return,
    strictly BEFORE returning it -- against the (st_dev, st_ino) the caller
    captured for that SAME final directory at some earlier, trusted point
    (for open_verified_generated_file_parent()/
    tighten_generated_private_file_mode(), that is RELEASE_DIR's own
    identity, captured by _install_locked() immediately after it creates
    RELEASE_DIR and strictly before the external `npm` subprocess this walk
    exists to defend against ever runs). Every per-component check this walk
    already performs (open_verified_directory_component(): real directory,
    not a symlink, owned by this UID, safe mode) validates properties
    IN THE MOMENT -- it cannot by itself distinguish the original directory
    from a same-UID actor's rename-swap replacement, because a replacement
    built from a different, legitimately-owned, correctly-permissioned REAL
    directory (not a symlink) genuinely satisfies every one of those checks.
    Comparing the terminal descriptor's inode against an identity captured
    before the untrusted window closes that gap regardless of which
    ancestor was swapped, or whether the swap used a symlink or a rename,
    because the resulting directory can only share the original's
    (st_dev, st_ino) by actually BEING it (independent Codex sol/max
    round-21 review, 2026-08-19, P1: round 20's fix refused a SYMLINKED
    RELEASE_DIR, but a same-UID rename-swap that replaced it with a
    different, real, 0700, self-owned directory -- reproduced by renaming
    the real release directory aside and renaming a prepared sibling real
    directory, containing an unrelated mode-0644 package-lock.json, into
    the release name -- passed every existing per-component check and let
    tighten_generated_private_file_mode() chmod the unrelated file).
    Defaults to None (no check) so every OTHER caller of this shared walk
    (ensure_local_link_parent(), verify_link_parent_descriptor(),
    remove_exact_symlink()'s BIN_LINK-parent resolution -- none of which
    hold an identity captured across an external subprocess window; each
    re-walks fresh immediately before use) is unaffected.
    """
    require_link_dir_fd_support()
    lexical_root = (USER_HOME if root is None else root).absolute()
    try:
        relative = link.parent.absolute().relative_to(lexical_root)
    except ValueError as exc:
        raise PrimeInstallError(f"path is outside {lexical_root}: {link}") from exc
    try:
        # The root itself is pre-existing authority, opened by path exactly
        # once as the root of the chain -- every component beneath it is
        # then resolved only relative to an already-verified descriptor,
        # never by path string again.
        current_descriptor = os.open(lexical_root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise PrimeInstallError(f"cannot open {lexical_root}") from exc
    display_path = lexical_root
    try:
        for part in relative.parts:
            display_path = display_path / part
            next_descriptor = open_verified_directory_component(
                current_descriptor, part, display_path, create_missing=create_missing
            )
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        if expected_identity is not None:
            resolved = os.fstat(current_descriptor)
            if (resolved.st_dev, resolved.st_ino) != expected_identity:
                raise PrimeInstallError(
                    "managed ancestor directory identity changed since "
                    f"capture: {display_path}"
                )
        return current_descriptor
    except BaseException:
        try:
            os.close(current_descriptor)
        except OSError:
            pass
        raise


def open_verified_ancestor_chain_or_absent(link: Path) -> int | None:
    """Same dir_fd-chained ancestor walk as
    open_verified_ancestor_chain(link, create_missing=False), except a
    genuinely missing component -- an ancestor, or link's own immediate
    parent, that simply does not exist -- returns None instead of raising,
    because that is the ordinary "nothing has ever been installed here"
    state. Every OTHER rejection the shared walk enforces (a symlinked
    ancestor, wrong owner/mode, a non-directory component, dir_fd support
    unavailable, link outside USER_HOME, ...) still raises PrimeInstallError
    and is never folded into "absent".

    Used by verify_command_state() to bind BIN_LINK's existence check to
    the SAME verified ancestor chain used for its content/identity
    verification (verify_link() -> verify_link_parent_descriptor()),
    instead of Path.is_symlink()/Path.exists(), which resolve through
    whatever an ancestor currently points to and can therefore report the
    managed link both disabled and absent -- while the real link, reachable
    only through the true, unswapped ancestor chain, is still live -- the
    instant a same-UID actor swaps an ancestor directory (e.g. ~/.local) for
    a symlink to a directory that does not contain bin/prime-agent
    (independent review round 5, 2026-08-18, P1-3: verify_command_state()
    and its uninstall call site resolved BIN_LINK's existence purely
    lexically, so that swap made uninstall silently report
    command_disabled=true/already_disabled=true while the managed link was
    untouched).
    """
    require_link_dir_fd_support()
    lexical_home = USER_HOME.absolute()
    try:
        relative = link.parent.absolute().relative_to(lexical_home)
    except ValueError as exc:
        raise PrimeInstallError(f"link path is outside the user home: {link}") from exc
    try:
        current_descriptor = os.open(lexical_home, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PrimeInstallError(f"cannot open user home: {lexical_home}") from exc
    display_path = lexical_home
    try:
        for part in relative.parts:
            display_path = display_path / part
            try:
                next_descriptor = open_verified_directory_component(
                    current_descriptor,
                    part,
                    display_path,
                    create_missing=False,
                    missing_ok=True,
                )
            except _AncestorComponentAbsent:
                os.close(current_descriptor)
                return None
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        return current_descriptor
    except BaseException:
        try:
            os.close(current_descriptor)
        except OSError:
            pass
        raise


def ensure_local_link_parent(link: Path) -> int:
    """Verify (creating as needed) the FULL ancestor chain from USER_HOME
    down to link's parent, and return an open file descriptor for the
    immediate parent, verified with O_NOFOLLOW|O_DIRECTORY and held open
    from this point onward.

    Every ancestor in the chain -- not only the immediate parent -- is
    opened relative to the previously opened and verified directory
    descriptor (see open_verified_ancestor_chain()), never re-resolved by
    path string, so a same-UID racer renaming or resymlinking any ancestor
    at any point during or after this walk cannot redirect the descriptor
    this returns. The caller (atomic_symlink()) must create and verify the
    link bound to this SAME descriptor rather than re-resolving the parent
    by path string, and must close it when done.
    """
    return open_verified_ancestor_chain(link, create_missing=True)


def verify_link_parent_descriptor(link: Path) -> int:
    """Read-only counterpart of ensure_local_link_parent(): open the SAME
    dir_fd-chained ancestor descriptor for link's parent, without creating
    any missing component, so verify_link() can inspect the link itself
    relative to a descriptor that is provably the real, non-symlinked
    ancestor chain -- rather than trusting a lexical path, which would
    silently resolve through any ancestor an attacker had since replaced
    with a symlink (independent review round 1, 2026-08-18, P1-2: the
    creation-side race was closed in the prior round, but verify_link() and
    verify_command_state() still re-resolved the link by plain path with no
    binding to the verified chain at all, so an interposed ~/.local symlink
    to an attacker directory holding its own bin/prime-agent -> <managed
    target> made verification report the command enabled with no race
    required).
    """
    return open_verified_ancestor_chain(link, create_missing=False)


def fsync_open_directory(descriptor: int) -> None:
    """fsync_directory()'s counterpart for an already-open, already-verified
    directory descriptor -- lets atomic_symlink() durably publish the newly
    created link without re-opening (and therefore re-resolving by path) its
    parent (independent Codex sol/xhigh review, 2026-08-16, P1-2)."""
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise PrimeInstallError(f"cannot sync managed link parent: fd {descriptor}") from exc


def atomic_symlink(target: Path, link: Path) -> None:
    try:
        resolved_target = target.resolve(strict=True)
        ssd_root = SSD_ROOT.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PrimeInstallError(f"symlink target is unavailable: {target}") from exc
    if not is_relative_to(resolved_target, ssd_root):
        raise PrimeInstallError(f"symlink target escaped Extreme SSD: {target}")
    # ensure_local_link_parent() already refuses to run at all when this
    # platform cannot bind directory operations to dir_fd (see
    # require_link_dir_fd_support()), so every operation below is always
    # dir_fd-bound -- there is no remaining path-based fallback to narrow.
    parent_descriptor = ensure_local_link_parent(link)
    try:
        target_text = os.fspath(target)
        try:
            os.symlink(target_text, link.name, dir_fd=parent_descriptor)
        except FileExistsError as exc:
            raise PrimeInstallError(f"refusing to replace existing path: {link}") from exc
        except OSError as exc:
            raise PrimeInstallError(f"cannot create managed symlink: {link}") from exc
        try:
            created = os.stat(
                link.name, dir_fd=parent_descriptor, follow_symlinks=False
            )
            created_target = os.readlink(link.name, dir_fd=parent_descriptor)
            if not stat.S_ISLNK(created.st_mode) or created_target != target_text:
                raise PrimeInstallError(
                    f"managed symlink identity changed immediately after creation: {link}"
                )
            fsync_open_directory(parent_descriptor)
        except (OSError, PrimeInstallError) as create_exc:
            # The symlink was created bound to parent_descriptor (the
            # verified, held-open directory), but the cleanup call below
            # re-resolves `link` by lexical path -- the same descriptor is
            # not threaded through remove_exact_symlink()'s quarantine
            # machinery. Re-verify the parent's on-disk identity
            # (st_dev, st_ino) against parent_descriptor immediately before
            # that lexical resolution, so a same-UID ancestor swap in this
            # narrow post-creation window is detected and refused instead of
            # silently letting cleanup inspect a different directory than
            # the one the link now actually lives in (independent review
            # round 1, 2026-08-18, P1-2 residual, in-family with the main
            # fix).
            try:
                parent_identity = os.fstat(parent_descriptor)
                current_parent = link.parent.lstat()
            except OSError as identity_exc:
                raise PrimeInstallError(
                    "managed symlink creation failed and its parent could not be "
                    f"re-verified for rollback; inspect {link}"
                ) from identity_exc
            if (current_parent.st_dev, current_parent.st_ino) != (
                parent_identity.st_dev,
                parent_identity.st_ino,
            ):
                raise PrimeInstallError(
                    "managed symlink creation failed and its parent directory was "
                    f"replaced before rollback could run; inspect {link} and "
                    f"{link.parent} by hand"
                ) from create_exc
            try:
                remove_exact_symlink(link, target)
            except PrimeInstallError as cleanup_exc:
                if link.exists() or link.is_symlink():
                    raise PrimeInstallError(
                        "managed symlink creation failed and conditional cleanup is "
                        f"incomplete; inspect {link}"
                    ) from cleanup_exc
                raise PrimeInstallError(
                    "managed symlink creation failed; the public link is absent but "
                    f"rollback durability is unconfirmed: {link}"
                ) from cleanup_exc
            raise PrimeInstallError(
                f"cannot durably create managed symlink; creation was rolled back: {link}"
            ) from create_exc
    finally:
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def partial_recovery_declarations() -> tuple[tuple[str, Path], ...]:
    return (
        ("release", RELEASE_DIR),
        ("state", STATE_DIR),
        ("probe-home", PROBE_HOME),
        ("sessions", managed_session_dir()),
    )


def validate_partial_recovery_item(label: str, path: Path) -> None:
    if path.is_symlink():
        raise PrimeInstallError(f"partial {label} path is a symlink; refusing recovery")
    if label == "release":
        verify_private_ssd_dir(path)
    elif label == "sessions":
        validate_empty_private_dir(path, "partial Prime Agent session directory")
    else:
        validate_partial_managed_home(path, f"partial Prime Agent {label}")


def quarantine_partial_release() -> dict[str, Any]:
    declarations = partial_recovery_declarations()
    present = [
        (label, path)
        for label, path in declarations
        if path.exists() or path.is_symlink()
    ]
    if not present:
        return {"ok": True, "state": "none"}
    if (
        RECEIPT_PATH.exists()
        or RECEIPT_PATH.is_symlink()
        or PENDING_PATH.exists()
        or PENDING_PATH.is_symlink()
        or bin_link_present()
        or STATE_LINK.exists()
        or STATE_LINK.is_symlink()
    ):
        raise PrimeInstallError("partial install has activation state; refusing automatic quarantine")
    for label, path in present:
        validate_partial_recovery_item(label, path)
    if RELEASE_DIR.exists():
        processes = managed_process_ids({"release_dir": os.fspath(RELEASE_DIR)})
        if processes:
            raise PrimeInstallError(f"partial Prime Agent release is still in use: {processes}")
    recovery_root = TOOL_ROOT / "recovery"
    ensure_private_dir(recovery_root)
    destination = recovery_root / f"partial-v{VERSION}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}"
    if destination.exists() or destination.is_symlink():
        raise PrimeInstallError("partial install recovery destination already exists")
    ensure_private_dir(destination)
    manifest_path = destination / "manifest.json"
    manifest = {
        "schema": "orca.prime-agent-partial-recovery.v1",
        "version": VERSION,
        "status": "moving",
        "items": [label for label, _ in present],
    }
    atomic_create_private_file(manifest_path, canonical_json(manifest), 0o600)
    moved: list[tuple[Path, Path]] = []
    try:
        for label, source in present:
            target = destination / label
            rename_noreplace(source, target)
            moved.append((source, target))
            fsync_directory(source.parent)
            fsync_directory(destination)
        # Commit the recovery-directory entry before publishing a terminal
        # manifest. If this barrier fails, the durable status remains moving.
        fsync_directory(recovery_root)
        manifest["status"] = "quarantined"
        atomic_write(manifest_path, canonical_json(manifest), 0o600)
    except (OSError, PrimeInstallError) as exc:
        # Never begin reverse moves while the manifest can still look
        # terminal. A crash after this durable marker is fail-closed in both
        # plan and recover, rather than fragmenting apparently finished
        # recovery evidence.
        manifest["status"] = "rolling_back"
        manifest["rollback_errors"] = []
        try:
            atomic_write(manifest_path, canonical_json(manifest), 0o600)
        except (OSError, PrimeInstallError) as marker_exc:
            raise PrimeInstallError(
                "cannot publish Prime Agent rollback intent; rollback was not "
                f"attempted; recovery evidence: {destination}"
            ) from marker_exc
        rollback_errors: list[str] = []
        for source, target in reversed(moved):
            try:
                target_present = target.exists() or target.is_symlink()
                source_present = source.exists() or source.is_symlink()
                if target_present and source_present:
                    rollback_errors.append(
                        f"rollback destination occupied; preserved both paths: {source}"
                    )
                elif target_present:
                    rename_noreplace(target, source)
                    fsync_directory(source.parent)
                    fsync_directory(target.parent)
                elif not source_present:
                    rollback_errors.append(
                        f"rollback source and recovery item are both missing: {source}"
                    )
            except (OSError, PrimeInstallError) as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        manifest["status"] = "rollback_failed" if rollback_errors else "rolled_back"
        manifest["rollback_errors"] = rollback_errors
        atomic_write(manifest_path, canonical_json(manifest), 0o600)
        raise PrimeInstallError(
            f"cannot quarantine partial Prime Agent install; recovery evidence: {destination}"
        ) from exc
    return {
        "ok": True,
        "state": "quarantined",
        "path": os.fspath(destination),
        "items": manifest["items"],
    }


def read_recovery_manifests() -> list[tuple[Path, dict[str, Any]]]:
    """Read and structurally validate recovery manifests without mutating them."""
    recovery_root = TOOL_ROOT / "recovery"
    if not recovery_root.exists() and not recovery_root.is_symlink():
        return []
    verify_private_ssd_dir(recovery_root)
    manifests: list[tuple[Path, dict[str, Any]]] = []
    try:
        destinations = sorted(recovery_root.iterdir())
    except OSError as exc:
        raise PrimeInstallError("cannot inspect Prime Agent recovery root") from exc
    for destination in destinations:
        verify_private_ssd_dir(destination)
        manifest_path = destination / "manifest.json"
        if not manifest_path.exists() and not manifest_path.is_symlink():
            raise PrimeInstallError(
                f"Prime Agent recovery directory has no manifest: {destination}"
            )
        manifest = strict_json(read_private_ssd_file(manifest_path))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != "orca.prime-agent-partial-recovery.v1"
            or manifest.get("version") != VERSION
            or not isinstance(manifest.get("items"), list)
            or not isinstance(manifest.get("status"), str)
        ):
            raise PrimeInstallError(
                f"Prime Agent recovery manifest requires explicit review: {manifest_path}"
            )
        items = manifest["items"]
        declared_labels = {label for label, _ in partial_recovery_declarations()}
        if (
            not items
            or any(not isinstance(label, str) for label in items)
            or len(set(items)) != len(items)
            or any(label not in declared_labels for label in items)
        ):
            raise PrimeInstallError(
                f"Prime Agent recovery manifest item set is invalid: {destination}"
            )
        manifests.append((destination, manifest))
    return manifests


def recovery_manifest_conflicts() -> list[str]:
    """Return unresolved recovery evidence for the read-only plan action."""
    terminal = {"quarantined", "rolled_back"}
    return [
        f"RECOVERY:{destination}:{manifest['status']}"
        for destination, manifest in read_recovery_manifests()
        if manifest["status"] not in terminal
    ]


def resume_incomplete_quarantine() -> dict[str, Any] | None:
    recovery_root = TOOL_ROOT / "recovery"
    unresolved: list[tuple[Path, dict[str, Any]]] = []
    for destination, manifest in read_recovery_manifests():
        status = manifest["status"]
        if status == "moving":
            unresolved.append((destination, manifest))
        elif status not in {"quarantined", "rolled_back"}:
            raise PrimeInstallError(
                "Prime Agent recovery manifest has unresolved or unknown status; "
                f"explicit review required: {destination / 'manifest.json'}"
            )
    if not unresolved:
        return None
    if len(unresolved) != 1:
        raise PrimeInstallError(
            "multiple incomplete Prime Agent recovery transactions require explicit review"
        )
    destination, manifest = unresolved[0]
    declared = dict(partial_recovery_declarations())
    items = manifest["items"]
    if "release" in items:
        processes = managed_process_ids({"release_dir": os.fspath(RELEASE_DIR)})
        if processes:
            raise PrimeInstallError(
                f"partial Prime Agent recovery is still in use: {processes}"
            )
    conflicts: list[str] = []
    for label in items:
        source = declared[label]
        target = destination / label
        source_present = source.exists() or source.is_symlink()
        target_present = target.exists() or target.is_symlink()
        if source_present and target_present:
            conflicts.append(f"both source and recovery item exist: {label}")
            continue
        if not source_present and not target_present:
            conflicts.append(f"source and recovery item are both missing: {label}")
            continue
        if target_present:
            validate_partial_recovery_item(label, target)
            # A crash may have occurred after rename but before either parent
            # was synced. Re-establish both halves of the rename durability
            # barrier before certifying the observed target as quarantined.
            verify_private_ssd_dir(source.parent)
            fsync_directory(source.parent)
            fsync_directory(destination)
            continue
        validate_partial_recovery_item(label, source)
        rename_noreplace(source, target)
        fsync_directory(source.parent)
        fsync_directory(destination)
    if conflicts:
        manifest["status"] = "recovery_conflict"
        manifest["recovery_errors"] = conflicts
        atomic_write(
            destination / "manifest.json", canonical_json(manifest), 0o600
        )
        raise PrimeInstallError(
            f"incomplete Prime Agent recovery has conflicting evidence: {destination}"
        )
    manifest["status"] = "quarantined"
    manifest["resumed_after_interruption"] = True
    atomic_write(destination / "manifest.json", canonical_json(manifest), 0o600)
    fsync_directory(recovery_root)
    return {
        "ok": True,
        "state": "quarantined",
        "path": os.fspath(destination),
        "items": items,
        "resumed_after_interruption": True,
    }


def expected_receipt_identity() -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "version": VERSION,
        "tag_commit": TAG_COMMIT,
        "release_dir": os.fspath(RELEASE_DIR),
        "state_dir": os.fspath(STATE_DIR),
        "state_link": os.fspath(STATE_LINK),
        "bin_link": os.fspath(BIN_LINK),
        "bin_target": os.fspath(RELEASE_DIR / "bin/prime-agent"),
        "launch_guard": os.fspath(RELEASE_DIR / "bin/prime-agent-launch-guard.py"),
        "lifecycle_lock": os.fspath(lifecycle_lock_path()),
        "node_target": os.fspath(RELEASE_DIR / "toolchain/bin/node"),
        "npm_target": os.fspath(RELEASE_DIR / "toolchain/lib/node_modules/npm/bin/npm-cli.js"),
        "probe_home": os.fspath(PROBE_HOME),
        "probe_agent_dir": os.fspath(PROBE_HOME / "agent"),
        "session_dir": os.fspath(managed_session_dir()),
        "asset_sha256": ASSETS,
        "node_asset_sha256": NODE_ASSET_SHA256,
        "upstream_lock_sha256": LOCK_SHA256,
        "license_sha256": LICENSE_SHA256,
        "production_lock_sha256": GENERATED_LOCK_SHA256,
        "patched_manifest_names": sorted(("prime-agent", *WORKSPACE_PACKAGES)),
        "closure": {
            "lock_sha256": GENERATED_LOCK_SHA256,
            "packages_checked": GENERATED_LOCK_PACKAGE_COUNT,
            "registry_packages_checked": GENERATED_LOCK_PACKAGE_COUNT - 4,
        },
        "node_version": NODE_VERSION,
        "npm_version": NPM_VERSION,
        "lifecycle_scripts_executed": False,
        "daemon_started": False,
        "credentials_configured": False,
        "command_default_enabled": False,
        "project_settings_default_allowed": False,
        "project_executable_resources_default_allowed": False,
        "telemetry_default_enabled": False,
        "telemetry_settings": os.fspath(STATE_DIR / "agent/settings.json"),
    }


def validate_receipt_identity(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise PrimeInstallError("managed install receipt is invalid")
    if any(receipt.get(key) != value for key, value in expected_receipt_identity().items()):
        raise PrimeInstallError("managed install receipt identity mismatch")
    return receipt


def finalize_pending_install(
    expected_lock_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    if not PENDING_PATH.exists() and not PENDING_PATH.is_symlink():
        raise PrimeInstallError("pending Prime Agent install journal is missing")
    journal_raw = read_private_ssd_file(PENDING_PATH)
    journal = strict_json(journal_raw)
    if (
        not isinstance(journal, dict)
        or set(journal) != {"schema", "receipt"}
        or journal.get("schema") != JOURNAL_SCHEMA
        or not isinstance(journal.get("receipt"), dict)
    ):
        raise PrimeInstallError("invalid Prime Agent install journal")
    receipt = validate_receipt_identity(journal["receipt"])
    if receipt.get("volume_uuid") != volume_uuid():
        raise PrimeInstallError("Prime Agent install journal SSD UUID mismatch")
    if receipt.get("orca_support") != verify_orca_support():
        raise PrimeInstallError("installed Orca Prime Agent support changed during recovery")
    if os.fspath(verify_lifecycle_lock_file()) != receipt.get("lifecycle_lock"):
        raise PrimeInstallError("Prime Agent lifecycle lock identity drifted")
    if expected_lock_identity is not None:
        # A critical use point reached while the caller's exclusive
        # lifecycle lock is (or should still be) held: re-assert the lock
        # path still names the exact inode acquired at lock time, closing
        # the same-UID inode-swap TOCTOU (independent review round 5,
        # 2026-08-18, P2-2; see assert_lifecycle_lock_path_identity()).
        assert_lifecycle_lock_path_identity(lifecycle_lock_path(), expected_lock_identity)
    release = verify_private_ssd_dir(RELEASE_DIR)
    state = validate_pristine_managed_home(STATE_DIR, "Prime Agent pending state")
    validate_pristine_managed_home(PROBE_HOME, "Prime Agent pending probe home")
    validate_empty_private_dir(
        managed_session_dir(), "Prime Agent pending session directory"
    )
    if read_private_ssd_file(STATE_DIR / "agent/settings.json") != expected_prime_settings():
        raise PrimeInstallError("Prime Agent pending telemetry settings drifted")
    if read_private_ssd_file(PROBE_HOME / "agent/settings.json") != expected_prime_settings():
        raise PrimeInstallError("Prime Agent pending probe settings drifted")
    digest, entries = tree_digest(release)
    if digest != receipt.get("release_tree_sha256") or entries != receipt.get("release_tree_entries"):
        raise PrimeInstallError("Prime Agent pending release tree drifted")
    if bin_link_present():
        raise PrimeInstallError("Prime Agent command path must remain disabled until enable")
    ensure_no_prime_agent_command()
    targets = ((STATE_LINK, state),)
    for link, target in targets:
        if link.is_symlink():
            verify_link(link, target)
        elif link.exists():
            raise PrimeInstallError(f"Prime Agent activation path is occupied: {link}")
        else:
            atomic_symlink(target, link)
    receipt_raw = canonical_json(receipt)
    if RECEIPT_PATH.exists() or RECEIPT_PATH.is_symlink():
        existing = read_private_ssd_file(RECEIPT_PATH)
        if existing != receipt_raw:
            raise PrimeInstallError("Prime Agent install receipt conflicts with pending journal")
    else:
        atomic_create_private_file(RECEIPT_PATH, receipt_raw, 0o600)
    remove_private_file_durable(PENDING_PATH, journal_raw)
    return receipt


def _recover_locked(lock_identity: tuple[int, int]) -> dict[str, Any]:
    resumed = resume_incomplete_quarantine()
    if resumed is not None:
        return resumed
    if PENDING_PATH.exists() or PENDING_PATH.is_symlink():
        receipt = finalize_pending_install(lock_identity)
        return {"ok": True, "state": "committed", "version": receipt["version"]}
    if RECEIPT_PATH.exists() or RECEIPT_PATH.is_symlink():
        verification = verify(lock_identity)
        return {
            "ok": True,
            "state": "already_committed",
            "version": VERSION,
            "command_enabled": verification["command_enabled"],
        }
    if any(
        path.exists() or path.is_symlink()
        for path in (RELEASE_DIR, STATE_DIR, PROBE_HOME, managed_session_dir())
    ):
        return quarantine_partial_release()
    return {"ok": True, "state": "none"}


def _install_locked(lock_identity: tuple[int, int]) -> dict[str, Any]:
    ensure_no_prime_agent_command()
    resume_incomplete_quarantine()
    if PENDING_PATH.exists() or PENDING_PATH.is_symlink():
        return finalize_pending_install(lock_identity)
    evidence = preflight()
    if any(
        path.exists() or path.is_symlink()
        for path in (RELEASE_DIR, STATE_DIR, PROBE_HOME, managed_session_dir())
    ):
        quarantine_partial_release()
    if (
        RECEIPT_PATH.exists()
        or RECEIPT_PATH.is_symlink()
        or bin_link_present()
    ):
        raise PrimeInstallError("managed Prime Agent path already exists; run verify or inspect before retry")
    if STATE_LINK.exists() or STATE_LINK.is_symlink():
        raise PrimeInstallError("~/.prime already exists; refusing to merge state")
    # Round 38, 2026-08-20 (independent Claude opus/max round-37 review,
    # P2-2): STATE_DIR, PROBE_HOME, and the session directory each already
    # get a "still present after the quarantine attempt above -> refuse"
    # gate, immediately below -- RELEASE_DIR was the one managed root
    # missing the identical gate. quarantine_partial_release() relocates
    # any leftover RELEASE_DIR it finds (see its own docstring), so a
    # genuinely successful quarantine never legitimately leaves one behind
    # -- but ensure_private_dir() (used to create/open RELEASE_DIR just
    # below), unlike create_fresh_private_dir(), ACCEPTS a pre-existing
    # same-UID 0700 directory rather than refusing one outright, which is
    # exactly the right behavior for TOOL_ROOT/the npm cache/scratch dirs
    # this file also uses it for, but was silently wrong here: if
    # quarantine_partial_release() above raised before completing (e.g.
    # "still in use" for a live process) or a same-UID racer recreated
    # RELEASE_DIR in the narrow window between that call returning and this
    # check, `ensure_private_dir(RELEASE_DIR)` would silently REUSE
    # whatever was already there instead of refusing -- the same
    # reuse-instead-of-refuse gap STATE_DIR/PROBE_HOME/the session
    # directory already close for themselves, applied here too.
    if RELEASE_DIR.exists() or RELEASE_DIR.is_symlink():
        raise PrimeInstallError("managed Prime Agent release already exists; refusing to reuse it")
    if STATE_DIR.exists() or STATE_DIR.is_symlink():
        raise PrimeInstallError("managed Prime Agent state already exists; refusing to reuse it")
    if PROBE_HOME.exists() or PROBE_HOME.is_symlink():
        raise PrimeInstallError("managed Prime Agent probe home already exists; refusing to reuse it")
    if managed_session_dir().exists() or managed_session_dir().is_symlink():
        raise PrimeInstallError("managed Prime Agent session directory already exists; refusing to reuse it")
    ensure_private_dir(TOOL_ROOT.parent)
    ensure_private_dir(TOOL_ROOT)
    ensure_private_dir(TOOL_ROOT / "releases")
    ensure_private_dir(RELEASE_DIR)
    # Capture RELEASE_DIR's own (st_dev, st_ino) identity right now --
    # immediately after this installer itself creates it, and strictly
    # before either external `npm` subprocess below (which this installer
    # does not control the runtime of) ever runs. This is the ONLY point
    # in the entire install where RELEASE_DIR's identity can be captured
    # with the same "before an external subprocess touches anything"
    # guarantee already established for the managed lifecycle lock
    # (assert_lifecycle_lock_path_identity(), lock_identity) and for each
    # locally patched asset (patched_asset_identities below). Threaded
    # through to tighten_generated_private_file_mode() (immediately after
    # `npm install --package-lock-only`) and re-asserted again immediately
    # after `npm ci` (assert_release_dir_identity()), so a same-UID racer
    # who renames RELEASE_DIR aside and renames a different, legitimately-
    # owned, correctly-permissioned real directory into its place -- at
    # any point during either npm subprocess's run -- is refused instead
    # of silently trusted, even though every per-component property check
    # open_verified_ancestor_chain() performs (real directory, not a
    # symlink, owned by this UID, safe mode) genuinely passes on the
    # replacement (independent Codex sol/max round-21 review, 2026-08-19,
    # P1: round 20's fix refused only a SYMLINKED RELEASE_DIR; a same-UID
    # rename-swap to another real, well-formed directory validates
    # properties-in-the-moment, not identity continuity, so it passed
    # every existing check).
    #
    # Deliberately NOT named `lock_identity` or `package_lock_identity`
    # for the same shadowing reason `manifest_identity` below is not
    # either -- see the comment at package_lock_identity's own capture.
    release_dir_stat = RELEASE_DIR.lstat()
    release_dir_identity = (release_dir_stat.st_dev, release_dir_stat.st_ino)
    # Round 24, 2026-08-19 (independent Codex sol/max round-23 review,
    # P1-1/P1-2 root-cause fix): hold an O_DIRECTORY|O_NOFOLLOW file
    # descriptor on RELEASE_DIR itself, opened immediately after this
    # installer creates it, for the ENTIRE remainder of this install -- not
    # merely a captured (st_dev, st_ino) VALUE re-checked at isolated
    # points (release_dir_identity, above, and assert_release_dir_identity(),
    # both kept unchanged for their existing checkpoints and regression
    # tests) but the literal open file descriptor, so the kernel itself
    # guarantees the underlying directory object cannot be deleted-and-
    # reused out from under this install for as long as it stays open. See
    # assert_release_dir_fd_identity() for the full reasoning, and
    # _install_locked_within_release_dir() -- the rest of this function's
    # body, split out so this descriptor can be closed via a plain
    # try/finally around the call rather than needing that entire body
    # re-indented under one -- for how it is threaded through every
    # subsequent release-relative operation: bracketing both `npm`
    # subprocess invocations (run_npm()'s release_dir_fd parameter), used
    # directly (via os.dup(), bypassing the SSD_ROOT ancestor re-walk
    # entirely) by tighten_generated_private_file_mode(), and used for the
    # post-`npm ci` directory operations (moving node_modules into place,
    # creating bin/) that used to resolve RELEASE_DIR by lexical path with
    # no identity binding at all.
    try:
        release_dir_fd = os.open(
            RELEASE_DIR, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as exc:
        raise PrimeInstallError(
            "cannot open managed Prime Agent release directory"
        ) from exc
    try:
        assert_release_dir_fd_identity(release_dir_fd)
        return _install_locked_within_release_dir(
            lock_identity, evidence, release_dir_identity, release_dir_fd
        )
    finally:
        try:
            os.close(release_dir_fd)
        except OSError:
            pass


def _install_locked_within_release_dir(
    lock_identity: tuple[int, int],
    evidence: dict[str, Any],
    release_dir_identity: tuple[int, int],
    release_dir_fd: int,
) -> dict[str, Any]:
    """The remainder of _install_locked(): every step from RELEASE_DIR's
    subdirectories being created through the final pending-install journal
    write, run while `release_dir_fd` -- an O_DIRECTORY|O_NOFOLLOW
    descriptor opened on RELEASE_DIR immediately after _install_locked()
    itself created it, strictly before either external `npm` subprocess
    below ever runs -- is held open by the caller for this call's entire
    duration. See assert_release_dir_fd_identity() for why holding the
    live descriptor is strictly stronger than only re-comparing captured
    (st_dev, st_ino) value tuples, and _install_locked() for how it is
    opened and closed.
    """
    assets_dir = ensure_private_dir(RELEASE_DIR / "assets")
    cache = ensure_private_dir(TOOL_ROOT / "npm-cache")
    install_home = ensure_private_dir(TOOL_ROOT / "install-home")
    install_tmp = ensure_private_dir(TOOL_ROOT / "install-tmp")
    ensure_private_dir(managed_session_dir())
    safe_download(LOCK_URL, RELEASE_DIR / "upstream-package-lock.json", LOCK_SHA256)
    safe_download(LICENSE_URL, RELEASE_DIR / "LICENSE", LICENSE_SHA256)
    # Round 38, 2026-08-20 (independent Claude opus/max round-37 review,
    # P2-1): safe_download() verifies downloaded bytes IN MEMORY against
    # LOCK_SHA256 and then writes them to disk -- this call used to re-open
    # that same file from disk by PATH via a bare `.read_bytes()` immediately
    # afterward, with no digest re-check at all, mirroring the exact TOCTOU
    # gap round 13/14 already closed for the Node.js toolchain tarball
    # (extract_node_toolchain()) and the four locally patched packages'
    # source tarballs (safe_extract_main_asset()) -- see either of those
    # functions' own comments for the full finding. Bounded by the
    # closure-hash gate below and by `--ignore-scripts` (a tampered
    # upstream lock cannot itself execute anything), but this file's own
    # established convention is to close a TOCTOU window it already has the
    # mechanism for rather than leave it merely bounded elsewhere -- mirror
    # that same read_private_file()-then-re-hash pattern exactly, rather
    # than a second, independently raceable open-by-path.
    upstream_lock_raw = read_private_file(
        RELEASE_DIR / "upstream-package-lock.json", max_bytes=MAX_DOWNLOAD_BYTES
    )
    if sha256_bytes(upstream_lock_raw) != LOCK_SHA256:
        raise PrimeInstallError(
            "upstream package lock changed on disk before use"
        )
    upstream_lock = strict_json(upstream_lock_raw)
    if not isinstance(upstream_lock, dict) or upstream_lock.get("lockfileVersion") != 3:
        raise PrimeInstallError("unexpected upstream lock identity")
    for name, digest in ASSETS.items():
        safe_download(
            f"https://github.com/PrimeIntellect-ai/prime-agent/releases/download/v{VERSION}/{name}",
            assets_dir / name,
            digest,
        )
    safe_download(
        f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ASSET}",
        assets_dir / NODE_ASSET,
        NODE_ASSET_SHA256,
        max_bytes=MAX_NODE_DOWNLOAD_BYTES,
    )
    # `toolchain_content_digests` (round 34, 2026-08-20, P1-2) is the
    # COMPLETE per-file digest map for every regular file extract_node_
    # toolchain() wrote under RELEASE_DIR/toolchain/, captured directly
    # from the digest-verified Node.js tarball's bytes as they were
    # streamed to disk -- not merely node_sha256/npm_cli_sha256's two
    # entry-point entries. Threaded into `release_relative_pinned_digests`
    # below, alongside `patched_content_digests`, so the ENTIRE toolchain
    # tree gets the same zero-window install-time pinning the four locally
    # patched packages' own full trees already have (round 27/28) -- see
    # extract_node_toolchain()'s own docstring for the reproduced finding
    # this closes (real npm library code under toolchain/lib/node_modules/
    # npm/lib/ was previously unpinned and ran, unverified, inside every
    # exact_tool_version() npm-version probe).
    node, npm_cli, node_sha256, npm_cli_sha256, toolchain_content_digests = (
        extract_node_toolchain(
            assets_dir / NODE_ASSET, RELEASE_DIR / "toolchain", NODE_ASSET_SHA256
        )
    )
    # Round 24, 2026-08-19 (independent Codex sol/max round-23 review,
    # P1-1): node_sha256/npm_cli_sha256, captured by extract_node_toolchain()
    # directly from the digest-verified tarball's bytes as they were
    # streamed to disk, are the ONLY independently-trustworthy record of
    # what these two pinned runtime files' content is SUPPOSED to be --
    # node/npm_cli themselves are bare Paths from here on, exec'd by
    # lexical path at every point below. Re-verify BOTH digests, via
    # verify_unchanged_private_ssd_asset_digest(), immediately before EVERY
    # subsequent point either binary is exec'd: a same-UID racer who swaps
    # either file's on-disk content in place at any later point -- most
    # dangerously, during the long `npm ci` subprocess window further down
    # (measured real window: 3m34s) -- is refused here instead of silently
    # exec'd. Round 22's RELEASE_DIR-identity guard (release_dir_identity/
    # release_dir_fd above) proves the DIRECTORY was never swapped; it says
    # nothing about a same-UID overwrite of a file reachable through it,
    # which is the gap this closes.
    node_stat = node.lstat()
    node_identity = (node_stat.st_dev, node_stat.st_ino)
    npm_cli_stat = npm_cli.lstat()
    npm_cli_identity = (npm_cli_stat.st_dev, npm_cli_stat.st_ino)
    verify_unchanged_private_ssd_asset_digest(node, node_sha256, node_identity)
    exact_tool_version(
        [os.fspath(node), "--version"],
        NODE_VERSION,
        "Node.js",
        cache=cache,
        install_home=install_home,
        install_tmp=install_tmp,
    )
    verify_unchanged_private_ssd_asset_digest(node, node_sha256, node_identity)
    verify_unchanged_private_ssd_asset_digest(npm_cli, npm_cli_sha256, npm_cli_identity)
    exact_tool_version(
        [os.fspath(node), os.fspath(npm_cli), "--version"],
        NPM_VERSION,
        "npm",
        cache=cache,
        install_home=install_home,
        install_tmp=install_tmp,
    )
    patched_assets: dict[str, str] = {}
    patched_manifests: dict[str, dict[str, Any]] = {}
    # Populated alongside patched_assets from each make_patched_asset() call
    # below and re-verified, together with patched_assets' digests, by
    # verify_patched_assets_unchanged() immediately before each of the two
    # npm invocations that independently re-read these paths from disk on
    # their own (round 16, 2026-08-18, P1; see
    # verify_unchanged_private_ssd_asset_digest()).
    patched_asset_identities: dict[str, tuple[int, int]] = {}
    # Round 27, 2026-08-19 (independent Claude opus/max round-27 review,
    # P1): EVERY make_patched_asset() call's own tarball-derived, per-file
    # digest map is kept here now, keyed by managed_name -- not just the
    # main "prime-agent" asset's. Round 25/26 discarded the three workspace
    # assets' own maps entirely (bound to `_` below) on the theory that only
    # prime-agent's extracted tree contains the entrypoint this installer
    # executes; that theory was correct about the entrypoint but wrong about
    # scope -- dist/bundle/cli.js statically and dynamically imports the
    # REST of prime-agent's own dist/bundle/ directory unconditionally, and
    # none of that, nor any file of the three workspace assets, was ever
    # content-verified after npm ci. Every entry here is threaded to
    # assert_locally_patched_package_matches_pinned_digests() further below,
    # once npm ci returns, so every file of all four locally patched
    # packages -- not only prime-agent's entrypoint -- is verified against
    # its own pre-npm-ci, tarball-derived baseline.
    patched_content_digests: dict[str, dict[Path, str]] = {}
    workspace_order = (
        "@earendil-works/pi-ai",
        "@earendil-works/pi-tui",
        "@earendil-works/pi-agent-core",
    )
    for managed_name in workspace_order:
        declaration = WORKSPACE_PACKAGES[managed_name]
        official_asset_name = str(declaration["official_asset"])
        patched, digest, manifest, identity, workspace_content_digests = make_patched_asset(
            assets_dir / official_asset_name,
            ASSETS[official_asset_name],
            upstream_lock,
            assets_dir,
            expected_name=str(declaration["source_name"]),
            managed_name=managed_name,
            output_name=str(declaration["patched_asset"]),
        )
        patched_assets[patched.name] = digest
        patched_manifests[managed_name] = manifest
        patched_asset_identities[patched.name] = identity
        patched_content_digests[managed_name] = workspace_content_digests
    main_asset_name = f"prime-agent-{VERSION}.tgz"
    (
        patched_asset,
        patched_sha,
        patched_manifest,
        patched_asset_identity,
        # The main "prime-agent" asset's own tarball-derived, per-file
        # digest map. Kept under its own name (rather than only reachable
        # via patched_content_digests["prime-agent"], set immediately below)
        # because the entrypoint-digest capture further below (round 25,
        # 2026-08-19, P1-A) reads specifically from this one map.
        main_content_digests,
    ) = make_patched_asset(
        assets_dir / main_asset_name,
        ASSETS[main_asset_name],
        upstream_lock,
        assets_dir,
        expected_name="prime-agent",
        managed_name="prime-agent",
        output_name=MAIN_PATCHED_ASSET,
    )
    patched_assets[patched_asset.name] = patched_sha
    patched_manifests["prime-agent"] = patched_manifest
    patched_asset_identities[patched_asset.name] = patched_asset_identity
    patched_content_digests["prime-agent"] = main_content_digests
    root_manifest = {
        "name": "orca-managed-prime-agent",
        "version": VERSION,
        "private": True,
        "dependencies": {"prime-agent": f"file:assets/{MAIN_PATCHED_ASSET}"},
    }
    manifest_path = RELEASE_DIR / "package.json"
    manifest_raw = canonical_json(root_manifest)
    atomic_create_private_file(manifest_path, manifest_raw, 0o600)
    manifest_stat = manifest_path.lstat()
    manifest_identity = (manifest_stat.st_dev, manifest_stat.st_ino)
    # Re-verify package.json is still exactly what was just published,
    # immediately before each npm invocation that independently re-reads it
    # from RELEASE_DIR on its own -- closing the verify-then-use gap between
    # publication and each of the two npm reads below (independent review
    # round 5, 2026-08-18, P2-3; see verify_unchanged_private_ssd_file()).
    verify_unchanged_private_ssd_file(manifest_path, manifest_raw, manifest_identity)
    # Same re-check, extended to the four locally patched tarballs
    # themselves -- `npm install --package-lock-only` reads all four of
    # them (via package.json's file: dependencies) to generate the lock
    # below, so they need the identical verify-then-use re-check
    # package.json just got, immediately before this same invocation
    # (round 16, 2026-08-18, P1).
    verify_patched_assets_unchanged(assets_dir, patched_assets, patched_asset_identities)
    verify_unchanged_private_ssd_asset_digest(node, node_sha256, node_identity)
    verify_unchanged_private_ssd_asset_digest(npm_cli, npm_cli_sha256, npm_cli_identity)
    run_npm(
        os.fspath(npm_cli),
        os.fspath(node),
        ["install", "--package-lock-only", "--omit=dev", "--ignore-scripts", "--install-links"],
        RELEASE_DIR,
        cache,
        install_home,
        install_tmp,
        # This is the one run_npm() call that GENERATES a brand-new file
        # (package-lock.json) whose initial mode this installer does not
        # control -- narrow the child's umask so npm creates it already
        # private, shrinking the window before
        # tighten_generated_private_file_mode() below explicitly fixes it.
        # Defense in depth only: that dir_fd-bound tightening step is what
        # actually closes the gap (see its own docstring and
        # open_verified_generated_file_parent()); this does not replace it
        # and is deliberately not applied to the `npm ci` call further down
        # (see run_npm()'s own docstring for why).
        child_umask=0o077,
        release_dir_fd=release_dir_fd,
    )
    lock_path = RELEASE_DIR / "package-lock.json"
    # `npm install --package-lock-only` just generated this file itself, at
    # whatever mode the subprocess's own ambient umask allows (empirically
    # 0o644 under the common umask 0o022) -- unlike every other private SSD
    # state file this installer writes, which atomic_write()/
    # atomic_create_private_file() already publish at 0o600 before any
    # content is ever visible at the final path. Tighten it to that same
    # 0o600 right now, before the plain read below or
    # verify_unchanged_private_ssd_file() further down apply
    # read_private_file()'s `st_mode & 0o077 == 0` private-file requirement
    # to it -- otherwise every real (non-mocked) install fails closed here
    # with "unsafe private file" (round 18, 2026-08-19, P1; see
    # tighten_generated_private_file_mode()).
    #
    # Deliberately NOT named `lock_identity`: this function's own parameter
    # is ALSO named `lock_identity` and carries the managed LIFECYCLE lock's
    # (st_dev, st_ino) identity all the way through to the
    # finalize_pending_install(lock_identity) call at the end of this
    # function. Reusing that name here for package-lock.json's identity used
    # to silently rebind (shadow) the parameter for the remainder of this
    # function, so the value finally reaching finalize_pending_install() was
    # package-lock.json's identity, not the lifecycle lock's -- which
    # finalize_pending_install() then compared against the REAL lifecycle
    # lock path's current identity and deterministically raised "managed
    # lifecycle lock identity changed while held" on every single successful
    # install, after the pending journal had already been durably written
    # (independent dual review round 6, 2026-08-18, P1: found independently
    # by both a Claude opus/max review and a separate Codex QA pass). Keep
    # this name distinct from the `lock_identity` parameter for the same
    # reason `manifest_identity` above is not named `lock_identity` either.
    package_lock_identity = tighten_generated_private_file_mode(
        lock_path,
        expected_parent_identity=release_dir_identity,
        parent_dir_fd=release_dir_fd,
    )
    generated_lock_raw = lock_path.read_bytes()
    generated_lock = strict_json(generated_lock_raw)
    if not isinstance(generated_lock, dict):
        raise PrimeInstallError("generated lock is invalid")
    closure = validate_generated_lock(generated_lock_raw, generated_lock, patched_assets)
    # Offline, pure comparison against upstream_lock (already parsed and
    # digest-verified above) -- see compute_lock_drift()'s own docstring.
    # Threaded into the receipt below so every real install now leaves a
    # durable, automatic record of this diff instead of it having to be
    # hand-reconstructed by a review agent from scratch (round-53 QA,
    # 2026-08-22, recommendation #1).
    lock_drift = compute_lock_drift(generated_lock["packages"], upstream_lock["packages"])
    # The set of top-level node_modules/ package directory names this
    # closure DECLARES -- derived from the SAME "packages" map
    # validate_generated_lock() just pinned the whole install to (via
    # GENERATED_LOCK_SHA256), captured now, before `npm ci` runs. Used by
    # assert_materialized_node_modules_matches_lock() further below, after
    # `npm ci` returns, to catch a same-UID racer planting an undeclared
    # sibling package directly on disk during that subprocess's own
    # runtime (round 25, 2026-08-19, P1-B; see that function's own
    # docstring for exactly what this does and does not cover).
    declared_top_level_packages = declared_top_level_node_modules_packages(
        generated_lock["packages"]
    )
    # The same closure's NESTED node_modules/ declarations (a package's own
    # sub-dependencies) -- round 27, 2026-08-19, P2-2 fix; see
    # declared_nested_node_modules_packages()'s own docstring. Captured here,
    # alongside the top-level set above, from the same pinned lock content
    # and before the same `npm ci` runs.
    declared_nested_packages = declared_nested_node_modules_packages(
        generated_lock["packages"]
    )
    # Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
    # P1-1): every REGISTRY dependency row this closure declares --
    # excluding the four locally patched packages/workspace assets, which
    # already get their own complete pre-npm-ci pinned digest map from
    # make_patched_asset() above -- captured from the SAME validated lock
    # content declared_top_level_packages/declared_nested_packages already
    # are, before `npm ci` runs. Used below, once `npm ci` returns, to
    # derive each materialized registry package's OWN complete per-file
    # digest map from npm's own local cache and verify it the same way the
    # four locally patched packages already are (see
    # verified_registry_package_tarball()'s own docstring for exactly how
    # and why that is trustworthy, and declared_registry_package_lock_rows()
    # for exactly which rows this covers).
    declared_registry_rows = declared_registry_package_lock_rows(
        generated_lock["packages"]
    )
    # Re-verify both package.json and the just-validated package-lock.json
    # are still exactly what was read/validated above, immediately before
    # `npm ci` independently re-reads both from RELEASE_DIR on its own
    # (same rationale as the "install" re-check above).
    verify_unchanged_private_ssd_file(manifest_path, manifest_raw, manifest_identity)
    verify_unchanged_private_ssd_file(lock_path, generated_lock_raw, package_lock_identity)
    # Same re-check as before the "install" step above, immediately before
    # `npm ci` independently re-reads all four patched tarballs from disk
    # on its own to actually install them (round 16, 2026-08-18, P1).
    verify_patched_assets_unchanged(assets_dir, patched_assets, patched_asset_identities)
    verify_unchanged_private_ssd_asset_digest(node, node_sha256, node_identity)
    verify_unchanged_private_ssd_asset_digest(npm_cli, npm_cli_sha256, npm_cli_identity)
    run_npm(
        os.fspath(npm_cli),
        os.fspath(node),
        ["ci", "--omit=dev", "--ignore-scripts", "--install-links"],
        RELEASE_DIR,
        cache,
        install_home,
        install_tmp,
        release_dir_fd=release_dir_fd,
    )
    # `npm ci` is the SECOND long external subprocess window this install
    # exposes RELEASE_DIR to -- and everything from here through the end of
    # this function (materializing/verifying node_modules, moving it to
    # lib/node_modules, chmod'ing the entrypoint, writing the launch guard
    # and command wrapper) resolves RELEASE_DIR by plain lexical path, with
    # no identity check at all, immediately after that window closes.
    # Re-assert RELEASE_DIR's identity right now, before any of that trusts
    # it again -- closing the SAME same-UID rename-swap gap
    # tighten_generated_private_file_mode() above closes for the FIRST
    # window, applied to the second (independent Codex sol/max round-21
    # review, 2026-08-19, P1 residual; see assert_release_dir_identity()).
    # run_npm() above already re-asserted the STRONGER, fd-based identity
    # (assert_release_dir_fd_identity()) both before and after this exact
    # subprocess call; this value-tuple check is kept, unchanged, as an
    # additional, independent layer and so this checkpoint's own regression
    # coverage keeps exercising it directly (round 24, 2026-08-19).
    assert_release_dir_identity(release_dir_identity)
    assert_release_dir_fd_identity(release_dir_fd)
    # Re-verify the pinned toolchain binaries one last time: `npm ci` is
    # the longest single external-subprocess window this install exposes
    # them to (measured real window for a same-UID content swap: 3m34s).
    # `node` is about to be baked into the launch guard as a permanent
    # future trust anchor below.
    #
    # Round 29, 2026-08-19 (independent Claude opus/max round-29 review,
    # P1-2): this call previously covered only `node`, even though this
    # exact comment already (incorrectly) claimed "the pinned toolchain
    # BINARIES", plural. `npm-cli.js` was re-verified only BEFORE each of
    # the two `npm` invocations above ran (so it could itself be trusted
    # for THAT exec) -- never AFTER `npm ci` returns, the one window that
    # actually matters for everything downstream: nothing else in this
    # function, and nothing in verify() before this round, ever compared
    # npm-cli.js's content against receipt['npm_cli_sha256'] again.
    # Reproduced: tampering npm-cli.js during `npm ci`'s own subprocess
    # window (measured real window: ~77 seconds, the full `npm ci`
    # runtime) went completely undetected -- receipt['npm_cli_sha256']
    # was written but never read back by anything (see verify()'s own
    # P1-2 fix, this same round, for the closing half: it now compares
    # receipt['node_sha256']/['npm_cli_sha256']/['entrypoint_sha256']
    # against freshly computed digests immediately before executing any
    # of the three on every FUTURE invocation, not merely here at install
    # time).
    verify_unchanged_private_ssd_asset_digest(node, node_sha256, node_identity)
    verify_unchanged_private_ssd_asset_digest(npm_cli, npm_cli_sha256, npm_cli_identity)
    # Round 25, 2026-08-19 (independent Claude opus/max round-25 review,
    # P1-B): before trusting ANYTHING `npm ci` materialized under
    # node_modules/, confirm npm did not ALSO materialize an undeclared
    # top-level package directory -- a same-UID racer's "sibling module",
    # planted directly on disk at any point during `npm ci`'s own
    # multi-minute runtime without ever touching RELEASE_DIR's own
    # directory entry, so none of the six assert_release_dir_identity()/
    # assert_release_dir_fd_identity() calls in this function would ever
    # raise for it. See assert_materialized_node_modules_matches_lock()'s
    # own docstring for exactly what this check does and does not cover.
    assert_materialized_node_modules_matches_lock(
        RELEASE_DIR,
        declared_top_level_packages,
        declared_nested_packages,
        packages=declared_registry_rows,
    )
    # Round 27, 2026-08-19 (independent Claude opus/max round-27 review,
    # P1): the check above is purely STRUCTURAL -- directory presence and
    # naming, at any nesting depth -- and says nothing about the CONTENT of
    # a package directory that IS declared. Verify every file of every one
    # of the four locally patched packages against its own pre-npm-ci,
    # tarball-derived digest map now, before ANY of their content is
    # trusted for publication -- see
    # assert_locally_patched_package_matches_pinned_digests()'s own
    # docstring for exactly what this closes and what remains an accepted
    # residual afterward.
    for locally_patched_name, pinned_digests in patched_content_digests.items():
        assert_locally_patched_package_matches_pinned_digests(
            RELEASE_DIR / "node_modules" / locally_patched_name,
            pinned_digests,
            locally_patched_name,
        )
    # Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
    # P1-1): the structural check above, and the per-package sweep just
    # above it, both stop at the four locally patched packages -- the
    # ~196 third-party REGISTRY dependency packages this closure declares
    # had no content verification at all through round 35. For every such
    # row actually materialized on this platform (a declared row that
    # `npm ci` never downloaded -- e.g. an optional, platform-specific
    # dependency -- has nothing on disk to verify, and is skipped exactly
    # like assert_materialized_node_modules_matches_lock() already
    # tolerates that same gap for its own structural check), derive a
    # complete per-file digest map from npm's own local cache (the SAME
    # `cache` directory `npm ci` just downloaded into) and verify the
    # actual on-disk tree against it via the SAME sweep the four locally
    # patched packages already get. `registry_pinned_digests` is kept for
    # the post-move re-sweep and the `release_relative_pinned_digests`
    # fold further below, so each package's cached tarball is only read
    # and decompressed once, not once per sweep.
    registry_pinned_digests: dict[str, dict[Path, str]] = {}
    # Round 38, 2026-08-20 (independent Claude opus/max round-37 review,
    # P1-1): every lock_path this ONE, attacker-writable-filesystem
    # presence probe below finds absent (or symlinked) at this exact
    # instant -- kept so the post-move completeness assertion further down
    # (after the node_modules -> lib/node_modules move) can confirm that
    # absence still holds against the POST-MOVE tree, rather than trusting
    # this single pre-move snapshot as the last word. See that assertion's
    # own comment for the full finding this closes; tree_digest()'s own
    # round-38 deny-unknown fix independently closes the same exploitation
    # path a different way (see its docstring), so this is defense in
    # depth, not the only mechanism this now relies on.
    skipped_registry_lock_paths: list[str] = []
    for lock_path in sorted(declared_registry_rows):
        package_dir = RELEASE_DIR / lock_path
        if package_dir.is_symlink() or not package_dir.is_dir():
            skipped_registry_lock_paths.append(lock_path)
            continue
        pinned_digests = registry_package_content_digests(
            verified_registry_package_tarball(
                cache, declared_registry_rows[lock_path]["integrity"], lock_path
            ),
            lock_path,
        )
        registry_pinned_digests[lock_path] = pinned_digests
        assert_locally_patched_package_matches_pinned_digests(
            package_dir, pinned_digests, lock_path
        )
    installed_package = RELEASE_DIR / "node_modules/prime-agent"
    if installed_package.is_symlink() or not installed_package.is_dir():
        raise PrimeInstallError("npm did not materialize a private Prime Agent package")
    # Round 27, 2026-08-19: the loop just above already verified
    # dist/bundle/cli.js's content against this SAME `main_content_digests`
    # pinned baseline, as part of prime-agent's complete file tree -- so the
    # content comparison below (pinned_entrypoint_sha256 vs.
    # observed_entrypoint_sha256) is now redundant with that broader sweep.
    # It is kept, unchanged, because it is NOT solely a content check: it
    # also captures `entrypoint_identity`, the (st_dev, st_ino) pair every
    # verify_unchanged_private_ssd_asset_digest() call below and after the
    # node_modules move needs to re-assert continuity through, and which
    # gets baked into the launch guard as a permanent future trust anchor --
    # a concern assert_locally_patched_package_matches_pinned_digests() does
    # not address. Both comparisons check the same on-disk bytes against
    # entries of the exact same pinned map, so this is a harmless, not a
    # divergent, duplicate.
    #
    # Round 24, 2026-08-19 (independent Codex sol/max round-23 review,
    # P1-2): capture the entrypoint's content digest right NOW -- strictly
    # before any further installer-side step (moving node_modules,
    # chmod'ing the entrypoint, generating the launch guard) gives a
    # same-UID racer more time to overwrite it in place. RELEASE_DIR's own
    # identity checks above prove the DIRECTORY was not swapped; they say
    # nothing about a same-UID actor rewriting THIS file's bytes while
    # leaving RELEASE_DIR itself untouched -- tree_digest() (used later, by
    # write_pending_install()/finalize_pending_install()) only RECORDS
    # whatever is on disk at its own, much later call time rather than
    # COMPARING against a value captured before this vulnerable window, so
    # it would silently treat a swap that happened before its first call as
    # the legitimate baseline. See verify_unchanged_private_ssd_asset_digest()
    # for how this is re-verified at each subsequent step below.
    #
    # Round 25, 2026-08-19 (independent Claude opus/max round-25 review,
    # P1-A): round 24's claim, directly above, that this is "the EARLIEST
    # point its final, trustworthy value can be known" was wrong -- this
    # read happens AFTER `npm ci` has already returned, so a same-UID
    # racer who overwrites this exact path DURING `npm ci`'s own runtime
    # (rather than after it returns) has THEIR bytes read here and would
    # be adopted as the permanent trust baseline, with every check below
    # merely re-confirming the racer's own value never changed again.
    # `main_content_digests` (returned by the make_patched_asset() call
    # above for the "prime-agent" asset) fixes this: it is
    # safe_extract_main_asset()'s own per-file digest map, captured
    # directly from the digest-verified ORIGINAL tarball's bytes as they
    # were streamed to disk -- strictly BEFORE `npm ci` (or anything else
    # in this function) ever ran, and never influenced by anything that
    # happens afterward, in-window or not. Comparing the freshly observed
    # on-disk digest below against THIS pinned value, rather than trusting
    # the observed value outright, closes the window entirely for this one
    # file: it does not matter WHEN a same-UID swap happened, only whether
    # the final bytes match what the original, digest-verified tarball
    # actually contained. (Empirically confirmed, by this round's own real,
    # non-mocked tests/sandbox_e2e.py run: real `npm ci`'s materialized
    # dist/bundle/cli.js is byte-identical to the raw tarball member, so
    # this is a direct equality check, not a heuristic.)
    pre_move_entrypoint = installed_package / "dist/bundle/cli.js"
    if pre_move_entrypoint.is_symlink() or not pre_move_entrypoint.is_file():
        raise PrimeInstallError("Prime Agent entrypoint missing")
    # `npm ci` just materialized this file itself, at whatever mode the
    # published tarball's own stored permissions specify -- empirically
    # 0o644 (world-readable) for a real prime-agent release, discovered by
    # this round's own real, non-mocked tests/sandbox_e2e.py run, not by
    # any mocked unit test (same root cause as round 18's package-lock.json
    # finding -- see tighten_generated_private_file_mode()'s docstring --
    # applied here to a DIFFERENT npm-generated file this round's new
    # digest-capture step is the first thing in this file to ever read via
    # the private-file discipline). capture_private_ssd_asset_digest()
    # reads via read_private_ssd_file(), which requires `st_mode & 0o077
    # == 0`; left untightened, it fails closed here with "unsafe private
    # file" on every real (non-mocked) install. Tighten to 0o700 -- the
    # SAME mode the original, pre-round-24 code already chmod'ed this file
    # to, just applied here instead of after the move -- right now, in
    # place, before any read of this file is attempted.
    os.chmod(pre_move_entrypoint, 0o700)
    observed_entrypoint_sha256, entrypoint_identity = capture_private_ssd_asset_digest(
        pre_move_entrypoint
    )
    pinned_entrypoint_sha256 = main_content_digests.get(Path("dist/bundle/cli.js"))
    if pinned_entrypoint_sha256 is None:
        # Cannot happen given safe_extract_main_asset()'s own
        # manifest/digest self-consistency check (every regular file it
        # extracted has a content_digests entry) together with the
        # pre_move_entrypoint.is_file() check just above, which already
        # proves `npm ci` materialized this exact relative path from the
        # patched tarball make_patched_asset() built from that same
        # extraction -- defense-in-depth against this function's own logic
        # drifting from that invariant, not a same-UID-attacker detection.
        raise PrimeInstallError(
            "Prime Agent entrypoint missing from the tarball-verified digest map"
        )
    if observed_entrypoint_sha256 != pinned_entrypoint_sha256:
        raise PrimeInstallError(
            "Prime Agent entrypoint content does not match the digest-verified "
            "original tarball -- a same-UID actor may have swapped it during "
            "or after npm ci"
        )
    # From here on, `entrypoint_sha256` is the PINNED, tarball-derived
    # value -- never the value freshly read from disk above -- so every
    # downstream re-verification, the launch guard's baked-in CLI_SHA256,
    # and the receipt's entrypoint_sha256 field are all anchored to a
    # baseline that was fixed before npm ever ran. The comparison above
    # already confirmed the two are equal for THIS install; using the
    # pinned one is what makes that comparison load-bearing for every
    # future invocation rather than a one-time self-check.
    entrypoint_sha256 = pinned_entrypoint_sha256
    global_root = RELEASE_DIR / "lib/node_modules"
    # Round 24, 2026-08-19: create "lib" and rename node_modules into it
    # via dir_fd-relative operations bound to release_dir_fd, rather than
    # RELEASE_DIR-prefixed lexical paths -- threading the held descriptor
    # through this publication step too, matching the established
    # rename_noreplace_dir_fd() no-clobber idiom this file already uses
    # elsewhere (remove_exact_symlink()) instead of silently downgrading to
    # plain os.rename()'s replace-if-present semantics.
    os.mkdir("lib", 0o700, dir_fd=release_dir_fd)
    lib_descriptor = os.open(
        "lib",
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=release_dir_fd,
    )
    try:
        rename_noreplace_dir_fd(
            release_dir_fd,
            "node_modules",
            lib_descriptor,
            "node_modules",
            display_destination=global_root,
        )
    finally:
        os.close(lib_descriptor)
    os.mkdir("bin", 0o700, dir_fd=release_dir_fd)
    bin_dir = RELEASE_DIR / "bin"
    entrypoint = global_root / "prime-agent/dist/bundle/cli.js"
    # The rename above moves node_modules -> lib/node_modules as a single
    # directory-entry rename; no descendant (including cli.js) is itself
    # re-created, so its inode -- and therefore the identity
    # capture_private_ssd_asset_digest() captured above, pre-move -- is
    # unchanged by the move itself. Re-verify anyway, immediately after the
    # move and again immediately before this content digest is baked into
    # the launch guard as a permanent trust anchor, rather than assuming
    # the rename's own atomicity is sufficient proof nothing else raced in
    # between.
    verify_unchanged_private_ssd_asset_digest(
        entrypoint, entrypoint_sha256, entrypoint_identity
    )
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise PrimeInstallError("Prime Agent entrypoint missing")
    # Already tightened to 0o700 above, pre-move; re-asserting it here is
    # now a harmless, defense-in-depth no-op (rename preserves mode) kept
    # for parity with the original, pre-round-24 chmod call site.
    os.chmod(entrypoint, 0o700)
    verify_unchanged_private_ssd_asset_digest(
        entrypoint, entrypoint_sha256, entrypoint_identity
    )
    verify_unchanged_private_ssd_asset_digest(node, node_sha256, node_identity)
    launch_guard = bin_dir / "prime-agent-launch-guard.py"
    # Round 32, 2026-08-19 (independent Claude opus/max round-31 review,
    # P1): the launch guard and command wrapper are the two files this
    # installer ITSELF generates and that get exec'd on every managed
    # `prime-agent` invocation thereafter (BIN_LINK -> command wrapper ->
    # launch guard -> node/entrypoint), yet -- unlike node/npm-cli (pinned
    # from a digest-verified tarball, streamed to disk) and the entrypoint
    # (pinned to the tarball's own per-file digest, see
    # `entrypoint_sha256` above) -- neither was ever included in
    # `release_relative_pinned_digests` below. Both were already read back
    # once, at creation time, by atomic_create_private_file()'s own
    # write-then-reopen-then-compare mechanism -- but that only proves the
    # bytes landed on disk correctly; it does not make them the trusted
    # release baseline. That step happens later, when write_pending_
    # install()'s tree_digest() walk reaches these paths -- and, absent a
    # pin, tree_digest() just RECORDS whatever it finds there THEN, with
    # zero comparison against anything. A same-UID racer who overwrites
    # either file in the real, externally observable window between this
    # atomic_create_private_file() call and tree_digest() later reaching
    # this same path (bin/ sorts after lib/, so this window is the
    # remainder of this function plus most of tree_digest()'s own walk --
    # reproduced end-to-end, ~1.5s warm-cache on a real ~26,911-entry
    # release tree, using the same busy-poll-for-appearance technique round
    # 29 used against its own analogous window) has the tampered bytes
    # silently adopted as the permanent `release_tree_sha256` baseline:
    # finalize_pending_install()/verify() recompute the tree digest and it
    # matches the (already-tampered) receipt, and neither file's content
    # was covered by verify()'s own pre-exec digest check (round 29's
    # node_sha256/npm_cli_sha256/entrypoint_sha256 loop) either --
    # permanent RCE on every future managed invocation, verify() reports OK
    # forever.
    #
    # The fix follows the SAME zero-window pattern round 29/30 established
    # for node/npm-cli/the entrypoint, but from an even stronger starting
    # position: this installer is the ORIGIN of the guard's and wrapper's
    # bytes, not merely an early observer of bytes some external tarball
    # produced, so their digest can be computed directly from the exact
    # `bytes` object about to be written -- no separate read, no
    # regenerating the script a second time (which would itself risk a
    # false-positive mismatch if `managed_launch_guard_script()`/
    # `managed_entrypoint_script()` were ever non-deterministic across
    # calls) -- before any untrusted window exists at all. `launch_guard_
    # raw`/`command_wrapper_raw` below are that exact object: hashed once,
    # then written via atomic_create_private_file() unchanged, then folded
    # into `release_relative_pinned_digests` so tree_digest()'s existing
    # same-read compare-then-fold mechanism (round 29/30) automatically
    # covers them with no new mechanism needed. Their digests are also
    # recorded in the receipt (`launch_guard_sha256`/`command_wrapper_
    # sha256`) and re-checked by verify()'s pre-exec loop on every future
    # invocation, exactly mirroring node/npm-cli/the entrypoint -- defense
    # in depth in case a future bug ever reopened the tree_digest()-level
    # protection.
    launch_guard_raw = managed_launch_guard_script(
        node, entrypoint, node_sha256, entrypoint_sha256
    )
    launch_guard_sha256 = sha256_bytes(launch_guard_raw)
    atomic_create_private_file(launch_guard, launch_guard_raw, 0o700)
    command_wrapper = bin_dir / "prime-agent"
    command_wrapper_raw = managed_entrypoint_script(node, entrypoint, launch_guard)
    command_wrapper_sha256 = sha256_bytes(command_wrapper_raw)
    atomic_create_private_file(command_wrapper, command_wrapper_raw, 0o700)
    telemetry_settings = initialize_prime_state()
    probe_agent_dir = initialize_probe_home()
    ensure_private_dir(TOOL_ROOT / "receipts")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "version": VERSION,
        "tag_commit": TAG_COMMIT,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "volume_uuid": evidence["volume_uuid"],
        "release_dir": os.fspath(RELEASE_DIR),
        "state_dir": os.fspath(STATE_DIR),
        "state_link": os.fspath(STATE_LINK),
        "bin_link": os.fspath(BIN_LINK),
        "bin_target": os.fspath(command_wrapper),
        "launch_guard": os.fspath(launch_guard),
        "lifecycle_lock": os.fspath(lifecycle_lock_path()),
        "node_target": os.fspath(node),
        "npm_target": os.fspath(npm_cli),
        # Round 24 (2026-08-19)/round 25 (2026-08-19, P1-A)/round 29
        # (2026-08-19, P1-2): the content digests captured at extraction
        # time (node/npm_cli) and, for the entrypoint, PINNED to the
        # original digest-verified prime-agent tarball's own per-file
        # digest for dist/bundle/cli.js -- confirmed (not merely read) to
        # match `npm ci`'s actual materialized output -- see
        # extract_node_toolchain() and make_patched_asset()'s
        # content_digests return value. Enforced DURING this install at
        # each re-verification point above; baked into the generated
        # launch guard as a permanent future trust anchor for `node`/the
        # entrypoint (managed_launch_guard_script()'s NODE_SHA256/
        # CLI_SHA256); and, as of round 29 (2026-08-19, P1-2), read back
        # and compared against a freshly computed digest by verify() on
        # every FUTURE invocation for all three fields, including
        # npm_cli_sha256 -- previously "provenance only", written but
        # never read back by anything, dead evidence that could not have
        # caught a same-UID racer who tampered one of these files DURING
        # this very install's own `npm ci` window and had the tampered
        # content adopted as tree_digest()'s own recorded baseline (see
        # that function's own P1-1 fix, this same round, for the
        # install-time half of this fix; verify()'s check is what catches
        # it on every invocation AFTER this one, independent of whatever
        # tree_digest() itself recorded).
        "node_sha256": node_sha256,
        "npm_cli_sha256": npm_cli_sha256,
        "entrypoint_sha256": entrypoint_sha256,
        # Round 32, 2026-08-19 (independent Claude opus/max round-31
        # review, P1): the launch guard's and command wrapper's own
        # content digests, computed directly from the exact bytes this
        # install wrote for each (see the `launch_guard_raw`/
        # `command_wrapper_raw` capture above) -- the same treatment as
        # node_sha256/npm_cli_sha256/entrypoint_sha256 just above, extended
        # to the two files this installer itself generates. Folded into
        # `release_relative_pinned_digests` (below) so tree_digest() closes
        # the install-time window the same way it already does for those
        # three, and read back by verify()'s pre-exec digest loop on every
        # future invocation for defense in depth.
        "launch_guard_sha256": launch_guard_sha256,
        "command_wrapper_sha256": command_wrapper_sha256,
        "probe_home": os.fspath(PROBE_HOME),
        "session_dir": os.fspath(managed_session_dir()),
        "asset_sha256": ASSETS,
        "node_asset_sha256": NODE_ASSET_SHA256,
        "upstream_lock_sha256": LOCK_SHA256,
        "license_sha256": LICENSE_SHA256,
        "production_lock_sha256": closure["lock_sha256"],
        "patched_asset_sha256": patched_assets,
        "patched_manifest_names": sorted(patched_manifests),
        # Round 36, 2026-08-20 (P1-1): the lock_path of every registry
        # dependency package this install actually content-verified against
        # npm's own local cache (see registry_pinned_digests, built above,
        # and declared_registry_package_lock_rows()'s own docstring for
        # exactly which declared rows this can and cannot cover). Recorded
        # for audit/observability -- not itself re-checked by verify(),
        # which relies on the ongoing full-tree release_tree_sha256
        # comparison for post-install drift the same way it already does
        # for every other file (see verify()'s own tree_digest() call).
        "registry_packages_content_verified": sorted(registry_pinned_digests),
        # See compute_lock_drift()'s own docstring: a durable, automatic
        # record of every registry package this install resolved to a
        # DIFFERENT version than the pinned upstream source lock
        # (LOCK_SHA256) declares, keyed by bare package name. Real,
        # useful signal for the next re-pin review -- not itself a
        # pass/fail gate; this install already succeeded by the time this
        # is recorded (round-53 QA, 2026-08-22, recommendation #1).
        "lock_drift_from_upstream_source": lock_drift,
        "generated_lock_pinned_at": GENERATED_LOCK_PINNED_AT,
        "closure": closure,
        "node_version": NODE_VERSION,
        "npm_version": NPM_VERSION,
        "orca_support": evidence["orca_support"],
        "lifecycle_scripts_executed": False,
        "daemon_started": False,
        "credentials_configured": False,
        "command_default_enabled": False,
        "project_settings_default_allowed": False,
        "project_executable_resources_default_allowed": False,
        "telemetry_default_enabled": False,
        "telemetry_settings": os.fspath(telemetry_settings),
        "probe_agent_dir": os.fspath(probe_agent_dir),
    }
    # Round 29, 2026-08-19 (independent Claude opus/max round-29 review,
    # P1-1): the per-package digest sweep above (assert_locally_patched_
    # package_matches_pinned_digests(), called once per locally patched
    # package immediately after `npm ci` returned, before the node_modules
    # move) verified content correctly, but its result was trusted across
    # a real, externally-observable window before write_pending_install()'s
    # tree_digest() call established the permanent baseline:
    # os.mkdir("lib", ...) above is a public signal a same-UID racer can
    # poll for, and tree_digest()'s own sorted walk takes real time to
    # reach any given path. Measured real window pre-fix: ~6.8 seconds
    # (1.3s from the sweep completing to the mkdir, plus up to 5.5s for
    # tree_digest()'s own walk to later reach a tampered path). Two
    # closes, applied together (see tree_digest()'s own docstring for the
    # first):
    #
    #   1. tree_digest() now accepts `pinned_relative_digests` and, for
    #      every RELEASE_DIR-relative path in the map built below,
    #      compares its own freshly computed content digest against the
    #      pinned value in the SAME read that becomes part of the
    #      recorded release_tree_sha256 baseline -- collapsing the CONTENT
    #      half of the window to whatever a single
    #      O_NOFOLLOW-open-then-fstat-identity-checked read cannot itself
    #      close.
    #   2. The per-package sweep is re-run here, immediately before
    #      write_pending_install(), at each package's POST-MOVE path (the
    #      packages now live under lib/node_modules/, not node_modules/).
    #      tree_digest()'s comparison (fix 1) cannot by itself detect a
    #      pinned file going MISSING (it simply would not appear in the
    #      walk) or an extra, undeclared file appearing in its place (it
    #      has no pinned entry to compare against, so it would be silently
    #      recorded like any other unpinned file) -- exactly the
    #      completeness checks assert_locally_patched_package_matches_
    #      pinned_digests()'s own missing/undeclared-file bookkeeping
    #      already performs.
    #
    # Honest measured result (real, non-mocked tests/sandbox_e2e.py replay
    # against this closure's real ~26,911-entry release tree on the
    # external SSD, round 29, 2026-08-19): the WALL-CLOCK time from this
    # re-sweep completing to write_pending_install()'s first tree_digest()
    # call completing is real and NOT small -- ~42.6s in that run, because
    # tree_digest()'s own sorted walk must read and hash every regular
    # file in the entire release tree (not only the pinned ones) before it
    # returns, and this closure's real content is large enough that this
    # is genuinely disk-I/O-bound work, not a cheap loop. That number is
    # NOT the size of a silent-acceptance security window, though, and
    # reporting it as one would itself be an over-claim in the other
    # direction: for every RELEASE_DIR-relative path present in
    # `release_relative_pinned_digests` (built just below), BOTH
    # tree_digest() calls this function makes (the one that establishes
    # release_identity, and the one after the durability-sync barrier)
    # independently compare their own freshly read content against that
    # SAME pinned baseline the instant they read it -- so content that
    # differs from the pinned value, whenever and wherever in this window
    # it is introduced, is caught by whichever of these reads observes it
    # first, and the whole install aborts before finalize_pending_install()
    # ever commits a receipt. There is therefore no point in this window at
    # which tampered content for a pinned path can become the committed
    # release_tree_sha256 without being caught -- the "gap between content
    # verified and content becomes the permanently-trusted baseline" this
    # finding asked to close is zero for every pinned path, not merely
    # narrowed, because the read that verifies IS the read that (if it
    # matches) contributes to the baseline. What the ~42.6s / ~69.6s
    # figures actually measure is wall-clock LATENCY, not an exploitable
    # window: a separate, controlled A/B benchmark (sha256_file_verified()
    # vs. the plain sha256_file() every other call site here already used,
    # run interleaved over the same 27,000 real files with a warm page
    # cache to remove ordering bias) measured sha256_file_verified()'s own
    # per-file overhead at roughly 15-20% over sha256_file() -- a few
    # hundred milliseconds total across a tree this size -- so the great
    # majority of the measured 42.6s/69.6s duration is pre-existing
    # full-tree content-hashing cost tree_digest() already paid, every time
    # it was called (at install AND at every verify()), before this round;
    # this round's fix made those already-slow calls fail closed on a
    # pinned-content mismatch instead of leaving that residual UNCLOSED, at
    # the cost of a small, separately-measured increment to a cost that was
    # already there.
    #
    # Round 32, 2026-08-19 (independent Claude opus/max round-31 review,
    # P1): the paragraph above, as written through round 30, characterized
    # the only remaining residual as the microsecond-scale lstat/open gap
    # (see below) without ever stating WHICH paths that claim actually
    # covered -- an over-claim by omission, because `release_relative_
    # pinned_digests` at that point covered only node, npm-cli, and the
    # four locally patched packages' own tarball-declared members (which
    # includes the entrypoint, dist/bundle/cli.js). It did NOT cover the
    # launch guard (bin/prime-agent-launch-guard.py) or the command
    # wrapper (bin/prime-agent) -- the two files THIS INSTALLER ITSELF
    # generates, and that get exec'd on every future managed `prime-agent`
    # invocation (BIN_LINK -> command wrapper -> launch guard ->
    # node/entrypoint) -- so a same-UID racer who tampered either one in
    # the window between its own atomic_create_private_file() call and
    # tree_digest() later reaching that same path had the tampered bytes
    # silently adopted as the permanent baseline: permanent RCE on every
    # future invocation, verify() reporting OK forever. Both are now
    # included in `release_relative_pinned_digests` above (see the comment
    # at their creation site, just before `launch_guard_raw`/`command_
    # wrapper_raw`), closing that gap the same zero-window way.
    #
    # What is actually true as of this fix: EVERY RELEASE_DIR-relative path
    # this installer or its generated scripts ever exec -- node, npm-cli,
    # the entrypoint (dist/bundle/cli.js), the launch guard, and the
    # command wrapper -- is now in `release_relative_pinned_digests`, so
    # tree_digest()'s same-read compare-then-fold mechanism covers all five
    # with zero window, and verify()'s pre-exec digest loop independently
    # re-checks all five on every future invocation as defense in depth.
    #
    # Round 34, 2026-08-20 (independent Claude opus/max round-33 review,
    # P1-1/P1-2): two fixes to the pinning mechanism itself, both closed
    # together this round:
    #
    #   P1-1: tree_digest()'s pinned-digest comparison only ever fired for
    #   a pinned path that was still a REGULAR FILE at walk time -- a
    #   pinned path replaced with a symlink, replaced with a directory, or
    #   deleted outright skipped the comparison entirely and got folded
    #   into (or silently omitted from) the baseline with no check at all.
    #   tree_digest() now asserts, after its walk, that every key of
    #   whatever `pinned_relative_digests` map it was given was actually
    #   observed and content-matched as a regular file -- see that
    #   function's own docstring for the full finding and fix.
    #
    #   P1-2: `release_relative_pinned_digests` previously pinned only TWO
    #   files under toolchain/ -- bin/node and lib/node_modules/npm/bin/
    #   npm-cli.js -- even though real npm-cli.js is a two-line forwarding
    #   stub (`require('../lib/cli.js')`) whose actual library code (lib/
    #   cli.js and everything it requires) was never pinned, despite
    #   exact_tool_version()'s npm-version probe genuinely executing it on
    #   every install and every verify(). `toolchain_content_digests`
    #   (extract_node_toolchain()'s now-complete return value; see its own
    #   docstring) is folded into `release_relative_pinned_digests` below
    #   for every regular file under toolchain/, not merely the two entry
    #   points -- the exact same full-tree pinning pattern the four locally
    #   patched packages already had since round 27/28, now applied to the
    #   toolchain too.
    #
    # What is FULLY pinned as of this round, matching the four locally
    # patched packages' own treatment: node, npm-cli, EVERY OTHER regular
    # file under toolchain/ (npm's full library tree, its own nested
    # dependencies, docs -- everything extract_node_toolchain() extracted),
    # the entrypoint (dist/bundle/cli.js) and the rest of the four locally
    # patched packages' own trees, the launch guard, and the command
    # wrapper. tree_digest()'s pinned-digest comparison plus this round's
    # P1-1 completeness assertion cover all of it with zero install-time
    # window, and verify()'s pre-exec digest loop separately re-checks the
    # five actually-exec'd files (node, npm-cli, entrypoint, launch guard,
    # command wrapper) on every future invocation as defense in depth --
    # unchanged this round, since the newly-pinned toolchain files beyond
    # node/npm-cli are not individually exec'd by anything, only reachable
    # through npm-cli.js's own `require()` chain, which IS exec'd (that is
    # the P1-2 finding this fix closes).
    #
    # The genuinely narrower residual that remains, accepted and
    # deliberately not fixed this round because it is NOT code that this
    # installer or its generated scripts ever execute: files under
    # RELEASE_DIR that are recorded in tree_digest()'s overall
    # release_tree_sha256 (so tampering them still shows up as detected
    # "release tree drifted" DRIFT) but are NOT individually pinned --
    # package.json, package-lock.json, LICENSE, and
    # upstream-package-lock.json. A same-UID racer who tampers one of
    # these in the same install-time window has that tampered content
    # silently adopted as part of the recorded tree_digest() baseline,
    # exactly as the pinned paths' pre-round-29 behavior was -- but nothing
    # in this installer or its generated scripts ever reads or executes
    # any of these four files' content again after install, so the
    # practical consequence of winning that race is inert recorded drift
    # (a receipt field that silently reflects attacker-chosen bytes
    # nothing acts on), not code execution. Closing this residual too is
    # straightforward given the mechanism now built (add each path's
    # already-known-correct digest -- e.g. LICENSE_SHA256, the generated
    # lock's own hash -- to the same map) but is intentionally out of THIS
    # round's required scope.
    #
    # Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
    # P1-1): the paragraph above, through round 34, separately noted that
    # "the ~196 third-party registry dependency packages' own file content
    # remains unpinned by this mechanism" as an accepted, NOT-newly-
    # introduced gap. That is no longer accurate, and leaving it unchanged
    # would itself be the over-claiming-by-omission bug class this file has
    # already flagged in itself at rounds 25, 27, 29, and 32 (this
    # paragraph's own history, just above): `registry_pinned_digests`,
    # built above from npm's own local cache (see
    # verified_registry_package_tarball()'s own docstring for exactly how
    # that is derived and independently re-verified), is folded into
    # `release_relative_pinned_digests` the identical way the four locally
    # patched packages' own maps are, immediately above. Every registry
    # dependency package this closure's `npm ci` actually materializes on
    # this platform is therefore now covered by this SAME zero-window
    # mechanism -- not merely re-recorded as inert drift, but content- and
    # completeness-verified (missing pinned file, extra undeclared file, or
    # content mismatch each fail closed) the same way the four locally
    # patched packages already were. What genuinely remains unmitigated,
    # stated as precisely as this round can measure it: (a) any declared
    # registry row this platform's `npm ci` never downloads at all -- a
    # platform-specific optional dependency skipped for darwin-arm64 --
    # has no cached tarball and no materialized directory, so there is
    # nothing to derive a digest from OR anything on disk to tamper; this
    # is not a gap in verification, it is the absence of anything to
    # verify, and is unconditionally excluded from
    # `declared_registry_package_lock_rows()`'s own registry_pinned_digests
    # construction the same way assert_materialized_node_modules_matches_lock()
    # already tolerates the identical gap for its own structural check; (b)
    # the four bookkeeping files named at the start of this paragraph
    # (package.json, package-lock.json, LICENSE, upstream-package-lock.json)
    # -- unrelated to this round's fix, unchanged from round 34; and (c) the
    # same microsecond-scale in-process lstat/open gap described in the
    # paragraph just below, which applies identically to every pinned path
    # regardless of which mechanism derived its expected digest.
    #
    # The one genuinely irreducible residual for every path that IS
    # pinned (including the toolchain's now-complete tree) is the same
    # class this file already accepts elsewhere (see e.g.
    # create_fresh_private_dir()'s own docstring): the microsecond-scale,
    # in-process gap inside a single sha256_file_verified() call between
    # its own lstat() and O_NOFOLLOW open() -- not something an external
    # racer can reliably win without a filesystem primitive (e.g. an
    # atomic snapshot) this installer does not have, and not something any
    # amount of re-sweeping can close further, since it is internal to the
    # one read a caller has no choice but to trust once taken.
    #
    # Round 36, 2026-08-20 (independent Claude opus/max round-35 review,
    # P1-2b): assert_materialized_node_modules_matches_lock() above was, for
    # every round through 35, called exactly ONCE -- before the move just
    # above -- despite this function's own docstring already claiming a
    # newly-planted sibling package is caught, full stop, with no caveat
    # that this was only true BEFORE the move. A same-UID racer who plants
    # an undeclared sibling package directly under global_root (the
    # POST-MOVE node_modules/, now at lib/node_modules/) in the window this
    # function spans -- the move itself, "bin" mkdir, the entrypoint chmod,
    # launch guard/wrapper generation -- went completely undetected: this
    # check was never run again against the tree that window actually
    # produces. Re-run it now, against the post-move tree (global_root.parent
    # is RELEASE_DIR/"lib", so global_root.parent/"node_modules" is
    # global_root itself), at the same point the per-package content sweep
    # just below is already re-run a second time post-move -- closing this
    # the same way round 29/30 already established for that sweep.
    assert_materialized_node_modules_matches_lock(
        global_root.parent,
        declared_top_level_packages,
        declared_nested_packages,
        packages=declared_registry_rows,
    )
    for locally_patched_name, pinned_digests in patched_content_digests.items():
        assert_locally_patched_package_matches_pinned_digests(
            global_root / locally_patched_name,
            pinned_digests,
            locally_patched_name,
        )
    # Round 36, 2026-08-20 (P1-1): the registry-package counterpart of the
    # loop just above -- re-verify every materialized registry package's
    # content against the SAME cache-derived digest map the pre-move sweep
    # already computed (registry_pinned_digests, reused rather than
    # re-derived so each package's cached tarball is only read once), now
    # against its post-move path.
    for lock_path, pinned_digests in registry_pinned_digests.items():
        assert_locally_patched_package_matches_pinned_digests(
            global_root.parent / lock_path, pinned_digests, lock_path
        )
    # Round 38, 2026-08-20 (independent Claude opus/max round-37 review,
    # P1-1): the pre-move presence probe that built `registry_pinned_
    # digests` above is a SINGLE snapshot on a filesystem the same-UID
    # attacker this file's whole threat model assumes can also write to --
    # a package directory it found absent (or symlinked) is permanently
    # excluded from that map, and every check that consults the map (the
    # sweep just above, the pinning fold further below) is structurally
    # blind to whatever was excluded. Re-derive completeness from the
    # POST-MOVE tree instead of trusting that one snapshot: any lock_path
    # the probe skipped that is NOW a real, materialized directory --
    # whether because a same-UID racer planted it in the window between
    # the probe and this point, or because it is one of the declared rows
    # this platform's `npm ci` unconditionally skips every single install
    # (round 37's own "zero-race" reproduction: no timing needed at all,
    # since nothing legitimate ever contends for that exact path) -- fails
    # closed here, with a specific, early error, rather than only being
    # caught downstream by tree_digest()'s own round-38 deny-unknown check
    # (which independently closes the identical exploitation path; see its
    # docstring -- this is deliberate defense in depth, not the sole
    # mechanism either check alone is required to be).
    #
    # Round 40, 2026-08-20 (independent Claude opus/max round-39 review,
    # P1-1): this check originally read `post_move_dir.is_dir() and not
    # post_move_dir.is_symlink()` -- which evaluates False, i.e. "still
    # absent, still fine", for a SYMLINK planted at `post_move_dir` (a
    # symlink is never `is_dir() is True` in the sense this condition
    # intended; `is_dir()` on a symlink-to-directory follows the link and
    # returns True, but `not is_symlink()` then makes the whole conjunction
    # False for exactly that case) -- the identical blind spot that
    # independently defeated tree_digest()'s own round-38 fix a different
    # way (see this file's tree_digest() docstring, round 40 paragraph, for
    # the full LICENSE-target reproduction). Fixed to fail closed on ANY
    # post-move materialization at all -- file, symlink (dangling or not),
    # or directory -- using the same `path.exists() or path.is_symlink()`
    # idiom already used everywhere else in this file to detect "something
    # is now here, of any kind" without silently following a symlink into a
    # False negative.
    for lock_path in skipped_registry_lock_paths:
        post_move_dir = global_root.parent / lock_path
        if post_move_dir.exists() or post_move_dir.is_symlink():
            raise PrimeInstallError(
                "registry package path is materialized (file, symlink, or "
                "directory) but was absent (or a symlink) at the "
                "pre-npm-ci-completion presence probe -- refusing to trust "
                f"unverified content: {lock_path}"
            )
    release_relative_pinned_digests: dict[str, str] = {
        os.fspath(node.relative_to(RELEASE_DIR)): node_sha256,
        os.fspath(npm_cli.relative_to(RELEASE_DIR)): npm_cli_sha256,
        # Round 32, 2026-08-19 (independent Claude opus/max round-31
        # review, P1): the launch guard and command wrapper -- the two
        # remaining files actually exec'd on every future managed
        # invocation -- pinned to the exact bytes this install itself
        # generated and wrote for each (see `launch_guard_raw`/
        # `command_wrapper_raw` above). This closes the round-31 finding:
        # every RELEASE_DIR-relative path that is ever exec'd by the
        # managed command (node, npm-cli, the entrypoint, the launch
        # guard, the command wrapper) now has a pinned entry here.
        os.fspath(launch_guard.relative_to(RELEASE_DIR)): launch_guard_sha256,
        os.fspath(command_wrapper.relative_to(RELEASE_DIR)): command_wrapper_sha256,
    }
    for locally_patched_name, pinned_digests in patched_content_digests.items():
        package_relative = os.fspath(
            (global_root / locally_patched_name).relative_to(RELEASE_DIR)
        )
        for member_relative, member_sha256 in pinned_digests.items():
            release_relative_pinned_digests[
                f"{package_relative}/{member_relative.as_posix()}"
            ] = member_sha256
    # Round 36, 2026-08-20 (P1-1): fold every registry package's own
    # cache-derived digest map into the same pinned baseline, exactly the
    # same way the four locally patched packages' own maps are folded just
    # above -- extending tree_digest()'s zero-install-time-window guarantee
    # (see the long comment above `release_relative_pinned_digests` for
    # what that guarantee actually is and is not) to every registry
    # dependency package this closure materializes on this platform, not
    # only the four locally patched ones.
    for lock_path, pinned_digests in registry_pinned_digests.items():
        package_relative = os.fspath(
            (global_root.parent / lock_path).relative_to(RELEASE_DIR)
        )
        for member_relative, member_sha256 in pinned_digests.items():
            release_relative_pinned_digests[
                f"{package_relative}/{member_relative.as_posix()}"
            ] = member_sha256
    # Round 34, 2026-08-20 (independent Claude opus/max round-33 review,
    # P1-2): fold the COMPLETE toolchain digest map into the same pinned
    # baseline, keyed the same way `node`/`npm_cli`'s own two explicit
    # entries above already are ("toolchain/" + the destination-relative
    # path extract_node_toolchain() extracted it to) -- both of those
    # explicit entries are themselves keys of `toolchain_content_digests`
    # (it is the SAME map node_sha256/npm_cli_sha256 were pulled from), so
    # this loop harmlessly re-assigns them to the identical value already
    # set above rather than diverging from it. This is the toolchain's
    # full-tree counterpart to the locally patched packages' loop just
    # above -- see extract_node_toolchain()'s own docstring for the
    # reproduced finding this closes (real npm library code under
    # toolchain/lib/node_modules/npm/lib/ executing, unpinned, inside
    # every exact_tool_version() npm-version probe).
    for member_relative, member_sha256 in toolchain_content_digests.items():
        release_relative_pinned_digests[
            f"toolchain/{member_relative.as_posix()}"
        ] = member_sha256
    write_pending_install(
        receipt, pinned_relative_digests=release_relative_pinned_digests
    )
    return finalize_pending_install(lock_identity)


def load_receipt() -> dict[str, Any]:
    if PENDING_PATH.exists() or PENDING_PATH.is_symlink():
        raise PrimeInstallError("pending Prime Agent install journal must be recovered first")
    if not RECEIPT_PATH.exists() and not RECEIPT_PATH.is_symlink():
        raise PrimeInstallError("managed install receipt missing")
    raw = read_private_ssd_file(RECEIPT_PATH)
    receipt = strict_json(raw)
    return validate_receipt_identity(receipt)


def verify_link(link: Path, expected_target: Path) -> None:
    # Bound to the SAME dir_fd-chained, non-symlinked ancestor descriptor
    # ensure_local_link_parent() creation goes through -- not a plain
    # lexical path -- so an ancestor an attacker has since replaced with a
    # symlink (e.g. ~/.local -> <attacker dir> holding its own
    # bin/prime-agent -> <managed target>) is refused here before the leaf
    # link is even inspected, instead of being silently followed (independent
    # review round 1, 2026-08-18, P1-2: previously link.lstat() and
    # os.readlink(link) resolved the full lexical path with no verification
    # that link's ancestors were still the real, non-symlinked USER_HOME
    # structure, so an interposed ancestor symlink made this function -- and
    # therefore verify_command_state() -- report success for a configuration
    # every other managed path in this file already rejects).
    parent_descriptor = verify_link_parent_descriptor(link)
    try:
        try:
            before = os.stat(link.name, dir_fd=parent_descriptor, follow_symlinks=False)
            target_text = os.readlink(link.name, dir_fd=parent_descriptor)
        except OSError as exc:
            raise PrimeInstallError(f"required link is missing: {link}") from exc
        if (
            not stat.S_ISLNK(before.st_mode)
            or target_text != os.fspath(expected_target)
        ):
            raise PrimeInstallError(f"required link target drifted: {link}")
        try:
            raw_target = Path(target_text)
            actual = (
                raw_target if raw_target.is_absolute() else link.parent / raw_target
            ).resolve(strict=True)
            expected = expected_target.resolve(strict=True)
            after = os.stat(link.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except (OSError, RuntimeError) as exc:
            raise PrimeInstallError(f"required link is broken: {link}") from exc
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or not stat.S_ISLNK(after.st_mode)
        ):
            raise PrimeInstallError(f"required link identity changed: {link}")
        if actual != expected or not is_relative_to(
            actual, SSD_ROOT.resolve(strict=True)
        ):
            raise PrimeInstallError(f"required link target drifted: {link}")
    finally:
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def prime_agent_search_directories() -> list[Path]:
    directories: list[Path] = []
    for raw_directory in os.get_exec_path():
        # execvp treats an empty PATH component as the current directory, and
        # any other relative component also changes meaning with cwd.  A plan
        # or verification performed in one project therefore cannot safely
        # certify command resolution for a later launch from another project.
        if not raw_directory or not os.path.isabs(raw_directory):
            raise PrimeInstallError(
                "PATH contains a cwd-dependent command directory; "
                "use absolute, non-empty PATH components"
            )
        directories.append(Path(raw_directory).absolute())
    nvm_versions = USER_HOME / ".nvm/versions/node"
    if nvm_versions.exists() or nvm_versions.is_symlink():
        try:
            if not nvm_versions.is_dir():
                raise OSError("not a directory")
            directories.extend(
                path / "bin"
                for path in sorted(nvm_versions.iterdir(), reverse=True)
                if path.is_dir()
            )
        except OSError as exc:
            raise PrimeInstallError("cannot inspect Orca's nvm command fallback") from exc
    directories.extend(
        (
            USER_HOME / ".volta/bin",
            USER_HOME / ".asdf/shims",
            USER_HOME / ".fnm/aliases/default/bin",
            USER_HOME / ".local/share/mise/shims",
            USER_HOME / ".local/bin",
            USER_HOME / "Library/pnpm",
            USER_HOME / ".yarn/bin",
            USER_HOME / ".bun/bin",
        )
    )
    unique: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        candidate = directory.absolute()
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def prime_agent_command_candidates() -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for directory in prime_agent_search_directories():
        candidate = (directory / "prime-agent").absolute()
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            info = candidate.stat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and os.access(candidate, os.X_OK):
            candidates.append(candidate)
    return candidates


def ensure_no_prime_agent_command() -> None:
    command_conflicts = prime_agent_command_candidates()
    if command_conflicts:
        raise PrimeInstallError(
            "an existing prime-agent command is visible to Orca; refusing install: "
            + ", ".join(os.fspath(path) for path in command_conflicts)
        )


def bin_link_present() -> bool:
    """dir_fd-bound counterpart of `BIN_LINK.exists() or BIN_LINK.is_symlink()`,
    using the SAME open_verified_ancestor_chain_or_absent() walk
    verify_command_state() already binds its own existence check to,
    rather than the plain lexical Path methods.

    A plain lexical existence check silently resolves through whatever an
    ancestor (e.g. ~/.local) currently points to. A same-UID actor who has
    interposed a symlink there could make BIN_LINK.exists()/.is_symlink()
    report "absent" while the real managed link, reachable only through
    the true, unswapped ancestor chain, is still present -- exactly the
    class of bug fixed for verify_command_state() itself in round 5. The
    callers here (quarantine_partial_release(), finalize_pending_install(),
    _install_locked()'s pre-install conflict check, and plan()'s read-only
    conflict report) all treat BIN_LINK being present as a reason to
    refuse or flag; silently under-detecting it would let a fresh install
    or an automatic quarantine proceed over -- or a plan report omit --
    activation state that is still actually live (independent dual review
    round 6, 2026-08-18, P2).

    Any genuinely missing ancestor is "absent", exactly like the lexical
    check it replaces; every OTHER ancestor-chain rejection (a symlinked
    ancestor, wrong owner/mode, dir_fd support unavailable, ...) still
    fails closed via the PrimeInstallError raised inside
    open_verified_ancestor_chain_or_absent() rather than being reported as
    absent.
    """
    parent_descriptor = open_verified_ancestor_chain_or_absent(BIN_LINK)
    if parent_descriptor is None:
        return False
    try:
        try:
            os.stat(BIN_LINK.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PrimeInstallError(
                f"cannot inspect managed command link: {BIN_LINK}"
            ) from exc
        return True
    finally:
        os.close(parent_descriptor)


def verify_command_state(receipt: dict[str, Any]) -> bool:
    # BIN_LINK's existence/kind is resolved through the SAME dir_fd-chained,
    # non-symlinked ancestor walk verify_link() uses -- never through
    # Path.is_symlink()/Path.exists(), which silently resolve through a
    # since-swapped ancestor and would let this function agree with
    # prime_agent_command_candidates() (also PATH/lexical) that the command
    # is both disabled and absent while the real managed link, reachable
    # only via the true ancestor chain, is still live (independent review
    # round 5, 2026-08-18, P1-3). A genuinely missing parent (nothing has
    # ever been installed under it) is the only case treated as "absent";
    # every other ancestor-chain failure -- a symlinked ancestor above all
    # -- fails closed via the PrimeInstallError raised inside
    # open_verified_ancestor_chain_or_absent()/open_verified_directory_component()
    # rather than being reported as disabled.
    target = Path(receipt["bin_target"])
    candidates = prime_agent_command_candidates()
    parent_descriptor = open_verified_ancestor_chain_or_absent(BIN_LINK)
    if parent_descriptor is None:
        if candidates:
            raise PrimeInstallError(f"Prime Agent is disabled but another command resolves: {candidates}")
        return False
    try:
        try:
            info = os.stat(BIN_LINK.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if candidates:
                raise PrimeInstallError(f"Prime Agent is disabled but another command resolves: {candidates}")
            return False
        except OSError as exc:
            raise PrimeInstallError(f"cannot inspect managed command link: {BIN_LINK}") from exc
    finally:
        os.close(parent_descriptor)
    if stat.S_ISLNK(info.st_mode):
        verify_link(BIN_LINK, target)
        if candidates != [BIN_LINK.absolute()]:
            raise PrimeInstallError(f"Prime Agent command resolution is ambiguous: {candidates}")
        return True
    raise PrimeInstallError("Prime Agent command path is occupied by a non-symlink")


def process_arguments_contain_identity(arguments: str, identity: str) -> bool:
    if not identity:
        return False
    offset = 0
    while True:
        index = arguments.find(identity, offset)
        if index < 0:
            return False
        end = index + len(identity)
        if (
            (index == 0 or arguments[index - 1].isspace())
            and (end == len(arguments) or arguments[end].isspace())
        ):
            return True
        offset = index + 1


def managed_process_ids(receipt: dict[str, Any]) -> list[int]:
    entrypoint = os.fspath(
        Path(receipt["release_dir"]) / "lib/node_modules/prime-agent/dist/bundle/cli.js"
    )
    identities = [entrypoint, os.fspath(BIN_LINK)]
    for identity_key in ("bin_target", "launch_guard"):
        identity = receipt.get(identity_key)
        if isinstance(identity, str):
            identities.append(identity)
    try:
        result = subprocess.run(
            ["/bin/ps", "-axww", "-o", "pid=,comm=,args="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrimeInstallError("cannot inspect managed Prime Agent processes") from exc
    pids: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        command_name = os.path.basename(fields[1])
        arguments = fields[2] if len(fields) == 3 else ""
        first_argument = arguments.strip().split(None, 1)[0] if arguments.strip() else ""
        argv0 = os.path.basename(first_argument)
        path_identity = any(
            process_arguments_contain_identity(arguments, identity)
            or identity == fields[1]
            for identity in identities
        )
        # Prime Agent v0.7.2 deliberately replaces argv with APP_NAME before
        # daemon startup.  Exact title matching is therefore required in
        # addition to absolute managed-path matching; substring matching would
        # incorrectly classify similarly named programs.
        title_identity = (
            command_name == PRIME_AGENT_PROCESS_TITLE
            or argv0 == PRIME_AGENT_PROCESS_TITLE
        )
        if not path_identity and not title_identity:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return sorted(set(pids))


def run_version_probe(receipt: dict[str, Any]) -> str:
    node = Path(receipt["node_target"])
    entrypoint = (
        Path(receipt["release_dir"])
        / "lib/node_modules/prime-agent/dist/bundle/cli.js"
    )
    entrypoint = resolve_ssd(entrypoint)
    probe_home = validate_pristine_managed_home(
        Path(receipt["probe_home"]), "Prime Agent probe home"
    )
    try:
        result = subprocess.run(
            [os.fspath(node), os.fspath(entrypoint), "--version"],
            cwd=probe_home,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env={
                "DO_NOT_TRACK": "1",
                "HOME": os.fspath(probe_home),
                "LC_ALL": "C",
                "NODE_DISABLE_COMPILE_CACHE": "1",
                "NO_COLOR": "1",
                "PATH": os.pathsep.join((os.fspath(node.parent), "/usr/bin", "/bin")),
                "PI_OFFLINE": "1",
                "PI_SKIP_VERSION_CHECK": "1",
                "PRIME_AGENT_CODING_AGENT_DIR": os.fspath(probe_home / "agent"),
                "PRIME_AGENT_CODING_AGENT_SESSION_DIR": os.fspath(
                    managed_session_dir()
                ),
                "PRIME_AGENT_SESSION_DIR": os.fspath(managed_session_dir()),
                "PRIME_AGENT_TELEMETRY": "0",
                "TERM": "dumb",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrimeInstallError("Prime Agent version probe failed") from exc
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    exact_channel_match = (stdout == VERSION and not stderr) or (
        stderr == VERSION and not stdout
    )
    if result.returncode != 0 or not exact_channel_match:
        stdout = " ".join(result.stdout.splitlines())[:300]
        stderr = " ".join(result.stderr.splitlines())[:300]
        raise PrimeInstallError(
            "Prime Agent version probe did not exactly match the pinned release: "
            f"exit={result.returncode} stdout={stdout!r} stderr={stderr!r}"
        )
    return VERSION


def verify(expected_lock_identity: tuple[int, int] | None = None) -> dict[str, Any]:
    evidence = preflight()
    receipt = load_receipt()
    if evidence["volume_uuid"] != receipt.get("volume_uuid"):
        raise PrimeInstallError("Extreme SSD UUID changed")
    if evidence["orca_support"] != receipt.get("orca_support"):
        raise PrimeInstallError("installed Orca Prime Agent support drifted")
    if os.fspath(verify_lifecycle_lock_file()) != receipt.get("lifecycle_lock"):
        raise PrimeInstallError("Prime Agent lifecycle lock identity drifted")
    if expected_lock_identity is not None:
        # Only supplied when verify() is reached while an ancestor caller's
        # exclusive lifecycle lock is still held (_recover_locked(),
        # _enable_locked()): re-assert the lock path still names the exact
        # inode acquired at lock time (independent review round 5,
        # 2026-08-18, P2-2; see assert_lifecycle_lock_path_identity()). The
        # standalone top-level `verify` action never holds this lock at all
        # and always passes None here, unchanged from before this fix.
        assert_lifecycle_lock_path_identity(lifecycle_lock_path(), expected_lock_identity)
    release = verify_private_ssd_dir(Path(receipt["release_dir"]))
    # Round 40, 2026-08-20 (independent Claude opus/max round-39 review,
    # P1-2): defense in depth alongside managed_launch_guard_script()'s own
    # equivalent, independently generated check -- see
    # assert_no_unexpected_ancestor_node_modules()'s own docstring.
    assert_no_unexpected_ancestor_node_modules(release)
    state = validate_runtime_state(Path(receipt["state_dir"]))
    validate_pristine_managed_home(Path(receipt["probe_home"]), "Prime Agent probe home")
    session_dir = verify_private_ssd_dir(Path(receipt["session_dir"]))
    verify_link(STATE_LINK, state)
    command_enabled = verify_command_state(receipt)
    digest, entries = tree_digest(release)
    if digest != receipt.get("release_tree_sha256") or entries != receipt.get("release_tree_entries"):
        raise PrimeInstallError("Prime Agent release tree drifted")
    node = resolve_ssd(Path(receipt["node_target"]))
    npm_cli = resolve_ssd(Path(receipt["npm_target"]))
    entrypoint = resolve_ssd(
        release / "lib/node_modules/prime-agent/dist/bundle/cli.js"
    )
    launch_guard = resolve_ssd(Path(receipt["launch_guard"]))
    command_wrapper = resolve_ssd(Path(receipt["bin_target"]))
    # Round 29, 2026-08-19 (independent Claude opus/max round-29 review,
    # P1-2): receipt['node_sha256']/['npm_cli_sha256']/['entrypoint_sha256']
    # -- each an independently, tarball-derived SHA-256 digest captured
    # strictly BEFORE `npm ci` ever ran during install (see
    # extract_node_toolchain() and make_patched_asset()) -- were written
    # into every install receipt since round 24/25 but never read back by
    # this function at all: dead evidence. The release_tree_sha256
    # comparison just above only detects drift AFTER whatever was recorded
    # as the trusted baseline at install time; it cannot detect a same-UID
    # racer who tampered one of these three files DURING that install's
    # own `npm ci` window and had the tampered content adopted as that
    # very baseline (see write_pending_install()'s/tree_digest()'s own
    # P1-1 fix, this same round, for the install-time half of this fix).
    # Comparing each file's CURRENT on-disk content against its
    # permanently-fixed, tarball-derived receipt digest -- independent of
    # whatever tree_digest() recorded -- catches that class of tampering
    # on every subsequent verify() call, immediately before any of the
    # three is executed below (exact_tool_version() execs `node` and, via
    # it, `npm_cli`; run_version_probe() execs `node` and `entrypoint`).
    #
    # Round 32, 2026-08-19 (independent Claude opus/max round-31 review,
    # P1): extended to the launch guard and command wrapper -- the two
    # files this installer itself GENERATES rather than merely observes
    # from a downloaded tarball (see the `launch_guard_sha256`/
    # `command_wrapper_sha256` receipt fields' own comment, and the
    # `release_relative_pinned_digests` construction in
    # _install_locked_within_release_dir(), for the install-time half of
    # this fix). Neither is actually exec'd by THIS function -- the launch
    # guard and command wrapper are exec'd only via BIN_LINK's own
    # end-user invocation path (command wrapper -> launch guard ->
    # node/entrypoint), not by verify() itself -- so this check is
    # deliberately defense in depth, exactly mirroring how this same loop
    # already treats node/npm_cli/the entrypoint: independent of whatever
    # tree_digest() recorded, and independent of whether anything in this
    # process tree is about to exec these two files right now.
    for label, path, field in (
        ("Node.js runtime", node, "node_sha256"),
        ("npm CLI", npm_cli, "npm_cli_sha256"),
        ("Prime Agent entrypoint", entrypoint, "entrypoint_sha256"),
        ("Prime Agent launch guard", launch_guard, "launch_guard_sha256"),
        ("Prime Agent command wrapper", command_wrapper, "command_wrapper_sha256"),
    ):
        expected = receipt.get(field)
        if not isinstance(expected, str) or not expected:
            raise PrimeInstallError(
                f"managed receipt is missing a pinned {label} digest"
            )
        if sha256_file_verified(path) != expected:
            raise PrimeInstallError(
                f"managed {label} content does not match the pinned "
                "installed digest"
            )
    cache = verify_private_ssd_dir(TOOL_ROOT / "npm-cache")
    install_home = verify_private_ssd_dir(TOOL_ROOT / "install-home")
    install_tmp = verify_private_ssd_dir(TOOL_ROOT / "install-tmp")
    exact_tool_version(
        [os.fspath(node), "--version"],
        NODE_VERSION,
        "Node.js",
        cache=cache,
        install_home=install_home,
        install_tmp=install_tmp,
    )
    exact_tool_version(
        [os.fspath(node), os.fspath(npm_cli), "--version"],
        NPM_VERSION,
        "npm",
        cache=cache,
        install_home=install_home,
        install_tmp=install_tmp,
    )
    observed_version = run_version_probe(receipt)
    processes = managed_process_ids(receipt)
    return {
        "ok": True,
        "version": VERSION,
        "tag_commit": TAG_COMMIT,
        "volume_uuid": evidence["volume_uuid"],
        "release_tree_sha256": digest,
        "release_tree_entries": entries,
        "orca_support": evidence["orca_support"],
        "command_enabled": command_enabled,
        "command_default_enabled": False,
        "version_probe": observed_version,
        "managed_process_ids": processes,
        "state_on_ssd": True,
        "session_dir": os.fspath(session_dir),
        "sessions_on_ssd": True,
        "runtime_on_ssd": True,
        "lifecycle_scripts_executed": False,
        "daemon_started_by_install": False,
        "credentials_configured": False,
        "project_settings_default_allowed": False,
        "project_executable_resources_default_allowed": False,
        "telemetry_default_enabled": False,
    }


# Packages with advisories already reviewed and explicitly accepted for
# this installer's generated production lock -- see README.md's "Pinned
# upstream evidence" section for the full justification each entry
# required. audit_installed_lock() fails closed on any advisory for a
# package NOT listed here, and on any unrecognized/unparseable `npm audit`
# response shape (see that function's own docstring). Matches by package
# name, not by advisory id: `npm audit --json`'s own `via` shape is not
# stable enough across npm versions to parse a specific GHSA id safely
# without real risk of a brittle parser silently accepting the wrong
# thing, so this deliberately accepts ALL current and future advisories
# against a listed package -- the coarser, fail-closed-favoring direction.
# Re-review this entry (and consider tightening it to a specific id, or
# removing it) whenever `npm audit` reports something new against a
# package already on this list.
# Round-54 dual review (2026-08-22, both Claude opus/max and Codex sol/max
# independently, rated P1/blocker): this was originally keyed by bare
# package name alone, which silently accepts EVERY current and future
# advisory against a listed package -- Codex's own real npm-source reading
# (@npmcli/arborist's vuln.js/audit-report.js) and both reviewers' real
# `npm audit` runs confirmed every advisory record reliably carries its own
# GHSA id inside `url` (`https://github.com/advisories/GHSA-xxxx-...`), so
# there was never a real reason to accept less-specific identity. Now keyed
# by the exact (package name, GHSA id) tuple actually reviewed --
# `audit_installed_lock()` fails closed if a record's `url` doesn't parse to
# a GHSA id at all (an unrecognized advisory-id scheme is exactly the "this
# file doesn't understand what it's looking at" case this file's fail-
# closed convention treats as unsafe, not as "probably fine").
ACCEPTED_ADVISORIES = {
    ("extract-zip", "GHSA-jmr9-qjv8-65gv"): (
        "extract-zip <=2.0.1, unvalidated symlink path traversal, no "
        "upstream fix released. Reachable only via its ZIP-extraction code "
        "path; this installer's supported darwin-arm64 target downloads "
        ".tar.gz assets (fd/rg) and extracts them with the system `tar`, "
        "never extract-zip's ZIP branch. Re-review if a future asset or "
        "platform target ever downloads a .zip file, or if a NEW extract-"
        "zip advisory appears (this entry does not cover one -- it is "
        "pinned to this exact GHSA id, not to the package as a whole).  "
        "Verified by independent round-53 QA, 2026-08-22."
    ),
}

_GHSA_ID_PATTERN = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}$")


def _advisory_ghsa_id(record: dict[str, Any]) -> str:
    url = record.get("url")
    if isinstance(url, str):
        match = _GHSA_ID_PATTERN.search(url)
        if match:
            return match.group(0)
    raise PrimeInstallError(
        "npm audit advisory record has no recognizable GHSA id in its url"
    )


def _leaf_advisory_records(vulnerabilities: dict[str, Any]) -> list[dict[str, Any]]:
    """Every root advisory record `npm audit --json` reports, walked out of
    every top-level `vulnerabilities` entry's own `via` list. Verified
    empirically 2026-08-22 (real `npm audit --omit=dev --json` run against
    this exact generated production lock, live registry, pinned Node/npm
    toolchain): a `via` entry is either a real advisory record (a dict,
    naming the package that is ACTUALLY vulnerable) or a bare package-name
    string meaning "vulnerable only because it depends on that other,
    already-separately-reported package" -- `npm audit` reports the SAME
    root advisory again, redundantly, under every package name in the
    affected dependency chain. For this lock that real run reported two
    top-level entries for the one underlying extract-zip advisory:
    `extract-zip` itself (a dict-shaped `via` record) and `prime-agent`
    (a string-shaped `via: ["extract-zip"]`, since it merely depends on
    the vulnerable package). Only the dict-shaped records carry real
    advisory identity and are returned here; string entries are the
    reason this exists at all -- naively checking every top-level key
    against ACCEPTED_ADVISORIES would wrongly flag "prime-agent"
    itself as an unreviewed package on every single audit run.
    """
    records: list[dict[str, Any]] = []
    for entry in vulnerabilities.values():
        if not isinstance(entry, dict):
            raise PrimeInstallError("npm audit returned an unexpected shape")
        via = entry.get("via")
        if not isinstance(via, list):
            raise PrimeInstallError("npm audit returned an unexpected shape")
        for item in via:
            if isinstance(item, dict):
                records.append(item)
            elif not isinstance(item, str):
                raise PrimeInstallError("npm audit returned an unexpected shape")
    return records


def audit_installed_lock() -> dict[str, Any]:
    """Read-only: run `npm audit` against an ALREADY-installed release's
    generated production lock (RELEASE_DIR/package-lock.json).
    managed_npm_environment() deliberately sets `npm_config_audit=false`
    for every OTHER npm invocation in this file (install/enable/verify
    stay fully offline, hermetic, and deterministic -- a transient
    advisory-API outage or rate limit must never fail closed on an
    ordinary lifecycle call). This is the separate, explicitly opt-in gate
    round-53's independent QA review asked for instead: call it whenever
    re-verifying trust (e.g. before a re-pin, or on whatever cadence a
    human decides), not as part of every plan/install/verify/enable.
    Requires live network access to the npm registry; does not modify
    managed install state (npm audit does write ordinary cache entries
    under the managed npm cache directory, the same as every other npm
    invocation in this file -- it does not touch RELEASE_DIR or any
    release-state file).

    Round-54 dual review (2026-08-22, both Claude opus/max and Codex
    sol/max independently, rated P1/blocker): the original version of
    this function only re-verified the Node.js and npm CLI binaries
    themselves before running `npm audit` -- never `package-lock.json`
    (the actual input the audit result is about), never the rest of the
    materialized release tree, never the rest of toolchain/lib/
    node_modules/npm/ that npm-cli.js loads and executes. Under this
    file's own established same-UID-racer threat model (defended against
    everywhere else at real, measured cost), a tampered lock or a planted
    RELEASE_DIR/.npmrc could have produced a false-clean audit result.
    Call verify() first, exactly the way the standalone `verify` CLI
    action already does (no lifecycle lock held, `expected_lock_identity`
    left at its default None) -- this reuses the SAME full, already-
    reviewed trust chain (tree_digest() against receipt['release_tree_
    sha256'], every pinned binary/generated-file digest, orca_support,
    the managed runtime state, and more) rather than re-deriving a
    weaker, partial subset of it here. Only once that has raised nothing
    does this proceed to actually run npm audit.
    """
    verify()
    receipt = load_receipt()
    release = verify_private_ssd_dir(Path(receipt["release_dir"]))
    node = resolve_ssd(Path(receipt["node_target"]))
    npm_cli = resolve_ssd(Path(receipt["npm_target"]))
    cache = verify_private_ssd_dir(TOOL_ROOT / "npm-cache")
    install_home = verify_private_ssd_dir(TOOL_ROOT / "install-home")
    install_tmp = verify_private_ssd_dir(TOOL_ROOT / "install-tmp")
    environment = managed_npm_environment(os.fspath(node), cache, install_home, install_tmp)
    # The one deliberate exception to managed_npm_environment()'s blanket
    # `npm_config_audit=false` -- this function's entire purpose is to run
    # that check for real, on demand.
    environment["npm_config_audit"] = "true"
    try:
        result = subprocess.run(
            [os.fspath(node), os.fspath(npm_cli), "audit", "--omit=dev", "--json"],
            cwd=release,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrimeInstallError("cannot run npm audit") from exc
    # `npm audit` exits non-zero whenever it finds any vulnerability at
    # all, including ones already on ACCEPTED_ADVISORIES -- exit code is
    # deliberately not treated as pass/fail here; the allowlist comparison
    # below is.
    try:
        report = strict_json(result.stdout.encode("utf-8"))
    except PrimeInstallError as exc:
        raise PrimeInstallError("npm audit returned unparseable output") from exc
    if not isinstance(report, dict) or report.get("auditReportVersion") != 2:
        raise PrimeInstallError("npm audit returned an unexpected shape")
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise PrimeInstallError("npm audit returned an unexpected shape")
    accepted: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    for record in _leaf_advisory_records(vulnerabilities):
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise PrimeInstallError("npm audit returned an unexpected shape")
        # Fails closed (raises) if this record's own identity cannot be
        # determined -- deliberately BEFORE the allowlist check, so an
        # advisory this file cannot even identify is never silently
        # treated as accepted just because its package name happens to
        # match one on the list.
        ghsa_id = _advisory_ghsa_id(record)
        info = {
            "name": name,
            "ghsa_id": ghsa_id,
            "severity": record.get("severity"),
            "url": record.get("url"),
            "title": record.get("title"),
        }
        key = (name, ghsa_id)
        (accepted if key in ACCEPTED_ADVISORIES else unexpected).append(info)
    if unexpected:
        unexpected_ids = sorted({f"{item['name']}/{item['ghsa_id']}" for item in unexpected})
        raise PrimeInstallError(
            f"npm audit found advisories not on the reviewed allowlist: {unexpected_ids}"
        )
    return {
        "ok": True,
        "accepted_advisories": accepted,
        "audited_at": datetime.now(timezone.utc).isoformat(),
    }


def _uninstall_locked(lock_identity: tuple[int, int]) -> dict[str, Any]:
    receipt = load_receipt()
    # enable() (_enable_locked() -> verify(lock_identity)) re-asserts the
    # lifecycle lock's identity before its own first mutation; uninstall()
    # used to omit this entirely despite performing the riskiest mutation
    # of the two (removing the managed command link, with process-scan and
    # restore-on-failure handling below) -- add the same re-assertion here,
    # before that first mutation (independent dual review round 6,
    # 2026-08-18, P2; see assert_lifecycle_lock_path_identity()).
    assert_lifecycle_lock_path_identity(lifecycle_lock_path(), lock_identity)
    command_enabled = verify_command_state(receipt)
    target = Path(receipt["bin_target"])
    removed = False
    if command_enabled:
        remove_exact_symlink(BIN_LINK, target)
        removed = True
    try:
        processes = managed_process_ids(receipt)
        if processes:
            raise PrimeInstallError(
                "managed Prime Agent processes are still running; "
                f"use prime-agent shutdown first: {processes}"
            )
        if verify_command_state(receipt):
            raise PrimeInstallError("managed Prime Agent command remained enabled")
    except PrimeInstallError as disable_exc:
        if not removed:
            raise
        try:
            atomic_symlink(target, BIN_LINK)
        except (OSError, PrimeInstallError) as restore_exc:
            raise PrimeInstallError(
                "Prime Agent disable failed; the prior command was not overwritten, and "
                "the managed command link could not be restored"
            ) from restore_exc
        raise PrimeInstallError(f"{disable_exc}; command was restored") from disable_exc
    return {
        "ok": True,
        "command_disabled": True,
        "already_disabled": not command_enabled,
        "release_retained": os.fspath(RELEASE_DIR),
        "state_retained": os.fspath(STATE_DIR),
        "sessions_retained": os.fspath(managed_session_dir()),
        "state_link_retained": os.fspath(STATE_LINK),
    }


def _enable_locked(lock_identity: tuple[int, int]) -> dict[str, Any]:
    verification = verify(lock_identity)
    if verification["command_enabled"]:
        return {"ok": True, "command_enabled": True, "already_enabled": True}
    receipt = load_receipt()
    target = Path(receipt["bin_target"])
    atomic_symlink(target, BIN_LINK)
    try:
        verify_command_state(receipt)
    except PrimeInstallError as verify_exc:
        try:
            remove_exact_symlink(BIN_LINK, target)
        except PrimeInstallError as cleanup_exc:
            raise PrimeInstallError(
                f"Prime Agent enable verification failed ({verify_exc}); command cleanup "
                f"did not complete: {cleanup_exc}"
            ) from cleanup_exc
        raise verify_exc
    return {"ok": True, "command_enabled": True, "bin_link": os.fspath(BIN_LINK)}


def install() -> dict[str, Any]:
    with exclusive_lifecycle_lock(create=True) as (_lock_path, lock_identity):
        return _install_locked(lock_identity)


def recover() -> dict[str, Any]:
    with exclusive_lifecycle_lock(create=True) as (_lock_path, lock_identity):
        return _recover_locked(lock_identity)


def uninstall() -> dict[str, Any]:
    with exclusive_lifecycle_lock(create=False) as (_lock_path, lock_identity):
        return _uninstall_locked(lock_identity)


def enable() -> dict[str, Any]:
    with exclusive_lifecycle_lock(create=False) as (_lock_path, lock_identity):
        return _enable_locked(lock_identity)


def plan() -> dict[str, Any]:
    evidence = preflight()
    conflicts = [
        os.fspath(path)
        for path in (
            RELEASE_DIR,
            STATE_DIR,
            PROBE_HOME,
            managed_session_dir(),
            RECEIPT_PATH,
            STATE_LINK,
        )
        if path.exists() or path.is_symlink()
    ]
    # BIN_LINK's existence is checked via the same dir_fd-chained ancestor
    # walk quarantine_partial_release(), finalize_pending_install(), and
    # _install_locked() use (bin_link_present()), not the lexical
    # `path.exists() or path.is_symlink()` the loop above uses for the
    # other paths -- those all live under TOOL_ROOT, which does not carry
    # BIN_LINK's interposed-ancestor-under-USER_HOME threat model
    # (independent dual review round 6, 2026-08-18, P2).
    if bin_link_present():
        conflicts.append(os.fspath(BIN_LINK))
    conflicts.extend(f"PATH:{path}" for path in prime_agent_command_candidates())
    recovery_conflicts = recovery_manifest_conflicts()
    conflicts.extend(recovery_conflicts)
    pending = PENDING_PATH.exists() or PENDING_PATH.is_symlink()
    return {
        "ok": not conflicts and not pending,
        "action": "plan",
        "version": VERSION,
        "tag_commit": TAG_COMMIT,
        "volume_uuid": evidence["volume_uuid"],
        "node_version": evidence["node_version"],
        "npm_version": evidence["npm_version"],
        "release_dir": os.fspath(RELEASE_DIR),
        "state_dir": os.fspath(STATE_DIR),
        "session_dir": os.fspath(managed_session_dir()),
        "asset_sha256": ASSETS,
        "upstream_lock_sha256": LOCK_SHA256,
        "license_sha256": LICENSE_SHA256,
        "generated_production_lock_sha256": GENERATED_LOCK_SHA256,
        "generated_lock_pinned_at": GENERATED_LOCK_PINNED_AT,
        "generated_lock_pin_age_days": generated_lock_pin_age_days(),
        "orca_support": evidence["orca_support"],
        "lifecycle_scripts": "disabled",
        "daemon": "not_started",
        "credentials": "not_configured",
        "command_default": "disabled_until_enable",
        "project_settings_default": "blocked_until_explicit_opt_in",
        "project_executable_resources_default": "disabled_until_explicit_opt_in",
        "conflicts": conflicts,
        "recovery_conflicts": recovery_conflicts,
        "pending_transaction": pending,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("plan", "install", "verify", "uninstall", "enable", "recover", "audit"),
    )
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
        elif args.action == "enable":
            result = enable()
        elif args.action == "audit":
            result = audit_installed_lock()
        else:
            result = recover()
    except PrimeInstallError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        # Round 51 fix (independent review, P2-2, pre-existing and
        # unrelated to round 49): PrimeInstallError is this tool's own,
        # deliberately-raised failure type, but it was never the only
        # exception a poisoned input could make an action dispatch raise
        # -- e.g. a same-UID-poisoned recovery manifest reaching
        # canonical_json()'s .encode("utf-8") with an unpaired surrogate,
        # or any other stdlib exception this file does not explicitly
        # anticipate (strict_json() now rejects that specific case at
        # parse time too, see its own docstring, but this widening stands
        # on its own as defense in depth for whatever this file's authors
        # did not foresee). Previously any such exception propagated all
        # the way out as a raw Python traceback and a non-zero exit from
        # the interpreter itself, instead of this tool's own
        # {"ok": false, "error": ...} JSON contract that every other
        # failure path here honors -- both install() and recover() reach
        # code that reads and re-serializes recovery-manifest content, so
        # a single poisoned byte sequence could permanently wedge BOTH
        # remediation commands an operator would reach for.
        #
        # Deliberately `except Exception`, never `except BaseException`:
        # SystemExit and KeyboardInterrupt are not Exception subclasses,
        # so Ctrl-C and sys.exit() keep propagating exactly as before,
        # unaffected by this handler. The exception's type name is
        # included (not a full traceback) so a genuine bug during
        # development is still diagnosable from the JSON alone, without
        # printing implementation detail beyond that to stdout.
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
