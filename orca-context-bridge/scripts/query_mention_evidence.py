#!/usr/bin/env python3
"""Read-only search entry point over mention-evidence.json (M8-1 query half,
Gate C of the cross-project catalog plan -- M8-DESIGN-FINAL-2026-08-23.md
sections 3.2 / 3.2.4).

build_mention_evidence.py build produces one derived artifact:

    /Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/mention-evidence.json

That file is a passive artifact, exactly like catalog.json itself -- this
script is the question:

    python3 query_mention_evidence.py search --global-id-either-side <global_id>

It answers "what text-mention edges exist for this global_id, as either the
`from` or `to` side of an edge?" -- with three mutually exclusive selectors
(--from-global-id / --to-global-id / --global-id-either-side) so a caller can
ask the narrower, directional question when that is what it actually wants.

READ-ONLY, WITH NO WRITE PATH AT ALL
------------------------------------
Same structural claim as query_catalog.py's own: no output file, no cache,
no lock, no tmp sibling, no `os.O_WRONLY`/`open(..., "w")` anywhere. This
script reads up to two files (mention-evidence.json, and catalog.json for
the staleness/existence check below) and prints to stdout.

WHY THIS SCRIPT ALSO READS catalog.json
--------------------------------------------------------------------------
mention-evidence.json is a DERIVED file: it was built from some catalog.json
snapshot in the past, and the real catalog.json can have moved on since
(new capabilities/pages added, an entry removed, its own content changed).
Two design-doc-mandated checks require comparing against the CURRENT
catalog.json, not just trusting mention-evidence.json's own numbers:

  1. STALENESS: mention-evidence.json's own `catalog_generated_at` (which
     catalog.json snapshot it was derived from) is compared against the
     real catalog.json's CURRENT `generated_at`. If the derived file's
     pinned snapshot is older, the derived data may already be missing
     edges for entries added since, or may still list edges for entries
     since removed -- reported explicitly (exit 3), never silently
     answered as if it were current.
  2. EXISTENCE: exit 1 ("confirmed none") is only reported when the
     queried global_id is a real member of the CURRENT catalog's entity
     set (capabilities[] union wiki_pages[]) -- not just "this id never
     appears in any mention-evidence.json edge", which is equally true for
     an id that never existed at all. See EXIT CODES below.

Both checks fail toward "cannot fully vouch for this" rather than toward a
false confident answer, matching query_catalog.py's own staleness
philosophy: "unprovable freshness must never read as proven freshness".

EXIT CODES (query/decision semantics, per M8-DESIGN-FINAL-2026-08-23.md
3.0.2 -- "0 = found a definite answer; 1 = confirmed none; 2 = usage error;
3 = partial trust; 4 = can't answer at all")
--------------------------------------------------------------------------
    0  at least one edge matched the requested global_id/side, AND
       mention-evidence.json's pinned catalog snapshot is at least as
       fresh as the real catalog.json's current one.
    1  confirmed none: mention-evidence.json loaded fine, its pinned
       snapshot is at least as fresh as the current catalog.json, the
       queried global_id IS a member of the current catalog's entity set,
       and it genuinely has zero edges.
    2  usage error (empty --catalog/--mention-evidence path, no side flag
       or more than one given -- the last two are enforced by argparse's
       own mutually-exclusive-group machinery, which already exits 2).
    3  partial trust -- covers FOUR distinct reasons, each reported as its
       own warning code so a caller can tell which:
         - mention_evidence_stale: mention-evidence.json's own
           catalog_generated_at is OLDER than the real catalog.json's
           current generated_at (or the real catalog.json could not be
           read/parsed at all, which is exactly as unprovable as stale --
           see WHY THIS SCRIPT ALSO READS catalog.json above).
         - catalog_path_mismatch: mention-evidence.json's own pinned
           catalog_path does not resolve to the SAME file as the
           catalog.json this run actually read. Without this check a
           `build --catalog <other-file>` run could make the timestamp-only
           staleness comparison pass by pure coincidence (a foreign catalog
           with an equal-or-later generated_at), producing an answer that
           was never actually derived from the catalog being queried.
           Treated identically to staleness: the pinned snapshot cannot be
           trusted against this catalog, so `fresh` is forced False and the
           timestamp comparison is skipped as meaningless.
         - global_id_unknown_to_catalog: zero edges matched AND the
           queried global_id is not a member of the current catalog's
           entity set at all -- reported here rather than as exit 1
           because exit 1's definition requires the id to actually exist
           (see EXISTENCE above); this is a deliberate design extension
           beyond the four cases the design doc spells out literally, not
           a case the doc names outright (see this file's own report to
           the calling task for the judgment call).
         - malformed_edges_skipped: one or more rows in mention-evidence.json's
           own `edges` list were not usable (not a JSON object, or missing
           from_global_id/to_global_id) and had to be skipped. Neither a
           "found" (0) nor a "confirmed none" (1) answer is honest when
           part of the data needed to answer completely could not be read
           -- see M8-DESIGN-FINAL-2026-08-23.md 3.2's own exit-3 case for
           this exact scenario ("内部部分行结构异常但其余部分仍可回答").
    4  cannot answer at all: mention-evidence.json missing, unreadable,
       unparseable, or not shaped like this tool's output (no top-level
       "edges" list).

catalog_session_hint.py (SessionStart) gets ZERO changes from this file --
see M8-DESIGN-FINAL-2026-08-23.md 3.2: any future SessionStart integration
needs its own independent sentinel token/authorization, not a side effect
of this build.

Run with:
    python3 query_mention_evidence.py search
        (--from-global-id ID | --to-global-id ID | --global-id-either-side ID)
        [--mention-evidence PATH] [--catalog PATH] [--json] [--quiet] [--limit N]
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

sys.dont_write_bytecode = True

SCRIPT_REL_PATH = "orca-context-bridge/scripts/query_mention_evidence.py"
QUERY_VERSION = "1.0.0"

DEFAULT_MENTION_EVIDENCE_DIR = Path("/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog")
DEFAULT_MENTION_EVIDENCE_PATH = DEFAULT_MENTION_EVIDENCE_DIR / "mention-evidence.json"
DEFAULT_CATALOG_PATH = DEFAULT_MENTION_EVIDENCE_DIR / "catalog.json"

MAX_BYTES = 16 * 1024 * 1024
DEFAULT_LIMIT = 50

WARN_STALE = "mention_evidence_stale"
WARN_GLOBAL_ID_UNKNOWN = "global_id_unknown_to_catalog"
WARN_CATALOG_UNAVAILABLE = "catalog_unavailable_for_freshness_check"
WARN_CATALOG_PATH_MISMATCH = "catalog_path_mismatch"
WARN_MALFORMED_EDGES_SKIPPED = "malformed_edges_skipped"


class QueryFatal(Exception):
    """Cannot answer the question at all -- maps to exit code 4."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Small shared helpers (copied, not imported -- see module docstring)
# ---------------------------------------------------------------------------


def _sanitize_line_separators(text: str) -> str:
    return text.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")


def _flatten_for_terminal(text: str) -> str:
    scrubbed = []
    for ch in text:
        if ch in ("\n", "\r", "\x1b") or ord(ch) < 0x20:
            scrubbed.append(" ")
        else:
            scrubbed.append(ch)
    return " ".join("".join(scrubbed).split())


def _reject_duplicate_keys(pairs: list) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON object")
        seen[key] = value
    return seen


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse exactly the format build_cross_project_catalog.py's now_iso()
    (and this tool's own build_mention_evidence.py) writes."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_bounded_file(path: Path, max_bytes: int) -> bytes:
    """Same O_NONBLOCK + S_ISREG + size-cap read as query_catalog.py's own
    read_catalog_bytes(); copied, not imported."""
    fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"not a regular file: {path}")
        if st.st_size > max_bytes:
            raise OSError(f"exceeds {max_bytes} bytes: {path}")
        chunks: list = []
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
        raise OSError(f"exceeds {max_bytes} bytes (grew during read): {path}")
    return raw


def _entry_str(entry: dict, key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) else None


def _catalog_paths_match(recorded: object, current: Path) -> bool:
    """True iff `recorded` (mention-evidence.json's own pinned
    `catalog_path`) resolves to the SAME file as `current` (the catalog.json
    this query run actually read). A `build --catalog <other-file>` can
    write mention-evidence.json's fixed output location from a catalog that
    is not the one `query` reads by default (or is given): without this
    check the timestamp-only freshness comparison can be satisfied by pure
    coincidence (a foreign catalog with an equal-or-later generated_at),
    making a permanently-fresh-looking answer that was never derived from
    the catalog actually being queried. Resolved (not just string-equal) so
    equivalent paths spelled differently (`~`, `.`, a trailing separator)
    are not flagged as false mismatches. Any error resolving either side
    (e.g. `recorded` is missing/not a string) counts as a mismatch -- an
    unprovable match must never read as a proven one, same failure
    direction as the freshness check right below it."""
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        recorded_resolved = Path(recorded).expanduser().resolve(strict=False)
        current_resolved = current.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return recorded_resolved == current_resolved


# ---------------------------------------------------------------------------
# Loading mention-evidence.json (the primary, required input)
# ---------------------------------------------------------------------------


def load_mention_evidence(path: Path) -> dict:
    try:
        raw = _read_bounded_file(path, MAX_BYTES)
    except FileNotFoundError:
        raise QueryFatal("mention_evidence_missing", str(path))
    except NotADirectoryError:
        raise QueryFatal("mention_evidence_missing", str(path))
    except OSError as exc:
        raise QueryFatal("mention_evidence_unreadable", str(exc))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QueryFatal("mention_evidence_unparseable", str(exc))
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise QueryFatal("mention_evidence_unparseable", str(exc))
    if not isinstance(doc, dict):
        raise QueryFatal("mention_evidence_malformed", "top level is not a JSON object")
    if not isinstance(doc.get("edges"), list):
        raise QueryFatal("mention_evidence_malformed", "no top-level 'edges' list")
    return doc


# ---------------------------------------------------------------------------
# Loading catalog.json (the secondary input, for freshness + existence --
# NEVER fatal to the overall query: a soft loader that reports a status
# string instead of raising, per the module docstring's WHY section)
# ---------------------------------------------------------------------------


def load_catalog_soft(path: Path) -> tuple[dict | None, str, str | None]:
    """Returns (catalog_or_None, status, reason). status is one of "ok",
    "missing", "unreadable", "unparseable", "malformed". Never raises."""
    try:
        raw = _read_bounded_file(path, MAX_BYTES)
    except FileNotFoundError:
        return None, "missing", str(path)
    except NotADirectoryError:
        return None, "missing", str(path)
    except OSError as exc:
        return None, "unreadable", str(exc)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, "unparseable", str(exc)
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, "unparseable", str(exc)
    if not isinstance(doc, dict):
        return None, "malformed", "top level is not a JSON object"
    return doc, "ok", None


def catalog_entity_ids(catalog: dict) -> set:
    ids: set = set()
    for key in ("capabilities", "wiki_pages"):
        rows = catalog.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    gid = row.get("global_id")
                    if isinstance(gid, str) and gid:
                        ids.add(gid)
    return ids


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def query_mention_evidence(
    mention_evidence: dict,
    catalog_status: str,
    catalog: dict | None,
    global_id: str,
    side: str,
    mention_evidence_path: Path,
    catalog_path: Path,
    limit: int,
) -> dict:
    """Pure function over already-loaded inputs: no filesystem access, no
    clock read for anything but reporting. `side` is one of "from", "to",
    "either"."""
    warnings: list = []

    me_catalog_generated_at = mention_evidence.get("catalog_generated_at")
    me_catalog_generated_at = me_catalog_generated_at if isinstance(me_catalog_generated_at, str) else None
    catalog_generated_at_current = catalog.get("generated_at") if catalog is not None else None
    catalog_generated_at_current = catalog_generated_at_current if isinstance(catalog_generated_at_current, str) else None

    if catalog_status != "ok":
        # Cannot prove freshness at all -- unprovable is treated as stale,
        # exactly like query_catalog.py's own verified_at-missing handling.
        fresh = False
        warnings.append(
            {
                "code": WARN_CATALOG_UNAVAILABLE,
                "message": f"catalog.json status={catalog_status!r}; cannot verify mention-evidence.json freshness",
            }
        )
    elif not _catalog_paths_match(mention_evidence.get("catalog_path"), catalog_path):
        # The catalog.json this run actually read is not the one
        # mention-evidence.json's pinned `catalog_path` names. Comparing
        # timestamps against the wrong catalog is meaningless (a foreign
        # catalog with a coincidentally equal-or-later generated_at would
        # otherwise make this look permanently fresh) -- see
        # _catalog_paths_match()'s docstring.
        fresh = False
        warnings.append(
            {
                "code": WARN_CATALOG_PATH_MISMATCH,
                "message": (
                    f"mention-evidence.json was built from catalog_path={mention_evidence.get('catalog_path')!r}, "
                    f"not the catalog.json actually read for this query ({catalog_path})"
                ),
            }
        )
    else:
        me_ts = _parse_utc_timestamp(me_catalog_generated_at)
        cat_ts = _parse_utc_timestamp(catalog_generated_at_current)
        if me_ts is None or cat_ts is None:
            fresh = False
        else:
            fresh = me_ts >= cat_ts
        if not fresh:
            warnings.append(
                {
                    "code": WARN_STALE,
                    "message": (
                        f"mention-evidence.json was derived from catalog_generated_at={me_catalog_generated_at!r}, "
                        f"older than the current catalog.json's generated_at={catalog_generated_at_current!r}"
                    ),
                }
            )
    stale = not fresh

    edges = mention_evidence.get("edges")
    if not isinstance(edges, list):
        edges = []

    matches: list = []
    skipped_malformed_edges = 0
    for edge in edges:
        if not isinstance(edge, dict):
            skipped_malformed_edges += 1
            continue
        from_id = _entry_str(edge, "from_global_id")
        to_id = _entry_str(edge, "to_global_id")
        if from_id is None or to_id is None:
            # Structurally anomalous: an edge row without both of its own
            # endpoints is not something this search can honestly evaluate
            # against `global_id` -- it might have been a match. Counted,
            # not silently absorbed; see WARN_MALFORMED_EDGES_SKIPPED below
            # and decide_exit_code()'s forced exit 3.
            skipped_malformed_edges += 1
            continue
        if side == "from" and from_id == global_id:
            matches.append(edge)
        elif side == "to" and to_id == global_id:
            matches.append(edge)
        elif side == "either" and (from_id == global_id or to_id == global_id):
            matches.append(edge)

    if skipped_malformed_edges:
        warnings.append(
            {
                "code": WARN_MALFORMED_EDGES_SKIPPED,
                "message": (
                    f"{skipped_malformed_edges} of {len(edges)} edge row(s) in mention-evidence.json were "
                    "structurally malformed (not an object, or missing from_global_id/to_global_id) and "
                    "were skipped -- this run's answer is not a complete retrieval"
                ),
            }
        )

    exists_in_catalog: bool | None = None
    if catalog_status == "ok" and catalog is not None:
        exists_in_catalog = global_id in catalog_entity_ids(catalog)
        if not matches and not exists_in_catalog:
            warnings.append(
                {
                    "code": WARN_GLOBAL_ID_UNKNOWN,
                    "message": f"{global_id!r} is not a member of the current catalog's capabilities[]/wiki_pages[] entity set",
                }
            )

    total_matches = len(matches)
    returned = matches[:limit]

    return {
        "ok": True,
        "global_id": global_id,
        "side": side,
        "mention_evidence_path": str(mention_evidence_path),
        "catalog_path": str(catalog_path),
        "mention_evidence_generated_at": mention_evidence.get("generated_at")
        if isinstance(mention_evidence.get("generated_at"), str)
        else None,
        "mention_evidence_catalog_generated_at": me_catalog_generated_at,
        "catalog_generated_at_current": catalog_generated_at_current,
        "catalog_status": catalog_status,
        "stale": stale,
        "exists_in_catalog": exists_in_catalog,
        "total_matches": total_matches,
        "returned": len(returned),
        "truncated": total_matches > len(returned),
        "skipped_malformed_edges": skipped_malformed_edges,
        "warnings": warnings,
        "matches": returned,
    }


def decide_exit_code(result: dict) -> int:
    if result["stale"]:
        return 3
    if result["skipped_malformed_edges"] > 0:
        # Some edge rows could not be evaluated at all -- neither a "found"
        # (0) nor a "confirmed none" (1) answer is honest when part of the
        # data this search would have needed to check was unreadable. See
        # WARN_MALFORMED_EDGES_SKIPPED above.
        return 3
    if result["total_matches"] > 0:
        return 0
    if result["exists_in_catalog"] is True:
        return 1
    # exists_in_catalog is False here (it cannot be None when not stale --
    # stale is forced True whenever catalog_status != "ok", the only case
    # exists_in_catalog stays None). Zero matches for an id the current
    # catalog does not even know about is not a "confirmed none" answer
    # about a real entity -- see WARN_GLOBAL_ID_UNKNOWN in the module
    # docstring's EXIT CODES section for why this is exit 3, not exit 1.
    return 3


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only search over mention-evidence.json: what text-mention edges exist for a global_id?"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser(
        "search",
        help="Find text-mention edges naming the given global_id as the from side, to side, or either.",
        description="Exit 0 = found, 1 = confirmed none, 2 = usage error, 3 = partial trust "
        "(stale derived data or unknown global_id), 4 = cannot answer at all.",
    )
    side = search.add_mutually_exclusive_group(required=True)
    side.add_argument("--from-global-id", dest="from_global_id", type=str, default=None, help="Match edges where this global_id is the FROM side.")
    side.add_argument("--to-global-id", dest="to_global_id", type=str, default=None, help="Match edges where this global_id is the TO side.")
    side.add_argument(
        "--global-id-either-side",
        dest="either_global_id",
        type=str,
        default=None,
        help="Match edges where this global_id is EITHER the from or to side.",
    )
    search.add_argument(
        "--mention-evidence",
        type=str,
        default=None,
        help=f"mention-evidence.json to read (default: {DEFAULT_MENTION_EVIDENCE_PATH}). Read-only.",
    )
    search.add_argument(
        "--catalog",
        type=str,
        default=None,
        help=f"catalog.json to read for the freshness/existence check (default: {DEFAULT_CATALOG_PATH}). Read-only.",
    )
    search.add_argument("--json", action="store_true", help="Print the result as one JSON object to stdout.")
    search.add_argument("--quiet", action="store_true", help="Suppress all output; rely on the exit code.")
    search.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_LIMIT,
        help=f"Maximum matches to show (default: {DEFAULT_LIMIT}). The reported total is never truncated.",
    )
    return parser


def _emit_error(args: argparse.Namespace, code: int, reason: str, message: str | None = None) -> int:
    if not getattr(args, "quiet", False):
        payload: dict = {"ok": False, "reason": reason}
        if message:
            payload["message"] = message
        if getattr(args, "json", False):
            print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False)), file=sys.stderr)
        else:
            suffix = f" ({_flatten_for_terminal(message)})" if message else ""
            print(f"error: {reason}{suffix}", file=sys.stderr)
    return code


def _print_human(result: dict) -> None:
    print(
        f"global_id={_flatten_for_terminal(result['global_id'])}  side={result['side']}  "
        f"catalog_status={result['catalog_status']}"
    )
    if result["stale"]:
        print("  PARTIAL TRUST: derived data may not reflect the current catalog.json")
    for warning in result["warnings"]:
        print(f"  WARN [{warning['code']}] {_flatten_for_terminal(warning['message'])}")

    if result["total_matches"] == 0:
        if result["exists_in_catalog"] is True:
            print("confirmed: zero text-mention edges for this global_id")
        else:
            print("no edges found (and this global_id is not confirmed to exist in the current catalog)")
        return

    shown = f", showing {result['returned']}" if result["truncated"] else ""
    print(f"{result['total_matches']} edge(s){shown}")
    for position, edge in enumerate(result["matches"], start=1):
        print()
        print(
            f"[{position}] {edge.get('from_global_id')} -> {edge.get('to_global_id')}  "
            f"confidence={edge.get('confidence')}  same_project={edge.get('same_project')}"
        )
        for combo in edge.get("matched_combinations") or []:
            print(
                f"     {combo.get('needle_field')}={_flatten_for_terminal(str(combo.get('needle_value')))!s} "
                f"in {combo.get('haystack_field')}: {_flatten_for_terminal(str(combo.get('snippet')))}"
            )


def cmd_search(args: argparse.Namespace) -> int:
    if args.from_global_id is not None:
        side, global_id = "from", args.from_global_id
    elif args.to_global_id is not None:
        side, global_id = "to", args.to_global_id
    else:
        side, global_id = "either", args.either_global_id

    if not global_id.strip():
        return _emit_error(args, 2, "empty_global_id", "global_id must contain a non-whitespace character")

    if args.mention_evidence is not None and not args.mention_evidence.strip():
        return _emit_error(args, 2, "empty_mention_evidence_path")
    if args.catalog is not None and not args.catalog.strip():
        return _emit_error(args, 2, "empty_catalog_path")

    mention_evidence_path = (
        Path(args.mention_evidence).expanduser() if args.mention_evidence is not None else DEFAULT_MENTION_EVIDENCE_PATH
    )
    catalog_path = Path(args.catalog).expanduser() if args.catalog is not None else DEFAULT_CATALOG_PATH

    try:
        mention_evidence = load_mention_evidence(mention_evidence_path)
    except QueryFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)

    catalog, catalog_status, _catalog_reason = load_catalog_soft(catalog_path)

    result = query_mention_evidence(
        mention_evidence,
        catalog_status,
        catalog,
        global_id,
        side,
        mention_evidence_path,
        catalog_path,
        args.limit,
    )
    exit_code = decide_exit_code(result)

    if not args.quiet:
        if args.json:
            print(_sanitize_line_separators(json.dumps(result, ensure_ascii=False, indent=1)))
        else:
            _print_human(result)

    return exit_code


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "search":
        return 2
    try:
        return cmd_search(args)
    except QueryFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)
    except BaseException as exc:
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
