#!/usr/bin/env python3
"""Capability/knowledge promotion pipeline (M8-3, Gate B of the cross-project
catalog plan).

THIS IS THE ONE PLACE IN M8 WHERE A PROCESS WRITES INTO ANOTHER PROJECT'S OWN
TRACKED FILES.  Every other M8 tool (M4's aggregator, M8-4 Tier-1) is
strictly read-only.  `approve` is the single write path, and it writes only
into the target project's own `wiki/reusable-capabilities.json` or
`wiki/orca-context-wiki.json` (+ `wiki/knowledge/<id>.md`) -- never anywhere
else, never any other project's files, never this tool's own repo.

Lifecycle (two independent state machines, see design doc §3.0.2):
    draft candidate:   pending_approval -> approved | rejected | withdrawn
An `amend` request is drafted exactly like a normal candidate (a special
`operation: "amend_depends_on"` shape) and must go through the same
draft -> approve path; nothing here ever patches a published file directly.

Subcommands
-----------
    draft    --from-json PATH --catalog PATH [--json]
        Hand-authored JSON in, one pending_approval candidate out. Read-only
        with respect to every project's own files; writes only inside this
        tool's own staging root (see PROMOTION_ROOT below).
    amend    --target-project ID --target "<kind>:<name>" --add-depends-on REF
             --source STR --catalog PATH [--json]
        Sugar for `draft` that produces an `operation: "amend_depends_on"`
        candidate.  `--source` is free text (deliberately NOT coupled to
        M8-1 -- see design §3.4.5): "human", or any other identifier a
        future signal wants to use once it has its own authorization.
    approve  --candidate-id ID --catalog PATH --approved-by STR --rationale STR
             [--confirm-non-human-source]
             [--no-commit --i-understand-this-leaves-an-uncommitted-tracked-path]
             [--json]
        THE write path. See "THE SINGLE WRITE EXCEPTION" below.
    reject   --candidate-id ID --decided-by STR --rationale STR [--json]
    withdraw --candidate-id ID --decided-by STR --rationale STR [--json]
        Terminal state transitions that touch only this tool's own staging
        record for that one candidate -- never any project's wiki/ files.

PRODUCT ROOT -- FIXED ABSOLUTE PATH, NEVER Path(__file__)-DERIVED
------------------------------------------------------------------
PROMOTION_ROOT below is a literal absolute path, not derived from this
script's own location. This is deliberate and has already bitten this same
plan twice for real (M1, M7) and was very nearly a third time on this same
milestone's own Gate A: a relative-from-`__file__` root is harmless while
this script sits in a scratch staging directory, but the moment a reviewed
candidate is promoted into its permanent home under a project's own tracked
`orca-context-bridge/scripts/`, that SAME relative derivation silently starts
creating a new, untracked, ever-growing directory inside an
AUTHORITY_TRACKED_PATHS root -- which is exactly the shape of change that
makes the next SessionStart NACK. Anchoring on a literal absolute path makes
this script's output location identical no matter where the script itself
is copied to.

`capability-promotion-pending-authorization` (not the design doc's eventual
production name `capability-promotion/`) for the same reason
`detect_capability_changes.py`'s own `compat-pending-authorization` isn't
named `compat/`: this delivery produces real output on disk, but wiring that
output into anything that treats it as authoritative (a discovery pipeline,
an automatic re-publish, anything downstream trusting it unattended) is a
separate, not-yet-granted authorization. Matching that established
precedent: do not rename this directory to the production name, and do not
add a flag that could point it somewhere else -- tests that need an
isolated root monkeypatch the PROMOTION_ROOT module constant directly (see
test_detect_capability_changes.py's own convention), never a CLI override.

This directory is a TOP-LEVEL sibling of `manifests/cross-project-catalog/`,
not nested inside it -- unlike `compat-pending-authorization/` above, which
*is* correctly nested there because the design doc's own directory tree
(M8-DESIGN-FINAL-2026-08-23.md 3.0.1) places `compat/` as a child of
`cross-project-catalog/` (M4's aggregated/authenticated-derived-data tree)
while it places `capability-promotion/` as an independent peer, deliberately
NOT nested under `cross-project-catalog/`, specifically so this staging
tree's name/location never implies it is downstream of or subordinate to
M4's read-only aggregation.

THE SINGLE WRITE EXCEPTION -- FULLY DOCUMENTED, DEFENDED IN DEPTH
------------------------------------------------------------------
`approve` writes into "the target project's own wiki/" and nowhere else,
via `build_project_root_index()`, which resolves project_id -> real_path
using ONLY catalog.json's own already-built `projects[]` array (never a
fresh `orca ... list` call, and never a candidate's own self-reported path
string).

`cmd_approve` holds the SAME global PROMOTION_ROOT lock draft/amend already
used across the entire find-candidate -> write -> record-transition
sequence below (round-N fix: an earlier revision relied on the fstat-based
identity recheck alone, with no lock at all, which a dual review
demonstrated is a real, reproducible lost-update race -- two concurrent
`approve` calls against the same target file can each observe "identity
unchanged" before either has written, and whichever call's os.replace()
lands second silently discards the first call's already-"approved" write).
The identity recheck described below is a defense against a DIFFERENT
actor -- something other than this tool editing the target file mid-flight
-- not a substitute for holding the lock against concurrent invocations of
this tool itself. `reject`/`withdraw` (`_terminal_transition()`) hold this
SAME lock too as of the round-2 fix below: an earlier revision let them run
their read-check-write sequence completely unlocked, so a reject/withdraw
could race a concurrent approve on the identical candidate and leave
promotion-ledger.jsonl with both an "approve" AND a "reject"/"withdraw"
entry for the same candidate_id -- a self-contradictory audit trail,
independently reproduced 5/5 runs before this fix. Two target shapes:

Round-2 fix, also documented here since it changes what this section's own
guarantees mean: `cmd_approve` now re-validates every field of the
candidate record it is about to act on (`_revalidate_record_for_approve()`)
using draft's own validators, before trusting any of it to build a
filesystem path. A candidate record is JSON this tool itself wrote under
PROMOTION_ROOT and later reads back; nothing previously re-checked that a
record still looked like something draft would have produced by the time
approve got to it. `resolve_knowledge_md_path()`'s own containment check
was also widened to cover the FINAL page_id-derived path, not just the
knowledge/ directory it is built from -- two independent layers over the
same field, deliberately, not one covering for the other's absence. See
that function's own docstring for the exact exploit this closes (a
tampered record's "id" containing "/../" segments escaping the target
project root entirely). `git_commit_paths()`'s commit call was also scoped
to the specific paths this write touched (`git commit -- <paths>`, not a
bare `git commit`) so it can no longer sweep up whatever else a caller had
already staged in the target repo for something unrelated.

  "reusable-capabilities.json": read existing file -> capture an fstat-based
  identity for the read (dev/ino/mtime_ns/size) -> build the new document ->
  reconfirm the identity is unchanged (refuse on a concurrent edit) -> atomic
  write (temp file + os.replace on the same filesystem) -> read the just-
  written bytes back and re-run this file's own copy of
  validate_reusable_capabilities.py's validate_document() as a self-check ->
  on failure, atomically restore the original bytes (rollback) and fail
  closed. No git commit here: reusable-capabilities.json is confirmed (see
  that validator's own module docstring) NOT one of
  reviewed-startup-pack-manifest.json's pinned shared_source_sha256s keys,
  so an uncommitted change to it does not by itself invalidate that pin --
  see KNOWN LIMITATIONS below for the residual risk this narrower guarantee
  still leaves.

  "orca-context-wiki.json": if the candidate has a staged `content.md`,
  confirm it still exists and read it into memory, and resolve+containment-
  check its final `wiki/knowledge/<id>.md` destination -- all BEFORE
  anything below touches the target wiki file, closing a round-fix P0 where
  a missing staged `content.md` used to be discovered only after the wiki
  file below had already been rewritten, leaving it modified-and-
  uncommitted with no matching "approved" record. That same containment
  step also now opens a directory file descriptor for `wiki/knowledge/` at
  the exact instant containment is confirmed (round-4-fix; see
  `_resolve_knowledge_md_path_and_dir_fd()`) -> build the candidate new
  payload (content_version incremented by exactly 1, since every write here
  adds a page) -> pin this script's own copy of wiki_edit_guard.py's
  SHA-256 the first time this run needs it -> re-verify the pinned hash is
  unchanged immediately before invoking it, refusing fail-closed on any
  mismatch -> write the already-read `content.md` bytes (if the candidate
  has one) THROUGH that same directory file descriptor to the
  already-resolved `wiki/knowledge/<id>.md` filename (round-4-fix: this now
  happens BEFORE the wiki write below, not after -- see the round-4-fix
  note further down and `_approve_orca_context_wiki()`'s own comments) ->
  subprocess-invoke wiki_edit_guard.py's own `--apply --json` (its
  semantic-diff / content_version bookkeeping logic is NOT copied here --
  see "WHY wiki_edit_guard.py IS CALLED, NOT COPIED" below) -- this is now
  the LAST mutating step in the sequence before the commit -> by default,
  `git add` + `git commit` in the TARGET project's own repo in the SAME
  call, eliminating the "written but not committed" NACK window that hit
  this same plan for real twice (M1, M7) -- `--no-commit` is an explicit,
  doubly-named escape hatch.
  -> detect whether the target project's own
  reviewed-startup-pack-manifest.json pins this exact path, and if so, print
  an UNSUPPRESSABLE notice that its next SessionStart will fail closed until
  a human re-signs it. `approve` never re-signs that manifest itself.

  round-4-fix (this bug class's SECOND fix -- the round-fix P0 above only
  closed the "missing content.md" trigger, not the general class): the
  knowledge-body write used to be a SEPARATE, LATER step that ran AFTER the
  wiki write above, so it could still fail for reasons other than "the
  staged file doesn't exist" -- e.g. a pre-existing-but-unwritable
  wiki/knowledge/ directory (mkdir's own exist_ok=True is a silent no-op on
  an already-existing directory, so it never catches this), or any other
  OSError on that write -- leaving the wiki file mutated-and-uncommitted
  with the candidate stuck at pending_approval forever and a retry blocked
  by duplicate_page_id. Independently reproduced end-to-end. A dedicated
  Grok final gate separately found a TOCTOU in the same area: the
  containment check and the eventual by-name write were separated by a
  real subprocess call (invoke_wiki_edit_guard()), a window in which
  wiki/knowledge could be swapped for a symlink to outside the project.
  Both are fixed together: the untracked knowledge body is now written
  FIRST (through a directory file descriptor captured at containment-check
  time, never by re-walking the path by name later), and the tracked wiki
  write is the LAST mutating step this function performs before the git
  commit -- see `_approve_orca_context_wiki()`'s own comments for why
  nothing after that last step can itself fail. This makes "wiki mutated +
  reported failure", "wiki mutated + candidate stuck pending forever", and
  "knowledge write escapes the project root" all provably impossible, not
  just less likely.

WHY wiki_edit_guard.py IS CALLED, NOT COPIED
------------------------------------------------------------------
Every other piece of M0-M7 logic this tool leans on (KIND_VALUES, the id/
name/path/depends_on grammar, path containment, atomic-write-with-lock) is
duplicated locally, per this project's "copy, don't import" convention
(kept independent trust surfaces per tool). wiki_edit_guard.py's semantic
deny-list projection is the one deliberate, documented exception (see
M8-DESIGN-FINAL-2026-08-23.md §3.0.2): it is 650+ lines that have already
had one real, fixed security bug in it, and copying it would recreate the
entire review burden "copy, don't import" exists to avoid. Calling it as a
subprocess keeps the trust boundary a process boundary (this script never
parses or reimplements its deny-list), at the cost of a real runtime
dependency this docstring states outright rather than hiding behind "copy a
small bit" language. The mitigation is the SHA-256 pin above: if the file
this script is about to invoke does not hash to what was pinned earlier in
this same run, approve refuses rather than trusting a binary it cannot
account for.

KNOWN LIMITATIONS (stated here, not hidden)
------------------------------------------------------------------
  * The "reusable-capabilities.json" path does not auto-commit. If the
    target project separately enforces "no uncommitted file under wiki/"
    (this repo's own AUTHORITY_TRACKED_PATHS convention does, but that is
    this repo's own SessionStart hook policy, not a universal property of
    every project in the fleet), a promoted capability entry can still leave
    an uncommitted tracked path there. This mirrors the design doc's own
    scoping (§3.4.4): the auto-commit guarantee is stated only for the
    orca-context-wiki.json path, whose write is the one provably pinned by
    reviewed-startup-pack-manifest.json's shared_source_sha256s everywhere
    that manifest exists. A future round that wants the same guarantee for
    reusable-capabilities.json needs its own review, not a silent copy-paste
    of this file's wiki-branch commit call.
  * If the wiki write succeeds but the subsequent git commit fails (dirty
    tree, no git identity configured, target is not actually a git repo --
    the last of which is checked BEFORE the write and refused up front, but
    the other two cannot be), the write is NOT rolled back: the wiki
    payload's own content_version bookkeeping only supports forward
    (+1) moves, so an automatic "undo" would itself be an unsupported
    backward move against the very guard meant to prevent silent drift.
    `approve` reports this candidly (git_committed: false + the same
    unsuppressable-notice treatment as the manifest-resign case) rather than
    pretending the promotion did not happen or attempting an unsafe revert.
  * `capability_ref_index` consulted for depends_on resolution/amend-target
    lookup comes from whatever catalog.json the caller points `--catalog`
    at -- it can be stale relative to the live file `approve` is about to
    write (see design doc §6 risk 5). The post-write self-check
    (validate_document on the just-written file) is the actual authority
    for whether the new document is internally consistent; the catalog-based
    checks earlier are advisory.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

# ---------------------------------------------------------------------------
# Fixed staging root -- see module docstring. Absolute, never __file__-derived.
# ---------------------------------------------------------------------------

PROMOTION_ROOT = Path(
    "/Volumes/Extreme SSD/Orca/manifests/"
    "capability-promotion-pending-authorization"
)
LEDGER_NAME = "promotion-ledger.jsonl"
LOCK_NAME = ".promotion.lock"
LOCK_STALE_SECONDS = 300

MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_WIKI_BYTES = 16 * 1024 * 1024
MAX_CONTENT_MD_BYTES = 2 * 1024 * 1024
MAX_CANDIDATE_BYTES = 2 * 1024 * 1024

PROPOSED_TARGETS = ("reusable-capabilities.json", "orca-context-wiki.json")
STATUS_VALUES = ("pending_approval", "approved", "rejected", "withdrawn")
OPERATION_VALUES = ("create", "amend_depends_on")

_CONTROL_CHARS = ("\n", "\r", "\t", chr(0x2028), chr(0x2029), "\x00")

CANDIDATE_SCHEMA_VERSION = 1


class PromoteFatal(Exception):
    """Environment broken -- maps to exit 4. No write happened, or (the one
    documented exception) a wiki write happened but the follow-up commit
    could not."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


class PromoteUsageError(Exception):
    """CLI-level usage problem -- maps to exit 2."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


class PromoteValidationError(Exception):
    """The requested state transition did not validate -- maps to exit 1.
    No write happened (or, for approve, was rolled back before this was
    raised)."""

    def __init__(self, reason: str, details: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


# ---------------------------------------------------------------------------
# Small shared helpers (deliberately duplicated across this project's tools)
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_line_separators(text: str) -> str:
    return text.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON object")
        seen[key] = value
    return seen


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _normalize(text: str) -> str:
    """NFC + casefold -- identical to query_catalog.py's own _normalize(),
    copied (not imported) per this project's convention."""
    return unicodedata.normalize("NFC", text).casefold()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _has_control_chars(value: str) -> bool:
    return any(ch in value for ch in _CONTROL_CHARS)


# ---------------------------------------------------------------------------
# Bounded, descriptor-level file reading (same guards as this plan's other
# tools: O_NONBLOCK against a planted FIFO, S_ISREG rejects anything else)
# ---------------------------------------------------------------------------


def _read_bounded(path: Path, max_bytes: int, *, follow_symlinks: bool) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if not follow_symlinks and hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a regular file")
        if st.st_size > max_bytes:
            raise OSError(f"{path} size {st.st_size} exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > max_bytes:
        raise OSError(f"{path} exceeds {max_bytes} bytes")
    return raw


def _load_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromoteUsageError(f"{label}_not_utf8", str(exc)) from exc
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PromoteUsageError(f"{label}_not_valid_json", str(exc)) from exc
    if not isinstance(doc, dict):
        raise PromoteUsageError(f"{label}_not_an_object", "top level must be a JSON object")
    return doc


def _stat_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        st = os.stat(str(path), follow_symlinks=False)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def read_with_identity(path: Path, max_bytes: int) -> tuple[bytes, tuple[int, int, int, int]]:
    """fstat-based identity capture: the identity is taken from the SAME fd
    the bytes were read from, so a concurrent swap between "stat the path"
    and "read the path" cannot hand back mismatched (identity, content)."""
    fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PromoteFatal("target_not_a_regular_file", str(path))
        if st.st_size > max_bytes:
            raise PromoteFatal("target_too_large", f"{st.st_size} bytes > {max_bytes}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    identity = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
    return raw, identity


def identity_unchanged(path: Path, identity: tuple[int, int, int, int]) -> bool:
    current = _stat_identity(path)
    return current == identity


def _read_bytes_via_dir_fd(dir_fd: int, filename: str, max_bytes: int) -> bytes:
    """Read-side counterpart of read_with_identity(), anchored to an
    already-open directory fd instead of a plain path (O_NOFOLLOW on the
    filename too). Resolves inside the EXACT directory inode dir_fd was
    opened against, immune to anything that has since happened to that
    directory's own name (or any ancestor of it) -- the same guarantee
    atomic_write_in_dir()'s dir_fd path already gives the write side.
    Used by _approve_reusable_capabilities()'s post-write self-check so it
    validates the SAME bytes the write actually landed: a plain
    wiki_path.read_bytes() there could otherwise be fooled by a directory
    swap performed between the write completing and that read (2026-08-26
    3-model cross-audit finding -- see
    test_post_write_self_check_reads_via_dir_fd_not_plain_path)."""
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PromoteFatal("target_not_a_regular_file", filename)
        if st.st_size > max_bytes:
            raise PromoteFatal("target_too_large", f"{st.st_size} bytes > {max_bytes}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# write_only_within -- confines every write THIS tool does to its own
# staging root (PROMOTION_ROOT). The one write that legitimately leaves that
# root (a target project's own wiki/) is handled by a SEPARATE, narrower
# helper below (resolve_wiki_target_path), never by this one.
# ---------------------------------------------------------------------------


def _has_symlink_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return True
    return False


def write_only_within(base_dir: Path, path_value: object) -> tuple[Path | None, str | None]:
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        return None, "invalid_path"
    path = Path(path_value)
    if not path.is_absolute():
        return None, "non_absolute_path"
    lexical_root = base_dir.absolute()
    lexical_path = path.absolute()
    try:
        lexical_path.relative_to(lexical_root)
    except ValueError:
        return None, "outside_base_dir"
    if _has_symlink_component(lexical_path, lexical_root):
        return None, "symlink_path"
    resolved_root = base_dir.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None, "outside_base_dir"
    if resolved_path == resolved_root:
        return None, "is_base_root"
    return resolved_path, None


def _open_atomic_write_dir_fd(write_dir: Path) -> int:
    """Seam for tests: open write_dir as a dir fd, O_NOFOLLOW-refusing a
    symlinked name. Split out from atomic_write_within() so a test can
    monkeypatch this exact call site to swap the directory's name for a
    symlink right before it opens -- same technique as this file's own
    _open_wiki_dir_fd_for_guard()."""
    return os.open(str(write_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def atomic_write_within(base_dir: Path, final_path: Path, payload: bytes) -> None:
    tmp_path = final_path.parent / f".{final_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    # Opened on final_path.parent, not the base_dir parameter: for every
    # call site in this codebase today the two name the same directory,
    # EXCEPT this file's own candidate/content-md writers (save_candidate/
    # save_content_md), where base_dir is an ancestor 1-2 levels above
    # final_path.parent -- opening base_dir there would create/replace the
    # tmp file in the wrong directory. Opened FIRST, before any
    # create/replace, so a symlink swapped in for this directory's own
    # name in the gap between the caller's earlier containment check and
    # these calls cannot redirect the write: dir_fd pins the real
    # directory's inode, and every relative (dir_fd=) call below still
    # resolves inside it even if the name is swapped out from under it
    # afterward.
    dir_fd = _open_atomic_write_dir_fd(final_path.parent)
    try:
        fd = os.open(tmp_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        try:
            try:
                _write_all_bytes(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path.name, final_path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_path.name, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        os.close(dir_fd)


def acquire_lock(base_dir: Path) -> Path:
    lock_path = base_dir / LOCK_NAME
    for attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                _write_all_bytes(fd, json.dumps({"pid": os.getpid(), "started_at": now_iso()}).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                age = time.time() - os.stat(str(lock_path)).st_mtime
            except OSError:
                age = 0.0
            if attempt == 0 and age > LOCK_STALE_SECONDS:
                try:
                    os.unlink(str(lock_path))
                except OSError:
                    pass
                continue
            raise PromoteFatal("lock_held")
        except OSError as exc:
            # Anything other than "already exists" -- most commonly
            # PermissionError on a read-only base_dir -- is a genuine
            # failure to create the lock, not a lock-held race. Name it
            # explicitly (exit 4, "lock_uncreatable") instead of letting it
            # propagate as a generic unexpected_error.
            raise PromoteFatal("lock_uncreatable", str(exc))
    raise PromoteFatal("lock_held")


def release_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        os.unlink(str(lock_path))
    except OSError:
        pass


def ensure_promotion_root() -> Path:
    root = PROMOTION_ROOT
    try:
        os.makedirs(str(root), mode=0o700, exist_ok=True)
    except OSError as exc:
        raise PromoteFatal("promotion_root_uncreatable", str(exc)) from exc
    if os.path.islink(str(root)):
        raise PromoteFatal("promotion_root_is_symlink")
    return root


def append_ledger(root: Path, entry: dict[str, Any]) -> None:
    """Append-only. Never rewrites, never truncates; a partial line from a
    prior crash (process killed mid-write, before this function's own
    os.fsync()/os.close() ever run) is a pre-existing on-disk fact this
    function does not try to repair -- only future append targets matter
    for a ledger.

    Uses _write_all_bytes() (round-6-fix P1 sibling, 2026-08-25 -- see that
    helper's own docstring) rather than a bare os.write(): unlike the
    tmp-file+rename writers elsewhere in this module, there is no tmp file
    to roll back here (this is a direct O_APPEND write to the live ledger),
    so this fix does not add a rollback that didn't exist before. What it
    does add: (a) a short-but-recoverable write (the OS accepted fewer
    bytes than requested even though more capacity exists, e.g. an
    interrupted syscall) now correctly continues and completes the full
    line instead of leaving it silently truncated, and (b) a write that
    genuinely cannot complete (e.g. disk truly full) now raises to the
    caller instead of silently reporting success with a truncated JSON
    line in the ledger -- the caller can then react (this file's own
    cmd_draft/cmd_approve/etc. already treat an append_ledger() failure as
    fatal), rather than the corruption going unnoticed until the ledger is
    later read back."""
    ledger_final, reason = write_only_within(root, str(root / LEDGER_NAME))
    if reason or ledger_final is None:
        raise PromoteFatal("ledger_path_invalid", reason or "invalid_path")
    line = _sanitize_line_separators(json.dumps(entry, ensure_ascii=False)) + "\n"
    fd = os.open(str(ledger_final), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        _write_all_bytes(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# catalog.json consumption -- READ ONLY. project_id -> real_path resolution
# uses ONLY catalog.json's own projects[] array (this round's explicit
# instruction), never a fresh `orca repo list`/`worktree list` call, and
# never a candidate's own self-reported path string.
# ---------------------------------------------------------------------------


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        raw = _read_bounded(path, MAX_CATALOG_BYTES, follow_symlinks=True)
    except FileNotFoundError as exc:
        raise PromoteFatal("catalog_missing", str(path)) from exc
    except OSError as exc:
        raise PromoteFatal("catalog_unreadable", str(exc)) from exc
    try:
        doc = _load_json_object(raw, label="catalog")
    except PromoteUsageError as exc:
        raise PromoteFatal("catalog_unparseable", exc.message) from exc
    return doc


def build_project_root_index(catalog: dict[str, Any]) -> dict[str, Path]:
    """project_id -> resolved real_path Path. When >1 row shares a
    project_id, prefer the single "ok"-status row if there is exactly one;
    otherwise the lexicographically first real_path -- same tie-break as
    detect_capability_changes.py's own build_project_root_index()."""
    rows = catalog.get("projects")
    by_id: dict[str, list[dict[str, Any]]] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = row.get("project_id")
            real_path = row.get("real_path")
            if not isinstance(pid, str) or not isinstance(real_path, str) or not real_path:
                continue
            by_id.setdefault(pid, []).append(row)

    roots: dict[str, Path] = {}
    for pid, members in by_id.items():
        if len(members) == 1:
            roots[pid] = Path(members[0]["real_path"])
            continue
        ok_rows = [m for m in members if m.get("status") == "ok"]
        chosen = ok_rows[0] if len(ok_rows) == 1 else sorted(members, key=lambda m: m["real_path"])[0]
        roots[pid] = Path(chosen["real_path"])
    return roots


def resolve_depends_on_against_catalog(
    catalog: dict[str, Any], target_project: str, depends_on: list[str]
) -> list[dict[str, Any]]:
    """Informational only (draft never blocks on this -- see module
    docstring's KNOWN LIMITATIONS: the catalog can be stale). Reuses
    capability_ref_index's own key shape ("<project>:<kind>:<name>")."""
    ref_index = catalog.get("capability_ref_index")
    ref_index = ref_index if isinstance(ref_index, dict) else {}
    results: list[dict[str, Any]] = []
    for raw in depends_on:
        parts = raw.split(":")
        if len(parts) == 2:
            qualified = f"{target_project}:{parts[0]}:{parts[1]}"
        elif len(parts) == 3:
            qualified = raw
        else:
            results.append({"raw": raw, "resolved": False, "global_id": None})
            continue
        global_id = ref_index.get(qualified)
        results.append(
            {
                "raw": raw,
                "resolved": _is_str(global_id),
                "global_id": global_id if _is_str(global_id) else None,
            }
        )
    return results


def capability_ref_key_known(catalog: dict[str, Any], ref_key: str) -> bool:
    ref_index = catalog.get("capability_ref_index")
    return isinstance(ref_index, dict) and _is_str(ref_index.get(ref_key))


# ===========================================================================
# COPIED FROM validate_reusable_capabilities.py, NOT IMPORTED.
#
# Per design doc §3.0.2: this file's validation logic is "relatively simple"
# (a handful of type/enum/grammar checks) and stays under the project's
# ordinary "copy a small bit, don't import" convention -- unlike
# wiki_edit_guard.py (see the module docstring for that named exception).
#
# KIND_VALUES below is one of the constants this project has already proven
# CAN silently drift between independently-maintained copies (see
# agent_capacity.py's two diverged copies). test_promote_capability.py
# therefore carries a cross-file equality test asserting this tuple is
# byte-for-byte identical to validate_reusable_capabilities.py's own
# KIND_VALUES and to build_cross_project_catalog.py's -- per §3.0.2's new
# mandate, that obligation applies to every "copy, don't import" constant
# this file introduces, not just this one at the moment it happened to be
# written.
#
# validate_document()/_validate_entry()/_validate_path()/_validate_depends_on()
# below are trimmed to what approve's post-write self-check actually needs
# (structural + depends_on-grammar + path-containment validation over an
# in-memory document); the CLI wrapper, --json line output, and project-id
# derivation from validate_reusable_capabilities.py are intentionally not
# reproduced here -- this file only ever calls validate_document() as a
# library function against a document it already holds in memory.
# ===========================================================================

SCHEMA_VERSION_SUPPORTED = 1
TOP_LEVEL_KEYS = frozenset({"schema_version", "project", "capabilities"})
ENTRY_KEYS = frozenset({"id", "kind", "name", "path", "summary", "last_verified_at", "depends_on"})
KIND_VALUES = ("skill", "script", "config-pattern")

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ID_MAX_LEN = 64
NAME_MAX_LEN = 120
SUMMARY_MAX_LEN = 300

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
MAX_FUTURE_SKEW_SECONDS = 300


def _is_plain_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class Finding:
    __slots__ = ("pointer", "code", "message")

    def __init__(self, pointer: str, code: str, message: str) -> None:
        self.pointer = pointer
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"pointer": self.pointer, "code": self.code, "message": self.message}


class ValidationRun:
    def __init__(self) -> None:
        self.errors: list[Finding] = []
        self.warnings: list[Finding] = []
        self.project: str | None = None
        self.capability_count = 0

    def error(self, pointer: str, code: str, message: str) -> None:
        self.errors.append(Finding(pointer, code, message))

    def warn(self, pointer: str, code: str, message: str) -> None:
        self.warnings.append(Finding(pointer, code, message))


def validate_document(
    doc: Any,
    *,
    project_root: Path,
    check_paths: bool,
    now: datetime,
) -> ValidationRun:
    run = ValidationRun()

    if not isinstance(doc, dict):
        run.error("", "not_an_object", "document root must be a JSON object")
        return run

    if "schema_version" not in doc:
        run.error(".schema_version", "missing_field", "missing required top-level key 'schema_version'")
        return run
    schema_version = doc.get("schema_version")
    if not _is_int_not_bool(schema_version) or schema_version != SCHEMA_VERSION_SUPPORTED:
        run.error(
            ".schema_version",
            "unsupported_schema_version",
            f"unsupported schema version {schema_version!r}; this validator only understands {SCHEMA_VERSION_SUPPORTED}",
        )
        return run

    unknown_top = sorted(set(doc.keys()) - TOP_LEVEL_KEYS)
    for key in unknown_top:
        run.error(f".{key}", "unknown_field", f"unexpected top-level key {key!r}")

    missing_top = sorted(TOP_LEVEL_KEYS - set(doc.keys()))
    for key in missing_top:
        run.error(f".{key}", "missing_field", f"missing required top-level key {key!r}")

    project = doc.get("project")
    if not _is_plain_str(project) or not project:
        run.error(".project", "bad_project", "project must be a non-empty string")
        project = None
    elif project != project.strip() or ":" in project or _has_control_chars(project):
        run.error(".project", "bad_project", "project must be trimmed, contain no ':' and no control characters")
        project = None
    run.project = project

    capabilities = doc.get("capabilities")
    if not isinstance(capabilities, list):
        run.error(".capabilities", "bad_capabilities", "capabilities must be a list")
        return run

    seen_ids: dict[str, int] = {}
    seen_kind_name: dict[tuple[str, str], int] = {}
    entries: list[dict[str, Any]] = []

    for index, raw_entry in enumerate(capabilities):
        pointer_base = f".capabilities[{index}]"
        if not isinstance(raw_entry, dict):
            run.error(pointer_base, "bad_entry", "each capabilities[] element must be a JSON object")
            continue
        entries.append(raw_entry)
        _validate_entry(
            raw_entry,
            pointer_base=pointer_base,
            run=run,
            project_root=project_root,
            check_paths=check_paths,
            now=now,
        )

        entry_id = raw_entry.get("id")
        if _is_plain_str(entry_id) and ID_RE.match(entry_id or ""):
            key = entry_id.lower()
            if key in seen_ids:
                run.error(f"{pointer_base}.id", "duplicate_id", f"duplicate id {entry_id!r} (also at capabilities[{seen_ids[key]}])")
            else:
                seen_ids[key] = index

        kind = raw_entry.get("kind")
        name = raw_entry.get("name")
        if _is_plain_str(kind) and kind in KIND_VALUES and _is_plain_str(name) and name.strip():
            kn_key = (kind, name.strip())
            if kn_key in seen_kind_name:
                run.error(f"{pointer_base}.name", "duplicate_kind_name", f"duplicate (kind, name) pair {kn_key!r}")
            else:
                seen_kind_name[kn_key] = index

    run.capability_count = len(entries)

    local_kind_names = set(seen_kind_name.keys())
    for index, raw_entry in enumerate(capabilities):
        if not isinstance(raw_entry, dict):
            continue
        pointer_base = f".capabilities[{index}]"
        _validate_depends_on(raw_entry, pointer_base=pointer_base, run=run, local_kind_names=local_kind_names, self_project=project)

    return run


def _validate_entry(
    entry: dict[str, Any],
    *,
    pointer_base: str,
    run: ValidationRun,
    project_root: Path,
    check_paths: bool,
    now: datetime,
) -> None:
    unknown = sorted(set(entry.keys()) - ENTRY_KEYS)
    for key in unknown:
        run.error(f"{pointer_base}.{key}", "unknown_field", f"unexpected key {key!r} in capability entry")

    missing = sorted(ENTRY_KEYS - set(entry.keys()))
    for key in missing:
        run.error(f"{pointer_base}.{key}", "missing_field", f"capability entry is missing required key {key!r}")

    entry_id = entry.get("id")
    if not _is_plain_str(entry_id):
        run.error(f"{pointer_base}.id", "bad_id", "id must be a string")
    elif not (1 <= len(entry_id) <= ID_MAX_LEN) or not ID_RE.match(entry_id):
        run.error(f"{pointer_base}.id", "bad_id", f"id {entry_id!r} must match ^[a-z0-9]+(-[a-z0-9]+)*$ and be 1..{ID_MAX_LEN} characters")

    kind = entry.get("kind")
    if not _is_plain_str(kind) or kind not in KIND_VALUES:
        run.error(f"{pointer_base}.kind", "bad_kind", f"kind must be one of {list(KIND_VALUES)}, got {kind!r}")

    name = entry.get("name")
    if not _is_plain_str(name):
        run.error(f"{pointer_base}.name", "bad_name", "name must be a string")
    elif not name:
        run.error(f"{pointer_base}.name", "bad_name", "name must be non-empty")
    elif name != name.strip():
        run.error(f"{pointer_base}.name", "bad_name", "name must not have leading or trailing whitespace")
    elif len(name) > NAME_MAX_LEN:
        run.error(f"{pointer_base}.name", "bad_name", f"name must be <= {NAME_MAX_LEN} characters")
    elif ":" in name or _has_control_chars(name):
        run.error(f"{pointer_base}.name", "bad_name", "name must not contain ':' or a control character")

    path_value = entry.get("path")
    if not _is_plain_str(path_value) or not path_value:
        run.error(f"{pointer_base}.path", "bad_path", "path must be a non-empty string")
    else:
        _validate_path(path_value, pointer=f"{pointer_base}.path", run=run, project_root=project_root, check_paths=check_paths)

    summary = entry.get("summary")
    if not _is_plain_str(summary):
        run.error(f"{pointer_base}.summary", "bad_summary", "summary must be a string")
    elif not summary:
        run.error(f"{pointer_base}.summary", "bad_summary", "summary must be non-empty")
    elif summary != summary.strip():
        run.error(f"{pointer_base}.summary", "bad_summary", "summary must not have leading or trailing whitespace")
    elif len(summary) > SUMMARY_MAX_LEN:
        run.error(f"{pointer_base}.summary", "bad_summary", f"summary must be <= {SUMMARY_MAX_LEN} characters")
    elif _has_control_chars(summary):
        run.error(f"{pointer_base}.summary", "bad_summary", "summary must be a single line with no control characters")

    last_verified_at = entry.get("last_verified_at")
    if last_verified_at is not None:
        if not _is_plain_str(last_verified_at) or not TIMESTAMP_RE.match(last_verified_at):
            run.error(f"{pointer_base}.last_verified_at", "bad_timestamp", "last_verified_at must be null or match the RFC3339-Z pattern")
        else:
            parsed = _parse_strict_utc_timestamp(last_verified_at)
            if parsed is None:
                run.error(f"{pointer_base}.last_verified_at", "bad_timestamp", f"last_verified_at {last_verified_at!r} is not a real calendar date/time")
            elif parsed > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
                run.error(f"{pointer_base}.last_verified_at", "future_verified_at", "last_verified_at is too far in the future")

    depends_on = entry.get("depends_on")
    if not isinstance(depends_on, list):
        run.error(f"{pointer_base}.depends_on", "bad_depends_on", "depends_on must be a list")


def _parse_strict_utc_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _validate_path(path_value: str, *, pointer: str, run: ValidationRun, project_root: Path, check_paths: bool) -> None:
    if path_value in (".", ""):
        run.error(pointer, "bad_path", "path must not be '.' or empty")
        return
    if path_value.startswith("/"):
        run.error(pointer, "bad_path", "path must be relative (must not start with '/')")
        return
    if re.match(r"^[A-Za-z]:", path_value):
        run.error(pointer, "bad_path", "path must not use a Windows drive letter")
        return
    if "\\" in path_value:
        run.error(pointer, "bad_path", "path must use POSIX separators only")
        return
    segments = path_value.split("/")
    if any(segment == ".." for segment in segments):
        run.error(pointer, "bad_path", "path must not contain a '..' segment")
        return
    if any(segment == "" for segment in segments):
        run.error(pointer, "bad_path", "path must not contain an empty segment")
        return

    if not check_paths:
        return

    target = project_root / path_value
    if not os.path.lexists(str(target)):
        run.error(pointer, "path_missing", f"path {path_value!r} does not exist under project root {project_root}")
        return
    try:
        resolved_root = project_root.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
    except OSError as exc:
        run.error(pointer, "path_unresolvable", f"could not resolve path {path_value!r}: {exc}")
        return
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        run.error(pointer, "path_escapes_root", f"path {path_value!r} resolves outside the project root")
        return
    if not os.path.exists(str(resolved_target)):
        run.error(pointer, "path_dangling_symlink", f"path {path_value!r} is a symlink whose target does not exist")


def _validate_depends_on(
    entry: dict[str, Any],
    *,
    pointer_base: str,
    run: ValidationRun,
    local_kind_names: set,
    self_project: str | None,
) -> None:
    depends_on = entry.get("depends_on")
    if not isinstance(depends_on, list):
        return

    self_kind = entry.get("kind")
    self_name = entry.get("name")
    self_key = (
        f"{self_kind}:{self_name.strip()}"
        if _is_plain_str(self_kind) and _is_plain_str(self_name) and self_name.strip()
        else None
    )

    seen_refs: set = set()
    for ref_index, ref in enumerate(depends_on):
        pointer = f"{pointer_base}.depends_on[{ref_index}]"
        if not _is_plain_str(ref) or not ref:
            run.error(pointer, "bad_depends_on_entry", "depends_on entries must be non-empty strings")
            continue
        if ref != ref.strip() or _has_control_chars(ref):
            run.error(pointer, "bad_depends_on_entry", "depends_on entry must be trimmed with no control characters")
            continue
        if ref in seen_refs:
            run.error(pointer, "duplicate_depends_on", f"duplicate depends_on entry {ref!r}")
            continue
        seen_refs.add(ref)

        parts = ref.split(":")
        stripped_parts = [p.strip() for p in parts]
        if len(parts) not in (2, 3) or any(p == "" for p in stripped_parts) or any(p != raw for p, raw in zip(stripped_parts, parts)):
            run.error(pointer, "bad_depends_on_grammar", f"depends_on entry {ref!r} must be \"<kind>:<name>\" or \"<project>:<kind>:<name>\"")
            continue

        ref_kind = parts[-2]
        ref_name = parts[-1]
        if ref_kind not in KIND_VALUES:
            run.error(pointer, "bad_kind", f"depends_on entry {ref!r} has kind {ref_kind!r}, must be one of {list(KIND_VALUES)}")
            continue
        if len(ref_name) > NAME_MAX_LEN:
            run.error(pointer, "bad_name", f"depends_on entry {ref!r} target name exceeds {NAME_MAX_LEN} characters")
            continue

        if len(parts) == 2:
            ref_key = f"{ref_kind}:{ref_name}"
            if self_key is not None and ref_key == self_key:
                run.error(pointer, "self_reference", f"depends_on entry {ref!r} refers to this same capability")
                continue
            if (ref_kind, ref_name) not in local_kind_names:
                run.error(pointer, "dangling_local_ref", f"same-project depends_on entry {ref!r} does not match any (kind, name) in this file")
        else:
            ref_project = parts[0]
            if self_project is not None and ref_project == self_project:
                run.error(
                    pointer,
                    "redundant_cross_project_ref",
                    f"depends_on entry {ref!r} names this file's own project; use \"{ref_kind}:{ref_name}\" instead",
                )

# ===========================================================================
# END COPY
# ===========================================================================


# ---------------------------------------------------------------------------
# Candidate input validation (draft-time) -- capability-type and
# knowledge-type payloads
# ---------------------------------------------------------------------------


def _require_str(payload: dict[str, Any], field: str, *, max_len: int | None = None, allow_colon: bool = True) -> str:
    value = payload.get(field)
    if not _is_plain_str(value) or not value:
        raise PromoteValidationError("bad_field", f"{field} must be a non-empty string")
    if value != value.strip():
        raise PromoteValidationError("bad_field", f"{field} must not have leading/trailing whitespace")
    if _has_control_chars(value):
        raise PromoteValidationError("bad_field", f"{field} must not contain a control character")
    if not allow_colon and ":" in value:
        raise PromoteValidationError("bad_field", f"{field} must not contain ':'")
    if max_len is not None and len(value) > max_len:
        raise PromoteValidationError("bad_field", f"{field} must be <= {max_len} characters")
    return value


def _validate_target_project(value: Any) -> str:
    if not _is_plain_str(value) or not value:
        raise PromoteValidationError("bad_target_project", "target_project must be a non-empty string")
    if _has_control_chars(value):
        raise PromoteValidationError("bad_target_project", "target_project must not contain a control character")
    segments = value.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise PromoteValidationError("bad_target_project", "target_project must not contain an empty, '.', or '..' path segment")
    return value


def _validate_relative_path(value: Any, *, field: str) -> str:
    if not _is_plain_str(value) or not value:
        raise PromoteValidationError("bad_field", f"{field} must be a non-empty string")
    if value in (".", "") or value.startswith("/") or "\\" in value or _has_control_chars(value):
        raise PromoteValidationError("bad_field", f"{field} must be a relative POSIX path with no control characters")
    segments = value.split("/")
    if any(seg == ".." for seg in segments) or any(seg == "" for seg in segments):
        raise PromoteValidationError("bad_field", f"{field} must not contain '..' or an empty segment")
    return value


def _validate_depends_on_list(raw: Any, *, self_kind: str, self_name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PromoteValidationError("bad_depends_on", "depends_on must be a list")
    self_key = f"{self_kind}:{self_name}"
    out: list[str] = []
    seen: set[str] = set()
    for ref in raw:
        if not _is_plain_str(ref) or not ref or ref != ref.strip() or _has_control_chars(ref):
            raise PromoteValidationError("bad_depends_on_entry", f"depends_on entry {ref!r} must be a trimmed, control-character-free string")
        if ref in seen:
            raise PromoteValidationError("duplicate_depends_on", f"duplicate depends_on entry {ref!r}")
        seen.add(ref)
        parts = ref.split(":")
        if len(parts) not in (2, 3) or any(p != p.strip() or not p for p in parts):
            raise PromoteValidationError("bad_depends_on_grammar", f"depends_on entry {ref!r} is not valid \"<kind>:<name>\" / \"<project>:<kind>:<name>\" grammar")
        ref_kind, ref_name = parts[-2], parts[-1]
        if ref_kind not in KIND_VALUES:
            raise PromoteValidationError("bad_kind", f"depends_on entry {ref!r} has kind {ref_kind!r} not in {list(KIND_VALUES)}")
        if len(parts) == 2 and f"{ref_kind}:{ref_name}" == self_key:
            raise PromoteValidationError("self_reference", f"depends_on entry {ref!r} refers to this same capability")
        out.append(ref)
    return out


def validate_capability_candidate_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a `proposed_target: reusable-capabilities.json` draft input.
    Returns the normalized field set used to build the candidate record."""
    kind = payload.get("kind")
    if not _is_plain_str(kind) or kind not in KIND_VALUES:
        raise PromoteValidationError("bad_kind", f"kind must be one of {list(KIND_VALUES)}, got {kind!r}")
    entry_id = _require_str(payload, "id", max_len=ID_MAX_LEN, allow_colon=False)
    if not ID_RE.match(entry_id):
        raise PromoteValidationError("bad_id", f"id {entry_id!r} must match ^[a-z0-9]+(-[a-z0-9]+)*$")
    name = _require_str(payload, "name", max_len=NAME_MAX_LEN, allow_colon=False)
    path_value = _validate_relative_path(payload.get("path"), field="path")
    summary = _require_str(payload, "summary", max_len=SUMMARY_MAX_LEN)
    last_verified_at = payload.get("last_verified_at")
    if last_verified_at is not None:
        if not _is_plain_str(last_verified_at) or not TIMESTAMP_RE.match(last_verified_at):
            raise PromoteValidationError("bad_timestamp", "last_verified_at must be null or match the RFC3339-Z pattern")
    depends_on = _validate_depends_on_list(payload.get("depends_on"), self_kind=kind, self_name=name)

    unknown = sorted(
        set(payload.keys())
        - {
            "target_project",
            "proposed_target",
            "id",
            "kind",
            "name",
            "path",
            "summary",
            "last_verified_at",
            "depends_on",
            "source",
        }
    )
    if unknown:
        raise PromoteValidationError("unknown_field", f"unexpected field(s) in capability candidate input: {unknown}")

    return {
        "id": entry_id,
        "kind": kind,
        "name": name,
        "title": None,
        "path": path_value,
        "summary": summary,
        "last_verified_at": last_verified_at,
        "wiki_status": None,
        "depends_on": depends_on,
        "content_md": None,
    }


def validate_knowledge_candidate_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a `proposed_target: orca-context-wiki.json` draft input."""
    if "depends_on" in payload:
        raise PromoteValidationError(
            "depends_on_not_supported_for_knowledge",
            "orca-context-wiki.json pages have no depends_on field yet (design doc §3.1's documented gap) -- "
            "do not submit depends_on on a knowledge candidate",
        )
    entry_id = _require_str(payload, "id", max_len=ID_MAX_LEN, allow_colon=False)
    if not ID_RE.match(entry_id):
        raise PromoteValidationError("bad_id", f"id {entry_id!r} must match ^[a-z0-9]+(-[a-z0-9]+)*$")
    title = _require_str(payload, "title", max_len=NAME_MAX_LEN)
    path_value = _validate_relative_path(payload.get("path"), field="path")
    summary = _require_str(payload, "summary", max_len=SUMMARY_MAX_LEN)
    wiki_status = payload.get("wiki_status")
    if wiki_status is None:
        wiki_status = "promoted-unverified"
    elif not _is_plain_str(wiki_status) or not wiki_status or _has_control_chars(wiki_status):
        raise PromoteValidationError("bad_field", "wiki_status must be a non-empty, control-character-free string")

    content_md = payload.get("content_md")
    if content_md is not None:
        if not _is_plain_str(content_md) or not content_md.strip():
            raise PromoteValidationError("bad_field", "content_md must be a non-empty string when present")
        if len(content_md.encode("utf-8")) > MAX_CONTENT_MD_BYTES:
            raise PromoteValidationError("bad_field", f"content_md exceeds {MAX_CONTENT_MD_BYTES} bytes")

    unknown = sorted(
        set(payload.keys())
        - {
            "target_project",
            "proposed_target",
            "id",
            "title",
            "path",
            "summary",
            "wiki_status",
            "content_md",
            "source",
        }
    )
    if unknown:
        raise PromoteValidationError("unknown_field", f"unexpected field(s) in knowledge candidate input: {unknown}")

    return {
        "id": entry_id,
        "kind": None,
        "name": None,
        "title": title,
        "path": path_value,
        "summary": summary,
        "last_verified_at": None,
        "wiki_status": wiki_status,
        "depends_on": [],
        "content_md": content_md,
    }


def validate_source(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("source")
    if not isinstance(source, dict):
        raise PromoteValidationError("bad_source", "source must be a JSON object with at least a 'mechanism' field")
    mechanism = source.get("mechanism")
    if not _is_plain_str(mechanism) or not mechanism or _has_control_chars(mechanism):
        raise PromoteValidationError("bad_source", "source.mechanism must be a non-empty, control-character-free string")
    return source


# ---------------------------------------------------------------------------
# Dedup: key + content_hash (design doc §3.4.3, algorithm defined verbatim)
# ---------------------------------------------------------------------------


def _name_or_title(fields: dict[str, Any]) -> str:
    return fields["name"] if fields["name"] is not None else fields["title"]


def _dedup_kind_label(proposed_target: str, fields: dict[str, Any]) -> str:
    # Only reusable-capabilities.json candidates carry a real KIND_VALUES
    # kind; knowledge candidates use a fixed internal label so the two
    # namespaces never collide under one dedup key even when the same name
    # string is reused across both. This label never appears in any
    # published file -- it exists only inside this tool's own dedup key.
    return fields["kind"] if proposed_target == "reusable-capabilities.json" else "knowledge"


def compute_key(target_project: str, proposed_target: str, fields: dict[str, Any]) -> str:
    kind_label = _dedup_kind_label(proposed_target, fields)
    material = _normalize(target_project) + "|" + kind_label + "|" + _normalize(_name_or_title(fields))
    return _sha256_hex(material.encode("utf-8"))


def compute_content_hash(fields: dict[str, Any]) -> str:
    body = fields.get("content_md") or ""
    material = _normalize(fields["summary"]) + _normalize(body)
    return _sha256_hex(material.encode("utf-8"))


def compute_amend_key(target_project: str, target_kind: str, target_name: str, ref: str) -> str:
    material = (
        _normalize(target_project)
        + "|amend|"
        + _normalize(f"{target_kind}:{target_name}")
        + "|"
        + _normalize(ref)
    )
    return _sha256_hex(material.encode("utf-8"))


def compute_amend_content_hash(source: str) -> str:
    return _sha256_hex(_normalize(source).encode("utf-8"))


# ---------------------------------------------------------------------------
# Candidate record I/O (all confined to PROMOTION_ROOT)
# ---------------------------------------------------------------------------


def project_dir(root: Path, target_project: str) -> Path:
    # Built directly from root.absolute() (lexical, unresolved) rather than
    # by resolving-then-rejoining: write_only_within() returns a fully
    # .resolve()d path, and feeding that back in as the base for a second
    # write_only_within() call -- while comparing it against a still-
    # unresolved `root` -- is exactly the resolved/lexical mismatch that
    # bit this same file's resolve_wiki_target_path() (see that function's
    # docstring); every caller below builds its FULL path in one shot from
    # root.absolute() for the same reason.
    resolved, reason = write_only_within(root, str(root.absolute() / target_project))
    if reason or resolved is None:
        raise PromoteFatal("target_project_path_invalid", reason or "invalid_path")
    return resolved


def candidate_path(root: Path, target_project: str, candidate_id: str) -> Path:
    resolved, reason = write_only_within(root, str(root.absolute() / target_project / f"{candidate_id}.json"))
    if reason or resolved is None:
        raise PromoteFatal("candidate_path_invalid", reason or "invalid_path")
    return resolved


def content_md_path(root: Path, target_project: str, candidate_id: str) -> Path:
    resolved, reason = write_only_within(root, str(root.absolute() / target_project / candidate_id / "content.md"))
    if reason or resolved is None:
        raise PromoteFatal("content_md_path_invalid", reason or "invalid_path")
    return resolved


def list_project_candidates(root: Path, target_project: str) -> list[dict[str, Any]]:
    proj_dir = project_dir(root, target_project)
    if not proj_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(proj_dir.glob("*.json")):
        try:
            raw = _read_bounded(entry, MAX_CANDIDATE_BYTES, follow_symlinks=False)
            doc = _load_json_object(raw, label="candidate")
        except (OSError, PromoteUsageError):
            continue
        out.append(doc)
    return out


def find_candidate(root: Path, candidate_id: str) -> tuple[dict[str, Any], Path] | None:
    if not ID_RE.match(candidate_id) and not re.match(r"^[a-z0-9-]+$", candidate_id or ""):
        return None
    if not root.is_dir():
        return None
    for proj_dir in sorted(root.iterdir()):
        if not proj_dir.is_dir():
            continue
        candidate_file = proj_dir / f"{candidate_id}.json"
        if not candidate_file.is_file():
            continue
        try:
            raw = _read_bounded(candidate_file, MAX_CANDIDATE_BYTES, follow_symlinks=False)
            doc = _load_json_object(raw, label="candidate")
        except (OSError, PromoteUsageError):
            continue
        return doc, candidate_file
    return None


def save_candidate(root: Path, record: dict[str, Any]) -> Path:
    final_path = candidate_path(root, record["target_project"], record["candidate_id"])
    try:
        os.makedirs(str(final_path.parent), mode=0o700, exist_ok=True)
    except OSError as exc:
        raise PromoteFatal("candidate_dir_uncreatable", str(exc)) from exc
    if os.path.islink(str(final_path.parent)):
        raise PromoteFatal("candidate_dir_is_symlink")
    payload = _sanitize_line_separators(json.dumps(record, indent=2, ensure_ascii=False)) + "\n"
    atomic_write_within(root, final_path, payload.encode("utf-8"))
    return final_path


def save_content_md(root: Path, target_project: str, candidate_id: str, content_md: str) -> Path:
    final_path = content_md_path(root, target_project, candidate_id)
    try:
        os.makedirs(str(final_path.parent), mode=0o700, exist_ok=True)
    except OSError as exc:
        raise PromoteFatal("content_md_dir_uncreatable", str(exc)) from exc
    if os.path.islink(str(final_path.parent)):
        raise PromoteFatal("content_md_dir_is_symlink")
    atomic_write_within(root, final_path, content_md.encode("utf-8"))
    return final_path


# ---------------------------------------------------------------------------
# The one write exception: resolving + writing into a TARGET PROJECT's own
# wiki/ directory. Separate, narrower helper from write_only_within above,
# which confines this tool's OWN staging writes to PROMOTION_ROOT --
# deliberately not reused here since this is the one place that must escape
# that root by design.
# ---------------------------------------------------------------------------


def resolve_wiki_target_path(project_root: Path, filename: str) -> Path:
    """Two-phase check, same discipline as write_only_within()/read_only_from()
    elsewhere in this plan: phase 1 walks the LEXICAL (unresolved,
    .absolute()-only) path from project_root downward looking for a
    symlink component -- comparing an unresolved path/root pair, never a
    resolved one, since resolving first would silently collapse every real
    symlink component before the check ever saw it (a no-op check that
    LOOKS like protection but is not). Phase 2 is a plain resolved-path
    containment check with no security role of its own -- it exists only
    to confirm the final answer actually sits where phase 1 implies.

    Deliberately asymmetric like read_only_from(): project_root ITSELF may
    sit behind a symlink (real on this machine -- e.g. macOS's own
    /var -> /private/var) and that is accepted; only components BELOW
    project_root are checked."""
    if not project_root.is_dir():
        raise PromoteFatal("target_project_root_missing", str(project_root))
    lexical_root = project_root.absolute()
    wiki_dir = project_root / "wiki"
    if not wiki_dir.is_dir():
        raise PromoteFatal("target_wiki_dir_missing", str(wiki_dir))

    final_path = wiki_dir / filename
    if _has_symlink_component(final_path.absolute(), lexical_root):
        raise PromoteFatal("target_path_has_symlink_component")

    real_root = project_root.resolve(strict=False)
    real_wiki = wiki_dir.resolve(strict=False)
    try:
        real_wiki.relative_to(real_root)
    except ValueError as exc:
        raise PromoteFatal("target_wiki_escapes_project_root") from exc
    if real_wiki == real_root:
        raise PromoteFatal("target_wiki_is_project_root")
    resolved_final = final_path.resolve(strict=False)
    try:
        resolved_final.relative_to(real_wiki)
    except ValueError as exc:
        raise PromoteFatal("target_path_escapes_wiki_dir") from exc
    if resolved_final.parent != real_wiki:
        raise PromoteFatal("target_path_not_direct_child_of_wiki")
    return resolved_final


def _resolve_knowledge_md_path_and_dir_fd(project_root: Path, page_id: str) -> tuple[Path, int]:
    # round-2-fix P0: this function's ONLY containment check used to be on
    # `real_knowledge` (the wiki/knowledge/ directory itself), never on the
    # actual final path `real_knowledge / f"{page_id}.md"`. The comment that
    # used to sit here ("page_id is already ID_RE-validated by this point")
    # was true for draft but NOT for approve: approve's page_id comes
    # straight from a candidate record JSON file read off disk (see
    # cmd_approve -> find_candidate()), with no re-validation of its own
    # between draft-time and approve-time. If that on-disk record is
    # tampered with (or simply corrupted) between draft and approve so its
    # "id" contains "/" segments -- e.g. "../../../OUTSIDE/PWNED" -- the old
    # code below would join it onto real_knowledge and hand back a path
    # that resolves OUTSIDE the project root entirely, and the caller
    # (atomic_write_in_dir) would write there without complaint. A dual
    # review demonstrated this is real and reproducible end-to-end (approve
    # returns status:"approved" with a file written outside the target
    # project). _revalidate_record_for_approve() (see cmd_approve) is the
    # PRIMARY fix -- it re-runs the exact same ID_RE check draft already
    # ran, against the record as it stands right now, closing the actual
    # attack surface. The containment check added below is the SECOND,
    # independent layer: even if that upstream re-validation were ever
    # skipped, weakened, or bypassed by some future caller, this function
    # must not be able to hand back a path outside its own knowledge_dir on
    # its own -- belt AND suspenders, deliberately, not either/or.
    #
    # Same lexical-vs-resolved discipline as resolve_wiki_target_path()
    # above: the symlink-component check MUST run on unresolved
    # (.absolute()-only) paths, since a resolved path has already had every
    # real symlink component collapsed out of it before the check would
    # ever see one.
    if not project_root.is_dir():
        raise PromoteFatal("target_project_root_missing", str(project_root))
    lexical_root = project_root.absolute()
    wiki_dir = project_root / "wiki"
    if not wiki_dir.is_dir():
        raise PromoteFatal("target_wiki_dir_missing", str(wiki_dir))
    knowledge_dir = wiki_dir / "knowledge"
    if _has_symlink_component(wiki_dir.absolute(), lexical_root):
        raise PromoteFatal("target_path_has_symlink_component")
    try:
        os.makedirs(str(knowledge_dir), mode=0o700, exist_ok=True)
    except PermissionError as exc:
        raise PromoteFatal("target_write_permission_denied", str(exc)) from exc
    except OSError as exc:
        raise PromoteFatal("knowledge_dir_uncreatable", str(exc)) from exc
    if os.path.islink(str(knowledge_dir)):
        raise PromoteFatal("knowledge_dir_is_symlink")

    real_root = project_root.resolve(strict=False)
    real_knowledge = knowledge_dir.resolve(strict=False)
    try:
        real_knowledge.relative_to(real_root)
    except ValueError as exc:
        raise PromoteFatal("target_knowledge_dir_escapes_project_root") from exc

    # THE FIX: resolve the FINAL path (directory + page_id-derived filename)
    # and check IT for containment, not just the directory it was built
    # from. A page_id containing "/" (e.g. from a tampered candidate
    # record) makes `f"{page_id}.md"` a multi-segment relative path when
    # joined onto real_knowledge; .resolve(strict=False) collapses any
    # ".."/".": components in it, and relative_to() below then rejects the
    # result if that collapse walked the path outside real_knowledge.
    knowledge_final = (real_knowledge / f"{page_id}.md").resolve(strict=False)
    try:
        knowledge_final.relative_to(real_knowledge)
    except ValueError as exc:
        raise PromoteFatal("target_knowledge_path_escapes_knowledge_dir") from exc
    if knowledge_final.parent != real_knowledge:
        raise PromoteFatal("target_knowledge_path_not_direct_child_of_knowledge_dir")
    if os.sep in knowledge_final.name or knowledge_final.name in (".", ".."):
        # Cannot actually happen given the parent-equality check just
        # above (a Path's .name is by construction a single component,
        # never containing a separator) -- asserted explicitly anyway
        # because this filename is about to be used directly against a
        # directory file descriptor below (never re-resolved as a full
        # path), and that call site must never be handed anything that
        # could be misread as a multi-component or ".."-relative name.
        raise PromoteFatal("target_knowledge_path_not_direct_child_of_knowledge_dir")

    # round-4-fix (this bug class's SECOND fix -- see the module docstring
    # "THE SINGLE WRITE EXCEPTION" section and _approve_orca_context_wiki()'s
    # own comments for the full history). Everything above this point is a
    # pure containment CHECK -- no mutation of the target project beyond,
    # at most, creating an EMPTY, untracked wiki/knowledge/ directory
    # (harmless and idempotent, same as before this fix). A dedicated Grok
    # final gate demonstrated that the caller used to take the plain Path
    # this function returned and re-walk it BY NAME much later, after a
    # real subprocess call (invoke_wiki_edit_guard()) had already run in
    # between -- a genuine TOCTOU window: if something with write access to
    # wiki/ replaced wiki/knowledge (or any ancestor) with a symlink to
    # outside the project during that window, the later by-name write
    # would silently follow it, and approve would report success with the
    # body landed outside the target project entirely.
    #
    # The fix: open a DIRECTORY FILE DESCRIPTOR for real_knowledge right
    # now, at the exact moment containment has just been confirmed, with
    # O_NOFOLLOW so the open itself refuses outright if the final path
    # component has, in the brief unavoidable gap since the .resolve()
    # calls above, already become a symlink. A dir_fd stays pinned to
    # THIS directory's specific inode for as long as it stays open --
    # completely independent of what the name "wiki/knowledge" resolves
    # to afterwards, no matter how much (or how little) work the caller
    # does before actually writing through it. The caller MUST perform the
    # actual knowledge-body write through this fd
    # (atomic_write_in_dir(..., dir_fd=...)), never by re-walking
    # "wiki/knowledge/<name>" as a fresh path by name -- doing so would
    # silently reopen exactly the window this fd exists to close. The
    # caller owns the fd and must close it (see _approve_orca_context_wiki()).
    try:
        dir_fd = os.open(str(real_knowledge), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except PermissionError as exc:
        raise PromoteFatal("target_write_permission_denied", str(exc)) from exc
    except OSError as exc:
        raise PromoteFatal("knowledge_dir_unopenable", str(exc)) from exc

    return knowledge_final, dir_fd


def resolve_knowledge_md_path(project_root: Path, page_id: str) -> Path:
    """Thin wrapper over _resolve_knowledge_md_path_and_dir_fd() for every
    caller that only wants the checked-and-resolved Path, not a live
    directory file descriptor -- e.g. every test in this suite that calls
    this function directly to exercise the containment logic in isolation,
    and any other caller with no TOCTOU-sensitive write to perform. Opens
    and immediately closes the dir_fd (the underlying containment checks
    and their PromoteFatal reasons are unchanged). The one caller that
    actually performs a knowledge-body write (_approve_orca_context_wiki())
    calls _resolve_knowledge_md_path_and_dir_fd() directly instead, so it
    can keep the fd open across the gap until the real write happens."""
    knowledge_final, dir_fd = _resolve_knowledge_md_path_and_dir_fd(project_root, page_id)
    try:
        os.close(dir_fd)
    except OSError:
        pass
    return knowledge_final


def _write_all_bytes(fd: int, payload: bytes) -> None:
    """os.write(fd, payload) does not guarantee the full payload is written
    in one call -- POSIX permits a short write for a regular file (round-6
    of the wiki_edit_guard.py TOCTOU fix's dedicated final-gate review
    empirically reproduced a genuine short write on this exact machine via
    RLIMIT_FSIZE: os.write() returned fewer bytes than requested with no
    exception raised, and wiki_edit_guard.py's own dir-fd writer -- which
    copies this exact function's technique -- silently rename()'d the
    truncated tmp file onto the live tracked wiki file while approve still
    reported `status: approved` and `git_committed: true`). This function
    has the identical unchecked-os.write() shape and is used for the
    knowledge-body write, so it gets the identical fix even though the
    knowledge .md target is untracked (lower blast radius than the tracked
    wiki file, but a silently truncated knowledge body is still a real
    defect, not a hypothetical one, given the sibling bug was just proven
    live). Loop until every byte is written; a zero-progress write (or any
    OSError from a subsequent write) raises immediately so the caller's
    existing `except BaseException: unlink tmp; raise` cleanup fires --
    fail-closed, not a silently truncated success. See
    wiki_edit_guard.py's own _write_all_bytes() for the sibling copy (copy,
    don't import, per this codebase's convention)."""
    view = memoryview(payload)
    while view:
        n = os.write(fd, view)
        if n == 0:
            raise OSError("short write: os.write() returned 0 (no forward progress)")
        view = view[n:]


def atomic_write_in_dir(final_path: Path, payload: bytes, *, dir_fd: int | None = None) -> None:
    """Same tmp-file+os.replace discipline as atomic_write_within, but for
    a write target OUTSIDE this tool's own PROMOTION_ROOT (the target
    project's wiki/ directory) -- containment for THIS write was already
    established by resolve_wiki_target_path/resolve_knowledge_md_path
    before this function is ever called.

    PermissionError (a project directory a caller made read-only, e.g. an
    isolation test, or a real permissions problem) is converted to a named
    PromoteFatal rather than propagating as a raw OSError/traceback --
    "a clean, meaningful failure, not a PermissionError traceback or a
    silent partial write" is an explicit requirement for this tool.

    dir_fd (round-4-fix): when given, every filesystem operation below is
    performed relative to this OPEN directory file descriptor (os.open's
    own dir_fd= parameter, os.rename's src_dir_fd=/dst_dir_fd=), never by
    re-walking final_path.parent as a string. final_path is still used
    only to derive the plain filename (final_path.name); its directory
    component is NEVER touched by name when dir_fd is given -- the caller
    is expected to have obtained dir_fd at the exact moment its own
    containment check passed (see _resolve_knowledge_md_path_and_dir_fd()),
    so writing through it guarantees the write lands in the SAME directory
    inode that was actually checked, regardless of anything that has since
    happened to the name that directory used to be reachable under (a
    later rename or symlink swap of that name, or any ancestor of it,
    cannot redirect a write already anchored to the fd).

    os.replace() has no dir_fd-capable form on this platform (confirmed
    empirically: os.replace not in os.supports_dir_fd, even though
    os.rename is). os.rename() is used instead for the dir_fd path, which
    is safe here because this whole function already assumes POSIX
    (O_NOFOLLOW and os.O_DIRECTORY do not exist on Windows either) and
    POSIX's rename(2) already atomically replaces an existing destination
    -- the exact guarantee os.replace() exists to add on top of
    os.rename() only for Windows' sake."""
    filename = final_path.name
    if dir_fd is not None and (os.sep in filename or filename in (".", "..")):
        # Cannot happen given how every current caller builds final_path
        # (always a direct child of the checked directory) -- asserted
        # explicitly anyway since this string is about to be used as a
        # dir_fd-relative name, and that call site must never be handed
        # anything that could be misread as a multi-component path.
        raise PromoteFatal("target_write_invalid_filename", filename)
    tmp_name = f".{filename}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    tmp_path = final_path.parent / tmp_name

    def _unlink_tmp() -> None:
        try:
            if dir_fd is not None:
                os.unlink(tmp_name, dir_fd=dir_fd)
            else:
                os.unlink(str(tmp_path))
        except OSError:
            pass

    try:
        if dir_fd is not None:
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
        else:
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except PermissionError as exc:
        raise PromoteFatal("target_write_permission_denied", str(exc)) from exc
    try:
        try:
            _write_all_bytes(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        if dir_fd is not None:
            os.rename(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.replace(str(tmp_path), str(final_path))
    except PermissionError as exc:
        _unlink_tmp()
        raise PromoteFatal("target_write_permission_denied", str(exc)) from exc
    except BaseException:
        _unlink_tmp()
        raise


# ---------------------------------------------------------------------------
# git helpers -- ALWAYS invoked with -C <target_project_real_path>, NEVER
# relying on process cwd. Every call site below passes an explicit path.
# ---------------------------------------------------------------------------


def is_git_repo(real_path: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(real_path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            timeout=15,
            text=True,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def git_commit_paths(real_path: Path, relpaths: list[str], message: str) -> tuple[bool, str | None, str | None]:
    """Returns (committed, commit_sha, error_message). NEVER runs without
    -C real_path; never touches any repo other than the one at real_path.

    round-2-fix: `git commit -m message` with NO pathspec commits the
    ENTIRE index, not just whatever this call's own `git add` just staged.
    A caller (a human, another tool, a CI step) that had already run its
    own `git add` on some unrelated file in this SAME target project's
    working tree -- for a change that has nothing to do with this
    approve -- would see that unrelated file silently swept into this
    tool's commit, with this tool's own commit message and authorship,
    the moment approve happened to run. Demonstrated concretely: stage an
    unrelated file, then call approve; the resulting commit's --name-only
    log included it. `git commit -- <relpaths>` is a partial commit: git
    commits ONLY the given paths (using their current working-tree state
    for that commit), and leaves anything else already in the index
    exactly as staged for a future commit -- so this tool's write is fully
    scoped to the files IT wrote, never anything else the caller's index
    happened to be carrying."""
    try:
        add_proc = subprocess.run(
            ["git", "-C", str(real_path), "add", "--"] + relpaths,
            capture_output=True,
            timeout=30,
            text=True,
        )
        if add_proc.returncode != 0:
            return False, None, f"git add failed: {add_proc.stderr.strip() or add_proc.stdout.strip()}"
        commit_proc = subprocess.run(
            ["git", "-C", str(real_path), "commit", "-m", message, "--"] + relpaths,
            capture_output=True,
            timeout=30,
            text=True,
        )
        if commit_proc.returncode != 0:
            return False, None, f"git commit failed: {commit_proc.stderr.strip() or commit_proc.stdout.strip()}"
        sha_proc = subprocess.run(
            ["git", "-C", str(real_path), "rev-parse", "HEAD"],
            capture_output=True,
            timeout=15,
            text=True,
        )
        sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else None
        return True, sha, None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, None, f"git invocation failed: {exc}"


# ---------------------------------------------------------------------------
# manifest re-sign detection (informational; approve never re-signs)
# ---------------------------------------------------------------------------


def check_requires_manifest_resign(real_path: Path, pinned_relpath: str) -> bool:
    """True iff <real_path>/.orca/context/reviewed-startup-pack-manifest.json
    exists, parses, and pins shared_source_sha256s for pinned_relpath's
    basename key ("wiki" for wiki/orca-context-wiki.json) -- in which case
    a write to that path leaves the manifest's pin stale. Best-effort: any
    failure to read/parse the manifest is treated as "cannot confirm it's
    pinned" (False) rather than blocking the write -- this check exists to
    ADD a warning, never to gate the one write path this tool exists for.
    """
    manifest_path = real_path / ".orca" / "context" / "reviewed-startup-pack-manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        raw = _read_bounded(manifest_path, MAX_CATALOG_BYTES, follow_symlinks=True)
        doc = _load_json_object(raw, label="manifest")
    except (OSError, PromoteUsageError):
        return False
    pins = doc.get("shared_source_sha256s")
    if not isinstance(pins, dict):
        return False
    return "wiki" in pins


# ---------------------------------------------------------------------------
# wiki_edit_guard.py subprocess invocation (the one named "call, don't copy"
# exception -- see module docstring)
# ---------------------------------------------------------------------------


def resolve_wiki_edit_guard_path() -> Path:
    return (Path(__file__).resolve().parent / "wiki_edit_guard.py").resolve()


def pin_wiki_edit_guard_sha256() -> tuple[Path, str]:
    guard_path = resolve_wiki_edit_guard_path()
    if not guard_path.is_file():
        raise PromoteFatal("wiki_edit_guard_missing", str(guard_path))
    try:
        data = guard_path.read_bytes()
    except OSError as exc:
        raise PromoteFatal("wiki_edit_guard_unreadable", str(exc)) from exc
    return guard_path, _sha256_hex(data)


def verify_wiki_edit_guard_sha256(guard_path: Path, pinned_sha256: str) -> None:
    """NOT called immediately before the subprocess invocation, despite an
    earlier version of this docstring claiming otherwise (caught by an
    independent review). The one and only call site is immediately after
    pin_wiki_edit_guard_sha256() at this function's own call site, BEFORE
    the knowledge-body write (atomic_write_in_dir) and well before
    invoke_wiki_edit_guard() actually spawns the subprocess ~130 lines
    later -- see the round-5-fix comment block above invoke_wiki_edit_guard()
    for why that later re-check re-validates wiki_path containment/identity
    but does NOT re-hash this guard binary. If the file's content no longer
    matches what was pinned moments earlier in this same run, refuse
    fail-closed rather than trusting a binary this process cannot account
    for -- but note the window this actually covers is the pin/verify pair
    itself, not the pin-to-subprocess-spawn window a reader might assume
    from the two calls' names alone."""
    try:
        data = guard_path.read_bytes()
    except OSError as exc:
        raise PromoteFatal("wiki_edit_guard_unreadable", str(exc)) from exc
    current = _sha256_hex(data)
    if current != pinned_sha256:
        raise PromoteFatal(
            "wiki_edit_guard_hash_mismatch",
            f"{guard_path} changed since it was pinned earlier in this run "
            f"(pinned={pinned_sha256}, current={current}); refusing to invoke an unaccounted-for guard",
        )


def _open_wiki_dir_fd_for_guard(real_path: Path) -> int:
    """round-6-fix: open a directory file descriptor for the target
    project's wiki/ directory, O_NOFOLLOW, so the open itself refuses
    outright if "wiki" has, in the brief unavoidable gap since
    _approve_orca_context_wiki()'s own re-checks just above this call,
    already become a symlink -- same technique as
    _resolve_knowledge_md_path_and_dir_fd()'s own dir_fd open a few
    hundred lines above (round-4-fix), applied here to the wiki/ directory
    itself rather than wiki/knowledge/. The caller (this function's one
    call site) MUST perform the guard invocation through this fd
    (invoke_wiki_edit_guard(..., wiki_dir_fd=...)), never by falling back
    to passing wiki_path as a plain string -- doing so would silently
    reopen exactly the window this fd exists to close. The caller owns the
    fd and must close it once the subprocess call returns, whether it
    succeeded or raised (see _approve_orca_context_wiki()'s finally
    block).

    Deliberately a THIN, single-purpose function (unlike
    _resolve_knowledge_md_path_and_dir_fd(), which also does its own
    containment resolution/validation): by the time this is called,
    _approve_orca_context_wiki() has already validated wiki/'s containment
    via resolve_wiki_target_path() at function entry and re-validated via
    _has_symlink_component()/identity_unchanged() immediately before this
    call -- this function's only job is to turn that already-validated
    directory into a fd as fast as possible, not to re-derive or
    re-justify containment itself.
    """
    wiki_dir = real_path / "wiki"
    try:
        return os.open(str(wiki_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except PermissionError as exc:
        raise PromoteFatal("target_write_permission_denied", str(exc)) from exc
    except OSError as exc:
        raise PromoteFatal("wiki_dir_unopenable_for_guard", str(exc)) from exc


def invoke_wiki_edit_guard(
    guard_path: Path,
    wiki_path: Path,
    new_payload: dict[str, Any],
    *,
    wiki_dir_fd: int | None = None,
) -> dict[str, Any]:
    """Runs wiki_edit_guard.py --apply --acknowledge-resign-pending --json
    against new_payload. Returns its parsed JSON body. Raises PromoteFatal
    for any I/O-level failure to even run the subprocess, or an unexpected
    exit code; raises PromoteValidationError for the guard's own refusal
    (its exit 2).

    wiki_dir_fd (round-6-fix, optional): when given, the guard is invoked
    in its dir-fd mode (--wiki-dir-fd/--wiki-name) instead of
    --wiki <path>. wiki_path is used ONLY to derive wiki_path.name (the
    basename passed as --wiki-name) in that case -- its directory
    component is never passed to the subprocess by string, and wiki_dir_fd
    (already open, already validated by the caller -- see
    _open_wiki_dir_fd_for_guard()) is handed to the child via pass_fds so
    it anchors every filesystem operation to that fd rather than
    re-resolving "wiki/<name>" by path string. See wiki_edit_guard.py's own
    module docstring "round-6 fix" note for the receiving side of this
    contract. When wiki_dir_fd is None (the default), behavior is
    byte-for-byte identical to before this parameter existed."""
    fd, tmp_name = tempfile.mkstemp(prefix="promote-wiki-candidate-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(new_payload, handle, ensure_ascii=False)
        try:
            if wiki_dir_fd is not None:
                argv = [
                    sys.executable,
                    str(guard_path),
                    "--wiki-dir-fd",
                    str(wiki_dir_fd),
                    "--wiki-name",
                    wiki_path.name,
                    "--new",
                    tmp_name,
                    "--apply",
                    "--acknowledge-resign-pending",
                    "--json",
                ]
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    timeout=60,
                    text=True,
                    pass_fds=(wiki_dir_fd,),
                )
            else:
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(guard_path),
                        "--wiki",
                        str(wiki_path),
                        "--new",
                        tmp_name,
                        "--apply",
                        "--acknowledge-resign-pending",
                        "--json",
                    ],
                    capture_output=True,
                    timeout=60,
                    text=True,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PromoteFatal("wiki_edit_guard_invocation_failed", str(exc)) from exc
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass

    try:
        body = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        body = {}

    if proc.returncode == 0:
        return body
    if proc.returncode == 2:
        # round-6-fix P2-1 (dedicated review): body is {} whenever the
        # guard's stdout was empty or non-JSON -- and {}.get("reason") is
        # None, a valid dict lookup, not a reason to skip proc.stderr. A
        # stale co-deployed guard that doesn't understand --wiki-dir-fd/
        # --wiki-name argparse-errors on exit 2 with nothing on stdout and
        # the real explanation on stderr; without this fallback the
        # operator sees reason=None and a misleading
        # "wiki_edit_guard_refused" label that reads as "your write was
        # rejected" rather than "the guard binary is stale/mismatched" --
        # exactly the confusion likely to push an operator toward
        # hand-editing the wiki, the bypass this guard exists to prevent.
        reason = body.get("reason") if isinstance(body, dict) else None
        raise PromoteValidationError("wiki_edit_guard_refused", reason or proc.stderr.strip())
    raise PromoteFatal(
        "wiki_edit_guard_unexpected_exit",
        f"exit {proc.returncode}: {body.get('reason') if isinstance(body, dict) else proc.stderr.strip()}",
    )


# ---------------------------------------------------------------------------
# cmd_draft
# ---------------------------------------------------------------------------


def cmd_draft(args: argparse.Namespace) -> dict[str, Any]:
    if not args.from_json.strip():
        raise PromoteUsageError("empty_from_json_path")
    if not args.catalog.strip():
        raise PromoteUsageError("empty_catalog_path")

    input_path = Path(args.from_json).expanduser()
    try:
        raw = _read_bounded(input_path, MAX_INPUT_BYTES, follow_symlinks=True)
    except FileNotFoundError as exc:
        raise PromoteUsageError("from_json_missing", str(input_path)) from exc
    except OSError as exc:
        raise PromoteUsageError("from_json_unreadable", str(exc)) from exc
    payload = _load_json_object(raw, label="candidate_input")

    target_project = _validate_target_project(payload.get("target_project"))
    proposed_target = payload.get("proposed_target")
    if proposed_target not in PROPOSED_TARGETS:
        raise PromoteValidationError("bad_proposed_target", f"proposed_target must be one of {list(PROPOSED_TARGETS)}")
    source = validate_source(payload)

    if proposed_target == "reusable-capabilities.json":
        fields = validate_capability_candidate_input(payload)
    else:
        fields = validate_knowledge_candidate_input(payload)

    catalog = load_catalog(Path(args.catalog).expanduser())
    project_roots = build_project_root_index(catalog)
    target_project_known = target_project in project_roots
    depends_on_resolution = resolve_depends_on_against_catalog(catalog, target_project, fields["depends_on"])

    key = compute_key(target_project, proposed_target, fields)
    content_hash = compute_content_hash(fields)

    root = ensure_promotion_root()
    lock_path = acquire_lock(root)
    try:
        # The dedup read (list_project_candidates) through the write
        # (save_candidate) below must all happen under this one lock.
        # Computing `existing`/`exact_duplicate` BEFORE the lock was
        # acquired was a real, demonstrated TOCTOU: two concurrent `draft`
        # calls for the identical candidate could both observe "no existing
        # duplicate" and both go on to write a pending_approval record,
        # defeating the exact-duplicate-rejection contract this dedup
        # algorithm exists to provide.
        existing = [
            doc
            for doc in list_project_candidates(root, target_project)
            if doc.get("key") == key and doc.get("status") in ("pending_approval", "approved")
        ]
        exact_duplicate = next((doc for doc in existing if doc.get("content_hash") == content_hash), None)
        if exact_duplicate is not None:
            raise PromoteValidationError(
                "exact_duplicate",
                {"existing_candidate_id": exact_duplicate.get("candidate_id")},
            )
        possible_revision_of = None
        if existing:
            possible_revision_of = sorted(existing, key=lambda d: d.get("created_at") or "")[-1].get("candidate_id")

        candidate_id = f"cand-{uuid.uuid4().hex[:20]}"
        created_at = now_iso()
        record: dict[str, Any] = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "status": "pending_approval",
            "operation": "create",
            "target_project": target_project,
            "target_project_known_in_catalog": target_project_known,
            "proposed_target": proposed_target,
            "id": fields["id"],
            "kind": fields["kind"],
            "name": fields["name"],
            "title": fields["title"],
            "path": fields["path"],
            "summary": fields["summary"],
            "last_verified_at": fields["last_verified_at"],
            "wiki_status": fields["wiki_status"],
            "depends_on": fields["depends_on"],
            "depends_on_resolution": depends_on_resolution,
            "has_content_md": fields["content_md"] is not None,
            "content_md_relpath": f"{candidate_id}/content.md" if fields["content_md"] is not None else None,
            "key": key,
            "content_hash": content_hash,
            "possible_revision_of": possible_revision_of,
            "source": source,
            "created_at": created_at,
            "updated_at": created_at,
            "approved_by": None,
            "decided_by": None,
            "rationale": None,
            "decided_at": None,
        }

        save_candidate(root, record)
        if fields["content_md"] is not None:
            save_content_md(root, target_project, candidate_id, fields["content_md"])
        append_ledger(
            root,
            {
                "ts": created_at,
                "action": "draft",
                "candidate_id": candidate_id,
                "target_project": target_project,
                "proposed_target": proposed_target,
                "status": "pending_approval",
                "key": key,
                "content_hash": content_hash,
                "possible_revision_of": possible_revision_of,
            },
        )
    finally:
        release_lock(lock_path)

    return {
        "candidate_id": candidate_id,
        "status": "pending_approval",
        "target_project": target_project,
        "target_project_known_in_catalog": target_project_known,
        "proposed_target": proposed_target,
        "key": key,
        "content_hash": content_hash,
        "possible_revision_of": possible_revision_of,
        "depends_on_resolution": depends_on_resolution,
    }


# ---------------------------------------------------------------------------
# cmd_amend -- sugar for draft producing an operation:"amend_depends_on"
# candidate. Only meaningful against reusable-capabilities.json entries
# (wiki pages have no depends_on -- see design §3.1's documented gap).
# ---------------------------------------------------------------------------


def cmd_amend(args: argparse.Namespace) -> dict[str, Any]:
    target_project = _validate_target_project(args.target_project)
    target = args.target or ""
    parts = target.split(":")
    if len(parts) != 2 or any(not p for p in parts):
        raise PromoteUsageError("bad_target", "--target must be \"<kind>:<name>\"")
    target_kind, target_name = parts
    if target_kind not in KIND_VALUES:
        raise PromoteUsageError("bad_target_kind", f"kind must be one of {list(KIND_VALUES)}")
    ref = args.add_depends_on or ""
    if not ref or ref != ref.strip() or _has_control_chars(ref):
        raise PromoteUsageError("bad_add_depends_on", "--add-depends-on must be a non-empty, trimmed, control-character-free string")
    ref_parts = ref.split(":")
    if len(ref_parts) not in (2, 3) or any(not p for p in ref_parts):
        raise PromoteUsageError("bad_add_depends_on_grammar", "--add-depends-on must be \"<kind>:<name>\" or \"<project>:<kind>:<name>\"")
    if ref_parts[-2] not in KIND_VALUES:
        raise PromoteUsageError("bad_add_depends_on_kind", f"kind must be one of {list(KIND_VALUES)}")
    if len(ref_parts) == 2 and f"{ref_parts[0]}:{ref_parts[1]}" == f"{target_kind}:{target_name}":
        raise PromoteUsageError("self_reference", "--add-depends-on must not name the amend target itself")
    source_text = args.source
    if not _is_plain_str(source_text) or not source_text or _has_control_chars(source_text):
        raise PromoteUsageError("bad_source", "--source must be a non-empty, control-character-free string")

    if not args.catalog.strip():
        raise PromoteUsageError("empty_catalog_path")
    catalog = load_catalog(Path(args.catalog).expanduser())
    project_roots = build_project_root_index(catalog)
    target_project_known = target_project in project_roots
    ref_key = f"{target_project}:{target_kind}:{target_name}"
    target_known_in_catalog = capability_ref_key_known(catalog, ref_key)

    key = compute_amend_key(target_project, target_kind, target_name, ref)
    content_hash = compute_amend_content_hash(source_text)

    root = ensure_promotion_root()
    lock_path = acquire_lock(root)
    try:
        # Same TOCTOU fix as cmd_draft above: the dedup read must happen
        # under the lock, not before it.
        existing = [
            doc
            for doc in list_project_candidates(root, target_project)
            if doc.get("key") == key and doc.get("status") in ("pending_approval", "approved")
        ]
        exact_duplicate = next((doc for doc in existing if doc.get("content_hash") == content_hash), None)
        if exact_duplicate is not None:
            raise PromoteValidationError("exact_duplicate", {"existing_candidate_id": exact_duplicate.get("candidate_id")})
        possible_revision_of = None
        if existing:
            possible_revision_of = sorted(existing, key=lambda d: d.get("created_at") or "")[-1].get("candidate_id")

        candidate_id = f"cand-{uuid.uuid4().hex[:20]}"
        created_at = now_iso()
        record: dict[str, Any] = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "status": "pending_approval",
            "operation": "amend_depends_on",
            "target_project": target_project,
            "target_project_known_in_catalog": target_project_known,
            "proposed_target": "reusable-capabilities.json",
            "amend_target_kind": target_kind,
            "amend_target_name": target_name,
            "amend_target_known_in_catalog": target_known_in_catalog,
            "amend_add_depends_on": ref,
            "id": None,
            "kind": None,
            "name": None,
            "title": None,
            "path": None,
            "summary": f"amend: add depends_on {ref!r} to {target_kind}:{target_name}",
            "last_verified_at": None,
            "wiki_status": None,
            "depends_on": [],
            "depends_on_resolution": [],
            "has_content_md": False,
            "content_md_relpath": None,
            "key": key,
            "content_hash": content_hash,
            "possible_revision_of": possible_revision_of,
            "source": {"mechanism": source_text},
            "created_at": created_at,
            "updated_at": created_at,
            "approved_by": None,
            "decided_by": None,
            "rationale": None,
            "decided_at": None,
        }

        save_candidate(root, record)
        append_ledger(
            root,
            {
                "ts": created_at,
                "action": "amend_draft",
                "candidate_id": candidate_id,
                "target_project": target_project,
                "proposed_target": "reusable-capabilities.json",
                "status": "pending_approval",
                "key": key,
                "content_hash": content_hash,
                "possible_revision_of": possible_revision_of,
            },
        )
    finally:
        release_lock(lock_path)

    return {
        "candidate_id": candidate_id,
        "status": "pending_approval",
        "operation": "amend_depends_on",
        "target_project": target_project,
        "amend_target_known_in_catalog": target_known_in_catalog,
        "key": key,
        "content_hash": content_hash,
        "possible_revision_of": possible_revision_of,
    }


# ---------------------------------------------------------------------------
# cmd_approve -- THE write path
# ---------------------------------------------------------------------------


def _approve_reusable_capabilities(
    record: dict[str, Any], real_path: Path, approved_by: str, rationale: str
) -> dict[str, Any]:
    wiki_path = resolve_wiki_target_path(real_path, "reusable-capabilities.json")
    if not wiki_path.is_file():
        raise PromoteFatal("target_reusable_capabilities_missing", str(wiki_path))

    old_raw, identity = read_with_identity(wiki_path, MAX_WIKI_BYTES)
    old_doc = _load_json_object(old_raw, label="target_reusable_capabilities")

    if record["operation"] == "create":
        new_entry = {
            "id": record["id"],
            "kind": record["kind"],
            "name": record["name"],
            "path": record["path"],
            "summary": record["summary"],
            "last_verified_at": record["last_verified_at"],
            "depends_on": record["depends_on"],
        }
        capabilities = old_doc.get("capabilities")
        if not isinstance(capabilities, list):
            raise PromoteFatal("target_reusable_capabilities_malformed", "capabilities is not a list")
        for existing in capabilities:
            if not isinstance(existing, dict):
                continue
            if existing.get("id") == new_entry["id"]:
                raise PromoteValidationError("duplicate_id", f"id {new_entry['id']!r} already exists in the target file")
            if existing.get("kind") == new_entry["kind"] and existing.get("name") == new_entry["name"]:
                raise PromoteValidationError("duplicate_kind_name", f"(kind, name) {(new_entry['kind'], new_entry['name'])!r} already exists")
        new_doc = copy.deepcopy(old_doc)
        new_doc["capabilities"].append(new_entry)
    else:  # amend_depends_on
        capabilities = old_doc.get("capabilities")
        if not isinstance(capabilities, list):
            raise PromoteFatal("target_reusable_capabilities_malformed", "capabilities is not a list")
        target_index = None
        for index, existing in enumerate(capabilities):
            if not isinstance(existing, dict):
                continue
            if existing.get("kind") == record["amend_target_kind"] and existing.get("name") == record["amend_target_name"]:
                target_index = index
                break
        if target_index is None:
            raise PromoteValidationError(
                "amend_target_not_found",
                f"{record['amend_target_kind']}:{record['amend_target_name']} does not exist in the target file",
            )
        new_doc = copy.deepcopy(old_doc)
        target_entry = new_doc["capabilities"][target_index]
        current_depends_on = target_entry.get("depends_on")
        if not isinstance(current_depends_on, list):
            raise PromoteFatal("target_entry_malformed", "amend target's depends_on is not a list")
        if record["amend_add_depends_on"] in current_depends_on:
            raise PromoteValidationError("depends_on_already_present", f"{record['amend_add_depends_on']!r} is already declared")
        target_entry["depends_on"] = current_depends_on + [record["amend_add_depends_on"]]

    if not identity_unchanged(wiki_path, identity):
        raise PromoteFatal("concurrent_modification_detected", str(wiki_path))

    # 2026-08-26 fix: this write used to call atomic_write_in_dir() with NO
    # dir_fd, unlike _approve_orca_context_wiki()'s round-5/round-6 hardening
    # of the SAME class of write (a sibling approval path -- this one for
    # reusable-capabilities.json, that one for orca-context-wiki.json -- both
    # write into the same target project's wiki/ directory, but only one of
    # the two ever got the TOCTOU hardening). A 3-model max-effort cross-audit
    # (2026-08-26) found this real, still-live gap: without a dir_fd, a
    # symlink swap of real_path/wiki (or of wiki_path's own name) between the
    # identity_unchanged() check above and the write below could still
    # redirect the write outside the project. Mirror the wiki-approval path's
    # own re-check + dir-fd-open sequence exactly (same order: symlink-
    # component check, then open the dir with O_NOFOLLOW, then write through
    # that fd) -- _open_wiki_dir_fd_for_guard() is reused here despite its
    # "for_guard" name (it is, and always was, a thin "open real_path/wiki
    # with O_NOFOLLOW" helper with no guard-specific logic in it -- see its
    # own docstring) rather than duplicating an identical second copy.
    if _has_symlink_component((real_path / "wiki" / wiki_path.name).absolute(), real_path.absolute()):
        raise PromoteFatal("wiki_path_symlink_introduced", str(wiki_path))
    if not identity_unchanged(wiki_path, identity):
        raise PromoteFatal("concurrent_modification_detected", str(wiki_path))

    new_bytes = (json.dumps(new_doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    wiki_dir_fd_for_write = _open_wiki_dir_fd_for_guard(real_path)
    try:
        atomic_write_in_dir(wiki_path, new_bytes, dir_fd=wiki_dir_fd_for_write)

        # Post-write integrity check: confirm the canonical "wiki" NAME
        # still resolves to the SAME directory wiki_dir_fd_for_write was
        # opened against (compare fstat(dir_fd) against a fresh stat() of
        # the name -- stat() follows symlinks, so this catches a symlink
        # swap AND a substituted real directory, not just one of the two).
        # The write itself is already immune to a name swap (dir_fd
        # anchoring) -- but if "wiki" was swapped in the gap between
        # opening that fd and this point, the just-written bytes, though
        # correctly landed in the originally-validated directory, are no
        # longer reachable at the path any caller or future reader would
        # actually look at. Fail loudly here rather than letting the
        # fd-anchored self-check below validate the now-orphaned write and
        # report success while the canonical path has been hijacked out
        # from under us (2026-08-26 3-model cross-audit finding -- see
        # test_post_write_self_check_reads_via_dir_fd_not_plain_path).
        try:
            wiki_name_stat = os.stat(real_path / "wiki")
        except OSError as exc:
            raise PromoteFatal("concurrent_modification_detected", str(exc)) from exc
        dir_fd_stat = os.fstat(wiki_dir_fd_for_write)
        if (wiki_name_stat.st_dev, wiki_name_stat.st_ino) != (dir_fd_stat.st_dev, dir_fd_stat.st_ino):
            raise PromoteFatal("concurrent_modification_detected", str(wiki_path))

        # Self-check: re-read what is ACTUALLY on disk now (not the
        # in-memory new_doc) and run this file's own copy of
        # validate_document() against it. Read through the SAME dir_fd the
        # write above just used -- NOT wiki_path.read_bytes() (a plain
        # path re-read), which would re-walk "wiki" and the filename from
        # scratch and so could be handed a completely different, merely
        # coincidentally-valid document if a directory (or filename)
        # symlink swap landed in the gap between the write finishing and
        # this read, while the actually-written bytes sit orphaned under
        # the pre-swap name -- a false "approved successfully" against a
        # decoy (2026-08-26 3-model cross-audit finding, confirmed real by
        # test_post_write_self_check_reads_via_dir_fd_not_plain_path).
        written_raw = _read_bytes_via_dir_fd(wiki_dir_fd_for_write, wiki_path.name, MAX_WIKI_BYTES)
    finally:
        try:
            os.close(wiki_dir_fd_for_write)
        except OSError:
            pass

    written_doc = json.loads(written_raw.decode("utf-8"))
    run = validate_document(written_doc, project_root=real_path, check_paths=True, now=datetime.now(timezone.utc))
    if run.errors:
        # Same dir-fd discipline as the write above, re-derived fresh (the
        # earlier fd is already closed, and re-checking immediately before
        # use -- not reusing a stale check -- is this file's own established
        # rule throughout): a symlink swap in the gap between the write above
        # and this rollback must not be able to redirect the rollback either.
        # rolled_back must reflect what ACTUALLY happened, not what was
        # merely attempted -- the guard below can legitimately skip the
        # rollback write (failing closed rather than writing rollback bytes
        # through an attacker-controlled symlink), and the reported field
        # must say so rather than unconditionally claiming success (the
        # second half of the same 2026-08-26 cross-audit finding).
        rolled_back = False
        if not _has_symlink_component((real_path / "wiki" / wiki_path.name).absolute(), real_path.absolute()):
            rollback_dir_fd = _open_wiki_dir_fd_for_guard(real_path)
            try:
                atomic_write_in_dir(wiki_path, old_raw, dir_fd=rollback_dir_fd)
                rolled_back = True
            finally:
                try:
                    os.close(rollback_dir_fd)
                except OSError:
                    pass
        raise PromoteValidationError(
            "post_write_self_check_failed",
            {"errors": [e.as_dict() for e in run.errors], "rolled_back": rolled_back},
        )

    return {
        "proposed_target": "reusable-capabilities.json",
        "wiki_path": str(wiki_path),
        "git_committed": None,
        "git_commit_sha": None,
        "requires_manifest_resign": False,
    }


def _approve_orca_context_wiki(
    record: dict[str, Any],
    real_path: Path,
    approved_by: str,
    rationale: str,
    *,
    do_commit: bool,
) -> dict[str, Any]:
    wiki_path = resolve_wiki_target_path(real_path, "orca-context-wiki.json")
    if not wiki_path.is_file():
        raise PromoteFatal("target_orca_context_wiki_missing", str(wiki_path))

    if do_commit and not is_git_repo(real_path):
        raise PromoteFatal("target_project_not_a_git_repo", str(real_path))

    # round-fix P0 (round 3 of this bug class -- see round-4-fix below for
    # the sequel): this whole block used to run AFTER invoke_wiki_edit_guard()
    # below -- i.e. after the target project's real wiki/orca-context-wiki.json
    # had ALREADY been rewritten on disk. A missing staged content.md (e.g. it
    # disappeared between draft and approve for any reason) was then only
    # discovered once that write had already landed, and the exception raised
    # here propagated out of this function before reaching git_commit_paths(),
    # leaving the wiki file modified-and-uncommitted in the target project's
    # git tree with no matching "approved" candidate record to show for it --
    # the exact AUTHORITY_TRACKED_PATHS NACK class ("an uncommitted tracked
    # path under a project's wiki/") this codebase otherwise takes care to
    # avoid. Moved here, before ANY write to the target wiki file, so a
    # missing/invalid staged content.md is refused cleanly with nothing on
    # disk ever touched.
    #
    # Also read the content.md bytes into memory now (not just is_file()),
    # and resolve+precompute knowledge_final/knowledge_md_relpath now too
    # (this does at most create an EMPTY untracked wiki/knowledge/ directory
    # -- see _resolve_knowledge_md_path_and_dir_fd()): everything about the
    # knowledge_final write that CAN be checked/prepared without touching the
    # target wiki file is done here.
    knowledge_final: Path | None = None
    knowledge_md_relpath: str | None = None
    content_md_bytes: bytes | None = None
    knowledge_dir_fd: int | None = None
    try:
        if record.get("has_content_md"):
            content_md_source = content_md_path(ensure_promotion_root(), record["target_project"], record["candidate_id"])
            if not content_md_source.is_file():
                raise PromoteFatal("staged_content_md_missing", str(content_md_source))
            # round-4-fix: use the dir_fd-returning variant, not plain
            # resolve_knowledge_md_path(), so the actual write further
            # below can go through a directory file descriptor captured at
            # the instant containment was confirmed instead of re-walking
            # "wiki/knowledge/<id>.md" by name later -- see that function's
            # own docstring/comments for the TOCTOU this closes.
            knowledge_final, knowledge_dir_fd = _resolve_knowledge_md_path_and_dir_fd(real_path, record["id"])
            content_md_bytes = content_md_source.read_bytes()
            # knowledge_final is a fully .resolve()d path; real_path (straight
            # from catalog.json) may not be (e.g. this machine's own
            # /var -> /private/var) -- relative_to() needs both sides in the
            # same basis, same class of bug documented on resolve_wiki_target_path().
            knowledge_md_relpath = str(knowledge_final.relative_to(real_path.resolve(strict=False)))

        old_raw, identity = read_with_identity(wiki_path, MAX_WIKI_BYTES)
        old_payload = _load_json_object(old_raw, label="target_orca_context_wiki")

        old_meta = old_payload.get("meta")
        if not isinstance(old_meta, dict) or not isinstance(old_meta.get("content_version"), int) or isinstance(old_meta.get("content_version"), bool):
            raise PromoteFatal(
                "target_wiki_meta_missing_needs_bootstrap",
                f"{wiki_path} has no valid meta.content_version; bootstrap it out-of-band with "
                f"wiki_edit_guard.py --bootstrap before using approve",
            )
        old_version = old_meta["content_version"]

        pages = old_payload.get("pages")
        if not isinstance(pages, list):
            raise PromoteFatal("target_wiki_pages_malformed", "pages is not a list")
        for page in pages:
            if isinstance(page, dict) and page.get("id") == record["id"]:
                raise PromoteValidationError("duplicate_page_id", f"page id {record['id']!r} already exists in the target wiki")

        new_page = {
            "id": record["id"],
            "title": record["title"],
            "path": record["path"],
            "summary": record["summary"],
            "status": record["wiki_status"],
        }
        new_payload = copy.deepcopy(old_payload)
        new_payload["pages"].append(new_page)
        new_payload["meta"] = {"content_version": old_version + 1, "updated_at": now_iso()}

        if not identity_unchanged(wiki_path, identity):
            raise PromoteFatal("concurrent_modification_detected", str(wiki_path))

        guard_path, pinned_sha256 = pin_wiki_edit_guard_sha256()
        verify_wiki_edit_guard_sha256(guard_path, pinned_sha256)

        # round-4-fix P0 (this bug class's SECOND fix -- round-fix P0 above
        # closed only the "missing content.md" trigger, not the general
        # class). This used to be invoke_wiki_edit_guard() FIRST, then the
        # knowledge-body write. That let a failure in the knowledge write
        # (e.g. a pre-existing-but-unwritable wiki/knowledge/ directory --
        # mkdir's own exist_ok=True is a silent no-op on an
        # already-existing directory, so it never catches this; or any
        # other OSError on that write) surface AFTER the target project's
        # tracked wiki/orca-context-wiki.json had already been mutated: the
        # exception propagated out of this function before cmd_approve
        # could mark the candidate approved, so it stayed pending_approval
        # forever, while the wiki file itself carried the new page and a
        # bumped content_version, uncommitted -- and a retry was blocked
        # outright by the duplicate_page_id check above, since the page
        # now already existed. Independently reproduced end-to-end.
        #
        # The fix: write the knowledge body -- an UNTRACKED, brand-new file
        # nothing references yet -- to its final destination FIRST (through
        # knowledge_dir_fd, per the TOCTOU note on
        # _resolve_knowledge_md_path_and_dir_fd()), and make the tracked
        # wiki write (invoke_wiki_edit_guard(), right below) the LAST
        # mutating step in this function before the git commit. If this
        # write fails, nothing has touched the target wiki file at all:
        # the candidate stays pending_approval, and a retry is always
        # possible (any orphaned wiki/knowledge/<id>.md left behind by a
        # partial attempt is silently overwritten by atomic_write_in_dir on
        # the next try, since the page id was never added to the wiki and
        # duplicate_page_id can't yet fire against it).
        #
        # If it succeeds, invoke_wiki_edit_guard() below is -- by
        # inspection of wiki_edit_guard.py's own _atomic_write_text()
        # (temp file + os.replace, the same discipline used throughout
        # this codebase) -- itself all-or-nothing: it either fully
        # rewrites the target wiki file or leaves it fully untouched,
        # never partially. Nothing AFTER it in this function can itself
        # raise: check_requires_manifest_resign() is a best-effort read
        # that already swallows its own OSError/PromoteUsageError
        # internally (see its own docstring), and git_commit_paths()
        # already never raises -- it reports failure through its own
        # return tuple, which is exactly the existing git_committed:false
        # / git_commit_error handling a few lines down (mirroring the
        # module docstring's own already-accepted "git commit can fail
        # after a successful write" case). So once invoke_wiki_edit_guard()
        # returns successfully, there is no remaining step in this
        # function that can turn a real, already-landed wiki mutation into
        # an uncaught exception -- there is deliberately no NEW fallback
        # branch built for that here, because there is nothing left for it
        # to catch.
        if knowledge_final is not None:
            assert content_md_bytes is not None
            assert knowledge_dir_fd is not None
            atomic_write_in_dir(knowledge_final, content_md_bytes, dir_fd=knowledge_dir_fd)

        # round-5-fix P1 (this bug class's THIRD fix -- see round-4-fix's
        # own comment block above for the first two). Grok's dedicated
        # final gate on round-4's fix found that moving the knowledge-body
        # write earlier in this function -- to make invoke_wiki_edit_guard()
        # below the LAST mutating step, closing the knowledge-write TOCTOU
        # -- relocated, but did not eliminate, the identical class of
        # vulnerability onto the wiki write itself, which is now
        # unavoidably that last mutating step. The identity_unchanged()
        # check earlier in this function (right after new_payload is
        # built) now runs BEFORE the knowledge-body write, so a real
        # window opened up between it and invoke_wiki_edit_guard() --
        # spanning pin_wiki_edit_guard_sha256()/verify_wiki_edit_guard_sha256()
        # and the knowledge-body write -- during which an attacker with
        # write access to the target project's wiki/ directory can swap
        # wiki_path itself for a symlink to a file OUTSIDE the project
        # (invoke_wiki_edit_guard() passes str(wiki_path) to
        # wiki_edit_guard.py as a subprocess argument, and that file's own
        # main() does its own `.expanduser().resolve(strict=False)` on
        # that string -- a symlink placed there in the meantime is
        # followed without complaint), or swap wiki/ itself for a symlink
        # to an outside directory holding a decoy copy of the wiki file.
        # Both were independently reproduced end-to-end: approve reports
        # success/approved/committed while the real mutation landed
        # outside the target project and the project's own path now
        # points outside itself.
        #
        # The fix: re-run the containment/identity checks again, right
        # here, as the very last thing before the subprocess is spawned --
        # as little CODE (not comments) as possible in between:
        #
        #   1. _has_symlink_component() again, on a freshly-built LEXICAL
        #      (unresolved, .absolute()-only) "wiki/<name>" path -- same
        #      discipline resolve_wiki_target_path() already documents and
        #      applies: checked FIRST here because it is the more specific
        #      diagnosis (a symlink was introduced) and gets its own
        #      distinct PromoteFatal reason, separate from
        #      "concurrent_modification_detected", so the two failure
        #      modes stay distinguishable in logs/tests. Deliberately
        #      re-derived from real_path/wiki_path.name rather than reusing
        #      the already-.resolve()d `wiki_path` variable: resolving
        #      first would silently collapse a just-introduced symlink
        #      component before this check ever saw it (the exact no-op
        #      pitfall resolve_wiki_target_path's own docstring warns
        #      about).
        #   2. identity_unchanged() again, reusing the exact same
        #      identity tuple captured earlier -- catches an in-place
        #      content swap of the wiki file that does not involve a
        #      symlink at all (e.g. the regular file's bytes rewritten
        #      out from under this process), which check 1 would not
        #      detect on its own.
        #
        # round-5's own text used to end here noting a residual window
        # between this re-check and wiki_edit_guard.py's OWN
        # os.open()/.resolve() call inside its own subprocess, bounded by
        # that subprocess's startup latency (~54ms, dedicated-review-
        # measured) and empirically demonstrated winnable (10/20 file-swap,
        # 20/20 directory-swap). round-6-fix (2026-08-25, user-authorized,
        # dedicated review): that residual is now closed by giving
        # wiki_edit_guard.py a NEW, OPTIONAL dir-fd mode
        # (--wiki-dir-fd/--wiki-name) instead of passing it a path string
        # to re-resolve. Immediately after the two re-checks directly
        # above -- as little code as possible in between, same discipline
        # as those checks themselves -- open a directory file descriptor
        # for the target project's wiki/ directory with O_NOFOLLOW
        # (_open_wiki_dir_fd_for_guard()), pass it to the guard subprocess
        # via pass_fds, and pass the wiki file's plain basename instead of
        # its full path. wiki_edit_guard.py's own dir-fd mode then anchors
        # BOTH its read of the current file and its atomic write of the
        # new one to that fd (dir_fd=), never re-resolving "wiki/<name>" by
        # string at all -- so a rename/symlink-swap of "wiki" itself after
        # this os.open() call cannot redirect the guard's work no matter
        # how long the child process takes to start, and a symlink-swap of
        # the wiki FILE's own name (within an otherwise-untouched wiki/
        # directory) makes the guard's own O_NOFOLLOW open of that name
        # fail closed rather than follow it -- see wiki_edit_guard.py's own
        # module docstring "round-6 fix" note for the receiving side, and
        # this codebase's "copy, don't import" convention: the dir_fd
        # TECHNIQUE is copied from atomic_write_in_dir()/
        # _resolve_knowledge_md_path_and_dir_fd() above (same os.rename()-
        # not-os.replace() dir_fd gotcha applies there too), never a shared
        # import between the two files.
        #
        # What remains, precisely: the two re-checks below still run
        # against a freshly-built path string (_has_symlink_component) and
        # the file identity captured earlier (identity_unchanged) -- there
        # is an irreducible handful of Python bytecode instructions between
        # those checks returning and the os.open() call that follows them,
        # not a subprocess spawn. What happens in that narrow gap depends
        # on what "wiki" is swapped FOR (dedicated-review P3-1, round-6,
        # re-derived and reproduced independently rather than taken on
        # faith): (a) a SYMLINK swap of "wiki" makes the os.open() call
        # itself fail outright (O_NOFOLLOW refuses to traverse a symlink),
        # refusing with a clean PromoteFatal -- not a silent follow; (b) a
        # REAL-DIRECTORY swap of "wiki" (a brand-new, non-symlink directory
        # created under the same name) is NOT something O_NOFOLLOW can see
        # -- confirmed empirically: the os.open() call below succeeds
        # against the substituted real directory exactly as it would
        # against the original one. This is bounded, not a write-escape --
        # the substituted directory is still a real directory that must
        # itself resolve to somewhere on this filesystem, and since it was
        # created AS "real_path / wiki" it is, by construction, still
        # inside the target project's own directory tree, so the guard's
        # write still lands inside the project, just not in the original
        # "wiki" directory's original inode. (c) A wiki FILE-only swap
        # (wiki/ itself untouched) is caught by wiki_edit_guard.py's own
        # O_NOFOLLOW open of the basename within the now-already-pinned
        # dir_fd, which likewise refuses rather than follows a symlink
        # there. Once os.open() below returns successfully, the fd it
        # hands back is permanently anchored to whichever directory inode
        # it actually opened -- (a)'s original one, or (b)'s substituted
        # one -- for as long as it stays open, regardless of anything that
        # happens to the name "wiki" afterwards; the only further TOCTOU
        # window is (c)'s already-covered file-level race, which is
        # fail-closed, not fail-open. Net effect: a real, malicious swap
        # can no longer make approve report success while data lands
        # OUTSIDE the target project, in any variant -- worst case (b) is
        # a write to an unintended real directory still bounded by the
        # project root, not an escape; every other variant either lands
        # correctly inside the project or approve refuses cleanly. See
        # test_wiki_dir_fd_symlink_swap_after_recheck_refused_or_safe in
        # test_promote_capability.py for the reproduction of this exact
        # scenario against the fixed code.
        if _has_symlink_component((real_path / "wiki" / wiki_path.name).absolute(), real_path.absolute()):
            raise PromoteFatal("wiki_path_symlink_introduced", str(wiki_path))
        if not identity_unchanged(wiki_path, identity):
            raise PromoteFatal("concurrent_modification_detected", str(wiki_path))

        wiki_dir_fd_for_guard = _open_wiki_dir_fd_for_guard(real_path)
        try:
            guard_result = invoke_wiki_edit_guard(guard_path, wiki_path, new_payload, wiki_dir_fd=wiki_dir_fd_for_guard)
        finally:
            try:
                os.close(wiki_dir_fd_for_guard)
            except OSError:
                pass
    finally:
        if knowledge_dir_fd is not None:
            try:
                os.close(knowledge_dir_fd)
            except OSError:
                pass

    requires_resign = check_requires_manifest_resign(real_path, "wiki/orca-context-wiki.json")

    git_committed: bool | None = None
    git_commit_sha: str | None = None
    git_commit_error: str | None = None
    if do_commit:
        relpaths = ["wiki/orca-context-wiki.json"]
        if knowledge_md_relpath is not None:
            relpaths.append(knowledge_md_relpath)
        message = (
            f"promote_capability: approve {record['candidate_id']} (orca-context-wiki.json)\n\n"
            f"approved_by: {approved_by}\n"
            f"rationale: {rationale}\n"
            f"candidate_id: {record['candidate_id']}\n"
        )
        git_committed, git_commit_sha, git_commit_error = git_commit_paths(real_path, relpaths, message)
    else:
        git_committed = False

    return {
        "proposed_target": "orca-context-wiki.json",
        "wiki_path": str(wiki_path),
        "knowledge_md_relpath": knowledge_md_relpath,
        "new_content_version": old_version + 1,
        "guard_resign_required": bool(guard_result.get("resign_required")) if isinstance(guard_result, dict) else None,
        "requires_manifest_resign": requires_resign,
        "git_committed": git_committed,
        "git_commit_sha": git_commit_sha,
        "git_commit_error": git_commit_error,
    }


def _revalidate_record_for_approve(record: dict[str, Any]) -> None:
    """round-2-fix P0, primary fix: re-run the EXACT SAME schema/format
    validation draft (or amend) already ran, against the record as it
    stands on disk right now, before any of its fields are used to build a
    filesystem path.

    Why this exists: draft/amend validate their input once, at draft time,
    and then write the resulting record to a JSON file under
    PROMOTION_ROOT. `approve` later reads that SAME file back
    (find_candidate()) and trusted it completely -- nothing between draft
    and approve re-checks that the file's contents still look like
    something draft would have produced. If that file is edited on disk in
    between (a bug elsewhere, a compromised process with write access to
    PROMOTION_ROOT, or simple corruption), approve had no independent
    opinion of its own and would happily use a tampered "id" to build a
    write path (see resolve_knowledge_md_path's docstring for the concrete
    exploit this closes: id="../../../OUTSIDE/PWNED" survives untouched
    from tamper to write). Reusing validate_capability_candidate_input()/
    validate_knowledge_candidate_input() here (rather than inventing a
    parallel set of checks) is deliberate: those functions ARE the
    authority on what a well-formed candidate looks like, and a second,
    independently-written check function is exactly the kind of thing that
    silently drifts out of sync with the first one (see KIND_VALUES's own
    documented history of that failure mode in this same file).

    Raises PromoteValidationError/PromoteUsageError-shaped
    PromoteValidationError on anything that fails; callers should let it
    propagate (approve has not written anything yet at the point this is
    called)."""
    target_project = record.get("target_project")
    proposed_target = record.get("proposed_target")
    operation = record.get("operation")

    if operation not in OPERATION_VALUES:
        raise PromoteValidationError("bad_operation", f"operation must be one of {list(OPERATION_VALUES)}, got {operation!r}")
    if proposed_target not in PROPOSED_TARGETS:
        raise PromoteValidationError("bad_proposed_target", f"proposed_target must be one of {list(PROPOSED_TARGETS)}, got {proposed_target!r}")
    # target_project itself is never used to build a write path (the actual
    # write target comes from catalog.json's own project_roots lookup, a
    # dict-key lookup, not a path join) -- still re-validated here for
    # defense in depth and because it IS used to build this tool's own
    # PROMOTION_ROOT-confined candidate/content paths (project_dir(),
    # candidate_path(), content_md_path()).
    _validate_target_project(target_project)

    if operation == "amend_depends_on":
        # round-2-fix round-2 (post-NO-GO): cmd_amend() hardcodes
        # proposed_target to "reusable-capabilities.json" -- it is never
        # legitimately "orca-context-wiki.json" for this operation. Without
        # this check, a record whose on-disk "operation" is tampered/left as
        # "amend_depends_on" but whose "proposed_target" is changed to
        # "orca-context-wiki.json" sails through this branch (which never
        # looks at proposed_target) and returns having validated NOTHING
        # that _approve_orca_context_wiki() is about to read: that function
        # unconditionally uses record["id"]/record["title"]/record["path"]/
        # record["wiki_status"], all of which are None on an amend record
        # (see cmd_amend), and writes+commits a page of literal nulls into
        # the target project's real wiki file before anything catches it.
        if proposed_target != "reusable-capabilities.json":
            raise PromoteValidationError(
                "bad_proposed_target_for_operation",
                "operation 'amend_depends_on' must have proposed_target "
                f"'reusable-capabilities.json', got {proposed_target!r}",
            )
        # amend candidates carry no id/kind/name/path/summary of their own
        # (see cmd_amend) -- re-validate the amend-specific fields using
        # the identical checks cmd_amend itself already applies to them.
        target_kind = record.get("amend_target_kind")
        target_name = record.get("amend_target_name")
        ref = record.get("amend_add_depends_on")
        if target_kind not in KIND_VALUES:
            raise PromoteValidationError("bad_target_kind", f"amend_target_kind must be one of {list(KIND_VALUES)}, got {target_kind!r}")
        if not _is_plain_str(target_name) or not target_name or _has_control_chars(target_name):
            raise PromoteValidationError("bad_target_name", "amend_target_name must be a non-empty, control-character-free string")
        if not _is_plain_str(ref) or not ref or ref != ref.strip() or _has_control_chars(ref):
            raise PromoteValidationError("bad_add_depends_on", "amend_add_depends_on must be a non-empty, trimmed, control-character-free string")
        ref_parts = ref.split(":")
        if len(ref_parts) not in (2, 3) or any(not p for p in ref_parts):
            raise PromoteValidationError("bad_add_depends_on_grammar", "amend_add_depends_on must be \"<kind>:<name>\" or \"<project>:<kind>:<name>\"")
        if ref_parts[-2] not in KIND_VALUES:
            raise PromoteValidationError("bad_add_depends_on_kind", f"kind must be one of {list(KIND_VALUES)}")
        if len(ref_parts) == 2 and f"{ref_parts[0]}:{ref_parts[1]}" == f"{target_kind}:{target_name}":
            raise PromoteValidationError("self_reference", "amend_add_depends_on must not name the amend target itself")
        return

    # Reuse validate_source() itself rather than re-checking source's shape
    # ad hoc -- same "don't reinvent" discipline as everything else in this
    # function. validate_source() reads payload["source"], so build a
    # minimal payload for it up front.
    source = validate_source({"source": record.get("source")})

    if proposed_target == "reusable-capabilities.json":
        payload = {
            "target_project": target_project,
            "proposed_target": proposed_target,
            "source": source,
            "id": record.get("id"),
            "kind": record.get("kind"),
            "name": record.get("name"),
            "path": record.get("path"),
            "summary": record.get("summary"),
            "last_verified_at": record.get("last_verified_at"),
            "depends_on": record.get("depends_on"),
        }
        validate_capability_candidate_input(payload)
    else:  # "orca-context-wiki.json"
        payload = {
            "target_project": target_project,
            "proposed_target": proposed_target,
            "source": source,
            "id": record.get("id"),
            "title": record.get("title"),
            "path": record.get("path"),
            "summary": record.get("summary"),
            "wiki_status": record.get("wiki_status"),
            # content_md itself is not re-validated here: the record never
            # stores the body text (only has_content_md/content_md_relpath
            # -- see cmd_draft), and the body is not used to build any
            # filesystem path (only "id" is, via resolve_knowledge_md_path).
            # It is re-read from PROMOTION_ROOT's own staged content.md file
            # further down in _approve_orca_context_wiki, whose read path is
            # built from candidate_id and target_project (re-validated
            # above), not from "id". candidate_id/target_project are NOT
            # re-checked here for well-formedness on their own -- they are
            # instead cross-checked in cmd_approve/_terminal_transition, by
            # _verify_record_matches_found_location(), against the actual
            # directory/filename find_candidate() found this record under
            # (see that function's docstring for why a field-level format
            # check alone is not enough here).
            "content_md": None,
        }
        validate_knowledge_candidate_input(payload)


def _verify_record_matches_found_location(root: Path, record: dict[str, Any], record_path: Path) -> None:
    """round-2-fix round-2 (post-NO-GO hardening).

    find_candidate() locates a candidate purely by scanning PROMOTION_ROOT's
    subdirectories for a file literally named "<the CLI-supplied
    candidate_id>.json" -- it never checks that the JSON record it reads
    back agrees, in its OWN "target_project"/"candidate_id" fields, with the
    directory/filename it was actually found under. Three call sites rebuild
    a path FROM those record fields rather than reusing record_path itself:

      - save_candidate() (used by cmd_approve's own final "approved" save,
        AND by _terminal_transition's "rejected"/"withdrawn" save) recomputes
        the write path from record["target_project"]/record["candidate_id"].
      - cmd_approve separately looks record["target_project"] up in
        catalog.json to decide which real project's wiki/capabilities file
        to actually mutate.
      - _approve_orca_context_wiki()'s staged-content.md lookup builds its
        read path from record["candidate_id"]/record["target_project"].

    If either field is tampered on disk in between draft/amend and
    approve/reject/withdraw, every one of those rebuilt paths silently
    diverges from where the record actually lives on disk:

      - a tampered "target_project" makes cmd_approve write the REAL target
        project's file (wiki or capabilities) for a completely different,
        merely catalog-known project than the one the candidate was staged
        against and reviewed for, and makes save_candidate() write the
        "approved" record into that other project's directory under
        PROMOTION_ROOT -- leaving the original staged file behind unchanged
        (still "pending_approval"), so the same candidate_id now exists
        twice on disk with contradictory status and a write has landed in a
        project nobody approved it for.
      - a tampered "candidate_id" makes content_md_path() look for the
        staged content.md under the WRONG id-named subdirectory. Before the
        later round-fix that moved that lookup's is_file() check to run
        BEFORE any write to the target project's real wiki file (see
        _approve_orca_context_wiki()'s own comments), this was only
        discovered *after* that function had already written the new page
        into the target project's real wiki file and bumped its
        content_version -- a partial, uncommitted write with no matching
        "approved" candidate record to show for it. That specific ordering
        gap is now closed, but this check remains the load-bearing defense
        for the sibling "tampered target_project" variant above, which the
        ordering fix does nothing for (a tampered target_project makes
        _approve_orca_context_wiki() mutate a completely different, merely
        catalog-known project's real wiki file well before any
        content_md_path() lookup ever runs) -- and is kept as defense in
        depth rather than relying on write-ordering alone to do this check's
        job. The same tampered candidate_id field also corrupts
        _terminal_transition's ledger/record the same way "target_project"
        does.

    Recomputing the expected path from the record's own fields with the
    exact same containment-checked helper draft/amend use to create it
    (candidate_path()), and requiring it to equal record_path (the file
    find_candidate() actually opened), catches both variants -- and any
    combination of the two -- before anything else runs.
    """
    try:
        expected_path = candidate_path(root, record.get("target_project"), record.get("candidate_id"))
    except PromoteFatal as exc:
        raise PromoteValidationError(
            "record_location_mismatch",
            f"target_project/candidate_id no longer resolve to a valid PROMOTION_ROOT path: {exc}",
        ) from exc
    if expected_path != record_path.resolve(strict=False):
        raise PromoteValidationError(
            "record_location_mismatch",
            "record's target_project/candidate_id fields do not match the file it was found in "
            "(the on-disk record was likely tampered with after draft/amend)",
        )


def cmd_approve(args: argparse.Namespace) -> dict[str, Any]:
    approved_by = (args.approved_by or "").strip()
    rationale = (args.rationale or "").strip()
    if not approved_by:
        raise PromoteUsageError("empty_approved_by")
    if not rationale:
        raise PromoteUsageError("empty_rationale")
    if not args.catalog.strip():
        raise PromoteUsageError("empty_catalog_path")
    if args.no_commit and not args.i_understand_this_leaves_an_uncommitted_tracked_path:
        raise PromoteUsageError(
            "no_commit_requires_acknowledgement",
            "--no-commit requires --i-understand-this-leaves-an-uncommitted-tracked-path",
        )

    root = ensure_promotion_root()
    # THE SINGLE WRITE EXCEPTION's own concurrency guard. Without this lock,
    # two concurrent `approve` invocations against the SAME target project's
    # wiki file each do their own read -> build-new-doc -> identity-recheck
    # -> write; both identity rechecks can observe "unchanged" if neither
    # process has written yet by the time the other checks, and whichever
    # process's os.replace() lands second silently overwrites the first
    # process's already-"approved" write with a document built from stale
    # bytes -- a real, demonstrated lost-update race with no error, no
    # rollback, and no trace (the losing candidate's own record still says
    # status: approved). Holding this same PROMOTION_ROOT lock (already used
    # by draft/amend) across the read-check-write sequence below, and across
    # the candidate-record status transition that follows it, serializes
    # every `approve`/`draft`/`amend` invocation of THIS tool against each
    # other and closes that race. This does not protect against some other
    # process editing the target file directly outside this tool -- that
    # residual risk is unchanged and is what identity_unchanged()'s own
    # concurrent_modification_detected check still exists for.
    lock_path = acquire_lock(root)
    try:
        found = find_candidate(root, args.candidate_id)
        if found is None:
            raise PromoteValidationError("candidate_not_found", args.candidate_id)
        record, record_path = found

        # round-2-fix round-2 (post-NO-GO): confirm the record's own
        # target_project/candidate_id fields still agree with the file
        # find_candidate() actually found it under, BEFORE anything else
        # (including the status check) trusts either field. See
        # _verify_record_matches_found_location()'s own docstring for the
        # exact target_project-redirect and candidate_id-mismatch exploits
        # this closes.
        _verify_record_matches_found_location(root, record, record_path)

        if record.get("status") != "pending_approval":
            raise PromoteValidationError("candidate_not_pending", f"status is {record.get('status')!r}")

        # round-2-fix P0: re-validate every field this candidate record
        # carries, using the SAME checks draft/amend already ran, before
        # any of it is trusted to build a filesystem path. See
        # _revalidate_record_for_approve()'s own docstring and
        # resolve_knowledge_md_path()'s docstring for the exact exploit
        # this closes. Deliberately placed BEFORE the catalog is even
        # loaded -- a record that fails this check is refused on its own
        # terms, independent of anything catalog.json says.
        _revalidate_record_for_approve(record)

        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        mechanism = source.get("mechanism")
        if mechanism != "human" and not args.confirm_non_human_source:
            raise PromoteUsageError(
                "confirm_non_human_source_required",
                f"candidate source.mechanism is {mechanism!r}; pass --confirm-non-human-source to approve it",
            )

        catalog = load_catalog(Path(args.catalog).expanduser())
        project_roots = build_project_root_index(catalog)
        target_project = record["target_project"]
        real_path = project_roots.get(target_project)
        if real_path is None:
            raise PromoteFatal("target_project_unknown_in_catalog", target_project)
        if not real_path.is_dir():
            raise PromoteFatal("target_project_real_path_missing", str(real_path))

        proposed_target = record["proposed_target"]
        if proposed_target == "reusable-capabilities.json":
            write_result = _approve_reusable_capabilities(record, real_path, approved_by, rationale)
        elif proposed_target == "orca-context-wiki.json":
            write_result = _approve_orca_context_wiki(
                record, real_path, approved_by, rationale, do_commit=not args.no_commit
            )
        else:
            raise PromoteFatal("candidate_proposed_target_invalid", proposed_target)

        decided_at = now_iso()
        record["status"] = "approved"
        record["approved_by"] = approved_by
        record["rationale"] = rationale
        record["decided_at"] = decided_at
        record["updated_at"] = decided_at
        record["approve_result"] = write_result
        save_candidate(root, record)
        append_ledger(
            root,
            {
                "ts": decided_at,
                "action": "approve",
                "candidate_id": record["candidate_id"],
                "target_project": target_project,
                "proposed_target": proposed_target,
                "status": "approved",
                "approved_by": approved_by,
            },
        )
    finally:
        release_lock(lock_path)

    result = {
        "candidate_id": record["candidate_id"],
        "status": "approved",
        "target_project": target_project,
        **write_result,
    }
    return result


# ---------------------------------------------------------------------------
# cmd_reject / cmd_withdraw
# ---------------------------------------------------------------------------


def _terminal_transition(args: argparse.Namespace, *, new_status: str, action: str) -> dict[str, Any]:
    decided_by = (args.decided_by or "").strip()
    rationale = (args.rationale or "").strip()
    if not decided_by:
        raise PromoteUsageError("empty_decided_by")
    if not rationale:
        raise PromoteUsageError("empty_rationale")

    root = ensure_promotion_root()
    # round-2-fix P1: this read-check-write sequence used to run with NO
    # lock at all, while draft/amend/approve all serialize against each
    # other under the same PROMOTION_ROOT lock. That let reject/withdraw
    # race a concurrent approve on the SAME candidate: approve can finish
    # writing the target project's wiki file and be partway through saving
    # its own "status: approved" candidate record when an unlocked
    # reject/withdraw reads the still-"pending_approval" record, decides it
    # is a valid transition, and writes "status: rejected" over it --
    # either clobbering approve's own write outright, or leaving a
    # "rejected" record for a candidate whose wiki write actually landed on
    # disk (the two states most needed to stay consistent for this ledger
    # to mean anything). Holding the same lock draft/amend/approve already
    # use makes this transition's own read-check-write atomic with respect
    # to every other subcommand of this tool.
    lock_path = acquire_lock(root)
    try:
        found = find_candidate(root, args.candidate_id)
        if found is None:
            raise PromoteValidationError("candidate_not_found", args.candidate_id)
        record, record_path = found

        # round-2-fix round-2 (post-NO-GO): same integrity check cmd_approve
        # now runs -- see _verify_record_matches_found_location()'s
        # docstring. Without it, a tampered "target_project"/"candidate_id"
        # makes this transition's own save_candidate() call below write the
        # "rejected"/"withdrawn" record into a different PROMOTION_ROOT
        # subdirectory than the one it was actually found in, leaving the
        # original file behind still showing "pending_approval" -- the same
        # split-record inconsistency the approve path is protected against.
        _verify_record_matches_found_location(root, record, record_path)

        if record.get("status") != "pending_approval":
            raise PromoteValidationError("candidate_not_pending", f"status is {record.get('status')!r}")

        decided_at = now_iso()
        record["status"] = new_status
        record["decided_by"] = decided_by
        record["rationale"] = rationale
        record["decided_at"] = decided_at
        record["updated_at"] = decided_at
        save_candidate(root, record)
        append_ledger(
            root,
            {
                "ts": decided_at,
                "action": action,
                "candidate_id": record["candidate_id"],
                "target_project": record["target_project"],
                "proposed_target": record["proposed_target"],
                "status": new_status,
                "decided_by": decided_by,
            },
        )
    finally:
        release_lock(lock_path)
    return {"candidate_id": record["candidate_id"], "status": new_status}


def cmd_reject(args: argparse.Namespace) -> dict[str, Any]:
    return _terminal_transition(args, new_status="rejected", action="reject")


def cmd_withdraw(args: argparse.Namespace) -> dict[str, Any]:
    return _terminal_transition(args, new_status="withdrawn", action="withdraw")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capability/knowledge promotion pipeline (M8-3, Gate B) -- the one M8 tool that writes into another project's own wiki/ files."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    draft = subparsers.add_parser("draft", help="Validate + dedup a hand-authored candidate; stage it as pending_approval.")
    draft.add_argument("--from-json", type=str, required=True, dest="from_json")
    draft.add_argument("--catalog", type=str, required=True)
    draft.add_argument("--json", action="store_true")

    amend = subparsers.add_parser("amend", help="Draft an amend_depends_on candidate (still requires approve).")
    amend.add_argument("--target-project", type=str, required=True, dest="target_project")
    amend.add_argument("--target", type=str, required=True, help='"<kind>:<name>" of the already-published entry to amend')
    amend.add_argument("--add-depends-on", type=str, required=True, dest="add_depends_on")
    amend.add_argument("--source", type=str, required=True, help="free text, e.g. \"human\" or a future signal's own identifier")
    amend.add_argument("--catalog", type=str, required=True)
    amend.add_argument("--json", action="store_true")

    approve = subparsers.add_parser("approve", help="THE write path: apply a pending_approval candidate to its target project's wiki/.")
    approve.add_argument("--candidate-id", type=str, required=True, dest="candidate_id")
    approve.add_argument("--catalog", type=str, required=True)
    approve.add_argument("--approved-by", type=str, required=True, dest="approved_by")
    approve.add_argument("--rationale", type=str, required=True)
    approve.add_argument("--confirm-non-human-source", action="store_true", dest="confirm_non_human_source")
    approve.add_argument("--no-commit", action="store_true", dest="no_commit")
    approve.add_argument(
        "--i-understand-this-leaves-an-uncommitted-tracked-path",
        action="store_true",
        dest="i_understand_this_leaves_an_uncommitted_tracked_path",
    )
    approve.add_argument("--json", action="store_true")

    reject = subparsers.add_parser("reject", help="Terminal transition: pending_approval -> rejected.")
    reject.add_argument("--candidate-id", type=str, required=True, dest="candidate_id")
    reject.add_argument("--decided-by", type=str, required=True, dest="decided_by")
    reject.add_argument("--rationale", type=str, required=True)
    reject.add_argument("--json", action="store_true")

    withdraw = subparsers.add_parser("withdraw", help="Terminal transition: pending_approval -> withdrawn.")
    withdraw.add_argument("--candidate-id", type=str, required=True, dest="candidate_id")
    withdraw.add_argument("--decided-by", type=str, required=True, dest="decided_by")
    withdraw.add_argument("--rationale", type=str, required=True)
    withdraw.add_argument("--json", action="store_true")

    return parser


def _print_result(args: argparse.Namespace, result: dict[str, Any]) -> None:
    payload = {"ok": True, **result}
    if getattr(args, "json", False):
        print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False, indent=1)))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")

    # UNSUPPRESSABLE notices -- there is deliberately no --quiet flag on any
    # subcommand in this tool (see module docstring), but these two lines
    # are printed unconditionally to stderr as well, specifically so no
    # future flag addition can accidentally make them go away.
    if result.get("requires_manifest_resign"):
        print(
            "NOTICE: this write leaves the target project's reviewed-startup-pack-manifest.json "
            "pinning a now-stale wiki hash -- its next SessionStart will fail closed with "
            "'central reviewed source freshness mismatch: wiki' until a human re-signs that "
            "manifest via the existing manual procedure. This tool does not re-sign it.",
            file=sys.stderr,
        )
    if result.get("git_committed") is False and result.get("proposed_target") == "orca-context-wiki.json":
        reason = result.get("git_commit_error") or "commit was skipped via --no-commit"
        print(
            f"NOTICE: the wiki write succeeded but was NOT committed ({reason}). "
            "The target project now has an uncommitted tracked-path change under wiki/ -- "
            "commit it manually before that project's next SessionStart to avoid a NACK.",
            file=sys.stderr,
        )


def _emit_error(args: argparse.Namespace, code: int, reason: str, message: Any = None) -> int:
    payload: dict[str, Any] = {"ok": False, "exit_code": code, "reason": reason}
    if message is not None:
        payload["message"] = message
    if getattr(args, "json", False):
        print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False)), file=sys.stderr)
    else:
        suffix = f" ({message})" if message else ""
        print(f"error: {reason}{suffix}", file=sys.stderr)
    return code


_COMMANDS = {
    "draft": cmd_draft,
    "amend": cmd_amend,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "withdraw": cmd_withdraw,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = _COMMANDS.get(args.command)
    if handler is None:
        return 2
    try:
        result = handler(args)
    except PromoteUsageError as exc:
        return _emit_error(args, 2, exc.reason, exc.message)
    except PromoteValidationError as exc:
        return _emit_error(args, 1, exc.reason, exc.details)
    except PromoteFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)
    except BaseException as exc:
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4
    _print_result(args, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
