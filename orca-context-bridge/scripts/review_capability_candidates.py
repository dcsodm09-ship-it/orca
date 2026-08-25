#!/usr/bin/env python3
"""review_capability_candidates.py -- M8-2 discovery-hit triage (Gate C).

STATUS: deployed to orca-context-bridge/scripts/, companion to
discover_capability_candidates.py in the same directory. Not registered in
any hook.

WHAT THIS TOOL IS ALLOWED TO TOUCH, AND WHY THAT MATTERS
----------------------------------------------------------------------------
This tool's `mark` command writes ONLY to discovery-hits.json's own record
for one hit (or, with `--apply-to-duplicate-cluster`, every hit sharing that
hit's content_sha256 -- see below). It NEVER touches any project's own
files, and NEVER touches catalog.json, reusable-capabilities.json, or
orca-context-wiki.json in ANY project. M8-DESIGN-FINAL-2026-08-23 section
3.4.1 declares exactly one exception to "no process writes into another
project's own directory": `promote_capability.py approve`. Nothing here
duplicates that exception -- a "discovery hit" is a different, deliberately
lower-stakes concept from a "promotion candidate" (see that design's 3.0.2
"discovery hit != promotion candidate"): marking a hit `dismissed` or
`triaged_for_promotion` is pure bookkeeping in this tool's own file, not a
step in the pipeline that ends with a write to any project's wiki/.

STATE MACHINE
----------------------------------------------------------------------------
`pending -> triaged_for_promotion | dismissed`. Both are terminal from this
tool's point of view -- there is no `mark` transition back to `pending`
(if a human changes their mind, that's a deliberate re-mark to the other
terminal state, not an "undo"; this tool does not special-case that). The
`pending` -> terminal transition is meant to happen once per hit, but this
tool does not refuse to re-mark an already-terminal hit (a person correcting
an earlier decision is a legitimate use, not a bug) -- it simply overwrites
the prior state/note/marked-by/timestamp with the new ones. Terminal-state
PRESERVATION ACROSS RESCANS is discover_capability_candidates.py's job
(the "content-hash-keyed state persistence" requirement in that file's
docstring); this tool only ever mutates the file that scan reads back next
run, and never needs to know anything about that persistence contract
itself.

--apply-to-duplicate-cluster
----------------------------------------------------------------------------
Discovery hits found by noise rule A (content-duplicate clustering, see
discover_capability_candidates.py's docstring) carry a shared
`content_sha256` when they are byte-identical copies of the same file
(the real, validated example: 6 dated staging snapshots each nesting a full
copy of the same tree, 140/405 real hits). Marking each copy one at a time
is real toil for a human triaging a cluster that is, by construction, the
exact same content. `mark --apply-to-duplicate-cluster` (default OFF) lets
a human explicitly apply one mark to every hit sharing the target hit's
content_sha256 in a single action. This never happens implicitly -- a plain
`mark` with no cluster flag touches exactly the one named --hit-id, even
if it happens to have duplicates. The command's own output always lists
`affected_hit_ids` explicitly, whether or not the flag was used, so a
caller never has to guess which records changed.

Cluster application is scoped to the SAME root_real_path as the named
--hit-id by default, mirroring discover_capability_candidates.py's own
default scoping for noise rule A itself: this codebase's own "copy, don't
import" convention (M8 design 3.0.2) deliberately manufactures
byte-identical files across projects, so a bare content_sha256 match alone
is not a safe default for "this human reviewed and dismissed all of
these" -- it would apply one note, written about one file, to an unrelated,
never-looked-at file in a different project. `--allow-cross-project-cluster`
opts back into matching purely by content_sha256, for a caller who has
confirmed (e.g. via `list --duplicates-only`) that the cluster genuinely
does span projects and that's intended.

Scoped on `root_real_path`, NOT `project_id` -- P1-1 in the max-tier Gate C
re-review found the P0-2 fix (hit_id moved from project_id to
root_real_path) was incomplete: this default safety scope was left on
project_id, which this machine's real catalog.json proves is neither
stable nor unique (two different real projects, e.g. "hgcloud" and "rn邮
箱", each map to multiple distinct real_path rows sharing one project_id
string). A `mark --apply-to-duplicate-cluster` without
`--allow-cross-project-cluster` could therefore still silently affect a
hit in a different real project, as long as that project happened to
share a project_id string with the one actually reviewed -- exactly the
leak this default scope exists to prevent. `--allow-cross-project-cluster`
now means "cluster/apply across different root_real_paths", not "across
different project_id strings" -- see
test_cluster_apply_scoped_by_root_real_path_not_shared_project_id.

WRITE-SURFACE CONFINEMENT FOR `mark`
----------------------------------------------------------------------------
`--hits-path` exists for tests and for pointing at a non-default output
location; it does NOT open up an arbitrary write target. `mark` refuses
(exit 2) any --hits-path whose basename isn't literally `discovery-hits.
json`, and refuses (exit 4, `hits_file_missing`) unless that path's parent
directory ALREADY exists -- it never calls os.makedirs() to create one.
Confirmed by direct reproduction that, before this fix, an arbitrary
--hits-path such as `<some-other-project>/wiki/orca-context-bridge/
discovery-hits.json` unconditionally created `wiki/` and
`wiki/orca-context-bridge/` inside a project that had neither, even on a
run that went on to fail -- see cmd_mark()'s own inline comment for the
full writeup.

M8-2 AUTHORIZATION NOTICE ON EVERY DEFAULT-PATH WRITE
----------------------------------------------------------------------------
`mark` still runs and writes normally regardless -- this is a visibility/
audit-trail improvement, not a new gate, and not a refusal. But whenever the
realpath of --hits-path (or its default, absent that flag) resolves to the
same file as this module's own literal production default
(`_PRODUCTION_DEFAULT_HITS_PATH`, a frozen copy of
`DEFAULT_OUTPUT_DIR / HITS_NAME`) -- compared by `os.path.realpath()`, not
literal string/Path equality, so an aliased spelling of the same production
file (a `..` segment, a symlink hop) cannot dodge it -- `mark` prints one
unsuppressible stderr line, the same mechanism discover_capability_
candidates.py uses for its own `--all-projects` warning: M8-2's own
independent authorization gate (design 3.3.3) has not been granted (Gate C
measured a 44-72% false-positive rate on real data), so discovery-hits.json
should not be treated as production-authoritative by anything that reads
it. A caller who passes an explicit --hits-path pointing elsewhere sees
nothing here. The check runs after `mark` has confirmed the target
directory exists and isn't a symlink (so it never announces a write against
a directory that provably doesn't exist yet) but before the write itself,
so the printed line describes an attempt, not a completed write -- a later
failure (e.g. `unknown_hit_id`) can still leave nothing written.

EXIT CODES
----------------------------------------------------------------------------
`list` / `show` follow the query/decision "query" family (M8 design 3.0.2):
    0  found >=1 matching record (list) / the named hit_id exists (show)
    1  confirmed no matches after a full, successful read (not a failure --
       the file was read fine, the answer is genuinely "none")
    2  usage error (bad filter value, empty --hit-id, etc.)
    3  discovery-hits.json's top-level structure was readable but contained
       one or more malformed entries that had to be skipped -- the query
       still ran and is answerable over the remaining good entries, but the
       answer is not a confirmed-complete "no" the way exit 1 is
    4  discovery-hits.json missing/unreadable/unparseable -- cannot answer
       at all

`mark` follows the decision family, per this round's explicit instruction
(overriding query-family exit 1's usual "confirmed absence" meaning for
THIS specific command -- an unknown --hit-id here is a usage mistake, not a
successfully-answered "no"):
    0  the requested state transition was written
    1  not used by this command (reserved; no code path returns it)
    2  usage error, INCLUDING an unknown --hit-id, an invalid --state
       value (argparse's own `choices=` already rejects a bad --state
       value with its own exit 2 before this tool's code ever runs), or a
       --hits-path whose basename isn't literally `discovery-hits.json`
       (see WRITE-SURFACE CONFINEMENT above)
    4  fatal -- discovery-hits.json missing/unreadable/unparseable, OR its
       parent directory doesn't already exist (mark never creates one --
       see WRITE-SURFACE CONFINEMENT above)

Run with:
    review_capability_candidates.py list [--state STATE] [--project-id ID]
        [--signal-type TYPE] [--duplicates-only] [--hits-path PATH] [--json]
    review_capability_candidates.py show --hit-id ID [--hits-path PATH] [--json]
    review_capability_candidates.py mark --hit-id ID
        --state {triaged_for_promotion,dismissed} --marked-by NAME
        [--note TEXT] [--apply-to-duplicate-cluster] [--allow-cross-project-cluster]
        [--hits-path PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

# Same fixed absolute root as discover_capability_candidates.py -- an
# independently-maintained copy of the same constant, not an import (M8
# design 3.0.2 "only copy, do not import": these two tools must keep
# independent trust surfaces even though they read/write the same file).
DEFAULT_OUTPUT_DIR = Path("/Volumes/Extreme SSD/Orca/manifests/capability-discovery")
HITS_NAME = "discovery-hits.json"
# Frozen copy of the literal default path `mark` writes to absent
# --hits-path, computed once at import time so it can never drift even if
# something later rebinds the module-level DEFAULT_OUTPUT_DIR name (this
# file's own test suite never does that -- it always passes an explicit
# --hits-path -- but the frozen copy keeps the comparison correct regardless
# of how a caller redirected). See the M8-2 authorization notice in
# cmd_mark().
_PRODUCTION_DEFAULT_HITS_PATH = DEFAULT_OUTPUT_DIR / HITS_NAME
LOCK_NAME = ".discovery.lock"
LOCK_STALE_SECONDS = 300

MARKABLE_STATES = ("triaged_for_promotion", "dismissed")
MAX_HITS_FILE_BYTES = 64 * 1024 * 1024


class ReviewFatal(Exception):
    """Cannot answer/act at all -- maps to exit code 4."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


class ReviewUsageError(Exception):
    """Maps to exit code 2."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Small shared helpers -- independently copied from discover_capability_
# candidates.py, not imported (see module docstring).
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_line_separators(text: str) -> str:
    return text.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")


def _encode_json(payload: dict[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=False, indent=1)
    return _sanitize_line_separators(text).encode("utf-8")


# ---------------------------------------------------------------------------
# Write surface -- independently-maintained copy of discover_capability_
# candidates.py's own copy (itself copied from detect_capability_changes.py
# / build_cross_project_catalog.py's pattern). Copied, not imported.
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


def atomic_write_within(base_dir: Path, final_path: Path, payload: bytes) -> None:
    tmp_path = final_path.parent / f".{final_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp_path), str(final_path))
    except BaseException:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(base_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def acquire_lock(base_dir: Path) -> Path:
    lock_path = base_dir / LOCK_NAME
    for attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, json.dumps({"pid": os.getpid(), "started_at": now_iso()}).encode("utf-8"))
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
            raise ReviewFatal("lock_held")
        except OSError as exc:
            # Anything other than "already exists" -- most commonly
            # PermissionError on a read-only base_dir -- is a genuine
            # failure to create the lock, not a lock-held race. Name it
            # explicitly (exit 4, "lock_uncreatable") instead of letting it
            # propagate as a generic unexpected_error.
            raise ReviewFatal("lock_uncreatable", str(exc))
    raise ReviewFatal("lock_held")


def release_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        os.unlink(str(lock_path))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# discovery-hits.json read / read-modify-write
# ---------------------------------------------------------------------------


def default_hits_path() -> Path:
    return DEFAULT_OUTPUT_DIR / HITS_NAME


def load_hits_document(path: Path) -> dict[str, Any]:
    """Full read of discovery-hits.json. Raises ReviewFatal (exit 4) if the
    file cannot be read/parsed at all, or its top level isn't the expected
    shape. Individual malformed hit rows inside an otherwise-valid document
    are NOT fatal here -- callers use `partial_hits()` to get the usable
    subset plus a count of what was skipped (exit 3 territory for list/
    show; mark treats an unresolvable --hit-id among the good rows as a
    plain usage error, see cmd_mark)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read(MAX_HITS_FILE_BYTES + 1)
    except FileNotFoundError as exc:
        raise ReviewFatal("hits_file_missing", str(path)) from exc
    except OSError as exc:
        raise ReviewFatal("hits_file_unreadable", str(exc)) from exc
    if len(raw) > MAX_HITS_FILE_BYTES:
        raise ReviewFatal("hits_file_too_large")
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReviewFatal("hits_file_unparseable", str(exc)) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("hits"), list):
        raise ReviewFatal("hits_file_malformed", "no 'hits' array at top level")
    return doc


def partial_hits(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Returns (usable hit records, count of malformed rows skipped)."""
    good: list[dict[str, Any]] = []
    skipped = 0
    for entry in doc["hits"]:
        if isinstance(entry, dict) and isinstance(entry.get("hit_id"), str):
            good.append(entry)
        else:
            skipped += 1
    return good, skipped


# ---------------------------------------------------------------------------
# list / show
# ---------------------------------------------------------------------------


def _noise_signals(hit: dict[str, Any]) -> list[Any]:
    """`noise_signals` defensively coerced to a list. A well-formed record
    from discover_capability_candidates.py always has a list here, but a
    malformed/hand-edited row can have the key present with an explicit
    `null` -- `dict.get(key, default)` does NOT fall back to `default` in
    that case (the key exists), so a naive `hit.get("noise_signals", [])`
    would hand `None` to `"x" in ...` and crash the whole query with an
    uncaught TypeError instead of just skipping/excluding this one row."""
    val = hit.get("noise_signals")
    return val if isinstance(val, list) else []


def _matches_filters(hit: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.state is not None and hit.get("state") != args.state:
        return False
    if args.project_id is not None and hit.get("project_id") != args.project_id:
        return False
    if args.signal_type is not None and hit.get("signal_type") != args.signal_type:
        return False
    if args.duplicates_only and "content_duplicate" not in _noise_signals(hit):
        return False
    return True


def cmd_list(args: argparse.Namespace) -> int:
    hits_path = Path(args.hits_path) if args.hits_path else default_hits_path()
    try:
        doc = load_hits_document(hits_path)
    except ReviewFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)

    good, skipped = partial_hits(doc)
    matched = [h for h in good if _matches_filters(h, args)]

    _print_records(args, matched, extra={"skipped_malformed_count": skipped})

    if skipped > 0:
        return 3
    return 0 if matched else 1


def cmd_show(args: argparse.Namespace) -> int:
    hit_id = (args.hit_id or "").strip()
    if not hit_id:
        return _emit_error(args, 2, "empty_hit_id")

    hits_path = Path(args.hits_path) if args.hits_path else default_hits_path()
    try:
        doc = load_hits_document(hits_path)
    except ReviewFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)

    good, skipped = partial_hits(doc)
    by_id = {h["hit_id"]: h for h in good}
    found = by_id.get(hit_id)

    if found is None:
        if not args.quiet:
            payload = {"ok": False, "reason": "hit_not_found", "hit_id": hit_id, "skipped_malformed_count": skipped}
            if args.json:
                print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False)))
            else:
                print(f"not found: {hit_id}")
        return 3 if skipped > 0 else 1

    _print_records(args, [found], extra={"skipped_malformed_count": skipped})
    return 0


def _print_records(args: argparse.Namespace, records: list[dict[str, Any]], *, extra: dict[str, Any]) -> None:
    if getattr(args, "quiet", False):
        return
    if getattr(args, "json", False):
        payload = {"ok": True, "count": len(records), "hits": records, **extra}
        print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False, indent=1)))
    else:
        if not records:
            print("no matching hits")
        for h in records:
            dup = f" duplicate_of={h.get('duplicate_of')}" if h.get("duplicate_of") else ""
            signals = ",".join(str(s) for s in _noise_signals(h)) or "-"
            print(
                f"{h.get('hit_id')}  state={h.get('state')}  project={h.get('project_id')}  "
                f"signal={h.get('signal_type')}  path={h.get('path')}  noise=[{signals}]{dup}"
            )
        if extra.get("skipped_malformed_count"):
            print(f"(skipped {extra['skipped_malformed_count']} malformed rows in discovery-hits.json)")


# ---------------------------------------------------------------------------
# mark -- the one command that writes.
# ---------------------------------------------------------------------------


def cmd_mark(args: argparse.Namespace) -> int:
    hit_id = (args.hit_id or "").strip()
    marked_by = (args.marked_by or "").strip()
    if not hit_id:
        return _emit_error(args, 2, "empty_hit_id")
    if not marked_by:
        return _emit_error(args, 2, "empty_marked_by")
    # args.state is already constrained to MARKABLE_STATES by argparse's own
    # `choices=`, which exits 2 itself on an invalid value before this
    # function ever runs -- see module docstring's EXIT CODES section.

    hits_path = Path(args.hits_path) if args.hits_path else default_hits_path()

    # `mark` must never be the reason a directory tree springs into
    # existence somewhere the caller didn't already have one. Two
    # independent hardenings, confirmed necessary by direct reproduction:
    #
    # (1) hits_path.name must literally be HITS_NAME. This tool's own
    #     docstring claims it "writes ONLY to discovery-hits.json's own
    #     record" -- a --hits-path pointing at some other existing JSON file
    #     that merely happens to have a top-level {"hits": [...]} array with
    #     a matching hit_id would otherwise be silently rewritten in place
    #     (load_hits_document()'s shape check alone does not catch this; it
    #     only rejects a document that ISN'T hits-shaped, not one that is
    #     hits-shaped but isn't actually this tool's own file).
    # (2) the parent directory must ALREADY EXIST -- this function no
    #     longer calls os.makedirs() on it. Before this fix,
    #     `mark --hit-id X --hits-path /some/other/project/wiki/
    #     orca-context-bridge/discovery-hits.json` unconditionally created
    #     `wiki/` and `wiki/orca-context-bridge/` inside a project that had
    #     neither, even though the command went on to fail with
    #     hits_file_missing -- a write into another project's tree with NO
    #     precondition at all (unlike the hand-crafted victim file (1)
    #     needs). Per this codebase's own AUTHORITY_TRACKED_PATHS lesson, a
    #     stray file/dir appearing under a project's own wiki/ or
    #     orca-context-bridge/ has already broken a live SessionStart hook
    #     twice. There is no legitimate case where mark needs to create this
    #     directory: if it doesn't exist, discovery-hits.json inside it
    #     can't exist either, so the command was always going to fail with
    #     hits_file_missing regardless -- refusing earlier costs nothing.
    if hits_path.name != HITS_NAME:
        return _emit_error(args, 2, "invalid_hits_path_name", f"--hits-path must name a file called {HITS_NAME!r}")

    output_dir = hits_path.parent
    if not output_dir.is_dir():
        return _emit_error(args, 4, "hits_file_missing", str(hits_path))
    if os.path.islink(str(output_dir)):
        return _emit_error(args, 4, "output_dir_is_symlink")

    # Notice placed here, not before the two directory checks above: printing
    # it any earlier announced a write against a target directory that
    # provably did not exist yet (confirmed by direct reproduction against
    # this machine's real, hits-file-less manifests/capability-discovery/) --
    # a caller would see "writing to ..." immediately followed by exit 4
    # hits_file_missing with nothing ever written. From this point on the
    # target directory is known to exist and isn't a symlink, so the notice
    # now describes a write that can actually reach the write_only_within /
    # acquire_lock / load_hits_document path below (it can still fail later,
    # e.g. unknown_hit_id -- this is a "we're about to attempt it" notice,
    # not a post-hoc confirmation).
    #
    # Compared by realpath, not by the literal Path object: a caller who
    # spells the same production file via a different-but-equivalent path
    # (e.g. an explicit --hits-path containing a `..` segment, or a symlink
    # hop) must not silently dodge the notice below just because the
    # unresolved strings differ. Confirmed by direct reproduction: an
    # aliased spelling of the real production path used to compare unequal
    # here (notice suppressed) while still resolving to, and writing, the
    # exact same on-disk file. realpath() is safe to call on a path whose
    # final component does not exist yet -- it only needs existing
    # ancestors to resolve symlinks, which is all this check does.
    if os.path.realpath(str(hits_path)) == os.path.realpath(str(_PRODUCTION_DEFAULT_HITS_PATH)):
        # Unsuppressible, same mechanism as discover_capability_candidates.
        # py's own --all-projects warning: no --quiet gate, printed
        # unconditionally before any write. Only fires when hits_path is
        # still this module's literal production default -- a caller who
        # passed an explicit --hits-path pointing elsewhere (every test in
        # this file's own suite does) sees nothing here.
        print(
            "NOTICE: writing to manifests/capability-discovery/discovery-hits.json, this tool's own "
            "default production path. M8-2's independent authorization gate (M8-DESIGN-FINAL-2026-08-23.md "
            "section 3.3.3) has not been granted: the Gate C validation run measured a 44%-72% AI-judged "
            "false-positive rate on a real 25-item sample (m8-gate-c-validation-STAGED-review-only/M8-GATE-C-VALIDATION-REPORT-2026-08-23.md section 2). "
            "This output should not be treated as production-authoritative by anything that reads it.",
            file=sys.stderr,
        )

    hits_final, reason = write_only_within(output_dir, str(hits_path))
    if reason or hits_final is None:
        return _emit_error(args, 2, reason or "invalid_hits_path")

    try:
        lock_path = acquire_lock(output_dir)
    except ReviewFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)

    try:
        try:
            doc = load_hits_document(hits_final)
        except ReviewFatal as exc:
            return _emit_error(args, 4, exc.reason, exc.message)

        good, _skipped = partial_hits(doc)
        by_id = {h["hit_id"]: h for h in good}
        target = by_id.get(hit_id)
        if target is None:
            return _emit_error(args, 2, "unknown_hit_id", hit_id)

        affected_ids = [hit_id]
        if args.apply_to_duplicate_cluster:
            content_sha256 = target.get("content_sha256")
            target_root_real_path = target.get("root_real_path")
            if content_sha256:
                for h in good:
                    if h.get("content_sha256") != content_sha256 or h["hit_id"] in affected_ids:
                        continue
                    # Cluster application is scoped to the SAME root_real_path
                    # as the named --hit-id by default, matching
                    # discover_capability_candidates.py's own noise-rule-A
                    # default scoping (see that file's
                    # apply_noise_rule_a_content_duplicates docstring): a
                    # human marking one hit dismissed should not silently
                    # dismiss a same-content file in a DIFFERENT, un-reviewed
                    # project. Reproduced directly: a plain content_sha256
                    # match alone let one `mark` stamp a note written about
                    # one project onto a legitimate, never-looked-at hit in
                    # another. --allow-cross-project-cluster opts back into
                    # matching purely by content_sha256, mirroring the scan
                    # side's own opt-in of the same name. Scoped on
                    # root_real_path, NOT project_id (P1-1, max-tier Gate C
                    # re-review): this machine's real catalog.json has
                    # multiple different real projects sharing one
                    # project_id string, so project_id alone is not a safe
                    # proxy for "same project" here.
                    if not args.allow_cross_project_cluster and h.get("root_real_path") != target_root_real_path:
                        continue
                    affected_ids.append(h["hit_id"])

        set_at = now_iso()
        for hid in affected_ids:
            rec = by_id[hid]
            rec["state"] = args.state
            rec["state_note"] = args.note
            rec["state_set_by"] = marked_by
            rec["state_set_at"] = set_at

        # `good` holds the SAME dict objects as doc["hits"] (partial_hits()
        # only filters, never copies) -- mutating `rec` above already
        # mutated the entry inside doc["hits"] in place. Any malformed rows
        # doc["hits"] already contained are left untouched and rewritten
        # as-is: mark() never silently drops a row it did not target.
        atomic_write_within(output_dir, hits_final, _encode_json(doc))
    finally:
        release_lock(lock_path)

    result = {
        "ok": True,
        "hit_id": hit_id,
        "state": args.state,
        "marked_by": marked_by,
        "affected_hit_ids": affected_ids,
    }
    if not args.quiet:
        if args.json:
            print(_sanitize_line_separators(json.dumps(result, ensure_ascii=False, indent=1)))
        else:
            print(f"marked {len(affected_ids)} hit(s) as {args.state}: {', '.join(affected_ids)}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="M8-2 discovery-hit triage: query and mark discovery-hits.json records. "
                     "Never writes to any project file, catalog.json, or any wiki/*.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_p = subparsers.add_parser("list", help="Read-only: list discovery hits, optionally filtered.")
    list_p.add_argument("--state", type=str, default=None, choices=["pending", *MARKABLE_STATES])
    list_p.add_argument("--project-id", type=str, default=None, dest="project_id")
    list_p.add_argument("--signal-type", type=str, default=None, dest="signal_type")
    list_p.add_argument("--duplicates-only", action="store_true", dest="duplicates_only")
    list_p.add_argument("--hits-path", type=str, default=None, dest="hits_path")
    list_p.add_argument("--json", action="store_true")
    list_p.add_argument("--quiet", action="store_true")
    list_p.set_defaults(func=cmd_list)

    show_p = subparsers.add_parser("show", help="Read-only: show one discovery hit by hit_id.")
    show_p.add_argument("--hit-id", type=str, required=True, dest="hit_id")
    show_p.add_argument("--hits-path", type=str, default=None, dest="hits_path")
    show_p.add_argument("--json", action="store_true")
    show_p.add_argument("--quiet", action="store_true")
    show_p.set_defaults(func=cmd_show)

    mark_p = subparsers.add_parser("mark", help="Write: transition one (or a duplicate cluster of) hit(s) to a terminal state.")
    mark_p.add_argument("--hit-id", type=str, required=True, dest="hit_id")
    mark_p.add_argument("--state", type=str, required=True, choices=list(MARKABLE_STATES))
    mark_p.add_argument("--marked-by", type=str, required=True, dest="marked_by")
    mark_p.add_argument("--note", type=str, default=None)
    mark_p.add_argument("--apply-to-duplicate-cluster", action="store_true", dest="apply_to_duplicate_cluster")
    mark_p.add_argument("--allow-cross-project-cluster", action="store_true", dest="allow_cross_project_cluster",
                         help="With --apply-to-duplicate-cluster, also match hits under OTHER scanned roots "
                              "sharing the same content_sha256 (default: cluster application is scoped to the "
                              "named hit's own root_real_path).")
    mark_p.add_argument("--hits-path", type=str, default=None, dest="hits_path")
    mark_p.add_argument("--json", action="store_true")
    mark_p.add_argument("--quiet", action="store_true")
    mark_p.set_defaults(func=cmd_mark)

    return parser


def _emit_error(args: argparse.Namespace, code: int, reason: str, message: str | None = None) -> int:
    if not getattr(args, "quiet", False):
        payload: dict[str, Any] = {"ok": False, "exit_code": code, "reason": reason}
        if message:
            payload["message"] = message
        if getattr(args, "json", False):
            print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False)), file=sys.stderr)
        else:
            suffix = f" ({message})" if message else ""
            print(f"error: {reason}{suffix}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = getattr(args, "func", None)
    if handler is None:
        return 2
    try:
        return handler(args)
    except ReviewUsageError as exc:
        return _emit_error(args, 2, exc.reason, exc.message)
    except ReviewFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)
    except BaseException as exc:
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
