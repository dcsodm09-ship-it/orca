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
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

MAX_CATALOG_BYTES = 16 * 1024 * 1024
_CONTROL_CHARS = ("\n", "\r", "\t", chr(0x2028), chr(0x2029), "\x00")


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


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


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
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "affected":
        return 2
    try:
        return cmd_affected(args)
    except CheckFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)
    except BaseException as exc:
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
