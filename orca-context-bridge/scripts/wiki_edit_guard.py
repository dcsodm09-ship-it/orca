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
import sys
import tempfile
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
    """
    path = path.expanduser()
    try:
        mode = path.stat().st_mode & 0o777
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


class _CliUsageError(ValueError):
    """A CLI-level usage/IO/JSON problem -- distinct from a guard refusal."""


def _read_json_object_with_raw(source: str, *, label: str) -> tuple[dict[str, Any], str]:
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
    """
    try:
        if source == "-":
            raw = sys.stdin.read()
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

    try:
        wiki_path = args.wiki.expanduser().resolve(strict=False) if args.wiki else resolve_default_wiki_path()
        old_payload, old_raw_text = _read_json_object_with_raw(str(wiki_path), label="current wiki")
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
            _atomic_write_text(wiki_path, new_serialized)
        except OSError as exc:
            # Round-2 P3 fix: an uncaught OSError here (e.g. an
            # unwritable/missing parent directory) previously escaped as a
            # raw traceback and a bare exit 1, outside the documented
            # 0/2/3/4 exit-code contract and with no --json output at all.
            # guard_wiki_write() already returned successfully -- the
            # candidate itself was fine -- so this is a usage/IO failure,
            # not a guard refusal.
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
                    "wiki_path": str(wiki_path),
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
