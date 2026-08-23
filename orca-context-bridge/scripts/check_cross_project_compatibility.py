#!/usr/bin/env python3
"""Read-only Tier-1 downstream-impact query (M8 / Gate A of the cross-project
catalog plan).

Given ONE capability's `global_id`, answers: "which other capabilities in
this fleet declared a `depends_on` edge that resolves to it?" -- i.e. what
would be affected if that capability changed or was removed. This is the
natural companion query to detect_capability_changes.py's `removed` /
`changed` classification: after that tool tells you a capability's content
hash changed or it disappeared, this one tells you who else might care.

NO WRITE PATH AT ALL
---------------------
Same posture as query_catalog.py: this script has no output file, no cache,
no lock, and no atomic-write machinery, because it never writes anything.
There is nothing to guard.

One caveat stated precisely rather than promised away, identical to
query_catalog.py's own: CPython writes `__pycache__/*.pyc` next to this
file when this module is IMPORTED (its own test suite does exactly that).
That is the import system caching bytecode around executing the module
body, so `sys.dont_write_bytecode = True` below is, by construction, too
late to prevent it for THIS module's own import -- it only affects modules
imported AFTER this line runs. RUNNING this file as a script never writes
it (CPython does not cache `__main__`). Either way, `__pycache__/` only
ever appears next to this file itself, in this tool's own directory --
never inside any project this tool reads from -- so it does not bear on
the isolation guarantee above; it is a bookkeeping accuracy note, not a
write-path exception.

PRIMARY SOURCE: capability_reverse_index, NOT A FRESH GRAPH WALK
--------------------------------------------------------------------------
build_cross_project_catalog.py already computed the fleet-wide dependency
graph once, at build time, and published the inbound side of it as
`capability_reverse_index[global_id] = {ref_key, in_degree,
referencing_project_ids, referenced_by[]}` (see that module's
assemble_catalog()). Consuming that published index is the whole point of
Tier-1 being fast and structural: this script never re-derives depends_on
resolution itself in the normal case.

DEGRADED FALLBACK: WHEN THE INDEX ITSELF IS UNUSABLE
--------------------------------------------------------------------------
If `capability_reverse_index` is missing from catalog.json entirely, or is
present but not a JSON object, the authoritative index cannot be consulted
at all -- but the SAME information is redundantly present in
`capabilities[].depends_on[].target_global_id` (every "resolved" edge in the
reverse index was built by iterating exactly this data). So rather than
refusing to answer, this script falls back to scanning `capabilities[]`
directly and reconstructing the answer for this ONE query from first
principles. This is reported as `mode: "fallback_scan"` and always exits 3
("partial trust") regardless of what it finds -- the exit code communicates
CONFIDENCE, not presence/absence, in this branch; the JSON body still
carries a real (possibly empty) `affected[]` list.

A malformed ENTRY inside an otherwise-fine `capability_reverse_index` (e.g.
the one row this query cares about exists but its `referenced_by` is not a
list, or only some of its entries parse) is a narrower problem than the
whole field being gone, and is handled without dropping to fallback_scan
mode: only that row's malformed shape is noted in `warnings[]`, and
whatever can be salvaged from it is still used -- UNLESS what could be
salvaged disagrees with the row's own declared `in_degree` count (see
`row_degraded` below), in which case the exit code degrades to 3 rather
than claiming 0 ("found") or 1 ("confirmed none") on data that does not
actually support that claim.

EXIT-CODE DESIGN DECISIONS, STATED EXPLICITLY (the task asks for this)
--------------------------------------------------------------------------
`--changed-global-id` can fail to resolve to anything for two very
different reasons, and this script treats them differently on purpose:

  1. The value is not SHAPED like a global_id at all (empty, contains a
     control/newline character, or has no '#' anywhere -- global_id is
     always "<project_id>#<suffix>", see
     build_cross_project_catalog.py's mint sites). This is a USAGE error
     (exit 2): the caller asked a question that cannot even be parsed as
     "which capability do you mean?".

  2. The value IS shaped like a plausible global_id, but it does not appear
     anywhere in the catalog right now. This is treated as "confirmed: no
     downstream dependents" (exit 1), NOT a usage error. Reasoning: the
     single most important real call site for this tool is exactly
     "detect_capability_changes.py just told me capability X was REMOVED --
     what depended on it?" -- at the moment of asking, X by definition no
     longer appears anywhere in the fresh catalog. Rejecting that as a
     usage error would break the primary intended workflow. The answer is
     still meaningful and honest: nothing in this catalog currently holds a
     resolved edge pointing at that id. The nuance ("never existed at all"
     vs "existed and was removed" vs "exists but has zero inbound edges")
     is preserved in the JSON body's `global_id_known_in_catalog` field
     rather than collapsed into the exit code, so a caller that wants the
     distinction can still get it without the exit-code contract growing a
     third "not found" value that would collide with 3's existing meaning.

A THIRD case that must NOT be confused with either of the above: the row
DOES exist in `capability_reverse_index` and IS shaped well enough to
attempt an answer, but what could actually be resolved from it disagrees
with what the row itself claims (its own `in_degree` integer does not
equal the number of `referenced_by` entries this script could parse --
build_cross_project_catalog.py's own invariant, stated at its
`capability_reverse_index` assembly site in assemble_catalog(), is that
these two always agree at build time). This can only happen against a
catalog.json this process did not itself just build -- exactly the
"untrusted data written by someone/something else" case every read-only
tool in this project already has to defend against. Reporting exit 1
("confirmed: no downstream dependents") here would be an outright LIE if
`in_degree` is nonzero: the row is telling us dependents exist, we simply
could not enumerate all of them. This is the SAME "queried but could not
be fully answered" situation fallback_scan mode is in, so it reuses that
mode's exit code (3) rather than growing a new value -- but `mode` itself
stays `"reverse_index"` (this is one inconsistent row, not the whole index
being unusable), and `row_degraded: true` plus a
`reverse_index_row_in_degree_mismatch` warning make the reason explicit in
the JSON body.

EXIT CODES
----------
    0  at least one downstream capability was found (reverse-index mode
       only -- fallback_scan mode never returns 0, see above)
    1  confirmed: no downstream dependents (reverse-index mode; includes
       both "the id is known but has zero inbound edges" and "the id does
       not appear in the catalog at all" -- see global_id_known_in_catalog)
    2  usage error -- see the exit-code design section above
    3  partial trust -- either (a) capability_reverse_index itself is
       missing / not an object, answered via a best-effort scan of
       capabilities[] instead (mode: fallback_scan), or (b) reverse-index
       mode found a matching row whose own declared in_degree disagrees
       with what could actually be resolved from referenced_by (mode stays
       reverse_index, row_degraded: true) -- in both cases, claiming 0
       ("found") or 1 ("confirmed none") would overstate confidence the
       data does not support
    4  cannot answer at all -- catalog.json missing/unreadable/unparseable,
       or NEITHER capability_reverse_index NOR capabilities[] is present in
       a usable shape

Run with:
    python3 check_cross_project_compatibility.py affected
        --changed-global-id <global_id> --catalog <path> [--json] [--quiet]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

MAX_CATALOG_BYTES = 16 * 1024 * 1024
_CONTROL_CHARS = ("\n", "\r", "\t", chr(0x2028), chr(0x2029), "\x00")

# ---------------------------------------------------------------------------
# M8 Gate D ("run" subcommand) constants. Tier-1 ("affected") above this line
# is unchanged by Gate D; everything below is additive.
# ---------------------------------------------------------------------------

# Same default location query_catalog.py already reads from -- reused so a
# caller that doesn't pass --catalog gets the same catalog.json every other
# tool in this family defaults to, not a second, silently different default.
DEFAULT_CATALOG_DIR = Path("/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog")
DEFAULT_CATALOG_PATH = DEFAULT_CATALOG_DIR / "catalog.json"

# Copied, not imported -- see query_catalog.py's and catalog_session_hint.py's
# own DEFAULT_STALE_AFTER_HOURS = 6 for the same rationale: M7's background
# rebuild trigger proposes a 6-hour freshness threshold, and "stale" must mean
# the same thing everywhere this catalog is consulted, not a fourth
# independently-tuned number.
DEFAULT_STALE_AFTER_HOURS = 6

# Hard ceiling on any declared timeout_seconds, enforced regardless of what a
# project's own wiki/compat-check.json declares (design doc 3.5.3): a
# project's self-declared timeout is a ceiling INPUT, never the final word.
DEFAULT_TIMEOUT_CEILING_SECONDS = 300.0

# Absolute, sibling to manifests/cross-project-catalog/ per the design doc's
# directory layout (3.0.1) -- deliberately NOT Path(__file__)-relative and
# NOT nested under cross-project-catalog/compat/ (that directory is Tier-1's
# own confirmed/deterministic hash-diff output; this one is Tier-2's raw,
# unauthenticated third-party execution output, a different trust tier that
# must not share a directory tree with it). A Path(__file__)-relative default
# has already caused two real incidents in this exact codebase this week
# (Gate A's DEFAULT_OUTPUT_DIR, Gate B's own test suite's REAL_REPO_ROOT) --
# this is deliberately not a third.
COMPAT_RUNS_ROOT = Path("/Volumes/Extreme SSD/Orca/manifests/compat-runs")

# Copied from promote_capability.py's LOCK_NAME/LOCK_STALE_SECONDS/
# acquire_lock()/release_lock() -- see the "run" subcommand section below.
LOCK_NAME = ".compat-runs.lock"
LOCK_STALE_SECONDS = 300

# wiki/compat-check.json is a small, hand-authored, git-committed file; this
# ceiling only bounds a hostile or corrupt one, same posture as
# MAX_CATALOG_BYTES above.
MAX_COMPAT_CHECK_BYTES = 2 * 1024 * 1024
COMPAT_CHECK_SCHEMA_VERSION_SUPPORTED = 1

# Per-stream captured-output ceiling (design doc 3.5.3 / task spec): stdout
# and stderr are each truncated independently, with an explicit
# truncated:true flag when cut, never silently. Fix-round note (adversarial
# review, injection-and-exec/authz-and-toctou P1): this ceiling used to be
# applied to output AFTER Popen.communicate() had already buffered the
# ENTIRE stream in this process's memory -- a check_command producing
# hundreds of MB/s could OOM the operator's machine long before this cap
# ever mattered. _run_one_check()/_drain_and_wait() below now read each
# stream incrementally and stop RETAINING bytes past this cap while still
# draining the pipe (so the child never blocks on backpressure), which
# makes this a genuine memory bound, not just a display truncation.
MAX_OUTPUT_BYTES = 64 * 1024

# Fix-round addition (adversarial review, authz-and-toctou P2-2 / path-and-
# containment implied by the same review): nothing previously capped how
# many check_command entries a single project's compat-check.json could
# declare, or bounded them to distinct ids. A 2MB document (the existing
# MAX_COMPAT_CHECK_BYTES ceiling) can hold well over ten thousand minimal
# entries, which at the per-check timeout ceiling is weeks of unattended
# serial execution from a single --authorize-project. A small, generous cap
# closes that without affecting any realistic hand-authored document.
MAX_CHECKS_PER_COMPAT_DOCUMENT = 64

# Fix-round addition (injection-and-exec/authz-and-toctou P0+P1: a
# grandchild that calls os.setsid() leaves the process group os.killpg()
# targets, survives SIGKILL, and can keep the inherited stdout/stderr pipe
# write end open forever). After the group is killed, _drain_and_wait()
# gives already-produced output one short, SEPARATELY-bounded grace window
# to drain -- never open-ended -- then gives up and reaps the direct child.
PROCESS_GROUP_KILL_DRAIN_GRACE_SECONDS = 2.0
PROCESS_WAIT_GRACE_SECONDS = 5.0
_READ_CHUNK_SIZE = 64 * 1024
_POLL_INTERVAL_SECONDS = 0.25

# Fix-round addition (injection-and-exec P1: subprocess.Popen was called
# with no `env=`, so a third-party check_command inherited this process's
# COMPLETE environment -- including capability tokens for Orca's own
# orchestration/messaging control plane, and any API key merely exported in
# the operator's shell). Deliberately an ALLOWLIST, not a denylist: a
# denylist silently fails open on the next secret-shaped variable someone
# adds to this machine later, an allowlist does not.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "USER", "LOGNAME", "SHELL")
_DEFAULT_CHECK_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class CheckFatal(Exception):
    """Cannot answer at all -- maps to exit code 4."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Small shared helpers (deliberately duplicated across this project's tools;
# see module docstring in detect_capability_changes.py for why)
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


def _reject_non_finite_json_constant(token: str) -> float:
    """Passed as json.loads()'s parse_constant. Fix-round addition
    (adversarial review, P1/P2-3): Python's json module accepts the bare
    tokens NaN / Infinity / -Infinity by default, and every ordinary
    numeric comparison against a NaN is False -- so `timeout_seconds: NaN`
    silently defeated the `<= 0` validation gate below, and `Infinity` for
    --stale-after-hours/--timeout-ceiling-seconds silently defeated the
    freshness/timeout ceilings the SAME way (`x > inf` and `x > nan` are
    both False). Rejecting these tokens at parse time, rather than trying
    to catch every downstream comparison, closes the whole class at once.
    """
    raise ValueError(f"non-finite JSON constant {token!r} is not permitted")


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _has_control_chars(value: str) -> bool:
    """Copied from promote_capability.py's own _has_control_chars()."""
    return any(ch in value for ch in _CONTROL_CHARS)


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Copied verbatim (not imported) from query_catalog.py's own
    _parse_utc_timestamp(): parses exactly the format
    build_cross_project_catalog.py's now_iso() writes. strptime rather than
    datetime.fromisoformat() for the same reason query_catalog.py uses it --
    this must keep working on the system interpreter, where
    fromisoformat() rejects a trailing 'Z' on older Pythons."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Read-only catalog loading
# ---------------------------------------------------------------------------


def read_catalog_bytes(path: Path) -> bytes:
    """Same descriptor-level guards as query_catalog.py's own
    read_catalog_bytes(): O_NONBLOCK against a planted FIFO, S_ISREG to
    reject anything else non-regular, no O_NOFOLLOW (no TOCTOU window here
    -- the path is either the documented default or one the caller typed,
    read with the caller's own credentials)."""
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise CheckFatal("catalog_missing", str(path))
    except NotADirectoryError:
        raise CheckFatal("catalog_missing", str(path))
    except OSError as exc:
        raise CheckFatal("catalog_unreadable", str(exc))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CheckFatal("catalog_not_a_regular_file", str(path))
        if st.st_size > MAX_CATALOG_BYTES:
            raise CheckFatal("catalog_too_large", f"{st.st_size} bytes > {MAX_CATALOG_BYTES}")
        chunks: list[bytes] = []
        remaining = MAX_CATALOG_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise CheckFatal("catalog_unreadable", str(exc))
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > MAX_CATALOG_BYTES:
        raise CheckFatal("catalog_too_large", f">{MAX_CATALOG_BYTES} bytes")
    return raw


def load_catalog(path: Path) -> dict[str, Any]:
    raw = read_catalog_bytes(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckFatal("catalog_unparseable", str(exc))
    try:
        doc = json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite_json_constant
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise CheckFatal("catalog_unparseable", str(exc))
    if not isinstance(doc, dict):
        raise CheckFatal("catalog_malformed", "top level is not a JSON object")
    return doc


# ---------------------------------------------------------------------------
# --changed-global-id shape validation (usage error vs "confirmed none" --
# see module docstring's exit-code design section)
# ---------------------------------------------------------------------------


def global_id_shape_is_valid(value: str) -> bool:
    if not value:
        return False
    if any(ch in value for ch in _CONTROL_CHARS):
        return False
    if "#" not in value:
        return False
    return True


# ---------------------------------------------------------------------------
# reverse-index consumption
# ---------------------------------------------------------------------------


def _extract_referenced_by(row: dict[str, Any], warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    referenced_by_raw = row.get("referenced_by")
    if not isinstance(referenced_by_raw, list):
        warnings.append(
            {
                "code": "reverse_index_row_referenced_by_not_a_list",
                "message": "the target row's referenced_by is not a list; treating as empty",
            }
        )
        return []
    out: list[dict[str, Any]] = []
    skipped = 0
    for item in referenced_by_raw:
        if not isinstance(item, dict) or not _is_str(item.get("capability_global_id")):
            skipped += 1
            continue
        out.append(
            {
                "global_id": item.get("capability_global_id"),
                "project_id": item.get("project_id") if _is_str(item.get("project_id")) else None,
                "scope": item.get("scope") if _is_str(item.get("scope")) else None,
                "raw": item.get("raw") if _is_str(item.get("raw")) else None,
            }
        )
    if skipped:
        warnings.append(
            {
                "code": "reverse_index_row_skipped_malformed_entries",
                "message": f"{skipped} referenced_by entr(ies) were malformed and skipped",
            }
        )
    return out


def _capability_known(catalog: dict[str, Any], global_id: str) -> bool | None:
    """True/False if capabilities[] is usable enough to answer; None if it
    is not (caller should treat "known" as undetermined rather than
    assuming False)."""
    capabilities = catalog.get("capabilities")
    if not isinstance(capabilities, list):
        return None
    for cap in capabilities:
        if isinstance(cap, dict) and cap.get("global_id") == global_id:
            return True
    return False


def _declared_in_degree(row_is_dict: bool, row: dict[str, Any] | None) -> int | None:
    """The row's own `in_degree` field, if -- and only if -- it is a genuine
    non-negative int. `bool` is deliberately excluded even though
    `isinstance(True, int)` is True in Python: a malformed `in_degree: true`
    must never be silently read as the integer 1. Returns None for "not
    declared / not usable", in which case the caller has nothing of the
    row's own to cross-check `affected` against, and no mismatch is
    possible."""
    if not row_is_dict:
        return None
    value = row.get("in_degree")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def answer_via_reverse_index(catalog: dict[str, Any], reverse_index: dict[str, Any], global_id: str) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    row = reverse_index.get(global_id)
    row_is_dict = isinstance(row, dict)
    if row is not None and not row_is_dict:
        warnings.append(
            {
                "code": "reverse_index_row_not_an_object",
                "message": "the target row in capability_reverse_index is not a JSON object; treating as absent",
            }
        )
        row = None
        row_is_dict = False

    affected = _extract_referenced_by(row, warnings) if row_is_dict else []
    declared_in_degree = _declared_in_degree(row_is_dict, row)
    in_degree = declared_in_degree if declared_in_degree is not None else len(affected)
    referencing_project_ids = (
        row.get("referencing_project_ids") if row_is_dict and isinstance(row.get("referencing_project_ids"), list) else None
    )

    if row is not None:
        known: bool | None = True
    else:
        capability_known_result = _capability_known(catalog, global_id)
        known = capability_known_result if capability_known_result is not None else False
        if capability_known_result is None:
            warnings.append(
                {
                    "code": "capabilities_not_a_list",
                    "message": "capabilities[] is absent or not a list; cannot confirm whether this global_id ever existed",
                }
            )

    # See the module docstring's "THIRD case" -- a row that IS present and
    # IS a dict can still disagree with itself: its own declared in_degree
    # and what we could actually resolve from referenced_by can diverge on
    # a catalog.json this process did not build. Reporting exit 1
    # ("confirmed none") in that state would overclaim confidence the data
    # does not support, so it degrades to exit 3 (partial trust) instead,
    # same posture as fallback_scan, while `mode` stays "reverse_index"
    # because only this one row is inconsistent, not the whole index.
    row_degraded = declared_in_degree is not None and declared_in_degree != len(affected)
    if row_degraded:
        warnings.append(
            {
                "code": "reverse_index_row_in_degree_mismatch",
                "message": (
                    f"row declares in_degree={declared_in_degree} but only "
                    f"{len(affected)} referenced_by entr(ies) could be resolved; "
                    "treating this row as partially trusted, not confirmed-none"
                ),
            }
        )

    if row_degraded:
        exit_code = 3
    elif affected:
        exit_code = 0
    else:
        exit_code = 1

    return {
        "mode": "reverse_index",
        "affected": affected,
        "in_degree": in_degree,
        "referencing_project_ids": referencing_project_ids,
        "global_id_known_in_catalog": known,
        "row_degraded": row_degraded,
        "warnings": warnings,
        "exit_code": exit_code,
    }


def answer_via_fallback_scan(catalog: dict[str, Any], global_id: str, degraded_reason: str) -> dict[str, Any]:
    capabilities = catalog["capabilities"]  # caller guarantees this is a list
    affected: list[dict[str, Any]] = []
    referencing_project_ids: list[str] = []
    skipped = 0
    for cap in capabilities:
        if not isinstance(cap, dict):
            skipped += 1
            continue
        depends_on = cap.get("depends_on")
        if not isinstance(depends_on, list):
            continue
        cap_global_id = cap.get("global_id")
        cap_project_id = cap.get("project_id") if _is_str(cap.get("project_id")) else None
        for dep in depends_on:
            if not isinstance(dep, dict):
                continue
            if dep.get("state") == "resolved" and dep.get("target_global_id") == global_id:
                affected.append(
                    {
                        "global_id": cap_global_id if _is_str(cap_global_id) else None,
                        "project_id": cap_project_id,
                        "scope": dep.get("scope") if _is_str(dep.get("scope")) else None,
                        "raw": dep.get("raw") if _is_str(dep.get("raw")) else None,
                    }
                )
                if cap_project_id is not None and cap_project_id not in referencing_project_ids:
                    referencing_project_ids.append(cap_project_id)

    warnings = [{"code": "reverse_index_unavailable", "message": degraded_reason}]
    if skipped:
        warnings.append({"code": "skipped_non_dict_entries", "message": f"{skipped} capabilities[] entries were not JSON objects"})

    known = _capability_known(catalog, global_id)
    if known is None:
        known = bool(affected)  # best-effort: found as a dependency target implies it exists

    return {
        "mode": "fallback_scan",
        "affected": affected,
        "in_degree": len(affected),
        "referencing_project_ids": referencing_project_ids,
        "global_id_known_in_catalog": known,
        # Always False here: fallback_scan's exit 3 is "the whole index was
        # unusable", a different reason than reverse_index mode's own
        # row_degraded ("the index was fine, but this one row disagreed
        # with itself") -- kept present-and-False rather than absent so
        # every result dict has the same key regardless of mode.
        "row_degraded": False,
        "warnings": warnings,
        "exit_code": 3,
    }


def check_affected(catalog: dict[str, Any], global_id: str) -> dict[str, Any]:
    reverse_index = catalog.get("capability_reverse_index")
    if isinstance(reverse_index, dict):
        result = answer_via_reverse_index(catalog, reverse_index, global_id)
    else:
        capabilities = catalog.get("capabilities")
        if not isinstance(capabilities, list):
            raise CheckFatal(
                "reverse_index_and_capabilities_both_unusable",
                "capability_reverse_index is missing/not an object, and capabilities[] is also missing/not a list",
            )
        reason = (
            "capability_reverse_index is missing from the catalog"
            if reverse_index is None
            else "capability_reverse_index is present but not a JSON object"
        )
        result = answer_via_fallback_scan(catalog, global_id, reason)

    result["changed_global_id"] = global_id
    result["catalog_verified_at"] = catalog.get("verified_at") if _is_str(catalog.get("verified_at")) else None
    result["counts"] = {"affected": len(result["affected"])}
    return result


# ===========================================================================
# M8 Gate D: "run" subcommand (Tier-2 -- ACTUALLY EXECUTES third-party
# declared commands). Everything below is new; nothing above this point is
# touched by Gate D.
#
# SECURITY POSTURE (read this before changing anything below)
# --------------------------------------------------------------------------
# This is the single highest-risk piece of code in the whole cross-project
# catalog plan: it runs argv declared by ANOTHER project's own committed
# wiki/compat-check.json, with no OS-level sandbox (accepted for v1). Three
# non-negotiable invariants, each with a name a future patch must not quietly
# undo:
#
#   1. NEVER shell=True, never string-join argv, never pass through a shell.
#      subprocess.Popen(argv, shell=False, ...) with argv a list is the only
#      call shape used anywhere below.
#   2. Every field pulled off disk that becomes a FILESYSTEM PATH is
#      format-validated IMMEDIATELY BEFORE that specific use, even if the
#      same string was already validated earlier in this same call for a
#      different purpose. This is Gate B's own P0 lesson (a `record["id"]`
#      that was ID_RE-validated at draft time but never re-validated at
#      approve-time, plus a containment check on an intermediate directory
#      instead of the FINAL joined path, together produced a real,
#      independently-reproduced path-traversal write outside the declared
#      project). See _project_id_shape_reason() and _compat_run_output_path()
#      below -- the latter re-validates project_id on every call, not just
#      once at the top of run_compatibility_checks().
#   3. A timeout kills the WHOLE process group the declared command spawned
#      (os.killpg), never just the direct child (proc.kill() alone would
#      leave orphaned grandchildren running on this real machine after this
#      tool has already reported the check "timed out").
# ===========================================================================


# ---------------------------------------------------------------------------
# project_id validation -- copied (not imported) from promote_capability.py's
# _validate_target_project(). NOT a strict [a-z0-9-]+ identifier regex on
# purpose: build_cross_project_catalog.py's derive_expected_project_id() can
# legitimately mint a project_id containing '/' (a "<topic>/<task>" shallow
# workspace layout), so a project_id string is only rejected for containing
# a control character or an empty/'.'/'..'  path segment -- the same
# permissive-but-still-traversal-safe rule this codebase already uses for
# exactly this field elsewhere, not a new one invented for this file.
# ---------------------------------------------------------------------------


def _project_id_shape_reason(value: Any) -> str | None:
    if not _is_str(value) or not value:
        return "project_id must be a non-empty string"
    if _has_control_chars(value):
        return "project_id must not contain a control character"
    segments = value.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return "project_id must not contain an empty, '.', or '..' path segment"
    return None


# ---------------------------------------------------------------------------
# Path containment -- copied (not imported) from promote_capability.py's /
# detect_capability_changes.py's write_only_within() + _has_symlink_component()
# family. Used here for TWO distinct purposes that need two distinct variants
# of the same underlying discipline:
#
#   * the compat-runs OUTPUT path (COMPAT_RUNS_ROOT/<run_id>/<project_id>.json)
#     -- write_only_within() itself, unmodified, is exactly the right shape:
#     project_id can contain '/' (see above), so the final joined path may
#     legitimately be more than one segment below run_dir, and
#     write_only_within() already handles that (it checks the FINAL resolved
#     path's containment via relative_to(), not a fixed parent-equality
#     check) -- this is the actual fix class from Gate B's P0, reused as-is
#     rather than re-derived.
#
#   * a compat-check.json entry's declared "cwd" -- NOT write_only_within(),
#     because that helper's "is_base_root" rule (reject path == base_dir)
#     exists to stop THIS tool from ever writing directly at the root of its
#     OWN staging area, which does not apply here: "cwd": "." (run the check
#     command in the project's own root) is an explicitly valid, ordinary
#     case in the compat-check.json schema. _resolve_check_cwd() below is the
#     same lexical + symlink-component + resolved triple layer, minus that
#     one inapplicable rule.
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
    """Copied verbatim from promote_capability.py's atomic_write_within()."""
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


def _resolve_check_cwd(project_root: Path, relative: Any) -> tuple[Path | None, str | None]:
    if not _is_str(relative) or not relative:
        return None, "cwd_not_a_non_empty_string"
    if relative.startswith("/") or "\\" in relative or _has_control_chars(relative):
        return None, "cwd_invalid_characters"
    segments = relative.split("/")
    if any(seg == "" for seg in segments):
        return None, "cwd_empty_path_segment"
    if any(seg == ".." for seg in segments):
        # Belt: obviously-traversal segments rejected outright before any
        # resolution is attempted. Suspenders: the resolve()+relative_to()
        # checks below catch this independently even if this line were ever
        # removed -- exactly the "don't rely on a single layer" posture
        # Gate B's P0 postmortem called for.
        return None, "cwd_parent_segment"

    lexical_root = project_root.absolute()
    lexical_path = (lexical_root / relative).absolute()
    try:
        lexical_path.relative_to(lexical_root)
    except ValueError:
        return None, "cwd_outside_project_root"
    if _has_symlink_component(lexical_path, lexical_root):
        return None, "cwd_symlink_component"

    resolved_root = project_root.resolve(strict=False)
    resolved_path = (project_root / relative).resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None, "cwd_outside_project_root"

    return resolved_path, None


def _compat_run_output_path(run_dir: Path, project_id: str) -> tuple[Path | None, str | None]:
    """THE point-of-use re-validation (invariant #2 in the module-section
    docstring above): project_id is re-checked here, immediately before it
    becomes a path component, regardless of whatever validation already
    happened earlier in this same call for this same string."""
    reason = _project_id_shape_reason(project_id)
    if reason is not None:
        return None, reason
    return write_only_within(run_dir, str(run_dir.absolute() / f"{project_id}.json"))


# ---------------------------------------------------------------------------
# Lock + run_id allocation -- copied (not imported) from promote_capability.py's
# acquire_lock()/release_lock(). Held ONLY long enough to allocate a
# not-yet-existing run_id directory; released before any check_command runs
# (design doc 3.5.3 / task spec step 7 -- a slow externally-declared check
# must never block an unrelated concurrent "run" invocation).
# ---------------------------------------------------------------------------


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
            raise CheckFatal("lock_held")
    raise CheckFatal("lock_held")


def release_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        os.unlink(str(lock_path))
    except OSError:
        pass


def ensure_compat_runs_root(root: Path) -> None:
    try:
        os.makedirs(str(root), mode=0o700, exist_ok=True)
    except OSError as exc:
        raise CheckFatal("compat_runs_root_uncreatable", str(exc)) from exc
    if os.path.islink(str(root)):
        raise CheckFatal("compat_runs_root_is_symlink")


def _allocate_run_id(compat_runs_root: Path) -> tuple[str, Path]:
    lock_path = acquire_lock(compat_runs_root)
    try:
        for _ in range(8):
            candidate = uuid.uuid4().hex
            run_dir = compat_runs_root / candidate
            try:
                os.mkdir(str(run_dir), 0o700)
                return candidate, run_dir
            except FileExistsError:
                continue
        raise CheckFatal("run_id_allocation_exhausted")
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Project-root resolution from catalog.json's own projects[] array. Copied
# (not imported) from promote_capability.py's build_project_root_index(),
# which itself already reproduces build_cross_project_catalog.py's own
# real_path/status conventions. project_id -> real_path only; never a fresh
# `orca repo list`/`worktree list` call, never a candidate/entry's own
# self-reported path string.
# ---------------------------------------------------------------------------


def build_project_root_index(catalog: dict[str, Any]) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    """Returns (project_id -> resolved root, warnings). Fix-round changes
    (adversarial review, path-and-containment P2-6/P3-2):
      * a `real_path` that is not an absolute path is now DROPPED entirely
        rather than kept as a candidate -- a relative real_path resolves
        against the CALLING process's own cwd, which is exactly the "staging
        location != deployed location" incident class this codebase has
        already hit twice this week (see COMPAT_RUNS_ROOT's own comment).
      * an ambiguous duplicate (more than one `status: "ok"` row for the
        same project_id) used to fall through to a silent lexicographic
        tie-break with no signal anywhere that it had happened. It still
        tie-breaks the same way (unchanged behavior for any caller already
        relying on it), but now also emits a warning the caller can surface."""
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
            if not Path(real_path).is_absolute():
                continue
            by_id.setdefault(pid, []).append(row)

    roots: dict[str, Path] = {}
    warnings: list[dict[str, Any]] = []
    for pid, members in by_id.items():
        if len(members) == 1:
            roots[pid] = Path(members[0]["real_path"])
            continue
        ok_rows = [m for m in members if m.get("status") == "ok"]
        if len(ok_rows) == 1:
            chosen = ok_rows[0]
        else:
            chosen = sorted(members, key=lambda m: m["real_path"])[0]
            warnings.append(
                {
                    "code": "ambiguous_project_root",
                    "message": (
                        f"projects[] has {len(members)} candidate real_path rows for "
                        f"project_id {pid!r}; chose {chosen['real_path']!r} by lexicographic tie-break"
                    ),
                }
            )
        roots[pid] = Path(chosen["real_path"])
    return roots, warnings


# ---------------------------------------------------------------------------
# wiki/compat-check.json: fresh read (TOCTOU-closing re-read, design doc
# 3.5.3 step 6b) + schema validation. The whole document is validated as a
# unit: ANY structural violation anywhere in it skips the WHOLE project's
# checks for this run (task spec: "that ONE project's result records a
# skipped outcome ... does not raise, does not abort other projects'
# checks") -- it does not attempt a finer-grained per-entry skip.
# ---------------------------------------------------------------------------


def _reject_duplicate_keys_local(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON object")
        seen[key] = value
    return seen


def _read_compat_check(project_root: Path) -> tuple[Any, str | None, str | None]:
    """Fresh, right-now, descriptor-level read of
    <project_root>/wiki/compat-check.json. Returns (doc, None, sha256_hex)
    on success, or (None, reason, None) for any failure -- never raises for
    an ordinary missing/malformed file (that is what makes this a
    per-project "skip", not a fatal abort of the whole run).

    Fix-round addition (adversarial review, path-and-containment P1-2):
    `os.open(..., O_NOFOLLOW)` on the FULL joined path only guards the
    FINAL path component (compat-check.json itself) -- it says nothing
    about the "wiki" directory component. A project could commit
    `wiki -> /somewhere/attacker-writable` (git stores symlinks natively)
    and this function would follow it transparently, executing whatever
    check_command lives at the symlink target instead of the project's own
    reviewed declaration. `_has_symlink_component()` (already used for the
    `cwd` field and for compat-run OUTPUT paths elsewhere in this file) is
    reused here for the exact same discipline on the READ side."""
    path = project_root / "wiki" / "compat-check.json"
    if _has_symlink_component(path, project_root):
        return None, "compat_check_symlink_component", None
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "compat_check_missing", None
    except OSError as exc:
        return None, f"compat_check_unreadable:{exc}", None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "compat_check_not_a_regular_file", None
        if st.st_size > MAX_COMPAT_CHECK_BYTES:
            return None, "compat_check_too_large", None
        chunks: list[bytes] = []
        remaining = MAX_COMPAT_CHECK_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        return None, f"compat_check_unreadable:{exc}", None
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > MAX_COMPAT_CHECK_BYTES:
        return None, "compat_check_too_large", None
    try:
        text = raw.decode("utf-8")
        doc = json.loads(
            text, object_pairs_hook=_reject_duplicate_keys_local, parse_constant=_reject_non_finite_json_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, "compat_check_not_valid_json", None
    return doc, None, hashlib.sha256(raw).hexdigest()


def _validate_compat_check_document(doc: Any, project_root: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Structural validation only -- never checks reviewed_by/reviewed_at
    for truthfulness (design doc 2.1#6 / 3.5.3: accepted, documented risk,
    not this function's job). Returns (validated_entries, None) on success,
    or (None, reason) on the FIRST structural violation found -- the whole
    document is treated as one unit, per this section's module comment."""
    if not isinstance(doc, dict):
        return None, "not_an_object"
    schema_version = doc.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != COMPAT_CHECK_SCHEMA_VERSION_SUPPORTED:
        return None, "unsupported_schema_version"
    checks = doc.get("checks")
    if not isinstance(checks, list):
        return None, "checks_not_a_list"
    if len(checks) > MAX_CHECKS_PER_COMPAT_DOCUMENT:
        # Fix-round addition (adversarial review, authz-and-toctou P2-2): no
        # limit here meant a single 2MB compat-check.json (MAX_COMPAT_CHECK_
        # BYTES) could declare over ten thousand entries, i.e. weeks of
        # unattended serial execution from one --authorize-project.
        return None, "too_many_checks"

    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(checks):
        if not isinstance(entry, dict):
            return None, f"entry_not_an_object:{index}"

        entry_id = entry.get("id")
        if not _is_str(entry_id) or not entry_id or _has_control_chars(entry_id):
            return None, f"entry_bad_id:{index}"
        if entry_id in seen_ids:
            # Fix-round addition: a duplicate id makes the persisted run
            # record ambiguous about which entry a given result row came
            # from (see the per-check "id" field in _execute_one_project_
            # plan's output) -- reject rather than silently keep both.
            return None, f"entry_duplicate_id:{index}"
        seen_ids.add(entry_id)

        depends_on_ref = entry.get("depends_on_ref")
        if not _is_str(depends_on_ref) or not depends_on_ref or _has_control_chars(depends_on_ref):
            return None, f"entry_bad_depends_on_ref:{index}"

        check_command = entry.get("check_command")
        if not isinstance(check_command, list) or not check_command:
            return None, f"entry_bad_check_command:{index}"
        for arg in check_command:
            if not _is_str(arg) or not arg or "\x00" in arg:
                # Fix-round addition: `id`/`depends_on_ref`/`cwd`/
                # `reviewed_by`/`reviewed_at` all reject control characters
                # via _has_control_chars(), but check_command's own argv
                # elements previously did not -- a NUL byte in an argv
                # element reaches subprocess.Popen() and raises an uncaught
                # `ValueError: embedded null byte` there (POSIX exec cannot
                # represent NUL inside an argument string). Rejecting it
                # here, at validation time, is strictly better than relying
                # on _execute_one_project_plan's own try/except to catch it
                # after the fact -- though that catch (see its "partial"
                # outcome handling) is also fixed as defense-in-depth.
                return None, f"entry_bad_check_command:{index}"

        resolved_cwd, cwd_reason = _resolve_check_cwd(project_root, entry.get("cwd"))
        if resolved_cwd is None:
            return None, f"entry_bad_cwd:{index}:{cwd_reason}"

        timeout_seconds = entry.get("timeout_seconds")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            # Fix-round addition: `not math.isfinite(...)` closes the P1
            # found in independent review -- `float('nan') <= 0` is False,
            # so a bare `timeout_seconds: NaN` used to sail through this
            # gate, then blow up as an uncaught ValueError deep inside
            # subprocess timeout handling AFTER the child had already been
            # spawned (spawn-then-leak: the process was never killed or
            # reaped on that path). parse_constant on the JSON reader above
            # also rejects the literal NaN/Infinity tokens at the door, so
            # this check is defense-in-depth for any value that reaches
            # here some other way (e.g. a huge finite float divided down).
            return None, f"entry_bad_timeout_seconds:{index}"

        reviewed_by = entry.get("reviewed_by")
        if not _is_str(reviewed_by) or not reviewed_by or _has_control_chars(reviewed_by):
            return None, f"entry_bad_reviewed_by:{index}"

        reviewed_at = entry.get("reviewed_at")
        if not _is_str(reviewed_at) or not reviewed_at or _has_control_chars(reviewed_at):
            return None, f"entry_bad_reviewed_at:{index}"

        validated.append(
            {
                "id": entry_id,
                "depends_on_ref": depends_on_ref,
                "check_command": list(check_command),
                "resolved_cwd": resolved_cwd,
                # Fix-round addition: the entry's OWN raw cwd string is kept
                # alongside the already-resolved path so the execution loop
                # can re-run _resolve_check_cwd() immediately before each
                # Popen (see the module-section invariant #2 comment, and
                # the loop in _execute_one_project_plan() below) -- a
                # sibling entry in the SAME document can rewrite this
                # directory between whole-document validation and this
                # entry's own turn to execute.
                "raw_cwd": entry.get("cwd"),
                "timeout_seconds": float(timeout_seconds),
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
            }
        )
    return validated, None


# ---------------------------------------------------------------------------
# Process execution -- shell=False always, argv always a list, process-group
# isolation + process-group kill on timeout (task spec's "DANGEROUS-PROCESS-
# TREE requirement": subprocess.run's default timeout kills only the direct
# child, which can leave a declared check_command's own grandchildren running
# as orphans after this tool has already reported "timed out").
# ---------------------------------------------------------------------------


def _truncate_output(data: bytes, exceeded_cap: bool) -> tuple[str, bool]:
    # `data` is already capped at MAX_OUTPUT_BYTES RAW bytes by
    # _drain_and_wait() below -- this no longer slices a fully-buffered
    # stream after the fact (see MAX_OUTPUT_BYTES's own comment). The
    # decode step is additionally bounded to MAX_OUTPUT_BYTES *characters*
    # too: `errors="replace"` can expand one invalid byte into a 3-byte
    # U+FFFD, so decoding a byte-capped buffer can still produce a string
    # up to ~3x the documented cap (adversarial review, P3) -- slicing
    # again after decode makes the documented bound actually true.
    text = data.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES]
    return text, exceeded_cap


def _minimal_check_env() -> dict[str, str]:
    """The environment handed to a third-party check_command: an explicit
    ALLOWLIST (see _ENV_ALLOWLIST's own comment), never
    subprocess.Popen's implicit whole-environment inheritance."""
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    env.setdefault("PATH", _DEFAULT_CHECK_PATH)
    return env


def _drain_and_wait(proc: subprocess.Popen, deadline: float) -> tuple[bytes, bool, bytes, bool, bool]:
    """Read `proc`'s stdout/stderr incrementally, each independently capped
    at MAX_OUTPUT_BYTES of RETAINED bytes (though always fully drained past
    that point, so the child never blocks on pipe backpressure), and never
    blocking past the absolute `deadline` (a time.monotonic() reading)
    regardless of whether either stream reaches EOF.

    This deliberately replaces Popen.communicate() rather than wrapping it.
    communicate() has two properties that were each an independently
    reproduced finding in adversarial review:
      (1) it buffers the ENTIRE stream in this process's memory before any
          cap can be applied (a check_command producing ~300 MB/s can OOM
          the operator's machine long before a 64KB display cap matters);
      (2) once a caller has already used its one bounded `timeout=`
          argument and must recover from TimeoutExpired, a SECOND,
          unbounded communicate() call is the only way to collect
          remaining output -- and that call blocks on pipe EOF, which a
          descendant that escaped the process group via its own
          os.setsid() (surviving the group's SIGKILL, still holding the
          inherited pipe write end open) can withhold forever. That is a
          hard hang, not a slow path: the tool's own hard timeout ceiling
          (DEFAULT_TIMEOUT_CEILING_SECONDS) becomes advisory.

    Returns (stdout_bytes, stdout_exceeded_cap, stderr_bytes,
    stderr_exceeded_cap, timed_out).
    """

    sel = selectors.DefaultSelector()
    buffers: dict[int, bytearray] = {}
    totals: dict[int, int] = {}
    fd_stdout = proc.stdout.fileno() if proc.stdout is not None else None
    fd_stderr = proc.stderr.fileno() if proc.stderr is not None else None
    open_fds: set[int] = set()
    for fd in (fd_stdout, fd_stderr):
        if fd is None:
            continue
        os.set_blocking(fd, False)
        sel.register(fd, selectors.EVENT_READ)
        buffers[fd] = bytearray()
        totals[fd] = 0
        open_fds.add(fd)

    def _read_until(read_deadline: float) -> None:
        while open_fds:
            remaining = read_deadline - time.monotonic()
            if remaining <= 0:
                return
            for key, _ in sel.select(timeout=min(remaining, _POLL_INTERVAL_SECONDS)):
                fd = key.fd
                try:
                    chunk = os.read(fd, _READ_CHUNK_SIZE)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    sel.unregister(fd)
                    open_fds.discard(fd)
                    continue
                totals[fd] += len(chunk)
                buf = buffers[fd]
                if len(buf) < MAX_OUTPUT_BYTES:
                    buf.extend(chunk[: MAX_OUTPUT_BYTES - len(buf)])
                # Bytes beyond MAX_OUTPUT_BYTES are read (to drain the pipe
                # and avoid stalling the child) and then discarded -- never
                # retained then sliced later. This is the actual memory
                # bound; `totals[fd]` alone tracks whether more arrived.

    # Fix-round addition (independently reproduced P1, two variants): the
    # process-group id is captured HERE, before any wait/reap can happen
    # below, and used as the fixed kill target for the rest of this
    # function. os.getpgid(proc.pid) would raise ProcessLookupError once
    # `proc` has been reaped -- but start_new_session=True guarantees the
    # GROUP itself was created equal to the direct child's own pid at
    # spawn time, and a process group persists past its founding member's
    # exit as long as any other member (e.g. a forked grandchild) is
    # still in it. Capturing the id now, while `proc` is still guaranteed
    # alive (Popen just returned successfully), keeps the kill target
    # valid regardless of when -- or whether -- the direct child gets
    # reaped first.
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None  # should not happen this soon after a successful Popen

    timed_out = False
    try:
        # Phase 1: read until both streams hit EOF or the declared deadline.
        _read_until(deadline)

        # BUG FIXED HERE (independent review, two reproduced variants):
        # `open_fds` emptying out only proves this check_command's own
        # stdout/stderr pipes hit EOF -- it does NOT prove the check (or
        # anything it spawned into the same process group) has actually
        # finished running. A direct child that closes/redirects fd 1/2
        # while continuing to run past its declared timeout, or a forked
        # grandchild that closes its own copies of those fds while a
        # short-lived direct child immediately os._exit()s, both make
        # `open_fds` empty long before anything in the group is actually
        # done -- the second case is the more severe of the two: it was
        # reported back as a false PASS (timed_out=False, exit_code=0)
        # while the grandchild kept running, un-killed.
        #
        # Signal A (unchanged from before the fix): the pipes never both
        # reached EOF by the deadline. Kept as-is because it is still the
        # ONLY signal that can catch a setsid()-escaped descendant -- one
        # that left the process group entirely and so cannot be reached
        # by the os.killpg() call below at all (see this function's own
        # docstring on that accepted, out-of-scope residual), but that
        # can still be holding the inherited pipe write end open.
        pipes_still_open = bool(open_fds)

        # Signal B (the fix): whether anything is still alive in the
        # check_command's process GROUP once its declared budget is
        # exhausted -- deliberately not `proc.poll()` on the direct
        # child alone, since a direct child that has already exited
        # (e.g. via os._exit() immediately after forking) can leave a
        # grandchild that inherited the same process group still
        # running, and `proc.poll()` alone would miss that grandchild
        # entirely (this is exactly the false-pass variant above).
        if not pipes_still_open:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                # Phase 1 returned early because both pipes hit EOF, not
                # because the deadline arrived. Give the direct child
                # the rest of its declared budget to actually finish
                # before deciding anything is stuck -- a fast,
                # well-behaved check that happens to close its own
                # stdio early and then exits cleanly BEFORE the deadline
                # must still be reported as a normal success, not
                # killed.
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    pass

        # Sending SIGKILL straight to the group and reading whether that
        # raised (nothing left to signal) IS the group-scoped equivalent
        # of "is it still running": it relies on the same os.killpg()
        # primitive this function already used before the fix, never on
        # pipe state, and deliberately does not track individual
        # descendant PIDs. On this platform (confirmed empirically), a
        # group whose only remaining member is an unreaped zombie --
        # e.g. the direct child's own zombie, after everything else
        # (such as a setsid()-escaped descendant) has left the group --
        # raises PermissionError rather than ProcessLookupError; a group
        # with a genuinely live member (direct child or descendant)
        # always succeeds regardless of whether the direct child itself
        # has already been reaped. Both exceptions mean the same thing
        # here: nothing reachable in this group is actually alive.
        group_was_alive = False
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
                group_was_alive = True
            except (ProcessLookupError, PermissionError):
                group_was_alive = False

        timed_out = pipes_still_open or group_was_alive

        if timed_out:
            # Phase 2: one short, SEPARATELY-bounded best-effort drain after
            # the kill. A descendant that stayed IN the process group dies
            # immediately, so this returns almost instantly; a grandchild
            # that escaped via setsid() cannot extend this call past the
            # fixed grace window no matter how long it keeps running.
            _read_until(time.monotonic() + PROCESS_GROUP_KILL_DRAIN_GRACE_SECONDS)
    finally:
        sel.close()

    try:
        proc.wait(timeout=PROCESS_WAIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass  # proc.returncode stays None below -- reported honestly, never guessed
    finally:
        # Close our end of the pipes explicitly rather than relying on
        # Popen's own GC-time cleanup -- this function may run once per
        # declared check_command (up to MAX_CHECKS_PER_COMPAT_DOCUMENT per
        # project), so leaving fds to close themselves is a slow fd leak
        # across a run with many checks, not just a ResourceWarning.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    stdout_bytes = bytes(buffers.get(fd_stdout, b"")) if fd_stdout is not None else b""
    stdout_exceeded = fd_stdout is not None and totals.get(fd_stdout, 0) > MAX_OUTPUT_BYTES
    stderr_bytes = bytes(buffers.get(fd_stderr, b"")) if fd_stderr is not None else b""
    stderr_exceeded = fd_stderr is not None and totals.get(fd_stderr, 0) > MAX_OUTPUT_BYTES
    return stdout_bytes, stdout_exceeded, stderr_bytes, stderr_exceeded, timed_out


def _run_one_check(argv: list[str], cwd: Path, timeout_seconds: float) -> dict[str, Any]:
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        # Defense-in-depth only: the normal document-validation path
        # (_validate_compat_check_document) already rejects a non-finite or
        # non-positive timeout_seconds long before it could reach here.
        # This guard exists so a future caller cannot resurrect the
        # "spawn the process, then hang or leak it forever" bug class found
        # in independent review just by skipping that earlier validation --
        # it refuses to call Popen at all rather than compute a deadline
        # that can never be reached (e.g. start + nan).
        return {
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": 0.0,
            "stdout": "",
            "stdout_truncated": False,
            "stderr": f"refusing to execute with a non-finite/non-positive timeout_seconds={timeout_seconds!r}",
            "stderr_truncated": False,
        }

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=_minimal_check_env(),
            start_new_session=True,  # POSIX process-group isolation
        )
    except OSError as exc:
        return {
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": time.monotonic() - start,
            "stdout": "",
            "stdout_truncated": False,
            "stderr": f"failed to start check_command: {exc}",
            "stderr_truncated": False,
        }

    deadline = start + timeout_seconds
    try:
        stdout_bytes, stdout_over, stderr_bytes, stderr_over, timed_out = _drain_and_wait(proc, deadline)
    except BaseException:
        # Fix-round addition: if anything above raises for a reason we did
        # not anticipate, the spawned process must still be killed and
        # reaped here rather than leaked -- this is the second half of the
        # "spawn-then-leak" lesson from independent review (the first half
        # is refusing to spawn at all on a bad timeout, above).
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=PROCESS_WAIT_GRACE_SECONDS)
        except Exception:
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        raise

    duration = time.monotonic() - start
    stdout_text, stdout_truncated = _truncate_output(stdout_bytes, stdout_over)
    stderr_text, stderr_truncated = _truncate_output(stderr_bytes, stderr_over)
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "stdout": stdout_text,
        "stdout_truncated": stdout_truncated,
        "stderr": stderr_text,
        "stderr_truncated": stderr_truncated,
    }


# ---------------------------------------------------------------------------
# Catalog freshness -- copied (not imported) from query_catalog.py's own
# search_catalog() freshness block: verified_at missing, unparseable, OR
# ahead of this clock all count as stale. Unprovable freshness must never
# read as proven freshness.
# ---------------------------------------------------------------------------


def _catalog_freshness(catalog: dict[str, Any], now: datetime, stale_after_seconds: float) -> tuple[bool, int | None, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    verified_at_raw = catalog.get("verified_at")
    verified_at = _parse_utc_timestamp(verified_at_raw)
    if verified_at is None:
        age_seconds: int | None = None
        stale = True
        if verified_at_raw is None:
            warnings.append({"code": "verified_at_missing", "message": "catalog has no verified_at"})
        else:
            warnings.append(
                {
                    "code": "verified_at_unparseable",
                    "message": f"verified_at {verified_at_raw!r} is not YYYY-MM-DDTHH:MM:SSZ",
                }
            )
    else:
        age_seconds = int((now - verified_at).total_seconds())
        stale = age_seconds > stale_after_seconds
        if age_seconds < 0:
            stale = True
            warnings.append(
                {
                    "code": "verified_at_in_future",
                    "message": f"verified_at {verified_at_raw!r} is ahead of this clock by {-age_seconds}s",
                }
            )
    return stale, age_seconds, warnings


# ---------------------------------------------------------------------------
# Per-project execution + atomic output write.
# ---------------------------------------------------------------------------


def _finalize_project_result(result: dict[str, Any], run_dir: Path, global_id: str) -> dict[str, Any]:
    """Writes `result`'s per-project output file. NEVER RAISES -- every
    failure mode is caught and folded into `result` itself (a warning, plus
    downgrading "ok" to "unrecorded" so a refused/failed write is visible in
    the outcome and not just in a warning list a caller might not inspect).

    Fix-round rationale (adversarial review, path-and-containment P1-1):
    this function used to let an OSError from the write itself (e.g.
    ENAMETOOLONG from an over-long but shape-valid project_id, a full disk,
    a read-only mount) propagate uncaught. The caller's own `except
    Exception` handler then called BACK into this same function to record
    the failure -- which raised the IDENTICAL error again, this time with
    no handler above it, aborting the entire run and losing every OTHER
    project's already-computed results along with it. Independently
    reproduced: a project_id of 300 'A's takes down a run that also
    contained an otherwise-successful second project. See also P2-2: a
    write that IS refused (containment check, symlinked run directory) used
    to leave `outcome` at "ok" even though nothing was ever persisted --
    that made a write failure invisible in the run's aggregate counts."""
    final_path, reason = _compat_run_output_path(run_dir, result["project_id"])
    if final_path is None:
        # Must not happen in practice (project_id already passed the same
        # shape check earlier before we ever got here) -- but if this
        # re-validation ever DOES disagree with the earlier one, that
        # disagreement itself is the bug class Gate B's P0 was: never
        # write, surface it instead of trusting the earlier check.
        result["warnings"].append({"code": "output_path_rejected", "message": str(reason)})
        if result["outcome"] == "ok":
            result["outcome"] = "unrecorded"
        return result
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.islink(str(final_path.parent)):
            raise CheckFatal("compat_run_project_dir_is_symlink")
    except OSError as exc:
        result["warnings"].append({"code": "output_dir_uncreatable", "message": str(exc)})
        if result["outcome"] == "ok":
            result["outcome"] = "unrecorded"
        return result
    except CheckFatal as exc:
        # `raise CheckFatal(...)` above used to sit inside a bare `except
        # OSError` block that cannot catch it (CheckFatal is not an
        # OSError) -- it escaped uncaught. Handled explicitly now.
        result["warnings"].append(
            {"code": exc.reason, "message": "refusing to write into a symlinked compat-run project directory"}
        )
        if result["outcome"] == "ok":
            result["outcome"] = "unrecorded"
        return result

    payload = {
        "global_id": global_id,
        "project_id": result["project_id"],
        "outcome": result["outcome"],
        "project_root": result.get("project_root"),
        "compat_check_sha256": result.get("compat_check_sha256"),
        "checks": result["checks"],
        "warnings": result["warnings"],
        "written_at": now_iso(),
    }
    try:
        body = _sanitize_line_separators(json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False)).encode(
            "utf-8"
        )
        atomic_write_within(run_dir, final_path, body)
    except OSError as exc:
        # The write itself failed (this is the exact P1-1 reproduction:
        # ENAMETOOLONG from an over-long but shape-valid project_id; also
        # reachable via a full disk or a read-only mount). This project's
        # checks may have ALREADY EXECUTED -- that cannot be undone -- but
        # this function must still return a normal result dict rather than
        # raise, both so THIS project's failure is reported honestly
        # (outcome "unrecorded", not silently lost) and so it can never
        # cascade into losing every OTHER project's results in the same run
        # the way it used to.
        result["warnings"].append({"code": "output_write_failed", "message": str(exc)})
        if result["outcome"] == "ok":
            result["outcome"] = "unrecorded"
        return result

    result["output_path"] = str(final_path)
    return result


def _load_one_project_plan(
    *,
    project_id: str,
    global_id: str,
    project_roots: dict[str, Path],
) -> dict[str, Any]:
    """Phase 1 for one authorized project_id: resolve its root, freshly
    read+validate its wiki/compat-check.json, and select the entries
    relevant to `global_id`. Does NOT execute anything. Every authorized
    project's Phase 1 runs to completion (see run_compatibility_checks)
    BEFORE any project's Phase 2 (_execute_one_project_plan) begins.

    Fix-round rationale (adversarial review, authz-and-toctou P1-3): when
    reading happened lazily, per-project, at that project's own turn in a
    single interleaved loop, an EARLIER authorized project's check_command
    -- which already runs with no sandbox by design -- could rewrite a
    LATER authorized project's wiki/compat-check.json before that project's
    own turn came up. That deterministically (not just racily, since
    execution order is `sorted()` and therefore predictable from the
    catalog alone) let one project's declared, reviewed check_command be
    silently substituted for another's, with the persisted run record still
    attributing the resulting execution to the victim project. Reading and
    validating every authorized project's document up front, before ANY of
    them executes so much as one check_command, closes that: no authorized
    project's own execution can happen early enough to affect what a LATER
    project's plan already captured. The existing "fresh read closes the
    catalog-to-execution TOCTOU" property (design doc 3.5.3 step 6b) is
    unaffected -- these reads still happen freshly, relative to run start,
    strictly after the affected-set computation over catalog.json.

    Returns {"ready": True, "project_root", "entries", "result"} when there
    is something to execute, or {"ready": False, "result"} when this
    project's outcome is already decided (its `result` still needs to go
    through _finalize_project_result by the caller either way)."""
    result: dict[str, Any] = {
        "project_id": project_id,
        "outcome": "skipped",
        "checks": [],
        "warnings": [],
        "output_path": None,
        "project_root": None,
        "compat_check_sha256": None,
    }

    # Invariant #2 (module-section docstring): re-validate project_id HERE,
    # at the very first point it could be used for anything filesystem-
    # related in this function, regardless of it having already passed the
    # --authorize-project subset check upstream (that check only proves
    # membership in a semi-trusted aggregate, never shape-safety for path
    # use -- see the project_id-path-injection test). If this is invalid,
    # this project gets NO output file at all: its own project_id is not
    # safe to use as that file's name either.
    shape_reason = _project_id_shape_reason(project_id)
    if shape_reason is not None:
        result["warnings"].append({"code": "project_id_invalid", "message": shape_reason})
        return {"ready": False, "result": result}

    try:
        real_path = project_roots.get(project_id)
        if real_path is None:
            result["warnings"].append(
                {"code": "project_root_unresolvable", "message": "no projects[] row for this project_id"}
            )
            return {"ready": False, "result": result}

        project_root = Path(real_path).resolve(strict=False)
        result["project_root"] = str(project_root)
        if not project_root.is_dir():
            result["warnings"].append({"code": "project_root_missing", "message": str(project_root)})
            return {"ready": False, "result": result}

        doc, read_reason, compat_check_sha256 = _read_compat_check(project_root)
        if read_reason is not None:
            result["warnings"].append({"code": read_reason, "message": "wiki/compat-check.json"})
            return {"ready": False, "result": result}
        result["compat_check_sha256"] = compat_check_sha256

        validated, violation_reason = _validate_compat_check_document(doc, project_root)
        if violation_reason is not None:
            result["warnings"].append({"code": "compat_check_invalid", "message": violation_reason})
            return {"ready": False, "result": result}

        # The narrower of the two readings of design doc 3.5.3: match the
        # SPECIFIC capability under test, not "any check declared anywhere
        # in this project's compat-check.json".
        selected = [entry for entry in validated if entry["depends_on_ref"] == global_id]
        if not selected:
            result["warnings"].append(
                {"code": "no_matching_checks", "message": f"no checks declared against {global_id}"}
            )
            return {"ready": False, "result": result}

        return {"ready": True, "project_root": project_root, "entries": selected, "result": result}
    except Exception as exc:  # noqa: BLE001 -- must never abort other projects' checks
        result["outcome"] = "skipped"
        result["warnings"].append({"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"})
        return {"ready": False, "result": result}


def _execute_one_project_plan(
    plan: dict[str, Any],
    *,
    run_dir: Path,
    global_id: str,
    timeout_ceiling_seconds: float,
) -> dict[str, Any]:
    """Phase 2 for one project whose Phase 1 (_load_one_project_plan)
    returned ready=True: actually run its selected check_command(s)."""
    result = plan["result"]
    project_root = plan["project_root"]
    checks_out: list[dict[str, Any]] = []
    try:
        for entry in plan["entries"]:
            # Invariant #2, re-applied here specifically for `cwd`: it was
            # already resolved once during whole-document validation
            # (_validate_compat_check_document, for every entry at once),
            # but a PRECEDING entry in this SAME document's own
            # check_command could have replaced the directory an
            # already-validated sibling entry's cwd points at (e.g.
            # swapping it for a symlink) before this entry's own turn to
            # run. Re-resolving from the entry's raw `cwd` string
            # immediately before this specific Popen -- not trusting the
            # earlier resolution -- is exactly the discipline this file's
            # own module-section docstring names as non-negotiable.
            fresh_cwd, cwd_reason = _resolve_check_cwd(project_root, entry["raw_cwd"])
            if fresh_cwd is None:
                checks_out.append(
                    {
                        "id": entry["id"],
                        "exit_code": None,
                        "timed_out": False,
                        "duration_seconds": 0.0,
                        "stdout": "",
                        "stdout_truncated": False,
                        "stderr": f"cwd re-validation failed immediately before execution: {cwd_reason}",
                        "stderr_truncated": False,
                        "check_command": list(entry["check_command"]),
                        "resolved_cwd": None,
                        "declared_timeout_seconds": entry["timeout_seconds"],
                        "effective_timeout_seconds": min(entry["timeout_seconds"], timeout_ceiling_seconds),
                        "reviewed_by": entry["reviewed_by"],
                        "reviewed_at": entry["reviewed_at"],
                    }
                )
                continue

            effective_timeout = min(entry["timeout_seconds"], timeout_ceiling_seconds)
            outcome = _run_one_check(entry["check_command"], fresh_cwd, effective_timeout)
            checks_out.append(
                {
                    "id": entry["id"],
                    "exit_code": outcome["exit_code"],
                    "timed_out": outcome["timed_out"],
                    "duration_seconds": outcome["duration_seconds"],
                    "stdout": outcome["stdout"],
                    "stdout_truncated": outcome["stdout_truncated"],
                    "stderr": outcome["stderr"],
                    "stderr_truncated": outcome["stderr_truncated"],
                    # Fix-round addition (adversarial review, path-and-
                    # containment P2-5): the persisted record used to omit
                    # BOTH what argv actually ran and where -- for the
                    # single highest-risk piece of code in this plan (one
                    # that exists to execute unauthenticated third-party
                    # argv), that made no run forensically reconstructable.
                    "check_command": list(entry["check_command"]),
                    "resolved_cwd": str(fresh_cwd),
                    "declared_timeout_seconds": entry["timeout_seconds"],
                    "effective_timeout_seconds": effective_timeout,
                    "reviewed_by": entry["reviewed_by"],
                    "reviewed_at": entry["reviewed_at"],
                }
            )
        result["outcome"] = "ok"
        result["checks"] = checks_out
        return _finalize_project_result(result, run_dir, global_id)
    except Exception as exc:  # noqa: BLE001 -- must never abort other projects' checks
        # Fix-round change (adversarial review, injection-and-exec P2): a
        # project whose Nth check raises (e.g. a NUL byte in a LATER
        # entry's argv -- structurally impossible today since check_command
        # elements are control-char-free strings by construction, but kept
        # as defense-in-depth) used to have checks 1..N-1's already-real
        # results discarded and reported as outcome "skipped" -- i.e.
        # "nothing ran" -- even though code demonstrably already executed
        # and may have left side effects. "partial" preserves what is
        # already known rather than pretending it didn't happen.
        result["outcome"] = "partial" if checks_out else "skipped"
        result["checks"] = checks_out
        result["warnings"].append({"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"})
        return _finalize_project_result(result, run_dir, global_id)


# ---------------------------------------------------------------------------
# Top-level orchestration -- pure(ish) over an already-loaded catalog dict
# plus an explicit `now` (same testability pattern as query_catalog.py's
# search_catalog(): the caller supplies the clock reading, this function
# never reads it itself), so staleness tests never need to freeze the real
# wall clock. The only non-pure parts are exactly what "run" exists to do:
# real subprocess execution and real (carefully-contained) filesystem
# writes under compat_runs_root.
# ---------------------------------------------------------------------------


def _run_result(exit_code: int, reason: str, *, global_id: str | None, message: str | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": exit_code in (0, 1),
        "exit_code": exit_code,
        "global_id": global_id,
        "reason": reason,
    }
    if message:
        result["message"] = message
    result.update(extra)
    return result


def run_compatibility_checks(
    catalog: dict[str, Any],
    *,
    global_id: str,
    authorize_project: list[str] | None,
    now: datetime,
    stale_after_seconds: float,
    allow_stale_catalog: bool,
    timeout_ceiling_seconds: float,
    compat_runs_root: Path,
) -> dict[str, Any]:
    # --- usage-level validation first: these are independent of any data in
    # the catalog and independent of the clock, so they are checked before
    # even looking at either. ---
    if not global_id_shape_is_valid(global_id):
        return _run_result(2, "global_id_malformed", global_id=global_id)
    if not authorize_project:
        return _run_result(2, "authorize_project_required", global_id=global_id)
    if (
        not isinstance(timeout_ceiling_seconds, (int, float))
        or isinstance(timeout_ceiling_seconds, bool)
        or not math.isfinite(timeout_ceiling_seconds)
        or timeout_ceiling_seconds <= 0
    ):
        # `not math.isfinite(...)` closes a P1 found in independent review:
        # `float('inf') <= 0` and `float('nan') <= 0` are both False, so
        # --timeout-ceiling-seconds nan/inf used to sail through this gate
        # and then, downstream, `min(declared, ceiling)` with a NaN/inf
        # ceiling returns the PROJECT'S OWN declared timeout unmodified --
        # i.e. the "hard ceiling regardless of what a project declares"
        # promise (DEFAULT_TIMEOUT_CEILING_SECONDS's own comment) silently
        # became a no-op.
        return _run_result(2, "timeout_ceiling_not_positive", global_id=global_id)
    if (
        not isinstance(stale_after_seconds, (int, float))
        or isinstance(stale_after_seconds, bool)
        or not math.isfinite(stale_after_seconds)
        or stale_after_seconds < 0
    ):
        # Same class, for the freshness gate: `age_seconds > stale_after_
        # seconds` is False whenever stale_after_seconds is NaN or +inf, so
        # --stale-after-hours nan/inf used to make a 48-hour-old catalog
        # report catalog_stale:false in the persisted run record -- an
        # honestly-false statement, not just a permissive setting.
        return _run_result(2, "stale_after_hours_negative", global_id=global_id)

    # --- freshness (design doc 3.5.3 / task spec step 2): stale + no
    # override => exit 4, zero execution attempted. ---
    stale, age_seconds, freshness_warnings = _catalog_freshness(catalog, now, stale_after_seconds)
    if stale and not allow_stale_catalog:
        return _run_result(
            4,
            "catalog_stale",
            global_id=global_id,
            message="catalog.json is stale and --allow-stale-catalog was not given; refusing to execute anything",
            catalog_age_seconds=age_seconds,
            warnings=freshness_warnings,
        )

    # --- resolve the affected set via THIS FILE's own existing "affected"
    # machinery, called directly (same-file reuse; not the cross-file
    # import this codebase's convention avoids). ---
    try:
        affected_result = check_affected(catalog, global_id)
    except CheckFatal as exc:
        return _run_result(4, exc.reason, global_id=global_id, message=exc.message, warnings=freshness_warnings)

    # A degraded/partial-trust affected-set computation (fallback_scan mode,
    # or a reverse_index row that disagrees with its own in_degree) is not a
    # safe basis for authorizing code execution: it might be missing
    # entries, or (equally dangerous for THIS tool's purpose) padded with
    # entries that do not actually hold up. "affected" itself only ever
    # reports this via exit_code 0/1/3 (see its own docstring); anything
    # other than 0/1 here means "not fully trusted" and this tool refuses
    # to proceed on it, full stop -- this is a judgment call this file's
    # own Tier-1 docstring does not make for its read-only callers, but
    # Tier-2 is not read-only.
    if affected_result["exit_code"] not in (0, 1):
        return _run_result(
            4,
            "affected_resolution_not_fully_trusted",
            global_id=global_id,
            message=(
                f"affected-set computation was not fully trusted (mode={affected_result['mode']}, "
                f"exit_code={affected_result['exit_code']}); refusing to authorize execution against "
                "an unreliable affected set"
            ),
            affected_result=affected_result,
            warnings=freshness_warnings,
        )

    affected_project_ids = {
        item["project_id"] for item in affected_result["affected"] if _is_str(item.get("project_id"))
    }

    requested = list(authorize_project)
    unauthorized = sorted({p for p in requested if p not in affected_project_ids})
    if unauthorized:
        return _run_result(
            2,
            "authorize_project_not_in_affected_set",
            global_id=global_id,
            message=f"not in the affected set for {global_id}: {', '.join(unauthorized)}",
            offending_project_ids=unauthorized,
            warnings=freshness_warnings,
        )

    authorized_sorted = sorted(set(requested))

    project_roots, project_root_warnings = build_project_root_index(catalog)
    all_warnings = freshness_warnings + project_root_warnings

    try:
        ensure_compat_runs_root(compat_runs_root)
        run_id, run_dir = _allocate_run_id(compat_runs_root)
    except CheckFatal as exc:
        return _run_result(4, exc.reason, global_id=global_id, message=exc.message, warnings=all_warnings)

    # Two-phase execution (fix-round change, adversarial review authz-and-
    # toctou P1-3): Phase 1 reads and validates EVERY authorized project's
    # wiki/compat-check.json before Phase 2 executes so much as one
    # check_command anywhere in this run. See _load_one_project_plan's own
    # docstring for why interleaving read-then-execute per project (the
    # original shape) let an earlier project's own check_command rewrite a
    # later project's declaration before that project's turn came up.
    plans = [
        _load_one_project_plan(project_id=project_id, global_id=global_id, project_roots=project_roots)
        for project_id in authorized_sorted
    ]
    project_results = [
        _finalize_project_result(plan["result"], run_dir, global_id)
        if not plan["ready"]
        else _execute_one_project_plan(
            plan, run_dir=run_dir, global_id=global_id, timeout_ceiling_seconds=timeout_ceiling_seconds
        )
        for plan in plans
    ]

    ok_count = sum(1 for r in project_results if r["outcome"] == "ok")
    skipped_count = sum(1 for r in project_results if r["outcome"] == "skipped")
    partial_count = sum(1 for r in project_results if r["outcome"] == "partial")
    unrecorded_count = sum(1 for r in project_results if r["outcome"] == "unrecorded")
    any_failed_or_timed_out = any(
        (chk["exit_code"] != 0 or chk["timed_out"])
        for r in project_results
        if r["outcome"] in ("ok", "partial", "unrecorded")
        for chk in r["checks"]
    )

    # skipped/partial/unrecorded are all "this run cannot be fully trusted
    # as a clean pass" -- see _finalize_project_result's own docstring for
    # why "ok" is no longer used when a write was refused or failed.
    if skipped_count > 0 or partial_count > 0 or unrecorded_count > 0:
        exit_code = 3
    elif any_failed_or_timed_out:
        exit_code = 1
    else:
        exit_code = 0

    return {
        "ok": exit_code in (0, 1),
        "exit_code": exit_code,
        "global_id": global_id,
        "run_id": run_id,
        "catalog_verified_at": catalog.get("verified_at") if _is_str(catalog.get("verified_at")) else None,
        "catalog_age_seconds": age_seconds,
        "catalog_stale": stale,
        "affected_project_ids": sorted(affected_project_ids),
        "authorized_projects": authorized_sorted,
        "projects": project_results,
        "counts": {
            "authorized": len(authorized_sorted),
            "ok": ok_count,
            "skipped": skipped_count,
            "partial": partial_count,
            "unrecorded": unrecorded_count,
        },
        "warnings": all_warnings,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only query: which capabilities in this fleet depend on a given capability's global_id?"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    affected = subparsers.add_parser(
        "affected", help="List capabilities whose depends_on resolves to --changed-global-id."
    )
    affected.add_argument("--changed-global-id", type=str, required=True, dest="changed_global_id")
    affected.add_argument("--catalog", type=str, required=True, help="Path to catalog.json (read-only).")
    affected.add_argument("--json", action="store_true", help="Print the result as one JSON object to stdout.")
    affected.add_argument("--quiet", action="store_true", help="Suppress all output; rely on the exit code.")

    run = subparsers.add_parser(
        "run",
        help=(
            "M8 Gate D (Tier-2): ACTUALLY EXECUTE the check_command(s) that affected projects "
            "self-declared in their own wiki/compat-check.json. Default-off, no --authorize-all."
        ),
    )
    run.add_argument("--global-id", type=str, required=True, dest="global_id")
    run.add_argument(
        "--authorize-project",
        action="append",
        default=None,
        dest="authorize_project",
        help="Repeatable. Must be a subset of --global-id's affected set. Required, at least one.",
    )
    run.add_argument(
        "--catalog",
        type=str,
        default=None,
        help=f"Path to catalog.json (read-only). Default: {DEFAULT_CATALOG_PATH}.",
    )
    run.add_argument(
        "--stale-after-hours",
        type=float,
        default=float(DEFAULT_STALE_AFTER_HOURS),
        help=f"Freshness threshold in hours (default {DEFAULT_STALE_AFTER_HOURS}).",
    )
    run.add_argument(
        "--allow-stale-catalog",
        action="store_true",
        help="Explicit override: proceed even if catalog.json is older than --stale-after-hours.",
    )
    run.add_argument(
        "--timeout-ceiling-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_CEILING_SECONDS,
        help=f"Hard ceiling applied to every declared timeout_seconds (default {DEFAULT_TIMEOUT_CEILING_SECONDS}).",
    )
    run.add_argument(
        "--compat-runs-root",
        type=str,
        default=None,
        help=f"Output root for this run's per-project result files. Default: {COMPAT_RUNS_ROOT}.",
    )
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


def _print_human(result: dict[str, Any]) -> None:
    if result["mode"] != "reverse_index":
        mode_label = "FALLBACK SCAN (partial trust)"
    elif result.get("row_degraded"):
        mode_label = "reverse index (PARTIAL TRUST -- row degraded, see warnings)"
    else:
        mode_label = "reverse index"
    print(f"changed_global_id: {result['changed_global_id']}  [{mode_label}]")
    print(f"  known in catalog: {result['global_id_known_in_catalog']}")
    print(f"  in_degree: {result['in_degree']}   affected: {result['counts']['affected']}")
    for warning in result["warnings"]:
        print(f"  WARN [{warning['code']}] {warning['message']}")
    if not result["affected"]:
        print("  no downstream capability currently depends on it")
        return
    for item in result["affected"]:
        print(f"  - {item['global_id']}  (project: {item['project_id']}, scope: {item['scope']}, raw: {item['raw']})")


def cmd_affected(args: argparse.Namespace) -> int:
    global_id = args.changed_global_id
    if not global_id_shape_is_valid(global_id):
        return _emit_error(
            args,
            2,
            "changed_global_id_malformed",
            "must be a non-empty string containing '#' with no control/newline characters",
        )

    if not args.catalog.strip():
        return _emit_error(args, 2, "empty_catalog_path")

    catalog_path = Path(args.catalog).expanduser()
    try:
        catalog = load_catalog(catalog_path)
        result = check_affected(catalog, global_id)
    except CheckFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)

    if not args.quiet:
        if args.json:
            # "ok" means "the question was successfully answered", matching
            # query_catalog.py's convention -- it is True for every exit
            # code this branch can reach (0 found / 1 confirmed-none / 3
            # partial-trust), never tied to whether the answer was "yes".
            # Only the _emit_error() paths (2, 4) ever set ok:false.
            payload = {"ok": True, **result}
            print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False, indent=1)))
        else:
            _print_human(result)

    return result["exit_code"]


def cmd_run(args: argparse.Namespace) -> int:
    """Always prints one full JSON object to stdout, regardless of exit
    code (task spec: "never silently swallow a partial result") -- unlike
    cmd_affected, this subcommand has no --json/--quiet toggle: an action
    this consequential does not get a quiet mode."""
    now = datetime.now(timezone.utc)
    try:
        catalog_path = Path(args.catalog).expanduser() if args.catalog else DEFAULT_CATALOG_PATH
        catalog = load_catalog(catalog_path)
        compat_runs_root = Path(args.compat_runs_root).expanduser() if args.compat_runs_root else COMPAT_RUNS_ROOT
        if not compat_runs_root.is_absolute():
            # Fix-round addition (adversarial review, path-and-containment
            # P3-4): the DEFAULT is hardcoded absolute (COMPAT_RUNS_ROOT
            # above), but the --compat-runs-root FLAG itself accepted a
            # relative value with no check, landing output relative to
            # wherever this process happened to be launched from -- the
            # same "staging location != deployed location" incident class
            # this codebase has already hit twice this week.
            raise CheckFatal("compat_runs_root_not_absolute", str(compat_runs_root))
        result = run_compatibility_checks(
            catalog,
            global_id=args.global_id,
            authorize_project=args.authorize_project,
            now=now,
            stale_after_seconds=args.stale_after_hours * 3600,
            allow_stale_catalog=args.allow_stale_catalog,
            timeout_ceiling_seconds=args.timeout_ceiling_seconds,
            compat_runs_root=compat_runs_root,
        )
    except CheckFatal as exc:
        result = _run_result(4, exc.reason, global_id=getattr(args, "global_id", None), message=exc.message)
    except Exception as exc:  # noqa: BLE001 -- always report, never a bare traceback
        result = _run_result(4, "unexpected_error", global_id=getattr(args, "global_id", None), message=str(exc))

    print(_sanitize_line_separators(json.dumps(result, ensure_ascii=False, indent=1, default=str, allow_nan=False)))
    return result["exit_code"]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "affected":
            return cmd_affected(args)
        if args.command == "run":
            return cmd_run(args)
        return 2
    except CheckFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)
    except BaseException as exc:
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
