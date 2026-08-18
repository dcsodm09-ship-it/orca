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
# npm ci is not allowed to run until that exact closure is reproduced.
GENERATED_LOCK_SHA256 = "f537ad6d7061987cd56faf2a268c0322b34c433ef7d257eb8052d54d3d2d224c"
GENERATED_LOCK_PACKAGE_COUNT = 200
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


def canonical_json(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def strict_json(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise PrimeInstallError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrimeInstallError("invalid JSON") from exc


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
    asset: Path, destination: Path
) -> tuple[Path, tuple[Path, ...], dict[Path, str], dict[Path, int]]:
    try:
        archive = tarfile.open(asset, "r:gz")
    except (OSError, tarfile.TarError) as exc:
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


def extract_node_toolchain(asset: Path, destination: Path) -> tuple[Path, Path]:
    expected_root = f"node-v{NODE_VERSION}-darwin-arm64"
    # Must be freshly created, never a pre-existing directory: see
    # create_fresh_private_dir's docstring for why ensure_private_dir()'s
    # accept-existing behavior was unsafe here (independent review round 2,
    # 2026-08-18, P1).
    create_fresh_private_dir(destination)
    try:
        archive = tarfile.open(asset, "r:gz")
    except (OSError, tarfile.TarError) as exc:
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
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        output.write(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if remaining:
                    raise PrimeInstallError(f"short Node.js archive member: {member.name}")
                os.replace(temp_path, target)
                os.chmod(target, mode)
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
    for name, path in (("node", node), ("npm", npm_cli)):
        try:
            info = path.lstat()
        except OSError as exc:
            raise PrimeInstallError(f"pinned {name} runtime is missing") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise PrimeInstallError(f"pinned {name} runtime is unsafe")
    return node, npm_cli


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
    upstream_lock: dict[str, Any],
    assets_dir: Path,
    *,
    expected_name: str,
    managed_name: str,
    output_name: str,
) -> tuple[Path, str, dict[str, Any]]:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", managed_name).strip("-")
    unpacked = RELEASE_DIR / f".unpacked-{safe_name}"
    # Must be freshly created, never a pre-existing directory: see
    # create_fresh_private_dir's docstring for why ensure_private_dir()'s
    # accept-existing behavior was unsafe here (independent review round 2,
    # 2026-08-18, P1).
    create_fresh_private_dir(unpacked)
    package_dir, extracted_relative_paths, content_digests, dir_modes = safe_extract_main_asset(
        original_asset, unpacked
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
    shutil.rmtree(unpacked)
    return patched, sha256_bytes(patched_raw), manifest


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


def validate_generated_lock(raw: bytes, generated: dict[str, Any]) -> dict[str, Any]:
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
            if version != VERSION or not isinstance(resolved, str):
                raise PrimeInstallError(f"generated managed asset identity drift: {name}")
            actual_asset = resolved_local_asset(resolved)
            expected_asset = resolve_ssd(local_assets[name])
            if actual_asset != expected_asset:
                raise PrimeInstallError(f"generated managed asset path drift: {name}")
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
) -> None:
    environment = managed_npm_environment(
        node_path, cache, install_home, install_tmp
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
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrimeInstallError("npm execution failed") from exc
    if result.returncode != 0:
        tail = " ".join(result.stdout.splitlines()[-5:])[:1_000]
        raise PrimeInstallError(f"npm failed with exit {result.returncode}: {tail}")


def tree_digest(root: Path) -> tuple[str, int]:
    resolved_root = verify_private_ssd_dir(root)
    root_info = root.lstat()
    digest = hashlib.sha256()
    digest.update(b"R\0" + str(stat.S_IMODE(root_info.st_mode)).encode() + b"\n")
    count = 1
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
            payload = b"L\0" + relative.encode() + b"\0" + target_text.encode()
        elif stat.S_ISREG(info.st_mode):
            payload = (
                b"F\0"
                + relative.encode()
                + b"\0"
                + str(stat.S_IMODE(info.st_mode)).encode()
                + b"\0"
                + sha256_file(path).encode()
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
    return digest.hexdigest(), count


def write_pending_install(receipt: dict[str, Any]) -> dict[str, Any]:
    """Cross the durable-tree barrier, then and only then publish the journal."""
    validate_pristine_managed_home(STATE_DIR, "Prime Agent pending state")
    validate_pristine_managed_home(PROBE_HOME, "Prime Agent pending probe home")
    if read_private_ssd_file(STATE_DIR / "agent/settings.json") != expected_prime_settings():
        raise PrimeInstallError("Prime Agent pending telemetry settings drifted")
    if read_private_ssd_file(PROBE_HOME / "agent/settings.json") != expected_prime_settings():
        raise PrimeInstallError("Prime Agent pending probe settings drifted")
    validate_empty_private_dir(
        managed_session_dir(), "Prime Agent pending session directory"
    )
    release_identity = tree_digest(RELEASE_DIR)
    for root in (RELEASE_DIR, STATE_DIR, PROBE_HOME, managed_session_dir()):
        sync_private_tree(root)
    if tree_digest(RELEASE_DIR) != release_identity:
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
    public_commands = "|".join(
        (
            "doctor",
            "list",
            "rename",
            "schedule",
            "send",
            "session",
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
        f'exec /usr/bin/python3 -B {shlex.quote(os.fspath(launch_guard))} "$@"',
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def managed_launch_guard_script(node: Path, cli: Path) -> bytes:
    lock = lifecycle_lock_path()
    ssd_root = SSD_ROOT
    source = f'''#!/usr/bin/python3
from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys

LOCK = {os.fspath(lock)!r}
SSD_ROOT = {os.fspath(ssd_root)!r}
NODE = {os.fspath(node)!r}
CLI = {os.fspath(cli)!r}
RESOURCE_GUARD_ENV = "ORCA_PRIME_AGENT_RESOURCE_GUARD"
RESOURCE_GUARDS = ("--no-extensions", "--no-skills", "--no-prompt-templates")
# Single source of truth shared with managed_entrypoint_script()'s generated
# shell resolve_managed_command() -- both derive from install_prime_agent.py's
# module-level LEADING_COMMAND_OPTIONS constant (see its docstring there) so
# a future leading option is recognized by both generated parsers together.
LEADING_COMMAND_OPTIONS = {LEADING_COMMAND_OPTIONS!r}
RUNTIME_PUBLIC_COMMANDS = frozenset(("agents", "attach", "model"))
# Historically mirrored the shell entrypoint's own "public_commands" set
# exactly; as of round 8, 2026-08-18 it deliberately no longer does. The two
# lists now answer two DIFFERENT questions and have intentionally diverged:
#   * managed_entrypoint_script()'s public_commands decides session_start --
#     whether the $PWD/.prime/agent/settings.json gate and
#     ORCA_PRIME_AGENT_RESOURCE_GUARD export apply at all. "config" and
#     "package" were removed from that list in round 8 (P1: upstream's own
#     handlers for both can still touch the launch project -- see
#     managed_entrypoint_script()'s public_commands comment for the full
#     finding) and now get full session-start protection.
#   * RUNTIME_NO_GUARD_COMMANDS here decides a narrower, DIFFERENT question:
#     given that ORCA_PRIME_AGENT_RESOURCE_GUARD=1 WAS exported (a genuine
#     session start), should RESOURCE_GUARDS actually be spliced into this
#     command's own argv? "config", "package", and "help" stay in this set
#     even though two of them left public_commands, because inserting
#     RESOURCE_GUARDS at the position this script's fallback branches use
#     for them has never been verified against upstream's real argv
#     acceptance for these three specific commands, and getting that
#     placement wrong is exactly the round-3/4 argv-corruption bug class
#     below. The settings-file gate (now applying to all three by default)
#     is the actual fix for the round-8 finding; leaving these three out of
#     guard-flag insertion is an intentional, separate, defense-in-depth
#     choice, not an oversight -- round 8 confirmed this set already
#     contained all three (so guarded_arguments() cannot corrupt their argv
#     even now that config/package can reach it with RESOURCE_GUARD_ENV=1
#     set) and left it unchanged.
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
        "help",
        "list",
        "package",
        "rename",
        "schedule",
        "send",
        "session",
        "shutdown",
        "status",
        "stop",
    )
)


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


def guarded_arguments(arguments: list[str]) -> list[str]:
    if os.environ.pop(RESOURCE_GUARD_ENV, None) != "1":
        return arguments
    resolved_command = resolve_upstream_public_command(arguments)
    prefix, remaining = split_leading_options(arguments)
    if resolved_command is not None and resolved_command in RUNTIME_NO_GUARD_COMMANDS:
        # A non-session public command never receives resource-guard flags,
        # regardless of how it was invoked -- matches the entrypoint's own
        # session_start=0 treatment for these exactly (see
        # RUNTIME_NO_GUARD_COMMANDS above). resolved_command, not
        # remaining[0], gates this: only a form upstream itself would
        # actually resolve to this command may skip guards.
        return [*prefix, *remaining]
    if resolved_command is not None and resolved_command in RUNTIME_PUBLIC_COMMANDS:
        try:
            separator = remaining.index("--")
        except ValueError:
            separator = len(remaining)
        return [
            *prefix,
            *remaining[:separator],
            *RESOURCE_GUARDS,
            *remaining[separator:],
        ]
    return [*prefix, *RESOURCE_GUARDS, *remaining]


def main() -> int:
    descriptor = -1
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
        completed = subprocess.run(
            [NODE, CLI, *guarded_arguments(sys.argv[1:])], check=False
        )
        return completed.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        return fail(f"managed launch guard failed: {{type(exc).__name__}}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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


def open_verified_ancestor_chain(link: Path, *, create_missing: bool) -> int:
    """Walk from USER_HOME to link's parent entirely via dir_fd-chained,
    openat()-style lookups (see open_verified_directory_component()) and
    return an open, verified descriptor for the immediate parent. Used by
    both ensure_local_link_parent() (create_missing=True, for link
    creation) and verify_link_parent_descriptor() (create_missing=False,
    for read-only verification), so creation and verification are bound to
    the identical ancestor-resolution algorithm rather than verification
    trusting a plain lexical path that could resolve through a since
    -interposed symlink ancestor.
    """
    require_link_dir_fd_support()
    lexical_home = USER_HOME.absolute()
    try:
        relative = link.parent.absolute().relative_to(lexical_home)
    except ValueError as exc:
        raise PrimeInstallError(f"link path is outside the user home: {link}") from exc
    try:
        # The user home itself is pre-existing authority, opened by path
        # exactly once as the root of the chain -- every component beneath
        # it is then resolved only relative to an already-verified
        # descriptor, never by path string again.
        current_descriptor = os.open(lexical_home, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise PrimeInstallError(f"cannot open user home: {lexical_home}") from exc
    display_path = lexical_home
    try:
        for part in relative.parts:
            display_path = display_path / part
            next_descriptor = open_verified_directory_component(
                current_descriptor, part, display_path, create_missing=create_missing
            )
            os.close(current_descriptor)
            current_descriptor = next_descriptor
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
    assets_dir = ensure_private_dir(RELEASE_DIR / "assets")
    cache = ensure_private_dir(TOOL_ROOT / "npm-cache")
    install_home = ensure_private_dir(TOOL_ROOT / "install-home")
    install_tmp = ensure_private_dir(TOOL_ROOT / "install-tmp")
    ensure_private_dir(managed_session_dir())
    safe_download(LOCK_URL, RELEASE_DIR / "upstream-package-lock.json", LOCK_SHA256)
    safe_download(LICENSE_URL, RELEASE_DIR / "LICENSE", LICENSE_SHA256)
    upstream_lock = strict_json((RELEASE_DIR / "upstream-package-lock.json").read_bytes())
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
    node, npm_cli = extract_node_toolchain(assets_dir / NODE_ASSET, RELEASE_DIR / "toolchain")
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
    patched_assets: dict[str, str] = {}
    patched_manifests: dict[str, dict[str, Any]] = {}
    workspace_order = (
        "@earendil-works/pi-ai",
        "@earendil-works/pi-tui",
        "@earendil-works/pi-agent-core",
    )
    for managed_name in workspace_order:
        declaration = WORKSPACE_PACKAGES[managed_name]
        patched, digest, manifest = make_patched_asset(
            assets_dir / str(declaration["official_asset"]),
            upstream_lock,
            assets_dir,
            expected_name=str(declaration["source_name"]),
            managed_name=managed_name,
            output_name=str(declaration["patched_asset"]),
        )
        patched_assets[patched.name] = digest
        patched_manifests[managed_name] = manifest
    patched_asset, patched_sha, patched_manifest = make_patched_asset(
        assets_dir / f"prime-agent-{VERSION}.tgz",
        upstream_lock,
        assets_dir,
        expected_name="prime-agent",
        managed_name="prime-agent",
        output_name=MAIN_PATCHED_ASSET,
    )
    patched_assets[patched_asset.name] = patched_sha
    patched_manifests["prime-agent"] = patched_manifest
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
    run_npm(
        os.fspath(npm_cli),
        os.fspath(node),
        ["install", "--package-lock-only", "--omit=dev", "--ignore-scripts", "--install-links"],
        RELEASE_DIR,
        cache,
        install_home,
        install_tmp,
    )
    lock_path = RELEASE_DIR / "package-lock.json"
    generated_lock_raw = lock_path.read_bytes()
    lock_stat = lock_path.lstat()
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
    package_lock_identity = (lock_stat.st_dev, lock_stat.st_ino)
    generated_lock = strict_json(generated_lock_raw)
    if not isinstance(generated_lock, dict):
        raise PrimeInstallError("generated lock is invalid")
    closure = validate_generated_lock(generated_lock_raw, generated_lock)
    # Re-verify both package.json and the just-validated package-lock.json
    # are still exactly what was read/validated above, immediately before
    # `npm ci` independently re-reads both from RELEASE_DIR on its own
    # (same rationale as the "install" re-check above).
    verify_unchanged_private_ssd_file(manifest_path, manifest_raw, manifest_identity)
    verify_unchanged_private_ssd_file(lock_path, generated_lock_raw, package_lock_identity)
    run_npm(
        os.fspath(npm_cli),
        os.fspath(node),
        ["ci", "--omit=dev", "--ignore-scripts", "--install-links"],
        RELEASE_DIR,
        cache,
        install_home,
        install_tmp,
    )
    installed_package = RELEASE_DIR / "node_modules/prime-agent"
    if installed_package.is_symlink() or not installed_package.is_dir():
        raise PrimeInstallError("npm did not materialize a private Prime Agent package")
    global_root = RELEASE_DIR / "lib/node_modules"
    global_root.parent.mkdir(mode=0o700)
    os.replace(RELEASE_DIR / "node_modules", global_root)
    bin_dir = RELEASE_DIR / "bin"
    bin_dir.mkdir(mode=0o700)
    entrypoint = global_root / "prime-agent/dist/bundle/cli.js"
    if not entrypoint.is_file():
        raise PrimeInstallError("Prime Agent entrypoint missing")
    os.chmod(entrypoint, 0o700)
    launch_guard = bin_dir / "prime-agent-launch-guard.py"
    atomic_create_private_file(
        launch_guard, managed_launch_guard_script(node, entrypoint), 0o700
    )
    atomic_create_private_file(
        bin_dir / "prime-agent",
        managed_entrypoint_script(node, entrypoint, launch_guard),
        0o700,
    )
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
        "bin_target": os.fspath(bin_dir / "prime-agent"),
        "launch_guard": os.fspath(launch_guard),
        "lifecycle_lock": os.fspath(lifecycle_lock_path()),
        "node_target": os.fspath(node),
        "npm_target": os.fspath(npm_cli),
        "probe_home": os.fspath(PROBE_HOME),
        "session_dir": os.fspath(managed_session_dir()),
        "asset_sha256": ASSETS,
        "node_asset_sha256": NODE_ASSET_SHA256,
        "upstream_lock_sha256": LOCK_SHA256,
        "license_sha256": LICENSE_SHA256,
        "production_lock_sha256": closure["lock_sha256"],
        "patched_asset_sha256": patched_assets,
        "patched_manifest_names": sorted(patched_manifests),
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
    write_pending_install(receipt)
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
        "action", choices=("plan", "install", "verify", "uninstall", "enable", "recover")
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
        else:
            result = recover()
    except PrimeInstallError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
