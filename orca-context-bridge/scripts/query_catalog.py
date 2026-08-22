#!/usr/bin/env python3
"""Read-only search entry point over the cross-project catalog (M6 of the
cross-project catalog plan).

`build_cross_project_catalog.py build` produces one consolidated file:

    /Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json

That file is a passive artifact -- something has to ask it a question. This
script is that question:

    python3 query_catalog.py search "wiki"

It matches a keyword, case-insensitively, as a substring of the fields the
plan names -- a capability's `id`/`name`/`summary` and a wiki page's
`id`/`title`/`summary` -- plus `project_id` and `global_id` on both entry
types (added after independent review: `global_id` is the catalog's own
documented join key and is printed on every result row, so searching the
exact value this tool just printed is the natural next query, and it must
not come back a false "nothing uses that word"). Matching happens across
BOTH `capabilities[]` and `wiki_pages[]`, and ranks exact identity matches
above substring ones. The point is the plan's: check whether something
already exists somewhere in the fleet before building it again.

READ-ONLY, WITH NO WRITE PATH AT ALL
------------------------------------
This is a strictly narrower trust boundary than the aggregator's, and the
difference is structural rather than guarded. build_cross_project_catalog.py
needs write_only_within(), an output pin, a lockfile, and an atomic
tmp-then-rename because it WRITES; this script has no output file, no cache,
no lock, no tmp sibling, and no `os.O_WRONLY`/`open(..., "w")` anywhere. It
opens exactly one file for reading and prints to stdout. There is therefore
no write guard here, because there is nothing to guard -- adding one would
imply a write path exists.

Two caveats stated precisely rather than promised away:

  1. CPython writes `__pycache__/query_catalog.cpython-*.pyc` next to this
     file when this module is IMPORTED (its test suite does). That is the
     import system caching bytecode around executing the module body, so
     `sys.dont_write_bytecode = True` below is by construction too late for
     this module's own cache -- exactly as documented in
     build_cross_project_catalog.py. RUNNING it as a script never writes it
     (CPython does not cache `__main__`). `__pycache__/` is gitignored, sits
     outside every project's `wiki/`, and is invisible to both git and the
     catalog. Test T-90 pins the stronger claim that actually matters: a run
     against a fully read-only tree leaves that tree byte-identical.
  2. Whatever the caller redirects stdout/stderr into is written by the
     shell, not by this process.

NO DEPENDENCY ON THE TRUST ANCHOR, AND NONE ON THE AGGREGATOR EITHER
--------------------------------------------------------------------
Zero imports from build_startup_bundle.py, matching M3/M4's discipline. It
also deliberately does not import build_cross_project_catalog.py: the only
thing it would want from there is `_sanitize_line_separators()`, a private
name whose whole body is two `str.replace` calls against a FIXED pair of
codepoints. Importing that module to get it would execute its body,
including its own `sys.path` manipulation and its
`validate_reusable_capabilities` import, dragging the aggregator's entire
dependency surface into a tool that reads one JSON file. The two-line
contract is reproduced locally instead (see _sanitize_line_separators
below), the same call this file's own tests pin -- the same trade
build_cross_project_catalog.py itself made for `_reject_duplicate_keys()`.

The reverse-index fields (`capability_reverse_index`, `in_degree`,
`referenced_by`) are deliberately NOT read or displayed here. Presenting
them is M5's surface, and this script must not fork a second rendering of
semantics that track owns.

RANKING
-------
Three tiers, best first. Within a tier, ordering is fully deterministic
(project_id, then entry type, then global_id, then catalog order), so the
same catalog and keyword always produce the same output:

    exact               keyword equals a capability `name`/`id` or a page
                        `title`/`id` outright (case-insensitive)
    identity-substring  keyword occurs inside one of those identity fields
    summary-substring   keyword occurs only in the `summary`

STALENESS
---------
Every run reports `verified_at`'s age, because the plan's whole use --
"check before doing the work twice" -- turns on whether a NO-MATCH result
can be trusted. A catalog older than --stale-after-hours (default 6, the
freshness threshold M7 proposes for its SessionStart trigger) is reported
as stale, and a `verified_at` that is missing, unparseable, OR AHEAD OF
THIS CLOCK counts as stale too: unprovable freshness must never read as
proven freshness, and a future timestamp is exactly as unprovable as a
missing one (clock skew, a corrupt field, or a hand-edited value with no
real expiry are all indistinguishable from here).

EXIT CODES
----------
    0  at least one match
    1  no match, and the search was COMPLETE (both capabilities[] and
       wiki_pages[] were real lists and were fully searched) -- grep's
       convention, a caller can branch on it directly; note that this
       means `set -e` scripts must handle it
    2  usage error (empty keyword, bad flag value)
    3  no match, but the search was PARTIAL: one of capabilities[]/
       wiki_pages[] was absent or not a list, so only the other one was
       actually searched. Distinct from 1 on purpose -- with --quiet (no
       warning text printed), exit 1 and exit 3 are the only place this
       distinction survives, and conflating them would make "confirmed
       absent" and "half the catalog was never searched" the same answer
       to the question this whole tool exists to answer.
    4  cannot answer at all: catalog missing, unreadable, or unparseable,
       or NEITHER searchable list is present. Distinct from 1/3 on
       purpose -- "I found nothing" and "I could not look at all" must
       never be the same answer to "does this already exist?"

Run with:
    python3 query_catalog.py search "<keyword>" [--json] [--quiet]
                                                [--limit N] [--catalog PATH]
                                                [--stale-after-hours H]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Belt-and-braces: this module imports nothing but the standard library, so
# there is no sibling module whose bytecode could land in a project tree.
# See caveat 1 in the module docstring for what this cannot cover.
sys.dont_write_bytecode = True


QUERY_VERSION = "1.0.0"
QUERY_SCRIPT_REL_PATH = "orca-context-bridge/scripts/query_catalog.py"

# The schema this script knows how to read. A catalog stamped with anything
# else is still searched (a query tool that refuses to answer because a
# field it does not use was added is worse than one that answers with a
# warning), but the mismatch is surfaced -- see WARN_SCHEMA_UNSUPPORTED.
CATALOG_SCHEMA_VERSION_SUPPORTED = 1

DEFAULT_CATALOG_DIR = Path("/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog")
CATALOG_NAME = "catalog.json"
DEFAULT_CATALOG_PATH = DEFAULT_CATALOG_DIR / CATALOG_NAME

# Same ceiling build_cross_project_catalog.py applies when it reads a
# previous catalog. The real file is ~165 KB; this only bounds a hostile or
# corrupt input.
MAX_CATALOG_BYTES = 16 * 1024 * 1024

DEFAULT_LIMIT = 20
# M7 of the plan proposes a 6-hour freshness threshold for its background
# rebuild trigger. Reused here rather than inventing a second number, so
# "stale" means the same thing in both places.
DEFAULT_STALE_AFTER_HOURS = 6
HUMAN_SUMMARY_MAX_CHARS = 200

RANK_EXACT = 0
RANK_IDENTITY_SUBSTRING = 1
RANK_SUMMARY_SUBSTRING = 2
# "identity" rather than "name" because the identity fields differ by entry
# type -- id/name for a capability, id/title for a wiki page -- and one
# label has to be honest about both.
RANK_LABELS = {
    RANK_EXACT: "exact",
    RANK_IDENTITY_SUBSTRING: "identity-substring",
    RANK_SUMMARY_SUBSTRING: "summary-substring",
}

# Field order here is also the order matched_fields is reported in, so that
# output is deterministic rather than dict-iteration-dependent.
#
# global_id and project_id are identity fields, not just id/name/title:
# global_id is the catalog's own documented join key (it keys
# capability_reverse_index) and is printed on every result row, so a
# copy-a-hit-and-search-it round trip is the natural next thing an agent
# does. Before this was added, searching the exact global_id or project_id
# the tool had just printed produced a false "nothing in the catalog uses
# that word" -- for a tool whose whole purpose is answering "does this
# already exist?", a false negative on its own primary key is the one
# failure mode that must not happen.
CAPABILITY_IDENTITY_FIELDS = ("id", "name", "project_id", "global_id")
CAPABILITY_TEXT_FIELDS = ("summary",)
PAGE_IDENTITY_FIELDS = ("id", "title", "project_id", "global_id")
PAGE_TEXT_FIELDS = ("summary",)

WARN_SCHEMA_UNSUPPORTED = "schema_version_unsupported"
WARN_VERIFIED_AT_MISSING = "verified_at_missing"
WARN_VERIFIED_AT_UNPARSEABLE = "verified_at_unparseable"
WARN_VERIFIED_AT_IN_FUTURE = "verified_at_in_future"
WARN_CAPABILITIES_NOT_A_LIST = "capabilities_not_a_list"
WARN_WIKI_PAGES_NOT_A_LIST = "wiki_pages_not_a_list"
WARN_SKIPPED_NON_DICT_ENTRIES = "skipped_non_dict_entries"

# Characters that must never reach a terminal verbatim. Summaries and
# titles are lifted from OTHER projects' files, so a single crafted entry
# could otherwise forge extra output lines (\n), erase what came before
# (\r), or drive the terminal directly (ESC, i.e. ANSI). Category "Cc"
# covers all of C0/C1 including those three; U+2028/U+2029 are line
# terminators to several consumers without being Cc; and the bidi
# override/isolate block is pure display control with no linguistic content,
# which is exactly what makes it a spoofing tool. ZWJ/ZWNJ are deliberately
# NOT stripped -- they carry real meaning in emoji and in Indic/Persian
# text. Spelled with chr() so the source can never itself contain the
# character being escaped.
_BIDI_CONTROLS = frozenset(
    chr(cp) for cp in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)
)
_LINE_SEPARATORS = frozenset((chr(0x2028), chr(0x2029)))


class QueryFatal(Exception):
    """Cannot answer the question at all -- maps to exit code 4. Raised while
    loading the catalog (missing/unreadable/unparseable file), AND ONCE MORE
    inside search_catalog() itself if the successfully-parsed JSON is not
    shaped like a catalog at all (neither capabilities[] nor wiki_pages[] is
    a list -- see search_catalog()'s docstring, which this one must not
    contradict). Every OTHER path through search_catalog() produces a
    result (possibly an empty one) rather than raising."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _sanitize_line_separators(text: str) -> str:
    """Escape U+2028/U+2029 in already-serialized JSON text.

    Deliberate local copy of the identical two-line contract in
    build_cross_project_catalog.py and validate_reusable_capabilities.py
    (see the module docstring for why it is copied rather than imported).
    json.dumps(..., ensure_ascii=False) leaves both characters raw -- legal
    inside a JSON string per RFC 8259, but treated as line terminators by
    some JS-family consumers, which breaks line-oriented readers and older
    `JSON.parse`. Applied ONCE per output boundary rather than per field
    that might echo another project's text.

    Every json.dumps() in this module that reaches a file descriptor goes
    through here: the --json result on stdout and the error payload on
    stderr. chr(0x2028)/chr(0x2029) rather than literal characters so the
    source is unambiguous in a diff.
    """
    return text.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")


def _flatten_for_terminal(text: str) -> str:
    """Make one field from another project's catalog safe to print as part
    of a line of human output.

    Stricter than _sanitize_line_separators() because the destination is
    different: JSON escaping keeps a control character intact (correctly --
    a consumer wants the real value), whereas a terminal would ACT on it.
    Every stripped character becomes a single space and runs of whitespace
    collapse, so a multi-line summary renders as one line and can neither
    forge output structure nor emit ANSI.
    """
    scrubbed = []
    for ch in text:
        if ch in _LINE_SEPARATORS or ch in _BIDI_CONTROLS or unicodedata.category(ch) == "Cc":
            scrubbed.append(" ")
        else:
            scrubbed.append(ch)
    return " ".join("".join(scrubbed).split())


def _truncate(text: str, limit: int = HUMAN_SUMMARY_MAX_CHARS) -> str:
    """Bound one human-output field by CHARACTERS. The catalog's own writer
    caps summaries at 300 chars, but --catalog can name any file, so the
    bound is enforced here rather than assumed upstream."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ..."


def _normalize(text: str) -> str:
    """Fold one string for comparison: NFC first, then casefold.

    NFC because this machine's project ids and titles come off an
    Apple-filesystem path enumeration, which can hand back decomposed forms
    -- a keyword typed as composed text would otherwise silently fail to
    match a stored decomposed one, and a silent miss is the single worst
    failure mode for a "does this already exist?" tool. casefold rather
    than lower() because it is the Unicode-correct case-insensitive
    comparison (lower() leaves e.g. 'ß' unequal to 'SS').
    """
    return unicodedata.normalize("NFC", text).casefold()


def _entry_str(entry: dict, key: str) -> str | None:
    """Only a real str is searchable. A field that is a number, null, list,
    or object is not an error in this tool -- it is simply not text, and is
    skipped rather than coerced into a match."""
    value = entry.get(key)
    return value if isinstance(value, str) else None


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_number(value: str) -> float:
    parsed = float(value)
    # `nan` is already rejected by `not parsed > 0` (every comparison with
    # nan is False). `inf` is NOT: `inf > 0` is True, so it used to pass
    # this gate and then blow up later converting seconds-since-epoch math
    # to an int ("cannot convert float infinity to integer") -- a usage
    # error surfacing as exit 4 ("could not read the catalog"), which is
    # the wrong bucket for a bad CLI argument the catalog had nothing to
    # do with. math.isfinite rejects both inf and nan in one call.
    if not parsed > 0 or not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse exactly the format build_cross_project_catalog.py's now_iso()
    writes. strptime rather than datetime.fromisoformat() because this must
    keep working on the system interpreter (/usr/bin/python3 is 3.9 here),
    where fromisoformat() rejects a trailing 'Z'."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def humanize_age(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ---------------------------------------------------------------------------
# Catalog loading -- the only filesystem access this script performs
# ---------------------------------------------------------------------------


def _reject_duplicate_keys(pairs: list) -> dict:
    """A duplicate key means the catalog was not produced by
    build_cross_project_catalog.py (json.dumps cannot emit one), i.e. it was
    hand-edited or tampered with. Python's default silently keeps the LAST
    value; here that is refused outright and becomes exit 4.

    That is the right severity for this tool specifically: the answer it
    exists to give is "does this already exist somewhere?", and a partially
    trusted catalog that quietly answers "no" is worse than one that
    declines to answer. (The aggregator's same-named helper degrades the
    source and continues instead -- it has 145 other projects to finish,
    this one has a single input.)
    """
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON object")
        seen[key] = value
    return seen


def _reject_non_finite_constant(name: str) -> float:
    """`json.loads`'s default constant handling is a non-standard Python
    extension: it accepts the bare tokens NaN/Infinity/-Infinity, which
    build_cross_project_catalog.py's own json.dumps() can never emit (a
    real catalog's numeric fields are all plain integers/finite floats).
    Accepting one here means it flows straight into this tool's OWN --json
    output via a later json.dumps() with no matching non-standard opt-out,
    producing a bare `NaN`/`Infinity` literal that is not valid per RFC
    8259 and that a strict downstream parser (e.g. JavaScript's
    JSON.parse) rejects outright. Same severity call as
    _reject_duplicate_keys() just above: refused outright, becomes exit 4,
    rather than silently laundering a non-catalog value into this tool's
    own supposedly-strict JSON boundary."""
    raise ValueError(f"non-finite JSON constant {name!r} is not valid JSON and is not producible by the aggregator")


def read_catalog_bytes(path: Path) -> bytes:
    """Read the catalog with the descriptor-level guards that actually
    matter for a read-only consumer, and no others.

    O_NONBLOCK: a FIFO planted at this path would otherwise block inside
    os.open() itself, in the kernel, before any S_ISREG check downstream
    could ever run -- the same trap build_cross_project_catalog.py hit and
    fixed in _read_previous_catalog(). With the flag, the open returns and
    S_ISREG rejects it. It is a no-op on the regular file this normally
    opens.

    S_ISREG: rejects a FIFO, a device, and a directory (os.open succeeds on
    a directory) with one legible reason instead of a surprise.

    O_NOFOLLOW is deliberately NOT used, diverging from
    _read_previous_catalog(). That function needs it because it reads a path
    its own process validated seconds earlier, so a symlink swapped into
    that window is a real TOCTOU. Here there is no such window and no
    privilege boundary: the path is either the documented default or one the
    caller typed, the process reads with the caller's own credentials, and
    the result is printed back to that same caller. Refusing a symlinked
    catalog would break a legitimate layout to defend nothing. Following one
    still cannot escape the size cap, the S_ISREG check, or the JSON parse.
    """
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        raise QueryFatal("catalog_missing", str(path))
    except NotADirectoryError:
        raise QueryFatal("catalog_missing", str(path))
    except OSError as exc:
        raise QueryFatal("catalog_unreadable", str(exc))

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise QueryFatal("catalog_not_a_regular_file", str(path))
        if st.st_size > MAX_CATALOG_BYTES:
            raise QueryFatal("catalog_too_large", f"{st.st_size} bytes > {MAX_CATALOG_BYTES}")
        chunks: list = []
        remaining = MAX_CATALOG_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise QueryFatal("catalog_unreadable", str(exc))
    finally:
        os.close(fd)

    raw = b"".join(chunks)
    # A file that GREW past the cap between fstat and the read loop lands
    # here rather than being silently truncated into a "corrupt" parse.
    if len(raw) > MAX_CATALOG_BYTES:
        raise QueryFatal("catalog_too_large", f">{MAX_CATALOG_BYTES} bytes")
    return raw


def load_catalog(path: Path) -> dict:
    raw = read_catalog_bytes(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QueryFatal("catalog_unparseable", str(exc))
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise QueryFatal("catalog_unparseable", str(exc))
    if not isinstance(doc, dict):
        raise QueryFatal("catalog_malformed", "top level is not a JSON object")
    return doc


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def classify_entry(
    entry: dict, needle: str, identity_fields: tuple, text_fields: tuple
) -> tuple | None:
    """Return (rank, matched_fields) for one entry, or None if it does not
    match. `needle` must already be _normalize()d by the caller -- doing it
    once per query rather than once per field per entry.

    An entry can match on several fields at once; the reported rank is the
    BEST one, while matched_fields lists every field that hit, so the human
    output can explain why something surfaced.
    """
    rank: int | None = None
    matched: list = []
    for field in identity_fields:
        value = _entry_str(entry, field)
        if value is None:
            continue
        normalized = _normalize(value)
        if normalized == needle:
            field_rank = RANK_EXACT
        elif needle in normalized:
            field_rank = RANK_IDENTITY_SUBSTRING
        else:
            continue
        matched.append(field)
        rank = field_rank if rank is None else min(rank, field_rank)
    for field in text_fields:
        value = _entry_str(entry, field)
        if value is None:
            continue
        if needle in _normalize(value):
            matched.append(field)
            rank = RANK_SUMMARY_SUBSTRING if rank is None else min(rank, RANK_SUMMARY_SUBSTRING)
    if rank is None:
        return None
    return rank, matched


def _capability_match(entry: dict, rank: int, matched: list) -> dict:
    return {
        "entry_type": "capability",
        "rank": RANK_LABELS[rank],
        "matched_fields": matched,
        "project_id": _entry_str(entry, "project_id"),
        "global_id": _entry_str(entry, "global_id"),
        "id": _entry_str(entry, "id"),
        "kind": _entry_str(entry, "kind"),
        "name": _entry_str(entry, "name"),
        "path": _entry_str(entry, "path"),
        "summary": _entry_str(entry, "summary"),
        "last_verified_at": _entry_str(entry, "last_verified_at"),
    }


def _page_match(entry: dict, rank: int, matched: list) -> dict:
    return {
        "entry_type": "wiki_page",
        "rank": RANK_LABELS[rank],
        "matched_fields": matched,
        "project_id": _entry_str(entry, "project_id"),
        "global_id": _entry_str(entry, "global_id"),
        "id": _entry_str(entry, "id"),
        "title": _entry_str(entry, "title"),
        "path": _entry_str(entry, "path"),
        "summary": _entry_str(entry, "summary"),
        "status": _entry_str(entry, "status"),
    }


def _collect(
    rows: object,
    needle: str,
    identity_fields: tuple,
    text_fields: tuple,
    entry_type: str,
    render,
    out: list,
) -> tuple:
    """Scan one of the catalog's two entry lists. Returns
    (row_count, skipped_non_dict). A non-list is the caller's problem to
    warn about; a non-dict ROW inside a real list is counted and skipped
    here so the count can be surfaced instead of vanishing."""
    if not isinstance(rows, list):
        return 0, 0
    skipped = 0
    for index, entry in enumerate(rows):
        if not isinstance(entry, dict):
            skipped += 1
            continue
        verdict = classify_entry(entry, needle, identity_fields, text_fields)
        if verdict is None:
            continue
        rank, matched = verdict
        match = render(entry, rank, matched)
        sort_key = (
            rank,
            _entry_str(entry, "project_id") or "",
            entry_type,
            _entry_str(entry, "global_id") or "",
            index,
        )
        out.append((sort_key, match))
    return len(rows), skipped


def search_catalog(
    catalog: dict,
    keyword: str,
    catalog_path: Path,
    now: datetime,
    limit: int = DEFAULT_LIMIT,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_HOURS * 3600,
) -> dict:
    """Pure function over an already-loaded catalog: no filesystem access,
    no clock read (the caller supplies `now`), no output.

    Can still raise QueryFatal("catalog_malformed") if `catalog` parsed as
    JSON but neither `capabilities` nor `wiki_pages` is a list -- a
    successfully-loaded document can still not be a catalog. Every OTHER
    failure mode (unsupported schema_version, missing/unparseable
    verified_at, non-list-but-recoverable shapes, non-dict rows) is a
    warning on the returned result, never an exception. A caller (M7's
    planned SessionStart integration in particular) MUST wrap this call in
    a try/except for QueryFatal, exactly like cmd_search() does below --
    do not assume "the catalog loaded" means "this call cannot raise"."""
    warnings: list = []

    schema_version = catalog.get("schema_version")
    if schema_version != CATALOG_SCHEMA_VERSION_SUPPORTED:
        warnings.append(
            {
                "code": WARN_SCHEMA_UNSUPPORTED,
                "message": (
                    f"catalog schema_version is {schema_version!r}, this tool understands "
                    f"{CATALOG_SCHEMA_VERSION_SUPPORTED}; fields it reads may have moved"
                ),
            }
        )

    verified_at_raw = catalog.get("verified_at")
    verified_at = _parse_utc_timestamp(verified_at_raw)
    if verified_at is None:
        age_seconds: int | None = None
        # Unprovable freshness is treated as staleness on purpose: the whole
        # point of the field is deciding whether a NO-MATCH can be trusted,
        # and "I could not tell" must not read as "it is fresh".
        stale = True
        if verified_at_raw is None:
            warnings.append({"code": WARN_VERIFIED_AT_MISSING, "message": "catalog has no verified_at"})
        else:
            warnings.append(
                {
                    "code": WARN_VERIFIED_AT_UNPARSEABLE,
                    "message": f"verified_at {verified_at_raw!r} is not YYYY-MM-DDTHH:MM:SSZ",
                }
            )
    else:
        age_seconds = int((now - verified_at).total_seconds())
        stale = age_seconds > stale_after_seconds
        if age_seconds < 0:
            # A verified_at ahead of this clock is exactly as unprovable as
            # a missing or unparseable one -- it could be a clock-skewed
            # aggregator, a corrupt/hand-edited field, or (year 9999, no
            # expiry ever) an attempt to defeat staleness checking
            # entirely. The same "cannot prove fresh -> treat as stale"
            # rule already applied to WARN_VERIFIED_AT_MISSING/
            # _UNPARSEABLE above must apply here too, or a caller only
            # checking the `stale` boolean (not reading every warning)
            # would trust an answer this function cannot actually vouch
            # for.
            stale = True
            warnings.append(
                {
                    "code": WARN_VERIFIED_AT_IN_FUTURE,
                    "message": f"verified_at {verified_at_raw!r} is ahead of this clock by {-age_seconds}s",
                }
            )

    capabilities = catalog.get("capabilities")
    wiki_pages = catalog.get("wiki_pages")
    if not isinstance(capabilities, list):
        warnings.append(
            {"code": WARN_CAPABILITIES_NOT_A_LIST, "message": "capabilities is absent or not a list; searched 0 of them"}
        )
    if not isinstance(wiki_pages, list):
        warnings.append(
            {"code": WARN_WIKI_PAGES_NOT_A_LIST, "message": "wiki_pages is absent or not a list; searched 0 of them"}
        )
    if not isinstance(capabilities, list) and not isinstance(wiki_pages, list):
        # Neither searchable list is present: this is not a catalog with a
        # problem, it is not a catalog. Returning "0 matches" here would be
        # the dangerous false negative exit code 4 exists to prevent.
        raise QueryFatal("catalog_malformed", "neither capabilities[] nor wiki_pages[] is a list")

    needle = _normalize(keyword)
    scored: list = []
    capability_count, skipped_caps = _collect(
        capabilities, needle, CAPABILITY_IDENTITY_FIELDS, CAPABILITY_TEXT_FIELDS, "capability", _capability_match, scored
    )
    page_count, skipped_pages = _collect(
        wiki_pages, needle, PAGE_IDENTITY_FIELDS, PAGE_TEXT_FIELDS, "wiki_page", _page_match, scored
    )
    skipped_entries = skipped_caps + skipped_pages
    if skipped_entries:
        warnings.append(
            {
                "code": WARN_SKIPPED_NON_DICT_ENTRIES,
                "message": f"{skipped_entries} entr(ies) were not JSON objects and were not searched",
            }
        )

    scored.sort(key=lambda pair: pair[0])
    matches = [match for _, match in scored]
    total_matches = len(matches)
    returned = matches[:limit]

    project_ids = set()
    for rows in (capabilities, wiki_pages):
        if isinstance(rows, list):
            for entry in rows:
                if isinstance(entry, dict):
                    project_id = _entry_str(entry, "project_id")
                    if project_id:
                        project_ids.add(project_id)

    # Defensive, not redundant with cmd_search()'s own check: this function
    # is documented as "every failure mode beyond this point is a warning,
    # never an exception" (except the QueryFatal above), and it is a public
    # function a future library caller (M7) could call directly with an
    # already-overflowed value, bypassing cmd_search()'s CLI-layer guard
    # entirely. int(inf) raises OverflowError; clamped to a value that
    # still int()-converts cleanly and is larger than any real catalog will
    # ever be (~2.9e11 years) rather than crash on a value this function
    # promised never to crash on.
    stale_after_seconds_int = int(stale_after_seconds) if math.isfinite(stale_after_seconds) else 2**63 - 1

    return {
        "ok": True,
        "keyword": keyword,
        "catalog": str(catalog_path),
        "schema_version": schema_version,
        "generated_at": catalog.get("generated_at") if isinstance(catalog.get("generated_at"), str) else None,
        "verified_at": verified_at_raw if isinstance(verified_at_raw, str) else None,
        "age_seconds": age_seconds,
        "age_human": humanize_age(age_seconds),
        "stale": stale,
        "stale_after_seconds": stale_after_seconds_int,
        "counts": {
            "capabilities": capability_count,
            "wiki_pages": page_count,
            "projects": len(project_ids),
            "skipped_entries": skipped_entries,
        },
        "total_matches": total_matches,
        "returned": len(returned),
        "truncated": total_matches > len(returned),
        "warnings": warnings,
        "matches": returned,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only search over the cross-project catalog: find a capability or wiki page "
        "that already exists somewhere in the fleet before building it again."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser(
        "search",
        help="Case-insensitive substring search across capabilities and wiki pages; exact matches first.",
        description="Search capabilities[] (id/name/summary) and wiki_pages[] (id/title/summary) in "
        "catalog.json. Exit 0 = matched, 1 = no match, 2 = usage error, 4 = catalog unavailable.",
    )
    search.add_argument("keyword", type=str, help="Keyword to look for. Matched case-insensitively as a substring.")
    search.add_argument(
        "--catalog",
        type=str,
        default=None,
        help=f"Catalog file to read (default: {DEFAULT_CATALOG_PATH}). Read-only; never written.",
    )
    search.add_argument(
        "--json", action="store_true", help="Print the result as one JSON object to stdout."
    )
    search.add_argument("--quiet", action="store_true", help="Suppress all output; rely on the exit code.")
    search.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_LIMIT,
        help=f"Maximum matches to show (default: {DEFAULT_LIMIT}). The reported total is never truncated.",
    )
    search.add_argument(
        "--stale-after-hours",
        type=positive_number,
        default=DEFAULT_STALE_AFTER_HOURS,
        help=f"Age at which the catalog is reported stale (default: {DEFAULT_STALE_AFTER_HOURS}).",
    )
    return parser


def _emit_error(args: argparse.Namespace, code: int, reason: str, message: str | None = None) -> int:
    if not getattr(args, "quiet", False):
        payload = {"ok": False, "reason": reason}
        if message:
            payload["message"] = message
        if getattr(args, "json", False):
            # Output boundary #2 (stderr). `message` can carry an OS error
            # string built from a caller-supplied path, so it gets the same
            # U+2028/U+2029 treatment as the stdout payload.
            print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False)), file=sys.stderr)
        else:
            suffix = f" ({_flatten_for_terminal(message)})" if message else ""
            print(f"error: {reason}{suffix}", file=sys.stderr)
    return code


def _print_human(result: dict) -> None:
    counts = result["counts"]
    header = (
        f"catalog last verified {result['age_human']} ago"
        if result["age_seconds"] is not None and result["age_seconds"] >= 0
        else f"catalog verified_at: {result['age_human']}"
    )
    print(
        f"{header}  ({_flatten_for_terminal(result['verified_at'] or '?')})  --  "
        f"{counts['capabilities']} capabilities, {counts['wiki_pages']} wiki pages, "
        f"{counts['projects']} projects"
    )
    if result["stale"]:
        # humanize_age() rather than integer-dividing by 3600, which renders
        # any sub-hour --stale-after-hours value as a misleading "> 0h".
        print(
            f"  STALE (> {humanize_age(result['stale_after_seconds'])}): rebuild with "
            "`build_cross_project_catalog.py build` before trusting this answer"
        )
    for warning in result["warnings"]:
        print(f"  WARN [{warning['code']}] {_flatten_for_terminal(warning['message'])}")

    keyword = _flatten_for_terminal(result["keyword"])
    if result["total_matches"] == 0:
        print(f'no match for "{keyword}"')
        print(
            "  No id/name/title/summary/project_id/global_id in the catalog contains that "
            "word -- but the catalog only covers projects that have adopted "
            "wiki/reusable-capabilities.json or wiki/orca-context-wiki.json."
        )
        return

    shown = f", showing {result['returned']}" if result["truncated"] else ""
    print(f'{result["total_matches"]} match(es) for "{keyword}"{shown}')
    for position, match in enumerate(result["matches"], start=1):
        project_id = _flatten_for_terminal(match["project_id"] or "?")
        identity = match.get("name") if match["entry_type"] == "capability" else match.get("title")
        kind = match.get("kind") if match["entry_type"] == "capability" else "page"
        fields = ",".join(match["matched_fields"])
        print()
        print(
            f"[{position}] {match['rank']:<18} {project_id}  "
            f"{_flatten_for_terminal(kind or '?')}  {_flatten_for_terminal(match['id'] or '?')}"
        )
        if identity:
            print(f"     {_flatten_for_terminal(identity)}")
        if match.get("path"):
            print(f"     path: {_flatten_for_terminal(match['path'])}")
        if match.get("summary"):
            print(f"     {_truncate(_flatten_for_terminal(match['summary']))}")
        print(f"     matched: {fields}   global_id: {_flatten_for_terminal(match['global_id'] or '?')}")


def cmd_search(args: argparse.Namespace) -> int:
    keyword = args.keyword
    # An empty (or all-whitespace) keyword is a substring of every field, so
    # it would "match" the entire catalog and mean nothing. Rejected rather
    # than answered. Note the emptiness TEST strips, but matching then uses
    # the keyword exactly as given -- silently rewriting a caller's query
    # would be worse than not accepting an empty one.
    if not keyword.strip():
        return _emit_error(args, 2, "empty_keyword", "keyword must contain a non-whitespace character")

    # `positive_number` already rejects a non-finite --stale-after-hours, but
    # a large FINITE hours value (e.g. 1e308) can still overflow to inf once
    # multiplied by 3600 -- checked again here, after the multiplication
    # that actually produces the value search_catalog() will int()-convert,
    # so this stays a usage error (exit 2) rather than surfacing later as
    # an "unexpected_error" exit 4 that has nothing to do with the catalog.
    stale_after_seconds = args.stale_after_hours * 3600
    if not math.isfinite(stale_after_seconds):
        return _emit_error(args, 2, "stale_after_hours_overflow", "--stale-after-hours, once converted to seconds, is not finite")

    catalog_path = Path(args.catalog).expanduser() if args.catalog is not None else DEFAULT_CATALOG_PATH
    try:
        catalog = load_catalog(catalog_path)
        result = search_catalog(
            catalog,
            keyword,
            catalog_path,
            now=datetime.now(timezone.utc),
            limit=args.limit,
            stale_after_seconds=stale_after_seconds,
        )
    except QueryFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)

    if not args.quiet:
        if args.json:
            # Output boundary #1 (stdout). matches[] echoes summaries and
            # titles lifted verbatim from other projects' files.
            print(_sanitize_line_separators(json.dumps(result, ensure_ascii=False, indent=1)))
        else:
            _print_human(result)

    if result["total_matches"]:
        return 0
    # A zero-match result where one of the two searchable lists was absent
    # or malformed (WARN_CAPABILITIES_NOT_A_LIST / WARN_WIKI_PAGES_NOT_A_LIST)
    # used to be indistinguishable from a genuinely complete search that
    # found nothing -- both were exit 1, and --quiet additionally suppresses
    # the warning text that is the ONLY other place this shows up. For a
    # tool whose entire purpose is answering "does this already exist?",
    # a caller that only checks the exit code (exactly what --quiet is
    # for) could not tell "confirmed absent" from "half the catalog was
    # never searched" -- the one silent-false-negative shape this tool's
    # own design already refuses to allow anywhere else. Exit 3 makes the
    # distinction reachable without reading --json/warnings.
    degraded_no_search = any(
        w["code"] in (WARN_CAPABILITIES_NOT_A_LIST, WARN_WIKI_PAGES_NOT_A_LIST) for w in result["warnings"]
    )
    return 3 if degraded_no_search else 1


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "search":
        return 2
    try:
        return cmd_search(args)
    except QueryFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)
    except BaseException as exc:
        # Same contract as build_cross_project_catalog.main(): never a raw
        # traceback, and KeyboardInterrupt/SystemExit are reported through
        # the same boundary and then RE-RAISED rather than flattened into
        # exit 4 -- swallowing Ctrl-C would break the caller's own interrupt
        # handling, and swallowing SystemExit would discard an exit code
        # something deliberately requested. `except Exception` would miss
        # both, since neither derives from Exception.
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
