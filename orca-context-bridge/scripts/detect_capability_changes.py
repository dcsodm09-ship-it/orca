#!/usr/bin/env python3
"""Read-only Tier-1 capability content-hash generator (M8 / Gate A of the
cross-project catalog plan).

Reads ONE file -- an already-built `catalog.json` from
build_cross_project_catalog.py -- and, for every capability whose `kind` is
on a fixed ALLOW-list (`script`, `config-pattern`), computes a stable content
hash that is meant to answer one question: "did this capability's own
declared content change since the last check?" -- WITHOUT re-running the
full fleet aggregator and WITHOUT executing anything.

kind ALLOW-LIST, NOT A DENY-LIST
---------------------------------
`kind == "skill"` and any kind value this script has never heard of both
fall into the SAME "not detected" bucket, on purpose. A future fifth `kind`
value therefore needs zero changes here: it silently does not participate in
Tier-1 hashing until someone deliberately adds it to KIND_VALUES_DETECTED,
exactly the posture build_cross_project_catalog.py's own KIND_VALUES enum
takes towards the capabilities schema in general.

WHAT GOES INTO THE HASH, AND WHY EACH EXCLUSION IS DELIBERATE
----------------------------------------------------------------
Included, in this fixed order: `name`, `kind`, `path`, `summary`, then a
normalized `depends_on` (see _normalized_depends_on_refs below), then --
`kind == "script"` only -- the literal byte content of the file `path`
points at, resolved relative to the capability's own project root.

Excluded: `last_verified_at`, and (defensively, should this catalog schema
ever grow one) any field whose name matches `*_at` or `*_timestamp`. Both are
provenance -- WHEN something was last checked, not WHAT it says -- and a
tool whose entire purpose is "did the content change" must never let a
re-verification with zero content edits present as a change. See
_ASSERT_NO_TIMESTAMP_LEAKAGE below: this is enforced structurally (the hash
input is built from an explicit field allow-list, not "everything on the
dict minus timestamps"), and the assertion exists as a second, independent
check that fires if a future edit ever widens that allow-list carelessly.

`depends_on` IN THIS FILE MEANS THE `raw` STRINGS, NOT THE RESOLVED GRAPH
--------------------------------------------------------------------------
catalog.json's `capabilities[].depends_on` is NOT the author's original
list of ref strings -- build_cross_project_catalog.py already resolved it
into a list of {raw, scope, ref_key, state, target_global_id?} objects (see
that module's assemble_catalog(), the `cap["depends_on"] = depends_on_out`
assignment). `state` and `target_global_id` are properties of the WHOLE
FLEET at build time (whether some OTHER project happens to declare a
matching capability right now), not properties of THIS capability's own
authored content. Hashing them would flap this capability's content hash
every time an unrelated project elsewhere in the fleet added, removed, or
renamed something -- exactly the false-positive-changed class this tool
exists to avoid, the same reasoning that excludes `last_verified_at`.

So only each dependency entry's own `raw` string is pulled out -- literally
what the author of THIS capability's source file wrote -- and the resulting
list is SORTED before hashing. Sorting (rather than preserving declared
order) is the deliberate normalization choice for this run: two
`depends_on` lists that name the same set of targets in a different order
describe the same dependency graph, and Tier-1 is a compatibility check, not
a formatting linter. (If a future round decides declaration order should
count as a real content change, this is the one line to revisit.)

SCRIPT BYTES: READ-ONLY, CONFINED TO THE CAPABILITY'S OWN PROJECT ROOT
--------------------------------------------------------------------------
The project root for a `script` capability's `path` is resolved from
catalog.json's own `projects[]` array (project_id -> real_path), per this
round's explicit instruction -- NOT by re-deriving it from
`capability["project_real_path"]`, and NOT by shelling out to
`orca repo list` / `orca worktree list` again. A capability naming a
project_id this catalog's `projects[]` never enumerated, an absolute `path`,
or a relative `path` that escapes the resolved project root (`../../etc`)
all degrade to `script_unreadable` rather than being read -- this script
inherits the aggregator's "read-only across every project boundary"
discipline even though it is a much smaller tool, because the whole point of
a project's `path` field is that it is DATA some other project's file wrote,
never a trusted instruction about where to read from.

A missing/unreadable/oversized script file is recorded as a NAMED
degradation (`script_unreadable: true` plus a `script_unreadable_reason`)
and processing continues with every other capability -- never a fatal error.
`project_id_ambiguous` (a real, observed condition -- see
build_cross_project_catalog.py's assign_project_ids()) is handled the same
way: the `status: "ok"` row is preferred among the colliding real_paths as
the best guess, and if that guess is not unique either, the row is chosen
deterministically (sorted by real_path) and the pick is recorded as
degraded so nothing here silently guesses without a trace.

BYTECODE CACHE CAVEAT (stated precisely rather than promised away)
--------------------------------------------------------------------------
Identical caveat to query_catalog.py's own: CPython writes
`__pycache__/*.pyc` next to this file when this module is IMPORTED (its
own test suite does exactly that). That is the import system caching
bytecode around executing the module body, so `sys.dont_write_bytecode =
True` below is, by construction, too late to prevent it for THIS module's
own import -- it only affects modules imported AFTER this line runs.
RUNNING this file as a script never writes it (CPython does not cache
`__main__`). Either way, `__pycache__/` only ever appears next to this
file itself, in this tool's own directory -- never inside any project
whose capabilities this tool reads -- so it does not bear on the
isolation guarantees above; it is a bookkeeping accuracy note, not a
write-surface exception.

WRITE SURFACE: A SEPARATE, MINIMAL COPY OF THE AGGREGATOR'S OWN PATTERN
--------------------------------------------------------------------------
`write_only_within()`, `atomic_write_within()`, and the lockfile pair below
are deliberately COPIED from build_cross_project_catalog.py's own
implementation, in reduced form, rather than imported -- the two tools must
keep independent trust surfaces (a bug in one write guard must not become a
bug in both by construction). The pinned output root is a FIXED absolute
path under the manifests root M4 already writes to and already has a
write-guarded, non-project-tracked home:
`/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/compat-pending-authorization/`
-- named `-pending-authorization` and deliberately NOT
`.../compat/` (the eventual production name) because wiring this tool's
output into anything that treats it as authoritative is a separate,
not-yet-granted step; this is only where the tool's own real output lands
today. DEFAULT_OUTPUT_DIR is an ABSOLUTE constant, not derived from
`Path(__file__)`'s location, on purpose: an earlier draft pinned it relative
to this script's own directory, which was correct only while the script
lived in a scratch staging area outside every project's tracked tree --
once relocated into a project's own `orca-context-bridge/scripts/` (the
normal end state for accepted work in this codebase), that same relative
pin would silently create a new, untracked directory INSIDE a project's own
tracked path, which is exactly the AUTHORITY_TRACKED_PATHS trap this whole
plan has hit twice before (see the M1 and M7 incident writeups). Pinning an
absolute path outside any project's tree removes the dependency on where
this file happens to be deployed entirely. There is no flag to point this
script's output anywhere else; the test suite redirects the pin by
rebinding the module-level `DEFAULT_OUTPUT_DIR` constant itself, exactly as
build_cross_project_catalog.py's own test suite documents doing for that
script.

EXIT CODES (generator semantics, matching build_cross_project_catalog.py)
--------------------------------------------------------------------------
    0  the run completed and wrote its output -- this is true even when
       zero capabilities matched the allow-list, and even when some
       capabilities degraded (script_unreadable etc.): that information is
       IN the JSON body (`degraded_count`, `considered_count`), never
       silently dropped, and never downgrades the exit code. A generator
       that ran to completion is exit 0 by definition here; only the
       CONSUMER (check_cross_project_compatibility.py, or a human) decides
       whether a degradation is actionable.
    2  usage error (bad flag value, unreadable --previous-hashes path
       supplied but not even a plausible file argument, etc.)
    4  could not run at all -- catalog.json missing/unreadable/unparseable,
       or this script's own output directory could not be secured (lock
       held, pin rejected, directory uncreatable). A fatal exit here is
       guaranteed to leave any PREVIOUS successful output byte-for-byte
       untouched: every fatal path returns before atomic_write_within() is
       ever called (see cmd_detect()).

Run with:
    python3 detect_capability_changes.py detect --catalog <path>
                                                 [--previous-hashes <path>]
                                                 [--json] [--quiet]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

SCRIPT_REL_PATH = "orca-context-bridge/scripts/detect_capability_changes.py"
GENERATOR_VERSION = "1.0.0"
OUTPUT_SCHEMA_VERSION = 1

# Fixed absolute path -- deliberately NOT derived from Path(__file__),
# see the module docstring's WRITE SURFACE section for why. NOT
# manifests/cross-project-catalog/compat/ (the eventual production name):
# wiring this tool's output into anything that treats it as authoritative
# is a separate, not-yet-granted authorization; do not rename this
# directory to the production name, and do not add a flag that could point
# it somewhere else.
DEFAULT_OUTPUT_DIR = Path("/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/compat-pending-authorization")
HASHES_NAME = "capability-content-hashes.json"
CHANGES_NAME = "capability-changes.json"
LOCK_NAME = ".detect.lock"
LOCK_STALE_SECONDS = 300

# Allow-list, not a deny-list -- see module docstring.
KIND_VALUES_DETECTED = ("script", "config-pattern")

MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_PREVIOUS_HASHES_BYTES = 16 * 1024 * 1024
# KNOWN LIMITATION (documented here, not just in review notes): a script
# file over this size degrades to script_unreadable / script_too_large
# rather than being read at all (see read_script_bytes()), and that
# degraded placeholder is a FIXED reason string, not a hash of whatever
# bytes exist. Two DIFFERENT oversized files -- even with completely
# different real content -- therefore produce the SAME content_hash for
# that capability, because the "content changed" signal this tool exists
# to provide never actually inspected either file's bytes. This is a real,
# structural blind spot for any script capability that stays above this
# threshold indefinitely (as opposed to one that crosses it once, which
# still correctly flips the hash the moment it goes from readable to
# too-large or back). Real project scripts are essentially never this
# large, so the threshold is not lowered or removed on its own -- but a
# reviewer or future maintainer should not mistake this generator's "the
# hash didn't change" for "the file didn't change" once a script sits
# above MAX_SCRIPT_BYTES.
MAX_SCRIPT_BYTES = 2 * 1024 * 1024

# Field-name shapes that must never be folded into the hash input, even by a
# future careless edit. Checked defensively in _assert_no_excluded_fields();
# the real enforcement is that the hash input is built from an explicit,
# fixed field list in the first place (see _capability_hash_fields()).
_EXCLUDED_FIELD_NAME_RE = re.compile(r"(^|_)(at|timestamp)$")

_CONTROL_CHARS = ("\n", "\r", "\t", chr(0x2028), chr(0x2029), "\x00")


class DetectFatal(Exception):
    """Cannot run at all -- maps to exit code 4. Never raised after a write
    has happened; see cmd_detect()."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Small shared helpers (deliberately duplicated across this project's tools;
# see module docstring)
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_line_separators(text: str) -> str:
    """Escape U+2028/U+2029 in already-serialized JSON text. Same two-line
    contract as build_cross_project_catalog.py / query_catalog.py; copied,
    not imported (see module docstring)."""
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


def _read_bounded_file(path: Path, max_bytes: int, *, follow_symlinks: bool) -> bytes:
    """Open, size-cap, and fully read one regular file, read-only.

    O_NONBLOCK: a FIFO planted at this path would otherwise block inside
    os.open() itself, in the kernel, before any S_ISREG check downstream
    could run -- the same trap build_cross_project_catalog.py's
    _read_previous_catalog() documents and fixes. S_ISREG then rejects a
    FIFO/device/directory outright.
    """
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if not follow_symlinks:
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"not a regular file: {path}")
        if st.st_size > max_bytes:
            raise OSError(f"exceeds {max_bytes} bytes: {path}")
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
        raise OSError(f"exceeds {max_bytes} bytes (grew during read): {path}")
    return raw


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        raw = _read_bounded_file(path, MAX_CATALOG_BYTES, follow_symlinks=True)
    except FileNotFoundError:
        raise DetectFatal("catalog_missing", str(path))
    except OSError as exc:
        raise DetectFatal("catalog_unreadable", str(exc))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DetectFatal("catalog_unparseable", str(exc))
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise DetectFatal("catalog_unparseable", str(exc))
    if not isinstance(doc, dict):
        raise DetectFatal("catalog_malformed", "top level is not a JSON object")
    return doc


def load_previous_hashes(path: Path) -> tuple[dict[str, Any], str, str | None]:
    """Returns (hashes_map, status, reason). Never raises -- an unusable
    --previous-hashes file degrades to "treat as empty" (status != "ok"),
    it must never abort the run (generator semantics: this is optional
    input)."""
    try:
        raw = _read_bounded_file(path, MAX_PREVIOUS_HASHES_BYTES, follow_symlinks=True)
    except FileNotFoundError:
        return {}, "absent", None
    except OSError as exc:
        return {}, "unreadable", str(exc)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {}, "unreadable", str(exc)
    if not isinstance(doc, dict) or not isinstance(doc.get("hashes"), dict):
        return {}, "unreadable", "no 'hashes' object at top level"
    hashes = doc["hashes"]
    out: dict[str, Any] = {}
    for global_id, entry in hashes.items():
        if isinstance(global_id, str) and isinstance(entry, dict) and isinstance(entry.get("content_hash"), str):
            out[global_id] = entry
    return out, "ok", None


# ---------------------------------------------------------------------------
# projects[] project_id -> real_path resolution
# ---------------------------------------------------------------------------


def build_project_root_index(catalog: dict[str, Any]) -> tuple[dict[str, Path], dict[str, bool]]:
    """project_id -> resolved real_path Path, using ONLY catalog.json's own
    `projects[]` array (never a fresh `orca ... list` call -- this round's
    explicit instruction). Returns (roots, ambiguous_flags).

    When >1 row shares a project_id (a real, observed condition -- see
    build_cross_project_catalog.py's assign_project_ids()), the row with
    status "ok" is preferred if exactly one such row exists; otherwise the
    first row sorted by real_path is used and `ambiguous_flags[project_id]`
    is set True so callers can record the pick as a degradation rather than
    silently guessing.
    """
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
    ambiguous: dict[str, bool] = {}
    for pid, members in by_id.items():
        if len(members) == 1:
            roots[pid] = Path(members[0]["real_path"])
            ambiguous[pid] = False
            continue
        ok_rows = [m for m in members if m.get("status") == "ok"]
        chosen = ok_rows[0] if len(ok_rows) == 1 else sorted(members, key=lambda m: m["real_path"])[0]
        roots[pid] = Path(chosen["real_path"])
        ambiguous[pid] = True
    return roots, ambiguous


# ---------------------------------------------------------------------------
# Script byte resolution -- read-only, confined to the resolved project root
# ---------------------------------------------------------------------------


def resolve_script_path(project_root: Path, path_value: object) -> tuple[Path | None, str | None]:
    """Returns (resolved_path, None) or (None, reason). `path_value` is
    untrusted data from another project's own file, exactly like every path
    build_cross_project_catalog.py's read_only_from() refuses to trust
    outright."""
    if not isinstance(path_value, str) or not path_value:
        return None, "no_path"
    if any(ch in path_value for ch in _CONTROL_CHARS):
        return None, "path_has_control_chars"
    candidate = Path(path_value)
    if candidate.is_absolute():
        return None, "path_is_absolute"
    try:
        real_root = project_root.resolve(strict=False)
        resolved = (project_root / candidate).resolve(strict=False)
    except OSError:
        return None, "path_unresolvable"
    try:
        resolved.relative_to(real_root)
    except ValueError:
        return None, "path_escapes_project_root"
    if resolved == real_root:
        return None, "path_is_project_root"
    return resolved, None


def read_script_bytes(project_root: Path, path_value: object) -> tuple[bytes | None, str | None]:
    resolved, reason = resolve_script_path(project_root, path_value)
    if reason:
        return None, reason
    assert resolved is not None
    try:
        # O_NOFOLLOW on the fully resolve()d path closes the TOCTOU window
        # (a symlink swapped in between resolve() and open()) rather than
        # rejecting a legitimate symlinked file inside the project --
        # resolve() already collapsed any symlink COMPONENTS, so this
        # os.open() only ever fails if something changed underneath us.
        return _read_bounded_file(resolved, MAX_SCRIPT_BYTES, follow_symlinks=False), None
    except FileNotFoundError:
        return None, "script_missing"
    except PermissionError:
        return None, "script_permission_denied"
    except OSError as exc:
        message = str(exc)
        if "exceeds" in message:
            return None, "script_too_large"
        return None, "script_open_error"


# ---------------------------------------------------------------------------
# Hash construction
# ---------------------------------------------------------------------------

_HASH_FIELD_SEP = b"\x1f"  # ASCII unit separator: avoids "ab"+"c" vs "a"+"bc" ambiguity


def _assert_no_excluded_fields(field_names: tuple[str, ...]) -> None:
    """Defensive check pinning the module docstring's exclusion promise:
    fires only if a future edit ever adds a *_at/*_timestamp-shaped name to
    the fixed field list _capability_hash_fields() draws from."""
    for name in field_names:
        if _EXCLUDED_FIELD_NAME_RE.search(name):
            raise AssertionError(f"hash input field {name!r} looks like a timestamp; must be excluded")


_HASH_FIELD_NAMES = ("name", "kind", "path", "summary", "depends_on")
_assert_no_excluded_fields(_HASH_FIELD_NAMES)


def _normalized_depends_on_refs(cap: dict[str, Any]) -> list[str]:
    """Pull the author-written `raw` ref string out of each already-resolved
    depends_on entry (see module docstring for why the resolved fields
    themselves must not be hashed), then sort for order-independence."""
    depends_on = cap.get("depends_on")
    refs: list[str] = []
    if isinstance(depends_on, list):
        for dep in depends_on:
            if isinstance(dep, dict) and isinstance(dep.get("raw"), str):
                refs.append(dep["raw"])
    return sorted(refs)


def compute_capability_hash(
    cap: dict[str, Any], project_roots: dict[str, Path], ambiguous_project_ids: dict[str, bool]
) -> dict[str, Any]:
    """Returns a result dict with at least: content_hash, kind, checked_at.
    Never raises -- any failure to read a script's bytes degrades to a named
    field on the result, never aborts this one capability let alone the run.
    """
    name = cap.get("name") if _is_str(cap.get("name")) else None
    kind = cap.get("kind") if _is_str(cap.get("kind")) else ""
    path = cap.get("path") if _is_str(cap.get("path")) else None
    summary = cap.get("summary") if _is_str(cap.get("summary")) else None
    depends_on_refs = _normalized_depends_on_refs(cap)

    def _b(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")

    parts = [
        b"name=" + _b(name),
        b"kind=" + _b(kind),
        b"path=" + _b(path),
        b"summary=" + _b(summary),
        b"depends_on=" + _b(depends_on_refs),
    ]

    result: dict[str, Any] = {"kind": kind, "checked_at": now_iso()}

    if kind == "script":
        project_id = cap.get("project_id") if _is_str(cap.get("project_id")) else None
        project_root = project_roots.get(project_id) if project_id is not None else None
        if project_root is None:
            script_bytes, reason = None, "project_root_unknown"
        else:
            script_bytes, reason = read_script_bytes(project_root, path)
        if project_id is not None and ambiguous_project_ids.get(project_id):
            result["project_id_ambiguous"] = True
        if script_bytes is None:
            result["script_unreadable"] = True
            result["script_unreadable_reason"] = reason
            parts.append(b"script_sha256=<unreadable>")
        else:
            script_sha256 = hashlib.sha256(script_bytes).hexdigest()
            parts.append(b"script_sha256=" + script_sha256.encode("ascii"))

    digest_input = _HASH_FIELD_SEP.join(parts)
    result["content_hash"] = hashlib.sha256(digest_input).hexdigest()
    return result


# ---------------------------------------------------------------------------
# Detection over capabilities[]
# ---------------------------------------------------------------------------


def detect_capability_hashes(catalog: dict[str, Any]) -> dict[str, Any]:
    capabilities = catalog.get("capabilities")
    project_roots, ambiguous_project_ids = build_project_root_index(catalog)

    hashes: dict[str, Any] = {}
    considered = 0
    skipped_kind = 0
    skipped_malformed = 0
    degraded_count = 0
    script_unreadable_count = 0

    if isinstance(capabilities, list):
        for cap in capabilities:
            if not isinstance(cap, dict):
                skipped_malformed += 1
                continue
            kind = cap.get("kind")
            global_id = cap.get("global_id")
            if kind not in KIND_VALUES_DETECTED:
                skipped_kind += 1
                continue
            if not isinstance(global_id, str) or not global_id:
                # No trustworthy join key for this capability -- cannot be
                # reported in hashes{} (which is keyed by global_id) or
                # matched against a previous run. Counted, not silently
                # dropped.
                skipped_malformed += 1
                continue
            considered += 1
            entry = compute_capability_hash(cap, project_roots, ambiguous_project_ids)
            if entry.get("script_unreadable"):
                script_unreadable_count += 1
                degraded_count += 1
            elif entry.get("project_id_ambiguous"):
                degraded_count += 1
            project_id = cap.get("project_id") if _is_str(cap.get("project_id")) else None
            if project_id is not None:
                entry["project_id"] = project_id
            hashes[global_id] = entry

    return {
        "hashes": hashes,
        "counts": {
            "total_capabilities_in_catalog": len(capabilities) if isinstance(capabilities, list) else 0,
            "considered_count": considered,
            "skipped_kind_count": skipped_kind,
            "skipped_malformed_count": skipped_malformed,
            "degraded_count": degraded_count,
            "script_unreadable_count": script_unreadable_count,
        },
    }


def diff_hashes(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    prev_ids = set(previous.keys())
    curr_ids = set(current.keys())
    added = sorted(curr_ids - prev_ids)
    removed = sorted(prev_ids - curr_ids)
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for global_id in sorted(curr_ids & prev_ids):
        prev_hash = previous[global_id].get("content_hash")
        curr_hash = current[global_id].get("content_hash")
        if prev_hash != curr_hash:
            changed.append({"global_id": global_id, "previous_hash": prev_hash, "current_hash": curr_hash})
        else:
            unchanged.append(global_id)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": len(unchanged),
        },
    }


# ---------------------------------------------------------------------------
# Output write: write_only_within / atomic_write_within / lockfile -- a
# reduced, independently-maintained copy of build_cross_project_catalog.py's
# own pattern (see module docstring: not imported, on purpose).
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


def write_only_within(output_dir: Path, path_value: object) -> tuple[Path | None, str | None]:
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        return None, "invalid_path"
    path = Path(path_value)
    if not path.is_absolute():
        return None, "non_absolute_path"
    lexical_root = output_dir.absolute()
    lexical_path = path.absolute()
    try:
        lexical_path.relative_to(lexical_root)
    except ValueError:
        return None, "outside_output_dir"
    if _has_symlink_component(lexical_path, lexical_root):
        return None, "symlink_path"
    resolved_root = output_dir.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None, "outside_output_dir"
    if resolved_path == resolved_root:
        return None, "is_output_root"
    return resolved_path, None


def atomic_write_within(output_dir: Path, final_path: Path, payload: bytes) -> None:
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
        dir_fd = os.open(str(output_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def acquire_lock(output_dir: Path) -> Path:
    lock_path = output_dir / LOCK_NAME
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
            raise DetectFatal("lock_held")
    raise DetectFatal("lock_held")


def release_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        os.unlink(str(lock_path))
    except OSError:
        pass


def _encode_json(payload: dict[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=False, indent=1)
    return _sanitize_line_separators(text).encode("utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Tier-1 capability content-hash generator over an already-built catalog.json."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect = subparsers.add_parser(
        "detect", help="Hash every script/config-pattern capability's content and optionally diff against a previous run."
    )
    detect.add_argument("--catalog", type=str, required=True, help="Path to catalog.json (read-only).")
    detect.add_argument(
        "--previous-hashes",
        type=str,
        default=None,
        help="Path to a previous capability-content-hashes.json to diff against.",
    )
    detect.add_argument("--json", action="store_true", help="Print the run summary as one JSON object to stdout.")
    detect.add_argument("--quiet", action="store_true", help="Suppress human-readable text; rely on the exit code.")
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


def cmd_detect(args: argparse.Namespace) -> int:
    # An empty/whitespace-only path cannot name anything -- the same class
    # of usage error query_catalog.py's cmd_search() rejects for an empty
    # keyword, checked before any filesystem access.
    if not args.catalog.strip():
        return _emit_error(args, 2, "empty_catalog_path")
    if args.previous_hashes is not None and not args.previous_hashes.strip():
        return _emit_error(args, 2, "empty_previous_hashes_path")

    # --catalog and --previous-hashes are READ paths with no write-side
    # security concern (unlike --output on the aggregator), so -- matching
    # query_catalog.py's own --catalog handling -- a relative path is
    # accepted as-is rather than rejected as a usage error.
    catalog_path = Path(args.catalog).expanduser()

    # Every fatal path (all catalog reading, all hashing) MUST complete
    # before any write is attempted, so a failed run never disturbs a
    # previous successful output -- mirrors
    # build_cross_project_catalog.py's CatalogFatal-before-write discipline.
    try:
        catalog = load_catalog(catalog_path)
    except DetectFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)

    previous_hashes: dict[str, Any] = {}
    previous_status = "not_requested"
    previous_reason: str | None = None
    previous_hashes_path: str | None = None
    if args.previous_hashes is not None:
        prev_path = Path(args.previous_hashes).expanduser()
        previous_hashes_path = str(prev_path)
        previous_hashes, previous_status, previous_reason = load_previous_hashes(prev_path)

    detection = detect_capability_hashes(catalog)

    output_dir = DEFAULT_OUTPUT_DIR
    try:
        os.makedirs(str(output_dir), mode=0o700, exist_ok=True)
    except OSError as exc:
        return _emit_error(args, 4, "output_dir_uncreatable", str(exc))
    if os.path.islink(str(output_dir)):
        return _emit_error(args, 4, "output_dir_is_symlink")

    hashes_final, reason = write_only_within(output_dir, str(output_dir / HASHES_NAME))
    if reason or hashes_final is None:
        return _emit_error(args, 4, reason or "invalid_path")
    changes_final, reason = write_only_within(output_dir, str(output_dir / CHANGES_NAME))
    if reason or changes_final is None:
        return _emit_error(args, 4, reason or "invalid_path")
    output_dir = hashes_final.parent

    try:
        lock_path = acquire_lock(output_dir)
    except DetectFatal as exc:
        return _emit_error(args, 4, exc.reason)

    try:
        generated_at = now_iso()
        hashes_payload = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "generator": {"script": SCRIPT_REL_PATH, "version": GENERATOR_VERSION},
            "generated_at": generated_at,
            "catalog_path": str(catalog_path),
            "catalog_verified_at": catalog.get("verified_at") if _is_str(catalog.get("verified_at")) else None,
            "counts": detection["counts"],
            "hashes": detection["hashes"],
        }
        atomic_write_within(output_dir, hashes_final, _encode_json(hashes_payload))

        changes_payload: dict[str, Any] | None = None
        if args.previous_hashes is not None:
            diff = diff_hashes(previous_hashes, detection["hashes"])
            changes_payload = {
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "generator": {"script": SCRIPT_REL_PATH, "version": GENERATOR_VERSION},
                "generated_at": generated_at,
                "previous_hashes_path": previous_hashes_path,
                "previous_hashes_status": previous_status,
                "previous_hashes_reason": previous_reason,
                **diff,
            }
            atomic_write_within(output_dir, changes_final, _encode_json(changes_payload))
    finally:
        release_lock(lock_path)

    if args.json and not args.quiet:
        summary = {
            "ok": True,
            "hashes_output": str(hashes_final),
            "changes_output": str(changes_final) if changes_payload is not None else None,
            "counts": detection["counts"],
            "diff_counts": changes_payload["counts"] if changes_payload is not None else None,
        }
        print(_sanitize_line_separators(json.dumps(summary, ensure_ascii=False, indent=1)))
    elif not args.quiet:
        counts = detection["counts"]
        print(
            f"detected {counts['considered_count']} of {counts['total_capabilities_in_catalog']} capabilities "
            f"(skipped {counts['skipped_kind_count']} by kind, {counts['skipped_malformed_count']} malformed; "
            f"{counts['degraded_count']} degraded, {counts['script_unreadable_count']} script_unreadable)"
        )
        print(f"wrote {hashes_final}")
        if changes_payload is not None:
            dc = changes_payload["counts"]
            print(
                f"wrote {changes_final} (added {dc['added']}, removed {dc['removed']}, "
                f"changed {dc['changed']}, unchanged {dc['unchanged']})"
            )

    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "detect":
        return 2
    try:
        return cmd_detect(args)
    except DetectFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)
    except BaseException as exc:
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
