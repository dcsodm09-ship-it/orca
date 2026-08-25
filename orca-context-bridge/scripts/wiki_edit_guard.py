#!/usr/bin/env python3
"""Guard writes to wiki/orca-context-wiki.json against unsigned drift.

The Orca startup gate pins wiki/orca-context-wiki.json's sha256 inside the
manually re-signed reviewed-startup-pack-manifest.json (schema v2, the file
that actually governs the live SessionStart hook today). Any edit to the
wiki changes that hash and makes the next SessionStart fail closed with
"central reviewed source freshness mismatch: wiki" until an operator
re-signs the manifest by hand.

This module is a pre-write guard, not a re-signer: guard_wiki_write() only
decides whether a proposed wiki write is allowed and whether it is
semantic (page/link content changed) or a pure timestamp/verification
touch-up. It never re-signs reviewed-startup-pack-manifest.json itself --
that stays a deliberate, separate, manual security-authority action (see
the re-sign notice this CLI prints, and the repo's existing manual
procedure) so that a write barrier can never silently re-authorize its own
writes.

Contract enforced by guard_wiki_write():
  - meta.content_version and meta.updated_at are the only version-control
    fields this guard understands. They live under a top-level "meta" key.
  - A wiki with no "meta" key at all has never been through this guard.
    The one-time bootstrap exception (allow_bootstrap=True / --bootstrap)
    lets an operator add meta={"content_version": 1, "updated_at": ...} on
    top of an existing wiki, but ONLY if doing so changes nothing else --
    every page and link must be byte-for-byte unchanged in the same write.
  - Once meta exists, every later write must either:
      * leave semantic content unchanged, in which case content_version
        must stay exactly the same (verification passes must not bump the
        version), and updated_at must not move backwards; or
      * change semantic content, in which case content_version must
        advance by exactly 1, and updated_at must be strictly newer than
        the prior value.
  - "Semantic" is decided by a deny-list projection (compute_semantic_diff),
    SCOPED to two exact locations, not a blanket recursive key-name strip:
      * meta.content_version and meta.updated_at specifically -- nothing
        else inside meta, and these two names count as semantic everywhere
        else in the document.
      * The six known per-entry bookkeeping fields (created_at, updated_at,
        verified_at, verification_status, source_mtime, migration_sequence),
        only when they are a direct key of one pages[]/links[] element --
        not nested inside one, not at the top level, not inside meta.
    Everywhere else, those same key names -- and any unknown/new field --
    are left alone and therefore count as semantic if present. This is
    deliberately fail-safe over a hand-maintained allow-list, and the exact
    two-location scoping is what stops "meta" (or the top level, or a
    nested object under an unrelated key) from being usable as a
    laundering channel for a change that should have required a
    content_version bump (round-2 fix, 2026-08-22: an earlier revision
    stripped these key names recursively at any depth/location, which a
    dual review caught as exploitable).
  - resign_required_for() compares the write's SERIALIZED BYTES (what
    reviewed-startup-pack-manifest.json's shared_source_sha256s.wiki
    actually pins), not semantic content. A write guard_wiki_write() allows
    as "non-semantic" (e.g. only meta.updated_at moved) still changes those
    bytes and therefore still requires a re-sign -- the only write that
    never requires one is a byte-for-byte no-op (round-2 fix: an earlier
    revision used compute_semantic_diff() here, which reported "no re-sign
    needed" for exactly the writes that most needed one).

This module is a write-time barrier, not an enforced choke point: nothing
prevents a future edit from bypassing it and writing wiki/orca-context-wiki.json
directly (a human, an editor, a script that doesn't know this exists). Any
such bypass is still caught, just later -- by check_wiki_freshness.py or the
next SessionStart's existing sha256 check -- which is exactly the two-layer
design's second, event-after layer. Treat "all wiki edits go through this
guard's --apply" as a documented convention this repo's operators/agents
must follow, not a technical guarantee this module enforces on its own.

round-6 fix (2026-08-25): this module's standalone CLI used to accept only
`--wiki <path>` and immediately do `.expanduser().resolve(strict=False)` on
it (see main()) before ever reading or writing the file -- resolving
FOLLOWS a symlink at the final path component, which erases, before any
protection could matter, the one piece of information ("this name is a
symlink, not a regular file") that would let a caller refuse it. A
dedicated review of promote_capability.py's approve path (the one caller
that matters in production) measured this as a real, empirically-winnable
race: swap the target project's wiki/orca-context-wiki.json (or wiki/
itself) for a symlink to an outside location in the window between
promote_capability.py's own last re-check and this subprocess's own path
resolution, and the write lands outside the project while approve still
reports success. Two independent hardenings were added for this, neither
changing guard_wiki_write()'s validation logic at all:
  1. A NEW, OPTIONAL dir-fd mode (`--wiki-dir-fd`/`--wiki-name`, additive,
     mutually exclusive with `--wiki`): the caller passes an
     already-opened, already-validated directory file descriptor for the
     wiki file's PARENT directory (obtained with O_NOFOLLOW at the exact
     instant its own containment check passed) via subprocess pass_fds,
     plus the wiki file's plain basename. Every filesystem operation this
     module performs in that mode -- the read of the current file and the
     atomic write of the new one -- is anchored to that fd (dir_fd=) and
     never re-resolves or re-opens "wiki/<name>" by path string. A dir_fd
     stays pinned to the directory's inode regardless of what its name
     later resolves to, so a rename/symlink-swap of that name (or any
     ancestor of it) after the fd was opened cannot redirect these
     operations -- see promote_capability.py's own
     `_open_wiki_dir_fd_for_guard()`/`invoke_wiki_edit_guard()` for the
     caller side of this contract.
  2. The EXISTING standalone `--wiki <path>` mode was independently
     hardened too, at zero behavioral cost to the normal case: the wiki
     path's own final component is no longer collapsed through
     Path.resolve() before use (only its parent directory is, same as
     before, for path normalization), and the read of it uses O_NOFOLLOW.
     A manual/standalone invocation now refuses outright (a clean usage
     error, not a silent follow) if the wiki file itself has been swapped
     for a symlink -- it cannot protect against a swap of an ANCESTOR
     directory the way the dir-fd mode can, since a standalone invocation
     has no pre-validated fd for anything above the file itself.
Neither hardening changes `--wiki <path>` standalone behavior for a wiki
file that is a plain regular file (the overwhelmingly common case, and the
only case this module's own manual re-sign procedure and SKILL.md exercise
by hand) -- see CliRealBytesTests/DirFdModeTests in
test_wiki_edit_guard.py.

Out of scope for this module (left for a separately-scoped follow-up):
  - The full per-page/per-link timestamp migration (created_at, verified_at,
    verification_status, source_mtime, migration_sequence, source_missing).
    Nothing here writes or reads those fields on individual pages/links.
  - Any change to the reviewed-startup-pack-manifest.json schema itself, or
    to build_startup_bundle.py's verify_reviewed_pack(). shared_source_sha256s.wiki
    stays a plain 64-hex-character string in this delivery's scope.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shlex
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Deliberately ZERO sibling imports from build_startup_bundle.py or
# build_context_digest.py (round-2 fix, 2026-08-22: an earlier revision
# imported now_iso, unused dead weight, and write_private, whose
# implementation differs materially between the deployed
# ~/.agents/skills/... copy and this workspace's tracked copy -- a dual
# review caught that importing it would make this guard's write-safety
# silently depend on which copy happens to be co-located wherever it's
# deployed). Everything this module needs is implemented locally below.


# Dropped only from the top-level "meta" object's own keys (not recursed
# elsewhere, not applied to anything but meta itself). These are the
# guard's own control fields -- excluding both is what makes "old has no
# meta" compare equal to "new has meta containing only these two control
# fields", which is the mechanism that classifies a bootstrap write as
# non-semantic. Any OTHER key placed inside meta -- or either of these two
# key names appearing anywhere other than directly inside meta -- survives
# the projection and is therefore semantic: meta is not a laundering
# channel (round-2 fix, 2026-08-22: an earlier revision only excluded
# content_version here and relied on a blanket recursive strip to also
# catch meta.updated_at, which meant every one of these key names was
# exempted everywhere in the document, not just inside meta -- a dual
# review caught this as exploitable; see PAGE_LINK_EXCLUDED_KEYS below for
# the correspondingly scoped fix to the other five bookkeeping fields).
META_ONLY_EXCLUDED_KEYS = frozenset({"content_version", "updated_at"})

# Dropped only from a pages[]/links[] element's own direct keys (not
# recursed into a nested value under one of these names, not applied
# anywhere else in the document -- including not inside meta, see
# META_ONLY_EXCLUDED_KEYS above for that key name's own scoped exclusion).
# These are bookkeeping fields the §1/§3 per-page timestamp migration (out
# of scope here) will eventually populate; excluding them at exactly this
# one location now means that migration will not need to touch this guard
# when it lands, without also creating a location-blind exemption a bug or
# a bad-faith write could smuggle an unrelated change through.
PAGE_LINK_EXCLUDED_KEYS = frozenset(
    {
        "created_at",
        "updated_at",
        "verified_at",
        "verification_status",
        "source_mtime",
        "migration_sequence",
    }
)

MIN_CONTENT_VERSION = 1
MAX_CONTENT_VERSION = 2**31 - 1

# A write whose meta.updated_at is further than this into the future is
# refused outright (see guard_wiki_write). Not part of the original design
# note's literal contract -- added because without a bound, one write with
# a year-2099 timestamp would permanently deadlock the "strictly newer"
# rule for every subsequent write.
MAX_FUTURE_SKEW_SECONDS = 300

# Mirrors check_wiki_freshness.py's knowledge-root inference so --wiki's
# default resolves to the same file that checker verifies. Kept as literal
# relative paths here (not imported constants) to hold this script to the
# minimal import list above.
_KNOWLEDGE_ROOT_MARKER_WIKI = Path("wiki") / "orca-context-wiki.json"
_KNOWLEDGE_ROOT_MARKER_MANIFEST = Path(".orca") / "context" / "reviewed-startup-pack-manifest.json"


def parse_timestamp(value: object) -> datetime | None:
    """Parse a timezone-aware ISO 8601 timestamp, or return None.

    Only accepts str input. Python 3.9's datetime.fromisoformat cannot
    parse a trailing "Z", so one is rewritten to "+00:00" first. A
    timestamp that parses but carries no tzinfo is rejected (None) rather
    than assumed to be UTC -- an ambiguous timestamp must not silently
    participate in the strictly-newer comparison below.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _project_entry(entry: Any) -> Any:
    """Strip PAGE_LINK_EXCLUDED_KEYS from one pages[]/links[] element's own
    direct keys. Applied at exactly this one level to exactly this one kind
    of location -- not recursive into a nested value, not applied to
    anything that isn't literally an element of pages[] or links[].
    """
    if not isinstance(entry, dict):
        return entry
    return {key: value for key, value in entry.items() if key not in PAGE_LINK_EXCLUDED_KEYS}


def _project_for_semantic_diff(payload: dict) -> dict:
    """Project a wiki payload down to what counts as semantic content.

    Two SCOPED exclusions, not a blanket recursive key-name strip:
      - meta.content_version and meta.updated_at specifically (see
        META_ONLY_EXCLUDED_KEYS).
      - The six per-entry bookkeeping fields, only as direct keys of a
        pages[]/links[] element (see PAGE_LINK_EXCLUDED_KEYS).
    Every other key, at every other location -- the top level, inside meta
    under any other name, nested inside an injected structure -- is left
    exactly as-is. An unknown/new field, or one of the excluded field names
    appearing somewhere other than its one designated location, therefore
    counts as semantic by default: fail-safe over a hand-maintained
    allow-list, and what keeps meta (or anywhere else) from being usable to
    launder a change that should have required a content_version bump.
    """
    projected: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "meta" and isinstance(value, dict):
            meta_projected = {k: v for k, v in value.items() if k not in META_ONLY_EXCLUDED_KEYS}
            if meta_projected:
                projected["meta"] = meta_projected
            # else: meta present but reduces to empty after stripping its
            # own two control fields -> omit the key entirely, which is
            # what makes "no meta key at all" compare equal to "meta
            # present with only content_version/updated_at" (the bootstrap
            # write's non-semantic classification).
        elif key in ("pages", "links") and isinstance(value, list):
            projected[key] = [_project_entry(item) for item in value]
        else:
            projected[key] = value
    return projected


def compute_semantic_diff(old: dict, new: dict) -> bool:
    """Return True iff old and new differ after the scoped bookkeeping-field
    projection above.

    Key order inside a JSON object is not semantic (json.dumps(...,
    sort_keys=True) normalizes it); array order IS semantic, so reordering
    pages[] or links[] correctly counts as a semantic change.
    """
    old_projected = _project_for_semantic_diff(copy.deepcopy(old))
    new_projected = _project_for_semantic_diff(copy.deepcopy(new))
    old_encoded = json.dumps(old_projected, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    new_encoded = json.dumps(new_projected, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return old_encoded != new_encoded


def guard_wiki_write(old_payload: dict, new_payload: dict, *, allow_bootstrap: bool = False) -> None:
    """Decide whether a proposed wiki write is allowed.

    Pure validation: no I/O, nothing is written here. Returns None on
    acceptance; raises ValueError with a human-readable reason on refusal.
    Callers must not catch the exception and write anyway.
    """
    if not isinstance(old_payload, dict):
        raise ValueError("old wiki payload must be a JSON object")
    if not isinstance(new_payload, dict):
        raise ValueError("new wiki payload must be a JSON object")

    old_has_meta = "meta" in old_payload

    new_meta = new_payload.get("meta")
    if not isinstance(new_meta, dict):
        raise ValueError("new wiki payload must have a meta object")
    new_version = new_meta.get("content_version")
    if (
        not isinstance(new_version, int)
        or isinstance(new_version, bool)
        or not (MIN_CONTENT_VERSION <= new_version <= MAX_CONTENT_VERSION)
    ):
        raise ValueError(
            f"new wiki meta.content_version must be an integer between "
            f"{MIN_CONTENT_VERSION} and {MAX_CONTENT_VERSION}"
        )
    new_updated_at = parse_timestamp(new_meta.get("updated_at"))
    if new_updated_at is None:
        raise ValueError("new wiki meta.updated_at must be a timezone-aware ISO 8601 timestamp")

    now = datetime.now(timezone.utc)
    if new_updated_at > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise ValueError(
            f"new wiki meta.updated_at is more than {MAX_FUTURE_SKEW_SECONDS} seconds in the future"
        )

    semantic_changed = compute_semantic_diff(old_payload, new_payload)

    if not old_has_meta:
        # Branch A -- bootstrap. Opt-in only: a stale or truncated read of
        # old_payload (e.g. old momentarily missing its meta key for some
        # unrelated reason) can never silently re-baseline the counter to 1
        # unless the caller explicitly asked for the bootstrap exception.
        if not allow_bootstrap:
            raise ValueError("old wiki content_version missing or invalid — refuse to guess a baseline")
        if semantic_changed:
            raise ValueError(
                "bootstrap write must not change page/link content in the same write — "
                "bootstrap meta alone first, then make content edits as normal guarded writes"
            )
        if new_version != 1:
            raise ValueError(f"bootstrap wiki meta.content_version must be exactly 1, got {new_version}")
        return None

    # Branch B -- normal (old already has a meta key).
    old_meta = old_payload.get("meta")
    old_version = old_meta.get("content_version") if isinstance(old_meta, dict) else None
    if not isinstance(old_version, int) or isinstance(old_version, bool):
        raise ValueError("old wiki content_version missing or invalid — refuse to guess a baseline")

    if semantic_changed:
        if new_version != old_version + 1:
            raise ValueError(
                f"semantic wiki change requires meta.content_version to advance by exactly 1 "
                f"(old={old_version}, new={new_version})"
            )
        old_updated_at = parse_timestamp(old_meta.get("updated_at"))
        if old_updated_at is None or new_updated_at <= old_updated_at:
            raise ValueError("meta.updated_at must be a valid timestamp strictly newer than the prior value")
    else:
        if new_version != old_version:
            raise ValueError(
                f"only timestamp/verification fields changed but content_version moved "
                f"(old={old_version}, new={new_version}) — verification passes must not bump content_version"
            )
        # Addition beyond the bare "version unchanged" rule: forbid the
        # timestamp itself from moving backwards even when nothing else
        # about the write is semantic.
        old_updated_at = parse_timestamp(old_meta.get("updated_at"))
        if old_updated_at is not None and new_updated_at < old_updated_at:
            raise ValueError("meta.updated_at must not move backwards")

    # DEFERRED (§1.3): validate_link_verified_at_derivation(new_payload) --
    # per-page/per-link verified_at derivation belongs to the timestamp
    # migration this delivery does not build. links here have only
    # from/to/relation, so there is nothing yet for that check to derive.

    return None


def resign_required_for(old_payload: dict, new_payload: dict) -> bool:
    """Best-effort, PAYLOAD-ONLY approximation of whether a write that
    guard_wiki_write() already allowed will leave the
    reviewed-startup-pack-manifest.json's pinned wiki hash stale.

    Only meaningful to call after guard_wiki_write() has returned
    successfully (raised nothing) for the same (old_payload, new_payload)
    pair.

    Compares SERIALIZED BYTES, not semantic content (round-2 fix,
    2026-08-22: an earlier revision compared semantic content via
    compute_semantic_diff() instead -- see the module docstring's round-2
    notes for why that was wrong). But this function re-serializes BOTH
    sides canonically; it has no access to the actual bytes on disk for
    old_payload. It is therefore only accurate when old_payload's real
    on-disk form already IS this canonical json.dumps(..., indent=2,
    ensure_ascii=False)+"\\n" serialization -- true for any wiki this
    guard itself last wrote via --apply, NOT guaranteed for a
    freshly-read file this guard has never written (including, notably,
    the --bootstrap case, and any file a human hand-edited outside the
    guard). A caller who only has parsed payloads and cannot get the raw
    on-disk text (e.g. a test, or a library caller working from an
    in-memory payload) has no better option than this approximation and
    should use it knowingly. main() below does NOT use this function for
    its own resign_required decision -- it compares the real raw bytes
    read from disk against the real bytes about to be written, which is
    the version of this check that actually matters for the CLI's
    behavior (round-2-of-round-2 fix, 2026-08-22: a dual review caught
    that this function's answer can itself be a false negative on a
    non-canonically-formatted disk file, the same failure shape as the
    bug it was written to fix).
    """
    old_serialized = json.dumps(old_payload, indent=2, ensure_ascii=False) + "\n"
    new_serialized = json.dumps(new_payload, indent=2, ensure_ascii=False) + "\n"
    return old_serialized != new_serialized


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to path atomically (temp file + os.replace).

    Preserves the target's existing file mode if it already exists;
    defaults to 0o600 for a brand-new file, matching this repo's convention
    for files that feed the reviewed-context trust chain.

    A small local helper, not a sibling import of
    build_context_digest.write_private (round-2 fix, 2026-08-22: that
    function's implementation differs materially between the deployed
    ~/.agents/skills/... copy of this skill and this workspace's tracked
    copy -- fd-hardened O_NOFOLLOW writer vs. a plain tempfile writer -- so
    importing it would make this guard's write-safety silently depend on
    which copy happens to be co-located wherever it's deployed. A dual
    review flagged this as a real risk.).

    Opened with newline="\\n" (round-3 fix, 2026-08-22: text mode with the
    default newline=None translates every "\\n" written to os.linesep on
    write, which happens to be a no-op on POSIX but was an implicit
    platform coincidence, not a guarantee -- paired with the read side's
    matching fix, see _read_json_object_with_raw()).

    Mode detection uses lstat(), not stat() (round-6 fix): stat() follows
    a symlink, so if `path`'s final component has been swapped for a
    symlink since this function's caller last looked, a plain stat() would
    silently adopt the OUTSIDE target's mode bits for the brand-new file
    this function is about to create in its place. This is not itself a
    write-escape (see the module docstring's round-6 note: os.replace()
    on a destination that is a symlink replaces the link's own directory
    entry, it never writes through to the link's target -- confirmed
    empirically), only an unnecessary information leak on top of that, and
    is closed here at zero cost to the normal (regular-file) case.
    """
    path = path.expanduser()
    try:
        lst = path.lstat()
        mode = 0o600 if stat.S_ISLNK(lst.st_mode) else lst.st_mode & 0o777
    except OSError:
        mode = 0o600
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        except BaseException:
            # os.fdopen() only takes ownership of fd once it successfully
            # returns a file object; if fdopen() itself raises, fd is not
            # yet wrapped and must be closed here explicitly, or it leaks.
            # Kept as its own try/except, separate from the `with handle:`
            # block below, specifically so this branch can never fire for
            # a failure INSIDE that block (where `with` has already closed
            # fd) and attempt a double close.
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_via_dir_fd(dir_fd: int, name: str, text: str, *, mode: int) -> None:
    """Same tmp-file+rename discipline as _atomic_write_text, but every
    operation is anchored to an already-open directory file descriptor
    (dir_fd=) instead of a path string (round-6 fix -- see the module
    docstring's round-6 note, and promote_capability.py's own
    atomic_write_in_dir(), which this function deliberately mirrors: same
    technique, copied not imported, per this codebase's convention).

    `name` is used ONLY as a dir_fd-relative name -- never joined into a
    Path or re-resolved -- so a rename/symlink-swap of `name` (or of
    whatever directory `dir_fd` used to be reachable under) after the
    caller obtained dir_fd cannot redirect this write: dir_fd stays pinned
    to the specific directory inode the caller validated at open time,
    completely independent of what that directory's name resolves to
    afterwards.

    os.replace() has no dir_fd-capable form on this platform (confirmed
    empirically, same finding promote_capability.py's atomic_write_in_dir
    already documents: os.replace not in os.supports_dir_fd even though
    os.rename is). os.rename() is used instead, which is safe here because
    this whole function already assumes POSIX (O_NOFOLLOW/os.O_DIRECTORY
    do not exist on Windows either) and POSIX's rename(2) already
    atomically replaces an existing destination -- the exact guarantee
    os.replace() exists to add on top of os.rename() only for Windows.
    """
    if os.sep in name or name in (".", ".."):
        # Cannot happen given how this module's own callers build `name`
        # (always the single-component --wiki-name CLI argument, validated
        # in main() before this function is ever reached) -- asserted
        # explicitly anyway since this string is used directly as a
        # dir_fd-relative name.
        raise _CliUsageError(f"invalid dir_fd-relative wiki filename: {name!r}")
    payload = text.encode("utf-8")
    tmp_name = f".{name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"

    def _unlink_tmp() -> None:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass

    fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        try:
            os.write(fd, payload)
            os.fsync(fd)
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass
        finally:
            os.close(fd)
        os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        _unlink_tmp()
        raise


class _CliUsageError(ValueError):
    """A CLI-level usage/IO/JSON problem -- distinct from a guard refusal."""


def _read_json_object_with_raw(
    source: str, *, label: str, no_follow_symlinks: bool = False
) -> tuple[dict[str, Any], str]:
    """Read and parse a JSON object, returning (payload, raw_text_as_read).

    The raw text is the actual bytes as they exist at `source` right now --
    used by main() to compute resign_required against the real on-disk
    wiki, not a re-serialized approximation of it (see resign_required_for's
    docstring for why the approximation alone is not sufficient).

    Deliberately read_bytes().decode(), not read_text() (round-3 fix,
    2026-08-22: Path.read_text() performs universal-newline translation --
    a CRLF or lone-CR line ending on disk is silently normalized to "\\n"
    before this function ever sees it, so a payload-identical --apply
    against a CRLF-terminated wiki would compare equal to the LF-only
    canonical serialization and wrongly report no re-sign needed, even
    though the write changes the file's real bytes. Same failure shape as
    the two bugs this module's round-2 fixes closed, caught in round 3.
    stdin has no newline-translation concern the way a file path does --
    sys.stdin.read() is left as-is.).

    no_follow_symlinks (round-6 fix): when True, `source` is opened with
    O_NOFOLLOW instead of going through Path.read_bytes() -- the read
    fails outright (a clean _CliUsageError, not a silent follow) if
    `source`'s own final path component is a symlink. Used by main() only
    for the CURRENT wiki file in standalone `--wiki <path>` mode -- see the
    module docstring's round-6 note for why this only protects the file's
    own name, not an ancestor directory's.
    """
    try:
        if source == "-":
            raw = sys.stdin.read()
        elif no_follow_symlinks:
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(Path(source).expanduser()), flags)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise OSError(f"{source} is not a regular file")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(fd, 1 << 20)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(fd)
            raw = b"".join(chunks).decode("utf-8")
        else:
            raw = Path(source).expanduser().read_bytes().decode("utf-8")
    except OSError as exc:
        raise _CliUsageError(f"could not read {label} from {source!r}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _CliUsageError(f"{label} at {source!r} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _CliUsageError(f"{label} at {source!r} must be a JSON object at the top level")
    return payload, raw


def _read_json_object(source: str, *, label: str) -> dict[str, Any]:
    payload, _raw = _read_json_object_with_raw(source, label=label)
    return payload


def _read_json_object_via_dir_fd(dir_fd: int, name: str, *, label: str) -> tuple[dict[str, Any], str, int]:
    """dir-fd-mode counterpart to _read_json_object_with_raw() (round-6
    fix): reads `name` as a single path component relative to an already-
    open, already-validated directory file descriptor, with O_NOFOLLOW, so
    the read refuses outright if `name` is a symlink rather than following
    it -- and never re-resolves or re-opens any path string for the
    directory component at all, since dir_fd already IS that validated
    directory. Returns (payload, raw_text_as_read, current_mode) --
    current_mode (the existing file's permission bits) lets main() preserve
    them on the write, mirroring _atomic_write_text's own mode-preservation
    for the standalone path.
    """
    if os.sep in name or name in (".", ".."):
        raise _CliUsageError(f"--wiki-name must be a single path component, got {name!r}")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise _CliUsageError(f"could not read {label} via --wiki-dir-fd for {name!r}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _CliUsageError(f"{label} at {name!r} is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise _CliUsageError(f"could not read {label} via --wiki-dir-fd for {name!r}: {exc}") from exc
    finally:
        os.close(fd)
    try:
        raw = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CliUsageError(f"{label} at {name!r} is not valid utf-8: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _CliUsageError(f"{label} at {name!r} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _CliUsageError(f"{label} at {name!r} must be a JSON object at the top level")
    return payload, raw, st.st_mode & 0o777


def resolve_default_wiki_path() -> Path:
    """Infer wiki/orca-context-wiki.json's path from this script's location.

    Accepted only if the inferred knowledge root also contains
    .orca/context/reviewed-startup-pack-manifest.json, so a copy of this
    script deployed somewhere shallower (e.g. ~/.agents/skills/...) fails
    closed instead of silently guarding the wrong file.
    """
    candidate_root = Path(__file__).resolve().parents[2]
    wiki_path = candidate_root / _KNOWLEDGE_ROOT_MARKER_WIKI
    manifest_path = candidate_root / _KNOWLEDGE_ROOT_MARKER_MANIFEST
    if wiki_path.is_file() and manifest_path.is_file():
        return wiki_path
    raise _CliUsageError(
        f"could not infer the wiki path from this script's location "
        f"(guessed knowledge root {candidate_root}); pass --wiki explicitly"
    )


RESIGN_NOTICE_TEMPLATE = """\
wiki meta.content_version: {old_version} -> {new_version}.  Write landed on disk.
.orca/context/reviewed-startup-pack-manifest.json now pins a STALE
shared_source_sha256s.wiki — the next SessionStart will fail closed with
"central reviewed source freshness mismatch: wiki".
observed sha256 = {observed_sha256}
Re-pin it with the repo's existing manual manifest re-sign procedure
(this guard deliberately does NOT re-sign: re-signing is the security
authority action and a write barrier that can self-sign is a backdoor).
Then confirm with:
  /usr/bin/python3 {freshness_check_path}
"""  # freshness_check_path is shlex.quote()'d before formatting -- this
# machine's own paths routinely contain spaces (e.g. "Extreme SSD"), and an
# unquoted absolute path pasted straight into a shell would be word-split.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate, and optionally apply, a proposed write to "
            "wiki/orca-context-wiki.json against the content_version/"
            "updated_at bookkeeping contract."
        )
    )
    parser.add_argument(
        "--new",
        required=True,
        metavar="PATH",
        help="path to the candidate JSON file, or - to read it from stdin",
    )
    parser.add_argument(
        "--wiki",
        type=Path,
        help="path to the current wiki JSON (default: inferred from this script's location)",
    )
    parser.add_argument(
        "--wiki-dir-fd",
        type=int,
        metavar="N",
        help=(
            "round-6: an already-open directory file descriptor number (inherited via "
            "subprocess pass_fds) for the wiki file's PARENT directory -- every filesystem "
            "operation is anchored to this fd instead of a path string. Must be given together "
            "with --wiki-name; mutually exclusive with --wiki. See the module docstring's "
            "round-6 note."
        ),
    )
    parser.add_argument(
        "--wiki-name",
        metavar="BASENAME",
        help="round-6: the wiki file's plain basename within --wiki-dir-fd's directory (single path component)",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="allow the one-time meta bootstrap on a wiki that has no meta key yet",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the candidate to disk if the guard allows it (default: validate only)",
    )
    parser.add_argument(
        "--acknowledge-resign-pending",
        action="store_true",
        help="exit 0 instead of 3 for a write that leaves the manifest's wiki pin stale",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single machine-readable JSON object to stdout instead of human text",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # round-6: --wiki-dir-fd/--wiki-name is a NEW, OPTIONAL, additive mode --
    # both must be given together, and never combined with --wiki. Validated
    # here, before either mode does any I/O, so a malformed combination
    # fails as a clean usage error (exit 4) rather than an inconsistent
    # partial code path further down.
    using_dir_fd = args.wiki_dir_fd is not None or args.wiki_name is not None
    if using_dir_fd and (args.wiki_dir_fd is None or args.wiki_name is None):
        _emit_error(args, exit_code=4, reason="--wiki-dir-fd and --wiki-name must both be given together")
        return 4
    if using_dir_fd and args.wiki is not None:
        _emit_error(args, exit_code=4, reason="--wiki cannot be combined with --wiki-dir-fd/--wiki-name")
        return 4

    old_mode: int | None = None
    try:
        if using_dir_fd:
            # wiki_path here is a DISPLAY-ONLY stand-in -- never used for
            # any actual filesystem operation in this mode, since dir_fd
            # already IS the validated, non-symlink-followed directory and
            # every operation below stays anchored to it by name, never by
            # re-resolving a path string (see the module docstring's
            # round-6 note).
            wiki_path = Path(args.wiki_name)
            old_payload, old_raw_text, old_mode = _read_json_object_via_dir_fd(
                args.wiki_dir_fd, args.wiki_name, label="current wiki"
            )
        else:
            # round-6: the final path component is deliberately NOT resolved
            # here -- only the parent directory is (same as this module has
            # always done for path normalization). Resolving the final
            # component would follow a symlink planted there and silently
            # operate on whatever it points to from here on, which is
            # exactly the residual this fix closes -- see the module
            # docstring's round-6 note.
            wiki_arg = args.wiki.expanduser() if args.wiki else resolve_default_wiki_path()
            wiki_dir_resolved = wiki_arg.parent.resolve(strict=False)
            wiki_path = wiki_dir_resolved / wiki_arg.name
            old_payload, old_raw_text = _read_json_object_with_raw(
                str(wiki_path), label="current wiki", no_follow_symlinks=True
            )
        new_payload = _read_json_object(args.new, label="candidate wiki")
    except _CliUsageError as exc:
        _emit_error(args, exit_code=4, reason=str(exc))
        return 4

    try:
        guard_wiki_write(old_payload, new_payload, allow_bootstrap=args.bootstrap)
    except ValueError as exc:
        _emit_error(args, exit_code=2, reason=str(exc))
        return 2

    old_meta = old_payload.get("meta") if isinstance(old_payload.get("meta"), dict) else {}
    old_version = old_meta.get("content_version")
    new_version = new_payload["meta"]["content_version"]

    # Authoritative resign_required decision: compare the REAL bytes on
    # disk right now (old_raw_text) against what will actually be written
    # (new_serialized) -- not resign_required_for()'s payload-only
    # approximation, which can false-negative when old_raw_text is not
    # already in canonical form (e.g. the very first --bootstrap against a
    # wiki this guard has never written, or any file a human hand-edited
    # outside the guard). This is what a dual review's second pass caught:
    # the approximation is the only thing main() had used until now.
    new_serialized = json.dumps(new_payload, indent=2, ensure_ascii=False) + "\n"
    resign_required = old_raw_text != new_serialized

    written = False
    observed_sha256: str | None = None
    if args.apply:
        try:
            if using_dir_fd:
                assert old_mode is not None
                _atomic_write_via_dir_fd(args.wiki_dir_fd, args.wiki_name, new_serialized, mode=old_mode)
            else:
                _atomic_write_text(wiki_path, new_serialized)
        except (OSError, _CliUsageError) as exc:
            # Round-2 P3 fix: an uncaught OSError here (e.g. an
            # unwritable/missing parent directory) previously escaped as a
            # raw traceback and a bare exit 1, outside the documented
            # 0/2/3/4 exit-code contract and with no --json output at all.
            # guard_wiki_write() already returned successfully -- the
            # candidate itself was fine -- so this is a usage/IO failure,
            # not a guard refusal. round-6: _CliUsageError is caught here
            # too since _atomic_write_via_dir_fd raises it for the (should
            # be unreachable, but asserted defensively) invalid-name case.
            _emit_error(args, exit_code=4, reason=f"could not write {wiki_path}: {exc}")
            return 4
        written = True
        observed_sha256 = hashlib.sha256(new_serialized.encode("utf-8")).hexdigest()

    if resign_required and not args.acknowledge_resign_pending:
        exit_code = 3
    else:
        exit_code = 0

    notice = None
    if resign_required and written:
        # Sibling path, matching the same "resolve relative to this
        # script's own location" convention used throughout this module
        # (round-3 P3 fix: this used to be a hard-coded repo-relative
        # string that only made sense from the knowledge root, out of
        # step with SKILL.md's <skill-dir>/scripts/... convention).
        freshness_check_path = Path(__file__).resolve().parent / "check_wiki_freshness.py"
        notice = RESIGN_NOTICE_TEMPLATE.format(
            old_version=old_version if old_version is not None else "none",
            new_version=new_version,
            observed_sha256=observed_sha256,
            freshness_check_path=shlex.quote(str(freshness_check_path)),
        )

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "written": written,
                    "resign_required": resign_required,
                    "acknowledged": bool(args.acknowledge_resign_pending),
                    "old_content_version": old_version,
                    "new_content_version": new_version,
                    "wiki_path": f"<wiki-dir-fd {args.wiki_dir_fd}>/{args.wiki_name}" if using_dir_fd else str(wiki_path),
                    "observed_sha256": observed_sha256,
                    "exit_code": exit_code,
                },
                ensure_ascii=False,
            )
        )
    else:
        if written:
            print(f"guard allowed the write; content_version {old_version if old_version is not None else 'none'} -> {new_version}")
        else:
            print(
                f"guard allowed this candidate (dry run, nothing written; pass --apply to write); "
                f"content_version {old_version if old_version is not None else 'none'} -> {new_version}"
            )
        if notice is not None:
            print(notice)
            print(notice, file=sys.stderr)
        elif resign_required and not written:
            print("note: applying this candidate would leave shared_source_sha256s.wiki stale and require a manual re-sign")

    return exit_code


def _emit_error(args: argparse.Namespace, *, exit_code: int, reason: str) -> None:
    if args.json:
        print(json.dumps({"ok": False, "exit_code": exit_code, "reason": reason}, ensure_ascii=False))
    else:
        print(reason, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
