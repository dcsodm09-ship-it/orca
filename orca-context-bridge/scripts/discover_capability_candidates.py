#!/usr/bin/env python3
"""discover_capability_candidates.py -- M8-2 auto-scan discovery (Gate C).

STATUS: staged candidate under m8-gate-c-STAGED-review-only/, not yet
deployed to any project's orca-context-bridge/scripts/. Not registered in
any hook. Never invoked from SessionStart -- this tool does full-tree
filesystem walks, an order of magnitude heavier than M4's fixed
three-filename-per-project read, and the design (M8-DESIGN-FINAL-2026-08-23
3.3) is explicit that it must be even MORE careful than M4 about never being
wired into an automatic startup path.

WHAT THIS IS, RELATIVE TO THE VALIDATION PROTOTYPE
----------------------------------------------------------------------------
`m8-gate-c-validation-STAGED-review-only/scripts/discovery_scan_prototype.py`
was a throwaway, read-only instrument built to answer two numeric questions
(hit/false-positive rate, I/O cost) with real data before anyone could
responsibly build this file. It answered them, and the answers were not
great: 44-72% AI-judged false-positive rate on a 25-item real sample
depending on strict/loose scoring, and >=51% of all 405 real hits falling
into four noise categories the original design never anticipated. The user
has explicitly decided to proceed with implementation anyway. This file
ports the prototype's core signal-detection logic near-verbatim (shebang +
argparse regex, SKILL.md marker, reports/-or-keyword knowledge match,
catalog-baseline dedup, project_id resolution) and adds the four concrete
noise mitigations the validation run's own output made obvious in
hindsight -- see NOISE RULES below. It does NOT pretend the mitigations
close the gap to something Gate C would call "validated"; they are
instrumentation and filtering, reported with exact per-rule effect counts
so a future re-validation run can measure whether they actually helped.

SCOPE: NARROWER THAN "SCAN EVERYTHING", ON PURPOSE
----------------------------------------------------------------------------
`scan` takes one or more explicit `--root PATH` arguments and touches only
those roots. It never auto-enumerates the 142+ Orca-managed worktrees. The
validation report's own I/O section measured a nearly 4-order-of-magnitude
spread across 3 sampled projects (14 files / 0.6ms vs 67,821 files / 2.1s)
and explicitly could not resolve that into anything narrower than a
"seconds to ~5.5 minutes" full-fleet estimate. This tool does not silently
commit callers to that unresolved cost. `--all-projects` is a separate,
explicitly opt-in flag: when passed, it resolves scan roots from
catalog.json's own `projects[].real_path` field -- the same read-only
project-root-resolution pattern every other M8 tool in this codebase
already uses (never a fresh `orca repo list` / `worktree list` call) -- and
prints an unsuppressible stderr warning before scanning starts.

NOISE RULES (A-D), EACH INDEPENDENTLY TOGGLEABLE
----------------------------------------------------------------------------
All four are ANNOTATIONS or PRUNES, never silent drops of a hit record that
was already produced (matching this codebase's "annotate degradation, never
hide it" convention -- see detect_capability_changes.py's degraded_count,
build_cross_project_catalog.py's degraded_projects). Each has its own
`--disable-noise-rule {A,B,C,D}` opt-out (repeatable) for a future
re-validation run that wants to measure the rules' own effect against fresh
real data, and the summary always reports exactly how many hits/dirs each
active rule affected -- never folded into one combined number.

  A. Content-duplicate clustering (generalizes the real 140/405, 34.6%
     "historical staging-snapshot" finding). Path-name pattern matching was
     rejected as the generalization -- path names are not a reliable signal
     in general -- in favor of CONTENT sha256: every hit's own file bytes
     (the script file, the knowledge .md file, or for a capability:skill
     hit, its marker SKILL.md) are hashed once, reusing bytes already read
     for signal detection where possible, never re-read. Hits sharing an
     identical content hash are clustered; the lexicographically shortest
     relative path is kept as `duplicate_of: null` (the "primary"), every
     other member gets `duplicate_of: "<primary hit_id>"` and
     `noise_signals: ["content_duplicate", ...]`. Nothing is dropped from
     `hits[]` -- this only makes the cluster trivially filterable/groupable,
     and gives review_capability_candidates.py's `--apply-to-duplicate-
     cluster` a natural target.
  B. Iteration-round mid-draft filenames (generalizes the real 50/177,
     28.2% "ROUND[0-9]+" finding). Case-insensitive `ROUND[0-9]+` in the
     basename annotates `noise_signal: iteration_round_artifact`. Still a
     full hit record, but excluded from the summary's convenience
     `new_discoveries_total` count by default;
     `--include-round-artifacts-in-summary` opts back into the raw number
     (both numbers are always present in the JSON body regardless of the
     flag -- see `summary.new_discoveries_total_excluding_round_artifacts`
     / `_including_round_artifacts` -- the flag only picks which one is
     also mirrored into the convenience `new_discoveries_total` field).
  C. This repo's own staging-directory convention: any directory component
     ending in exactly "-STAGED-review-only" (confirmed by grep across this
     repo's own untracked directories to be this whole M8 effort's
     consistently-used naming convention for in-flight review staging) is
     pruned from the walk before any signal detection runs on it or
     anything beneath it -- these are always in-flight review artifacts,
     never real discoverable capabilities.
  D. Accidental full-tree overlay copies (generalizes the real 16/24,
     66.7% ".pty-overlay*" finding, without hardcoding that one literal
     name): any directory whose basename starts with "." and contains
     "overlay" case-insensitively is pruned the same way.

Pruning (C, D) is counted separately from the pre-existing dev-tooling
excludes (node_modules, __pycache__, .venv, dist, build, etc. -- SENSIBLE_
EXCLUDE, unconditional, not one of the four toggleable rules) in a
`dirs_pruned_by_noise_rule: {"C": n, "D": n}` summary field, distinct from
the plain `dirs_pruned_total` field so a reader can tell which mechanism
did the pruning.

CONTENT-HASH-KEYED STATE PERSISTENCE ACROSS RERUNS -- THE SINGLE MOST
IMPORTANT CORRECTNESS REQUIREMENT IN THIS FILE
----------------------------------------------------------------------------
`scan` never overwrites discovery-hits.json wholesale. Before writing, it
reads the EXISTING file (if present, under the same lock used for the
write), and for every freshly-computed hit whose `hit_id` matches an
existing record already in a TERMINAL state (`triaged_for_promotion` or
`dismissed`), the new record PRESERVES that state plus its human-authored
fields (`state_note`, `state_set_by`, `state_set_at`) rather than resetting
to `pending`. Only genuinely new hit_ids start at `pending`. Getting this
wrong would silently undo every human triage decision on every rerun -- see
TestContentHashKeyedStatePersistence in the test suite, which is the single
test this file's author was told not to treat as "one test among many".

`hit_id = sha256(root_real_path|signal_type|path)` -- keyed on the
RESOLVED REAL PATH of the scanned root, not on `project_id`. An earlier
revision of this file used `project_id` (from catalog.json's own
`resolve_project_id()`) as the identity component instead, reasoning that
the round's own worked persistence example (mark dismissed, rerun with
IDENTICAL filesystem input, still dismissed) was the concrete contract to
honor literally. That reasoning missed that `project_id` is not purely a
function of "identical filesystem input": it is also a function of
catalog.json's state at scan time, which is a second, independent input the
"identical rerun" framing never accounted for. Two confirmed, reproduced
failures resulted:

  1. A project that is not yet in catalog.json resolves to an `unmatched:`
     fallback project_id. The moment that project is added to catalog.json
     (the entire point of this M8-2 tool: surface un-catalogued things so
     they CAN be catalogued) and rescanned with byte-identical files, every
     hit's project_id flips from `unmatched:...` to the real catalog id,
     hit_id changes, the old record is orphaned (never matched again,
     carried forward forever), and a brand-new `pending` record appears
     under the new hit_id -- silently discarding the human's triage
     decision. Reproduced directly; see
     test_state_persists_across_project_id_resolution_change.
  2. catalog.json can (and, on this machine's real, current catalog.json,
     DOES: 155 projects, project_ids `hgcloud` and `rn邮箱` each shared by
     multiple distinct `real_path` rows) contain more than one project row
     with the SAME project_id but a DIFFERENT real_path. Two physically
     different files in two different projects then computed the identical
     hit_id, so `review_capability_candidates.py`'s `by_id` dict (and this
     file's own `load_existing_hits()`) silently collapsed onto one record,
     and a `mark` on one physical file silently mutated the other. See
     test_duplicate_catalog_project_ids_across_different_real_paths_do_not_
     collide.

Both failures share one root cause: `project_id` is mutable (it can change
for the same physical root as catalog.json changes) and, in real data, not
unique. `root_real_path` -- `os.path.realpath()` of the `--root` argument
actually passed to this run -- is neither: it identifies exactly one
physical location on disk, stable across catalog.json edits, and (module
symlink games outside this tool's threat model, same as everywhere else in
this codebase) collision-free across distinct roots. Keying identity on it
fixes both failures at once, and is a strict improvement on the
`unmatched:{name}:{sha256(real)[:16]}` trick `resolve_project_id()` already
used for its OWN fallback-uniqueness problem (see that function's
docstring) -- that trick embedded a real-path hash inside project_id
specifically to avoid a collision that keying hit_id on real_path directly
now avoids by construction, for both the matched and unmatched cases alike.
`project_id` is still computed and still stored on every hit record (for
`review list --project-id` filtering and for catalog-baseline dedup, which
legitimately IS a project_id-keyed concept) -- it is simply no longer part
of the identity used for rerun-to-rerun state matching.

`content_sha256` is a SEPARATE field carried on each hit record (used only
by noise rule A's clustering, recomputed fresh every run) -- it is NOT part
of the `hit_id` identity used for rerun-to-rerun state matching. A file
whose content changes keeps the same hit_id (same root_real_path/signal/
path) and therefore keeps its prior triage state across the content edit;
only a path/root/signal_type change produces a new hit_id and starts at
`pending`.

A run's SCOPE also matters to this contract, not just each hit's identity.
`scan` writes the full discovery-hits.json, not a per-root patch, so a
naive implementation that only "trusts this run's own hits" would silently
delete every hit -- pending or terminal -- belonging to a project this run
never scanned, the moment scope narrows (e.g. `--root A` after a prior
`--root A --root B` run, or an `--all-projects` run where one project row
temporarily failed to resolve to a directory). That is exactly the kind of
silent triage-loss this section opens by warning against, just triggered by
scope instead of by an identical-input rerun. `scan` therefore also carries
forward, byte-for-byte and untouched, any existing record whose
`root_real_path` was not among the real paths actually scanned this run
(see `scanned_real_paths` / `carried_forward_hits` in `cmd_scan()`, and
`summary.hits_carried_forward_from_unscanned_projects`). A hit whose root
WAS scanned this run but genuinely no longer exists on disk (or whose
content has changed) is still correctly dropped, not carried forward --
see test_hit_that_disappears_from_disk_is_not_carried_forward.

A hit whose root WAS scanned this run but simply was not RE-DETECTED by
this run's own signal detection is a THIRD, narrower case (P2-1, max-tier
Gate C re-review): a noise rule (C/D) toggled between the run that
produced the record and this one can prune a directory this run without
the underlying file having moved at all, and a transient read error
(EMFILE/ENFILE/EACCES) on one file during a large `--all-projects` walk
can suppress detection without the file changing either. Treating
"not redetected this run" as equivalent to "genuinely gone" would silently
undo a human's terminal-state triage decision through a different trigger
than the identical-rerun case above -- confirmed reproducible. `scan`
therefore also carries forward a TERMINAL-state record whose root WAS
scanned this run but wasn't redetected, UNLESS this run finds POSITIVE
on-disk evidence the file is actually gone or its content changed (see
`_hit_is_positively_gone_or_changed()` and
`summary.hits_carried_forward_scanned_but_not_redetected`). A `pending`
record not redetected under this same condition is NOT specially carried
forward -- it has no human-authored state to lose and simply reappears
fresh the next time it IS redetected.

An existing discovery-hits.json that exists but cannot be READ AT ALL as a
valid document (corrupt JSON, wrong top-level shape, or over
MAX_EXISTING_HITS_BYTES -- `load_existing_hits()`'s "unreadable" status,
distinct from "absent") is never silently treated as empty and overwritten:
its raw bytes are backed up to a sibling file
(`discovery-hits.json.corrupt-backup-<run_id>`) under the same trusted
output directory before the fresh write proceeds, and the backup's path is
named in `summary.existing_hits_file_backup_path` plus an unsuppressible
stderr warning. If even the backup read fails (a genuine I/O/permissions
problem, not just a content problem), `scan` refuses outright (exit 4,
`existing_hits_file_unreadable_backup_failed`) rather than risk destroying
state nobody could then recover -- see
test_corrupt_existing_hits_file_is_backed_up_not_silently_destroyed and
test_unreadable_existing_hits_file_backup_failure_is_fatal_not_silent.

WRITE SURFACE
----------------------------------------------------------------------------
Fixed absolute output root (never Path(__file__)-relative -- this codebase
has hit the AUTHORITY_TRACKED_PATHS trap from a relative pin twice before,
see detect_capability_changes.py's own docstring for the M1/M7 writeups):

    /Volumes/Extreme SSD/Orca/manifests/capability-discovery/
        discovery-hits.json
        .discovery.lock
        runs/<run_id>.ndjson

`write_only_within()` / `atomic_write_within()` / `acquire_lock()` /
`release_lock()` below are an independently-maintained copy of
detect_capability_changes.py's own reduced copy of
build_cross_project_catalog.py's pattern -- copied, not imported, per this
codebase's "only copy, do not import" convention (M8 design 3.0.2): every
new tool keeps an independent trust surface for its own write guard.

EXIT CODES (generator convention, matching detect_capability_changes.py)
----------------------------------------------------------------------------
    0  scan completed, even with zero hits. baseline_stale and every
       noise rule's own affected-count are named fields in the JSON body,
       never silently absorbed.
    2  usage error: no scope given (`--root` nor `--all-projects`), a
       `--root` value that is not a directory, or `--max-file-bytes` <= 0.
    4  fatal: catalog.json unreadable in a way that blocks baseline
       computation AND no `--root` was given at all (nothing to fall back
       on); or zero scan roots ultimately resolved; or the output
       directory / lock could not be secured; or an existing discovery-
       hits.json was unreadable AND the raw-bytes backup of it also failed
       (see WRITE SURFACE's own paragraph on this -- the backup itself
       succeeding is NOT fatal, only its failure is). catalog.json merely
       being stale or missing while `--root` roots ARE given degrades
       gracefully (empty-but-flagged baseline), per the prototype's own
       existing behavior -- that is NOT treated as fatal.

Run with:
    discover_capability_candidates.py scan --root PATH [--root PATH ...]
        [--all-projects] [--catalog PATH] [--max-file-bytes N]
        [--disable-noise-rule {A,B,C,D}]... [--include-round-artifacts-in-summary]
        [--allow-cross-project-cluster] [--json] [--quiet]
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCRIPT_REL_PATH = "orca-context-bridge/scripts/discover_capability_candidates.py"
GENERATOR_VERSION = "1.0.0"
OUTPUT_SCHEMA_VERSION = 1

# Fixed absolute path -- deliberately NOT derived from Path(__file__). See
# module docstring's WRITE SURFACE section. Tests redirect this by rebinding
# the module-level constant itself, exactly as detect_capability_changes.py's
# own test suite documents doing.
DEFAULT_OUTPUT_DIR = Path("/Volumes/Extreme SSD/Orca/manifests/capability-discovery")
DEFAULT_CATALOG_PATH = "/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json"
HITS_NAME = "discovery-hits.json"
RUNS_DIR_NAME = "runs"
LOCK_NAME = ".discovery.lock"
LOCK_STALE_SECONDS = 300

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB cap on content reads

# Ordinary dev-tooling excludes -- unconditional, not one of the four
# toggleable noise rules below.
GITONLY_EXCLUDE = {".git"}
SENSIBLE_EXCLUDE = GITONLY_EXCLUDE | {
    "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", "target", ".pytest_cache", ".mypy_cache", ".tox",
}

KNOWLEDGE_KEYWORDS = ["REVIEW", "REPORT", "CANDIDATE", "研究", "调研", "笔记"]

SHEBANG_RE = re.compile(r"^#!.*python")
ARGPARSE_RE = re.compile(r"\bargparse\b|ArgumentParser\s*\(")
ROUND_ARTIFACT_RE = re.compile(r"(?<![A-Za-z])round[0-9]+(?![A-Za-z])", re.IGNORECASE)
STAGED_REVIEW_ONLY_SUFFIX = "-STAGED-review-only"

TERMINAL_STATES = ("triaged_for_promotion", "dismissed")
ALL_NOISE_RULES = ("A", "B", "C", "D")

MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_EXISTING_HITS_BYTES = 64 * 1024 * 1024


class DiscoverFatal(Exception):
    """Cannot run at all -- maps to exit code 4. Never raised after a write
    has happened."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_line_separators(text: str) -> str:
    """Escape U+2028/U+2029 in already-serialized JSON text. Same contract
    as every other tool in this codebase; copied, not imported."""
    return text.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")


def _normalize(text: str) -> str:
    """Fold one string for comparison: NFC first, then casefold. Literal
    copy of query_catalog.py::_normalize() (M8 design 3.0.2)."""
    return unicodedata.normalize("NFC", text).casefold()


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def compute_hit_id(root_real_path: str, signal_type: str, path: str) -> str:
    """Identity used for cross-rerun state persistence: keyed on the
    SCANNED ROOT'S RESOLVED REAL PATH, not on catalog-derived project_id --
    see module docstring's CONTENT-HASH-KEYED STATE PERSISTENCE section for
    why (two confirmed P0s: a project_id that changes when catalog.json
    catches up to a project, and duplicate project_ids for distinct
    real_paths in real catalog data, both silently break state persistence
    when project_id is part of this identity)."""
    return sha256_hex(f"{root_real_path}|{signal_type}|{path}")


def sha256_file(path: Path) -> str | None:
    """Streaming sha256 of a trusted local file (this project's own
    reusable-capabilities.json / orca-context-wiki.json, for staleness
    comparison) -- not used on untrusted scanned content, which goes
    through read_file_bounded() instead."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def read_file_bounded(path: Path, max_bytes: int) -> tuple[bytes | None, str | None]:
    """Read a regular file's bytes, bounded. Returns (bytes, None) on
    success, (None, reason) otherwise. O_NONBLOCK avoids the FIFO-blocking-
    open trap documented in detect_capability_changes.py's own bounded
    reader. O_NOFOLLOW IS set here (an earlier revision of this docstring
    argued it could be safely omitted because scanned files are reached by
    a real os.walk() traversal rather than by resolving an untrusted path
    string -- that argument only addressed a TOCTOU *race*, not the much
    simpler static case: os.walk()'s own `filenames` list includes symlink
    entries verbatim, unresolved, so a single symlink planted anywhere
    inside a scanned tree -- by another contributor, an accidental build
    artifact, a stray .venv/node_modules symlink, or literally anyone with
    write access to any `--all-projects`-scanned project -- would silently
    have its TARGET's bytes opened, hashed, sized, and (for a `.py` file
    whose first line happens to match the shebang pattern) have that first
    line copied verbatim into discovery-hits.json, regardless of whether
    the target lives anywhere on the filesystem this process can read.
    Confirmed by direct reproduction: a symlink under a scan root pointing
    at a file outside it had its content read and partially echoed into
    the written hit record before this fix. O_NOFOLLOW makes `os.open`
    fail with ELOOP on a symlink instead, so such an entry is skipped (and
    counted) rather than dereferenced -- see
    test_symlink_inside_scan_root_is_not_dereferenced."""
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
        fd = os.open(str(path), flags)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            return None, "symlink_skipped"
        return None, "open_error"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not_regular_file"
        if st.st_size > max_bytes:
            return None, "too_large"
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None, "read_error"
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > max_bytes:
        return None, "too_large"
    return raw, None


def _encode_json(payload: dict[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=False, indent=1)
    return _sanitize_line_separators(text).encode("utf-8")


# ---------------------------------------------------------------------------
# Signal detection -- ported from discovery_scan_prototype.py essentially
# as-is (see that file's detect_script_signal / detect_skill_dir_signal /
# detect_knowledge_md_signal). The one structural change: this version
# separates "read the bytes" from "decide the signal" so the caller can
# reuse the same bytes for content_sha256 (noise rule A) without a second
# read, and uses read_file_bounded() (an fstat-checked, FIFO-safe reader)
# in place of the prototype's plain stat()+open() (the prototype was a
# throwaway read-only instrument; this is the production tool).
# ---------------------------------------------------------------------------


def detect_script_signal(path: Path, stats: dict, max_bytes: int) -> tuple[dict | None, bytes | None]:
    """Capability-type signal: .py file with a python shebang AND an
    argparse feature. Returns (signal_or_None, raw_bytes_or_None) -- raw is
    returned whenever a read succeeded, hit or not, so hit content-hashing
    never needs a second read."""
    stats["files_stated"] += 1
    raw, reason = read_file_bounded(path, max_bytes)
    if raw is None:
        if reason == "too_large":
            stats["skipped_too_large"] += 1
        elif reason == "symlink_skipped":
            stats["symlinks_skipped"] += 1
        else:
            stats["read_errors"] += 1
        return None, None
    stats["files_content_read"] += 1
    stats["bytes_read"] += len(raw)
    text = raw.decode("utf-8", errors="replace")
    first_line = text.splitlines()[0] if text else ""
    has_shebang = bool(SHEBANG_RE.match(first_line))
    has_argparse = bool(ARGPARSE_RE.search(text))
    if has_shebang and has_argparse:
        return {
            "signal_type": "capability:script",
            "evidence": {"shebang": first_line.strip(), "argparse_matched": True},
            "size_bytes": len(raw),
        }, raw
    return None, raw


def detect_skill_dir_signal(dirpath: Path, filenames: list[str]) -> dict | None:
    """Capability-type signal: directory containing SKILL.md."""
    if "SKILL.md" in filenames:
        return {"signal_type": "capability:skill", "evidence": {"marker": "SKILL.md"}}
    return None


def detect_knowledge_md_signal(path: Path, rel_parts: tuple[str, ...]) -> dict | None:
    """Knowledge-type signal: .md file under a reports/ dir, OR filename
    matches a research/notes keyword pattern. Path-based only -- does not
    require reading the file's content (content is read afterward, only for
    hits, purely to compute content_sha256 for noise rule A)."""
    in_reports_dir = any(_normalize(part) == "reports" for part in rel_parts[:-1])
    basename_normalized = _normalize(path.stem)
    matched_keywords = [kw for kw in KNOWLEDGE_KEYWORDS if _normalize(kw) in basename_normalized]
    if in_reports_dir or matched_keywords:
        return {
            "signal_type": (
                "knowledge:reports_dir"
                if in_reports_dir and not matched_keywords
                else ("knowledge:filename_keyword" if matched_keywords and not in_reports_dir
                      else "knowledge:reports_dir+filename_keyword")
            ),
            "evidence": {"in_reports_dir": in_reports_dir, "matched_keywords": matched_keywords},
        }
    return None


# ---------------------------------------------------------------------------
# Catalog baseline -- ported from discovery_scan_prototype.py's
# load_catalog_baseline / resolve_project_id essentially as-is. Never
# raises; a missing/corrupt catalog degrades to an empty, explicitly-
# flagged baseline (see module docstring's exit-code section: this is what
# makes a stale/missing catalog non-fatal whenever --root roots exist).
# ---------------------------------------------------------------------------


def load_catalog_baseline(catalog_path: Path) -> dict:
    result: dict[str, Any] = {
        "catalog_path": str(catalog_path),
        "catalog_readable": False,
        "generated_at": None,
        "project_real_path_to_id": {},
        "capability_paths_by_project": {},
        "wiki_paths_by_project": {},
        "staleness_by_project": {},
        "load_error": None,
    }
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            raw = f.read(MAX_CATALOG_BYTES + 1)
    except OSError as e:
        result["load_error"] = f"{type(e).__name__}: {e}"
        return result
    if len(raw) > MAX_CATALOG_BYTES:
        result["load_error"] = f"catalog.json exceeds size cap ({MAX_CATALOG_BYTES} bytes)"
        return result
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        result["load_error"] = f"{type(e).__name__}: {e}"
        return result

    result["catalog_readable"] = True
    result["generated_at"] = data.get("generated_at")

    projects = data.get("projects", [])
    if not isinstance(projects, list):
        projects = []
    for p in projects:
        if not isinstance(p, dict):
            continue
        rp = p.get("real_path")
        pid = p.get("project_id")
        if isinstance(rp, str) and isinstance(pid, str):
            result["project_real_path_to_id"][os.path.realpath(rp)] = pid

    for c in data.get("capabilities", []) or []:
        if not isinstance(c, dict):
            continue
        pid = c.get("project_id")
        path = c.get("path")
        if isinstance(pid, str) and isinstance(path, str):
            result["capability_paths_by_project"].setdefault(pid, set()).add(_normalize(path))

    for w in data.get("wiki_pages", []) or []:
        if not isinstance(w, dict):
            continue
        pid = w.get("project_id")
        path = w.get("path")
        if isinstance(pid, str) and isinstance(path, str):
            result["wiki_paths_by_project"].setdefault(pid, set()).add(_normalize(path))

    for p in projects:
        if not isinstance(p, dict):
            continue
        pid = p.get("project_id")
        rp = p.get("real_path")
        sources = p.get("sources", {})
        if not isinstance(pid, str) or not isinstance(rp, str) or not isinstance(sources, dict):
            continue
        proj_root = Path(rp)
        entry: dict[str, Any] = {"checked_sources": {}, "stale": False, "reasons": []}
        for fname in ("reusable-capabilities.json", "orca-context-wiki.json"):
            rec = sources.get(fname)
            if not isinstance(rec, dict) or rec.get("status") != "ok":
                continue
            recorded_sha = rec.get("sha256")
            actual_path = proj_root / "wiki" / fname
            actual_sha = sha256_file(actual_path) if actual_path.exists() else None
            match = actual_sha is not None and recorded_sha == actual_sha
            entry["checked_sources"][fname] = {
                "recorded_sha256": recorded_sha,
                "actual_sha256_now": actual_sha,
                "matches": match,
            }
            if not match:
                entry["stale"] = True
                entry["reasons"].append(
                    f"{fname}: recorded sha256 in catalog.json no longer matches the file on disk."
                )
        result["staleness_by_project"][pid] = entry

    return result


def resolve_project_id(root: Path, baseline: dict) -> tuple[str, bool]:
    """Exact real_path match against catalog.json's projects[]; else a
    flagged fallback, never treated as a trustworthy Orca-derived
    project_id.

    NOTE: as of the fix for the two P0s documented in the module docstring's
    CONTENT-HASH-KEYED STATE PERSISTENCE section, this function's return
    value is NO LONGER part of `hit_id`'s identity -- `hit_id` is now keyed
    on the root's resolved real path directly (see `compute_hit_id()` /
    `cmd_scan()`). `project_id` remains a real, stored field (used for
    `review list --project-id` filtering and catalog-baseline dedup, both
    legitimately project_id-keyed concepts) but a collision or a rename
    here can no longer break state persistence.

    The fallback is still kept real-path-keyed anyway (`f"unmatched:
    {root.name}:{sha256(real)[:16]}"` rather than just `f"unmatched:
    {root.name}"`), for two independent reasons even though it no longer
    feeds hit_id: (1) it is still displayed and filterable via `review list
    --project-id`, where a basename-only collision between two unrelated
    worktrees would still be a confusing, misleading label even though it
    can no longer corrupt state; (2) it is still used for catalog-baseline
    staleness lookups (`baseline["staleness_by_project"]`), which are
    genuinely keyed by this value. Confirmed by direct reproduction (before
    this uniqueness fix): two roots with distinct file content and the same
    basename produced the identical fallback project_id -- see
    test_unmatched_roots_with_same_basename_do_not_collide."""
    real = os.path.realpath(str(root))
    pid = baseline["project_real_path_to_id"].get(real)
    if pid is not None:
        return pid, True
    return f"unmatched:{root.name}:{sha256_hex(real)[:16]}", False


# ---------------------------------------------------------------------------
# Walk -- SENSIBLE_EXCLUDE dev-tooling prune (unconditional) plus noise
# rules C (staging-dir suffix) and D (overlay dirs), each independently
# toggleable via `disabled_rules`.
# ---------------------------------------------------------------------------


def _is_overlay_dirname(name: str) -> bool:
    return name.startswith(".") and "overlay" in name.lower()


def scan_root(root: Path, exclude_set: set[str], max_file_bytes: int, disabled_rules: set[str]) -> tuple[list[dict], dict]:
    hits: list[dict] = []
    stats: dict[str, Any] = {
        "root": str(root),
        "dirs_visited": 0,
        "dirs_pruned": 0,
        "dirs_pruned_by_noise_rule": {"C": 0, "D": 0},
        "files_seen_total": 0,
        "py_files_seen": 0,
        "md_files_seen": 0,
        "files_stated": 0,
        "files_content_read": 0,
        "bytes_read": 0,
        "skipped_too_large": 0,
        "symlinks_skipped": 0,
        "read_errors": 0,
        "skill_dirs_found": 0,
        "wall_seconds": None,
    }
    root = root.resolve()
    t0 = time.monotonic()

    def _onerror(_exc: OSError) -> None:
        stats["read_errors"] += 1

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_onerror):
        stats["dirs_visited"] += 1

        pruned_here = [d for d in dirnames if d in exclude_set]
        if pruned_here:
            stats["dirs_pruned"] += len(pruned_here)
            dirnames[:] = [d for d in dirnames if d not in exclude_set]

        if "C" not in disabled_rules:
            staged = [d for d in dirnames if d.endswith(STAGED_REVIEW_ONLY_SUFFIX)]
            if staged:
                stats["dirs_pruned_by_noise_rule"]["C"] += len(staged)
                dirnames[:] = [d for d in dirnames if not d.endswith(STAGED_REVIEW_ONLY_SUFFIX)]

        if "D" not in disabled_rules:
            overlay = [d for d in dirnames if _is_overlay_dirname(d)]
            if overlay:
                stats["dirs_pruned_by_noise_rule"]["D"] += len(overlay)
                dirnames[:] = [d for d in dirnames if not _is_overlay_dirname(d)]

        dp = Path(dirpath)
        rel_dir = dp.relative_to(root)

        skill_hit = detect_skill_dir_signal(dp, filenames)
        if skill_hit:
            stats["skill_dirs_found"] += 1
            skill_md_raw, skill_md_reason = read_file_bounded(dp / "SKILL.md", max_file_bytes)
            content_sha256 = hashlib.sha256(skill_md_raw).hexdigest() if skill_md_raw is not None else None
            hits.append({
                "path": str(rel_dir) if str(rel_dir) != "." else "(root)",
                "kind": "dir",
                "content_sha256": content_sha256,
                "content_hash_status": "ok" if skill_md_raw is not None else (skill_md_reason or "error"),
                **skill_hit,
            })

        for fname in filenames:
            stats["files_seen_total"] += 1
            fpath = dp / fname
            rel_path = fpath.relative_to(root)
            if fname.endswith(".py"):
                stats["py_files_seen"] += 1
                sig, raw = detect_script_signal(fpath, stats, max_file_bytes)
                if sig:
                    content_sha256 = hashlib.sha256(raw).hexdigest() if raw is not None else None
                    hits.append({
                        "path": str(rel_path),
                        "kind": "file",
                        "content_sha256": content_sha256,
                        "content_hash_status": "ok" if raw is not None else "error",
                        **sig,
                    })
            elif fname.endswith(".md"):
                stats["md_files_seen"] += 1
                sig = detect_knowledge_md_signal(fpath, rel_path.parts)
                if sig:
                    try:
                        st = fpath.stat()
                        size, mtime = st.st_size, st.st_mtime
                    except OSError:
                        size, mtime = None, None
                    raw, reason = read_file_bounded(fpath, max_file_bytes)
                    content_sha256 = hashlib.sha256(raw).hexdigest() if raw is not None else None
                    hits.append({
                        "path": str(rel_path),
                        "kind": "file",
                        "size_bytes": size,
                        "mtime": mtime,
                        "content_sha256": content_sha256,
                        "content_hash_status": "ok" if raw is not None else (reason or "error"),
                        **sig,
                    })

    stats["wall_seconds"] = time.monotonic() - t0
    return hits, stats


def annotate_hits_for_root(
    hits: list[dict], project_id: str, project_id_is_catalog_match: bool, baseline: dict, root_source: str,
    root_real_path: str,
) -> None:
    cap_baseline = baseline["capability_paths_by_project"].get(project_id, set())
    wiki_baseline = baseline["wiki_paths_by_project"].get(project_id, set())
    for h in hits:
        h["project_id"] = project_id
        h["project_id_is_catalog_match"] = project_id_is_catalog_match
        h["root_source"] = root_source
        h["root_real_path"] = root_real_path
        norm_path = _normalize(h["path"])
        if h["signal_type"].startswith("capability:"):
            h["already_in_catalog_baseline"] = norm_path in cap_baseline
        else:
            h["already_in_catalog_baseline"] = norm_path in wiki_baseline
        h["is_new_discovery"] = not h["already_in_catalog_baseline"]
        # Identity for rerun-to-rerun state persistence: keyed on
        # root_real_path, NOT project_id -- see module docstring's
        # CONTENT-HASH-KEYED STATE PERSISTENCE section and
        # compute_hit_id()'s own docstring for why.
        h["hit_id"] = compute_hit_id(root_real_path, h["signal_type"], h["path"])
        h["duplicate_of"] = None
        h["noise_signals"] = []


# ---------------------------------------------------------------------------
# Noise rules A/B -- global (cross-root) post-processing over the combined
# hit list. C/D are directory prunes applied during the walk (see
# scan_root above); A/B annotate individual hit records instead.
# ---------------------------------------------------------------------------


def apply_noise_rule_a_content_duplicates(
    all_hits: list[dict], disabled_rules: set[str], allow_cross_project_cluster: bool = False
) -> int:
    """Cluster hits sharing an identical content_sha256; keep the
    lexicographically shortest relative path as primary. Returns the count
    of hits marked as non-primary duplicates.

    Clustering is scoped to (root_real_path, content_sha256) by default, NOT
    content_sha256 alone. The real, validated finding this rule generalizes
    (140/405 hits, 34.6%) was six dated snapshots INSIDE ONE repo. Clustering
    across project boundaries by default instead swept up genuinely distinct
    projects: this codebase's own "copy, don't import" convention (M8 design
    3.0.2) -- visible in these two files' own independently-maintained
    write_only_within/acquire_lock copies -- deliberately manufactures
    byte-identical files across projects, as does any skill deployed into
    multiple worktrees. Reproduced directly: a legitimate, un-catalogued
    capability in project P2 was silently marked dismissed by a
    --apply-to-duplicate-cluster call that named a hit in an unrelated
    project P1, stamped with P1's own note. `--allow-cross-project-cluster`
    opts back into the old cross-project behavior for a caller who
    genuinely wants it (matching review_capability_candidates.py's own
    mark --apply-to-duplicate-cluster --allow-cross-project-cluster).

    Scoped on `root_real_path`, NOT `project_id` -- P1-1 in the max-tier
    Gate C re-review found the P0-2 fix (hit_id moved from project_id to
    root_real_path) was incomplete: this clustering key was left on
    project_id, which this machine's real catalog.json proves is neither
    stable nor unique (two different real projects, e.g. "hgcloud" and
    "rn邮箱", each map to multiple distinct real_path rows sharing one
    project_id string). Scoping on project_id therefore let a cluster
    silently span two different real projects that merely happened to
    share a project_id, exactly the cross-project leak this scoping exists
    to prevent -- see
    test_cluster_scoped_by_root_real_path_not_shared_project_id."""
    if "A" in disabled_rules:
        return 0
    by_hash: dict[tuple[str, str] | str, list[dict]] = {}
    for h in all_hits:
        sha = h.get("content_sha256")
        if sha:
            key = sha if allow_cross_project_cluster else (h.get("root_real_path"), sha)
            by_hash.setdefault(key, []).append(h)
    affected = 0
    for group in by_hash.values():
        if len(group) < 2:
            continue
        primary = min(group, key=lambda h: (len(h["path"]), h["path"], h["hit_id"]))
        for h in group:
            if h is primary:
                continue
            h["duplicate_of"] = primary["hit_id"]
            if "content_duplicate" not in h["noise_signals"]:
                h["noise_signals"].append("content_duplicate")
            affected += 1
    return affected


def apply_noise_rule_b_round_artifacts(all_hits: list[dict], disabled_rules: set[str]) -> int:
    """Annotate (not drop) any hit whose basename matches ROUND[0-9]+,
    case-insensitive. Returns the count of hits newly annotated."""
    if "B" in disabled_rules:
        return 0
    affected = 0
    for h in all_hits:
        basename = Path(h["path"]).name
        if ROUND_ARTIFACT_RE.search(basename):
            if "iteration_round_artifact" not in h["noise_signals"]:
                h["noise_signals"].append("iteration_round_artifact")
                affected += 1
    return affected


# ---------------------------------------------------------------------------
# Write surface: write_only_within / atomic_write_within / lockfile --
# an independently-maintained copy of detect_capability_changes.py's own
# reduced copy of build_cross_project_catalog.py's pattern. Copied, not
# imported (M8 design 3.0.2).
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


def append_ndjson_line(base_dir: Path, final_path: Path, entry: dict[str, Any]) -> None:
    """Append one line to an NDJSON file, creating it if absent. Same
    O_APPEND discipline as promote_capability.py's append_ledger(). Also
    fsyncs the containing directory after the first line creates the file,
    matching atomic_write_within()'s own durability discipline for the same
    base_dir -- otherwise a crash right after a fresh run log's first
    write could lose the new directory entry even though the line itself
    was fsynced to the (now-orphaned) file."""
    line = _sanitize_line_separators(json.dumps(entry, ensure_ascii=False)) + "\n"
    existed_before = os.path.lexists(str(final_path))
    fd = os.open(str(final_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    if not existed_before:
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
            raise DiscoverFatal("lock_held")
    raise DiscoverFatal("lock_held")


def release_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        os.unlink(str(lock_path))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Existing discovery-hits.json -- read for state-persistence merge. Never
# raises; an unusable existing file degrades to "treat as empty" (every new
# hit starts at pending), matching detect_capability_changes.py's
# load_previous_hashes() tolerance for its own optional prior-run input.
# ---------------------------------------------------------------------------


def load_existing_hits(path: Path) -> tuple[dict[str, dict], str, str | None]:
    """Returns (hit_id -> record map, status, reason)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read(MAX_EXISTING_HITS_BYTES + 1)
    except FileNotFoundError:
        return {}, "absent", None
    except OSError as exc:
        return {}, "unreadable", str(exc)
    if len(raw) > MAX_EXISTING_HITS_BYTES:
        return {}, "unreadable", "exceeds size cap"
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {}, "unreadable", str(exc)
    if not isinstance(doc, dict) or not isinstance(doc.get("hits"), list):
        return {}, "unreadable", "no 'hits' array at top level"
    out: dict[str, dict] = {}
    for entry in doc["hits"]:
        if isinstance(entry, dict) and isinstance(entry.get("hit_id"), str):
            out[entry["hit_id"]] = entry
    return out, "ok", None


def _hit_is_positively_gone_or_changed(rec: dict, max_file_bytes: int) -> bool:
    """True only when we have POSITIVE, on-disk evidence that a previously
    recorded hit's underlying file is gone or its content changed -- i.e.
    the record must NOT be blindly carried forward. Absence of evidence
    (path can't be reconstructed, a transient read error, or the content
    genuinely still matches) means the caller should carry the record
    forward. See P2-1 (max-tier Gate C re-review): a hit not being
    re-detected in a given run because a noise rule setting differs from
    the run that produced it, or because of a transient read error
    (EMFILE/ENFILE/EACCES on one file in a large --all-projects walk), is
    NOT equivalent to "the file is gone" and must not be treated as such --
    only used for hits whose root WAS scanned this run but which this
    run's own signal detection did not happen to reproduce (see cmd_scan);
    a root never scanned this run at all carries forward unconditionally,
    with no need to consult this function."""
    root_real_path = rec.get("root_real_path")
    rel_path = rec.get("path")
    if not isinstance(root_real_path, str) or not isinstance(rel_path, str):
        return False  # can't reconstruct a path to check -- no evidence, do not drop
    abs_path = Path(root_real_path) if rel_path == "(root)" else Path(root_real_path) / rel_path
    if rec.get("kind") == "dir":
        if not abs_path.is_dir():
            return True  # positively gone
        target = abs_path / "SKILL.md"
    else:
        if not abs_path.exists():
            return True  # positively gone
        target = abs_path
    prior_sha = rec.get("content_sha256")
    if not isinstance(prior_sha, str):
        return False  # nothing recorded to compare content against -- no evidence either way
    raw, _reason = read_file_bounded(target, max_file_bytes)
    if raw is None:
        return False  # unreadable right now (possibly the same transient condition that
        # suppressed redetection this run) -- not positive evidence of change
    return hashlib.sha256(raw).hexdigest() != prior_sha


def _backup_unreadable_hits_file(base_dir: Path, src_path: Path, run_id: str) -> Path:
    """Streams src_path's raw bytes to a fresh sibling file under base_dir,
    named uniquely by this run's run_id so it can never collide with (or be
    silently overwritten by) a prior backup. Called only when
    load_existing_hits() reported "unreadable" -- i.e. the file exists but
    couldn't be parsed as a valid discovery-hits.json (corrupt JSON, wrong
    top-level shape, or over MAX_EXISTING_HITS_BYTES). Raises OSError (never
    swallowed) if EITHER the source can't be read at all (a permissions
    problem, not just a content problem -- the caller must refuse to
    proceed in that case, not silently destroy unrecoverable state) or the
    destination can't be written. Streams in fixed-size chunks rather than
    reading the whole file into memory first, since the file that triggered
    this path may itself be the oversized one."""
    dst_path = base_dir / f"{HITS_NAME}.corrupt-backup-{run_id}"
    with open(src_path, "rb") as src:
        fd = os.open(str(dst_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                os.write(fd, chunk)
            os.fsync(fd)
        finally:
            os.close(fd)
    return dst_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="M8-2 auto-scan discovery: read-only filesystem scan for un-catalogued capability/knowledge candidates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Scan one or more project roots for discovery hits.")
    scan.add_argument("--root", action="append", dest="root", default=None,
                       help="Project root to scan (repeatable). Not used to auto-enumerate the fleet.")
    scan.add_argument("--all-projects", action="store_true", dest="all_projects",
                       help="Additionally resolve scan roots from catalog.json's own projects[].real_path. "
                            "Prints an unsuppressible stderr warning: may take seconds to several minutes.")
    scan.add_argument("--catalog", type=str, default=DEFAULT_CATALOG_PATH, help="Path to catalog.json (read-only).")
    scan.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES, dest="max_file_bytes")
    scan.add_argument("--disable-noise-rule", action="append", dest="disable_noise_rule",
                       choices=list(ALL_NOISE_RULES), default=None,
                       help="Disable one noise rule (repeatable): A=content-duplicate clustering, "
                            "B=ROUND-artifact annotation, C=-STAGED-review-only pruning, D=overlay-dir pruning.")
    scan.add_argument("--include-round-artifacts-in-summary", action="store_true",
                       dest="include_round_artifacts_in_summary",
                       help="Include noise-rule-B-annotated hits in the summary's convenience "
                            "new_discoveries_total count (both numbers are always in the JSON body regardless).")
    scan.add_argument("--allow-cross-project-cluster", action="store_true",
                       dest="allow_cross_project_cluster",
                       help="Let noise rule A cluster content-duplicates across DIFFERENT scanned "
                            "roots (default: clustering is scoped to within one root_real_path).")
    scan.add_argument("--json", action="store_true", help="Print the run summary as one JSON object to stdout.")
    scan.add_argument("--quiet", action="store_true", help="Suppress human-readable text; rely on the exit code.")
    scan.set_defaults(func=cmd_scan_entry)

    return parser


def _emit_error(args: argparse.Namespace, code: int, reason: str, message: str | None = None) -> int:
    if not getattr(args, "quiet", False):
        payload: dict[str, Any] = {"ok": False, "reason": reason}
        if message:
            payload["message"] = message
        if getattr(args, "json", False):
            print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False)), file=sys.stderr)
        else:
            suffix = f" ({message})" if message else ""
            print(f"error: {reason}{suffix}", file=sys.stderr)
    return code


def cmd_scan_entry(args: argparse.Namespace) -> int:
    try:
        return cmd_scan(args)
    except DiscoverFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)


def cmd_scan(args: argparse.Namespace) -> int:
    disabled_rules = set(args.disable_noise_rule or [])

    if not args.root and not args.all_projects:
        return _emit_error(args, 2, "no_scope_given", "must pass at least one --root or --all-projects")

    if args.max_file_bytes <= 0:
        return _emit_error(args, 2, "invalid_max_file_bytes", str(args.max_file_bytes))

    explicit_roots: dict[str, Path] = {}
    for r in args.root or []:
        p = Path(r).expanduser()
        if not p.is_dir():
            return _emit_error(args, 2, "root_not_a_directory", str(p))
        explicit_roots[os.path.realpath(str(p))] = p

    catalog_path = Path(args.catalog).expanduser()
    baseline = load_catalog_baseline(catalog_path)

    resolved_roots: list[tuple[Path, str]] = [(p, "explicit") for p in explicit_roots.values()]
    resolved_real: set[str] = set(explicit_roots.keys())
    all_projects_skipped_not_directory: list[str] = []

    if args.all_projects:
        # Unsuppressible: no --quiet gate on this one line, on purpose (see
        # module docstring's SCOPE section) -- nobody should pay this
        # unresolved I/O cost without seeing the warning first.
        print(
            "WARNING: --all-projects resolves scan roots from catalog.json's projects[].real_path and "
            "walks every one of them. Real per-project cost measured 14 files/0.6ms to 67,821 files/2.1s "
            "in the M8-2 Gate C validation run (M8-GATE-C-VALIDATION-REPORT-2026-08-23.md section 2.4); a "
            "full-fleet extrapolation was explicitly left unresolved (\"seconds to ~5.5 minutes\"). This "
            "may take anywhere in that range depending on fleet size.",
            file=sys.stderr,
        )
        if not baseline["catalog_readable"]:
            if not resolved_roots:
                raise DiscoverFatal("catalog_unreadable_and_no_root_fallback", baseline.get("load_error"))
            # Explicit --root roots exist to fall back on; --all-projects
            # simply contributes nothing this run. Not fatal.
        else:
            for real_path, _project_id in baseline["project_real_path_to_id"].items():
                if real_path in resolved_real:
                    continue
                p = Path(real_path)
                if not p.is_dir():
                    all_projects_skipped_not_directory.append(real_path)
                    continue
                resolved_roots.append((p, "all_projects"))
                resolved_real.add(real_path)

    if not resolved_roots:
        raise DiscoverFatal("no_scan_roots_resolved")

    all_hits: list[dict] = []
    per_root_stats: list[dict] = []
    for root, source in resolved_roots:
        project_id, is_match = resolve_project_id(root, baseline)
        # The resolved real path is the identity key hit_id is now built on
        # (see module docstring's CONTENT-HASH-KEYED STATE PERSISTENCE
        # section) -- stable across catalog.json edits, unlike project_id.
        root_real_path = os.path.realpath(str(root))
        hits, stats = scan_root(root, SENSIBLE_EXCLUDE, args.max_file_bytes, disabled_rules)
        annotate_hits_for_root(hits, project_id, is_match, baseline, source, root_real_path)
        stats["project_id"] = project_id
        stats["project_id_is_catalog_match"] = is_match
        stats["root_real_path"] = root_real_path
        stats["baseline_stale"] = baseline["staleness_by_project"].get(project_id, {}).get("stale", False)
        stats["baseline_stale_reasons"] = baseline["staleness_by_project"].get(project_id, {}).get("reasons", [])
        stats["root_source"] = source
        all_hits.extend(hits)
        per_root_stats.append(stats)

    # Every root_real_path actually scanned this run. Used below (inside the
    # lock) to distinguish "this hit's root was scanned and the hit is
    # genuinely gone" (drop -- see test_hit_that_disappears_from_disk_is_
    # not_carried_forward) from "this hit's root was not part of THIS run's
    # scope at all" (an unrelated project simply wasn't asked for -- carry
    # its prior record forward untouched). Without this distinction a
    # scope-narrowed rerun (e.g. `--root A` after a prior `--root A --root
    # B` run) would silently delete every human triage decision recorded
    # for project B, even though B was never rescanned and nothing about it
    # is actually known to have changed. That would violate this file's own
    # documented "single most important correctness requirement". Keyed on
    # root_real_path, not project_id, for the same reason hit_id itself is
    # (see CONTENT-HASH-KEYED STATE PERSISTENCE): project_id can change for
    # an unchanged physical root, and a stale/missing project_id on an old
    # record must not be mistaken for "not scanned" just because it no
    # longer matches this run's freshly-resolved project_id.
    scanned_real_paths = {s["root_real_path"] for s in per_root_stats}

    affected_a = apply_noise_rule_a_content_duplicates(
        all_hits, disabled_rules, allow_cross_project_cluster=bool(args.allow_cross_project_cluster)
    )
    affected_b = apply_noise_rule_b_round_artifacts(all_hits, disabled_rules)

    dirs_pruned_by_noise_rule = {
        "C": sum(s["dirs_pruned_by_noise_rule"]["C"] for s in per_root_stats),
        "D": sum(s["dirs_pruned_by_noise_rule"]["D"] for s in per_root_stats),
    }

    # Every signal_type actually seen this run gets an entry in BOTH maps,
    # even if a given map's count for it is 0 -- a signal_type whose only
    # hits are noise-rule-B-excluded must show up as `0`, not vanish from
    # new_by_signal_excl entirely. A consumer diffing the two maps should
    # see a count change, never a schema change (see P3-2 in the Gate C
    # adversarial review).
    by_signal: dict[str, int] = {}
    new_by_signal_excl: dict[str, int] = {}
    new_by_signal_incl: dict[str, int] = {}
    for h in all_hits:
        st = h["signal_type"]
        by_signal[st] = by_signal.get(st, 0) + 1
        new_by_signal_excl.setdefault(st, 0)
        new_by_signal_incl.setdefault(st, 0)
        if h["is_new_discovery"]:
            new_by_signal_incl[st] += 1
            if "iteration_round_artifact" not in h["noise_signals"]:
                new_by_signal_excl[st] += 1

    new_discoveries_excl = sum(
        1 for h in all_hits if h["is_new_discovery"] and "iteration_round_artifact" not in h["noise_signals"]
    )
    new_discoveries_incl = sum(1 for h in all_hits if h["is_new_discovery"])
    new_discoveries_default = new_discoveries_incl if args.include_round_artifacts_in_summary else new_discoveries_excl
    new_by_signal_default = new_by_signal_incl if args.include_round_artifacts_in_summary else new_by_signal_excl

    output_dir = DEFAULT_OUTPUT_DIR
    try:
        os.makedirs(str(output_dir), mode=0o700, exist_ok=True)
    except OSError as exc:
        raise DiscoverFatal("output_dir_uncreatable", str(exc)) from exc
    if os.path.islink(str(output_dir)):
        raise DiscoverFatal("output_dir_is_symlink")

    hits_final, reason = write_only_within(output_dir, str(output_dir / HITS_NAME))
    if reason or hits_final is None:
        raise DiscoverFatal(reason or "invalid_path")
    runs_dir_final, reason = write_only_within(output_dir, str(output_dir / RUNS_DIR_NAME))
    if reason or runs_dir_final is None:
        raise DiscoverFatal(reason or "invalid_path")
    try:
        os.makedirs(str(runs_dir_final), mode=0o700, exist_ok=True)
    except OSError as exc:
        raise DiscoverFatal("runs_dir_uncreatable", str(exc)) from exc
    if os.path.islink(str(runs_dir_final)):
        raise DiscoverFatal("runs_dir_is_symlink")
    output_dir = hits_final.parent

    lock_path = acquire_lock(output_dir)
    try:
        run_id = f"{int(time.time() * 1000)}-{os.getpid()}"
        generated_at = now_iso()

        existing_map, existing_status, existing_reason = load_existing_hits(hits_final)

        # An existing discovery-hits.json that could not be read/parsed
        # ("unreadable" -- corrupt JSON, wrong shape, or over
        # MAX_EXISTING_HITS_BYTES) must NEVER be silently dropped and
        # overwritten: it may still hold every human triage decision ever
        # recorded. Confirmed by direct reproduction: before this fix, a
        # corrupted discovery-hits.json produced exit 0 and a fresh
        # all-pending document with no trace beyond a buried
        # existing_hits_file_status field, and 6 real records collapsed to
        # 3 fresh ones with no way back. Back the raw bytes up (streamed,
        # not loaded whole into memory) to a sibling file under the SAME
        # trusted output_dir before proceeding; if even that read fails
        # (e.g. a permissions problem, not just a content problem), refuse
        # to write at all rather than risk destroying unrecoverable state --
        # see _backup_unreadable_hits_file()'s own docstring.
        existing_hits_backup_path: str | None = None
        existing_hits_backup_status: str | None = None
        if existing_status == "unreadable":
            try:
                backup_path = _backup_unreadable_hits_file(output_dir, hits_final, run_id)
            except OSError as exc:
                raise DiscoverFatal("existing_hits_file_unreadable_backup_failed", str(exc)) from exc
            existing_hits_backup_path = str(backup_path)
            existing_hits_backup_status = "ok"
            print(
                f"WARNING: existing {HITS_NAME} at {hits_final} was unreadable ({existing_reason}); "
                f"its original bytes were backed up to {backup_path} before this run's fresh write. "
                "Prior triage state in it could not be automatically recovered.",
                file=sys.stderr,
            )

        preserved_count = 0
        for h in all_hits:
            prev = existing_map.get(h["hit_id"])
            if prev is not None and prev.get("state") in TERMINAL_STATES:
                h["state"] = prev["state"]
                h["state_note"] = prev.get("state_note")
                h["state_set_by"] = prev.get("state_set_by")
                h["state_set_at"] = prev.get("state_set_at")
                preserved_count += 1
            else:
                h["state"] = "pending"
                h["state_note"] = None
                h["state_set_by"] = None
                h["state_set_at"] = None

        # Carry forward, byte-for-byte, any existing hit record whose
        # project was NOT part of this run's scanned scope at all (see
        # `scanned_project_ids` above). This is what keeps a scope-narrowed
        # rerun (e.g. `--root A` after a prior `--root A --root B` run, or
        # an --all-projects run where one project row temporarily failed to
        # resolve) from silently wiping another project's discovery history
        # -- including terminal triage states -- out of the persisted file.
        # A hit whose project WAS scanned this run but simply wasn't
        # rediscovered (e.g. the file was deleted) is correctly NOT carried
        # forward here -- see test_hit_that_disappears_from_disk_is_not_
        # carried_forward, which this must continue to satisfy.
        current_hit_ids = {h["hit_id"] for h in all_hits}
        carried_forward_hits: list[dict] = []
        carried_forward_not_redetected_count = 0
        for hit_id, rec in existing_map.items():
            if hit_id in current_hit_ids or not isinstance(rec, dict):
                continue
            if rec.get("root_real_path") not in scanned_real_paths:
                carried_forward_hits.append(rec)
                continue
            # P2-1 (max-tier Gate C re-review): the root WAS scanned this
            # run, but this run's own signal detection did not happen to
            # reproduce this hit -- e.g. a noise rule (C/D) toggled between
            # this run and the one that produced the record, pruning the
            # directory this run without the underlying file having moved;
            # or a transient read error (EMFILE/ENFILE/EACCES) on this one
            # file during a large --all-projects walk. Neither is positive
            # evidence the file is gone. Only a TERMINAL-state record is
            # worth this extra on-disk check -- a `pending` record not
            # redetected here carries no human-authored state to lose and
            # simply reappears fresh the next time it IS redetected.
            if rec.get("state") not in TERMINAL_STATES:
                continue
            if not _hit_is_positively_gone_or_changed(rec, args.max_file_bytes):
                carried_forward_hits.append(rec)
                carried_forward_not_redetected_count += 1

        scope_payload = {
            "explicit_roots": [str(p) for p in explicit_roots.values()],
            "all_projects": bool(args.all_projects),
            "all_projects_skipped_not_directory": all_projects_skipped_not_directory,
            "resolved_roots": [{"root": str(r), "source": s} for r, s in resolved_roots],
        }
        noise_rules_payload = {
            "A": {"active": "A" not in disabled_rules, "description": "content_duplicate clustering", "hits_affected": affected_a},
            "B": {"active": "B" not in disabled_rules, "description": "iteration_round_artifact annotation", "hits_affected": affected_b},
            "C": {"active": "C" not in disabled_rules, "description": f"prune dirs ending {STAGED_REVIEW_ONLY_SUFFIX!r}", "dirs_pruned": dirs_pruned_by_noise_rule["C"]},
            "D": {"active": "D" not in disabled_rules, "description": "prune '.'+'overlay' dirs", "dirs_pruned": dirs_pruned_by_noise_rule["D"]},
        }
        summary_payload = {
            "total_hits": len(all_hits),
            "hits_by_signal_type": by_signal,
            "new_discoveries_total": new_discoveries_default,
            "new_discoveries_total_excluding_round_artifacts": new_discoveries_excl,
            "new_discoveries_total_including_round_artifacts": new_discoveries_incl,
            "new_discoveries_by_signal_type": new_by_signal_default,
            "include_round_artifacts_in_summary": bool(args.include_round_artifacts_in_summary),
            # NOT the total of all dirs pruned by every mechanism -- this
            # counts only SENSIBLE_EXCLUDE (node_modules, __pycache__, etc,
            # unconditional dev-tooling excludes), NOT noise rules C/D (see
            # dirs_pruned_by_noise_rule below for those). Named explicitly
            # rather than "..._total" so a reader can't mistake a nonzero
            # dirs_pruned_by_noise_rule alongside a "total" of 0 for nothing
            # having been pruned (see P3-1 in the Gate C adversarial review).
            "dirs_pruned_by_sensible_exclude": sum(s["dirs_pruned"] for s in per_root_stats),
            "dirs_pruned_by_noise_rule": dirs_pruned_by_noise_rule,
            "states_preserved_from_previous_terminal_run": preserved_count,
            "hits_carried_forward_from_unscanned_projects": len(carried_forward_hits) - carried_forward_not_redetected_count,
            # P2-1 (max-tier Gate C re-review): a SEPARATE bucket from the
            # field above -- these hits' roots WERE scanned this run, but
            # this run's own signal detection did not happen to reproduce
            # them (a noise rule toggle, or a transient read error), and
            # on-disk evidence did not positively show the file gone or
            # changed. Kept distinct so a reader can tell "this project
            # wasn't in scope" apart from "this project WAS scanned but one
            # of its hits wasn't redetected" -- see
            # _hit_is_positively_gone_or_changed().
            "hits_carried_forward_scanned_but_not_redetected": carried_forward_not_redetected_count,
            "existing_hits_file_status": existing_status,
            "existing_hits_file_reason": existing_reason,
            "existing_hits_file_backup_path": existing_hits_backup_path,
            "existing_hits_file_backup_status": existing_hits_backup_status,
        }
        catalog_baseline_payload = {
            "catalog_path": baseline["catalog_path"],
            "catalog_readable": baseline["catalog_readable"],
            "load_error": baseline["load_error"],
            "catalog_generated_at": baseline["generated_at"],
            "staleness_by_project": baseline["staleness_by_project"],
        }

        payload = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "generator": {"script": SCRIPT_REL_PATH, "version": GENERATOR_VERSION},
            "generated_at": generated_at,
            "run_id": run_id,
            "catalog_baseline": catalog_baseline_payload,
            "scope": scope_payload,
            "noise_rules": noise_rules_payload,
            "roots_scanned": per_root_stats,
            "summary": summary_payload,
            "hits": all_hits + carried_forward_hits,
        }
        atomic_write_within(output_dir, hits_final, _encode_json(payload))

        # The main write surface -- discovery-hits.json -- is the one thing
        # this function's exit code contract ("0 scan completed, even with
        # zero hits") is actually about. It has already landed, durably, by
        # this point. The per-run NDJSON audit log below is a SEPARATE,
        # best-effort artifact: it exists for later forensics/debugging, not
        # for state persistence (that contract lives entirely in
        # discovery-hits.json, already satisfied above). A write failure
        # here (disk full, a permissions problem on runs/, a stale/rotated
        # directory) must NOT be reported as a fatal scan failure -- doing
        # so would make a caller distrust or retry a scan whose real,
        # load-bearing output already succeeded and is already correct on
        # disk. Confirmed by direct reproduction: a read-only runs/ dir
        # caused an uncaught PermissionError to propagate past cmd_scan_
        # entry's narrow `except DiscoverFatal`, past main()'s catch-all,
        # and report exit 4 "unexpected_error" -- even though discovery-
        # hits.json had already been written correctly with the run's real
        # hit. Failures here are therefore caught and annotated instead
        # (this codebase's "annotate degradation, never silently drop"
        # convention -- see module docstring's NOISE RULES section), never
        # allowed to flip the exit code away from what the hits-file write
        # alone earned. See test_ndjson_run_log_write_failure_does_not_
        # fail_an_otherwise_successful_scan.
        run_log_path = runs_dir_final / f"{run_id}.ndjson"
        run_log_errors: list[str] = []
        for stats in per_root_stats:
            try:
                append_ndjson_line(runs_dir_final, run_log_path, {"type": "root_scanned", "run_id": run_id, **stats})
            except OSError as exc:
                run_log_errors.append(str(exc))
        try:
            append_ndjson_line(runs_dir_final, run_log_path, {
                "type": "run_summary",
                "run_id": run_id,
                "generated_at": generated_at,
                "scope": scope_payload,
                "noise_rules": noise_rules_payload,
                "summary": summary_payload,
            })
        except OSError as exc:
            run_log_errors.append(str(exc))
    finally:
        release_lock(lock_path)

    if args.json and not args.quiet:
        print(_sanitize_line_separators(json.dumps({
            "ok": True,
            "hits_output": str(hits_final),
            "run_log": str(run_log_path),
            "run_log_write_errors": run_log_errors,
            "counts": summary_payload,
        }, ensure_ascii=False, indent=1)))
    elif not args.quiet:
        print(
            f"scanned {len(resolved_roots)} root(s): {summary_payload['total_hits']} hits, "
            f"{summary_payload['new_discoveries_total']} new discoveries "
            f"(A={affected_a} dup, B={affected_b} round-artifact, "
            f"C={dirs_pruned_by_noise_rule['C']} dirs pruned, D={dirs_pruned_by_noise_rule['D']} dirs pruned)"
        )
        print(f"wrote {hits_final}")
        if run_log_errors:
            print(f"warning: run log at {run_log_path} was NOT fully written ({len(run_log_errors)} line(s) failed): "
                  f"{run_log_errors[0]}", file=sys.stderr)
        else:
            print(f"wrote {run_log_path}")

    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = getattr(args, "func", None)
    if handler is None:
        return 2
    try:
        return handler(args)
    except DiscoverFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)
    except BaseException as exc:
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
