#!/usr/bin/env python3
"""Validate a wiki/reusable-capabilities.json file (M3 of the cross-project
catalog plan).

reusable-capabilities.json is a sibling of the hand-maintained
wiki/orca-context-wiki.json, NOT script-generated. It catalogs the
CAPABILITY half of the "技能/脚本/工具 和 知识/经验" split (reusable skills,
scripts, and config-patterns); project-specific facts stay in
orca-context-wiki.json, which M4's aggregator handles separately. There is
deliberately no "knowledge" value in this file's `kind` enum.

This script is a standalone, read-only structural validator -- the M3
delivery. It does not build anything downstream (that is M4's aggregator and
M5's reverse index); it only decides whether one or more candidate files are
well-formed enough for those later stages to consume safely.

Hard property: this script is strictly READ-ONLY. It never writes, never
mkdirs, never fills in last_verified_at, never touches
.orca/context/reviewed-startup-pack-manifest.json or re-signs anything.
reusable-capabilities.json is NOT one of that manifest's
shared_source_sha256s keys (verified: those are exactly
{capabilities, graphify_catalog, wiki}, schema_version 2, read directly) --
so this validator deliberately imports nothing from build_startup_bundle.py.
A validator for an unpinned file has no business coupling itself to the
pinned trust anchor.

Top-level schema (exactly 3 keys):
  {
    "schema_version": 1,        # integer literal 1, not "1", not 1.0
    "project": "orca/完善orca",  # project identity string
    "capabilities": [ ... ]     # array, may be empty
  }

Capability entry (exactly 7 keys, all required):
  {
    "id": "wiki-freshness-check",
    "kind": "script",                 # "skill" | "script" | "config-pattern"
    "name": "check_wiki_freshness.py",
    "path": "orca-context-bridge/scripts/check_wiki_freshness.py",
    "summary": "...",
    "last_verified_at": "2026-08-22T05:13:40Z",  # or null
    "depends_on": ["script:build_startup_bundle.py"]
  }

depends_on grammar (what M5's reverse index will consume):
  same-project  : "<kind>:<name>"             e.g. "script:build_startup_bundle.py"
  cross-project : "<project>:<kind>:<name>"   e.g. "orca/完善orca:script:build_knowledge_graph.py"
Disambiguated by colon-part COUNT (2 vs 3): ':' is banned inside
project/kind/name, so the second-to-last colon-separated part is always
<kind> and the last is always <name>; anything before that is <project>
(which may itself contain '/').

CROSS-PROJECT refs (3-part form) are format-checked only and NEVER resolved
-- this script does not open, stat, or look for any other project's
reusable-capabilities.json. That resolution is M4's aggregator's job. A
cross-project ref that cannot resolve today (because the target project has
no reusable-capabilities.json yet) is a normal, valid entry here.

ONE deliberate exception to "never resolved": a 3-part ref whose <project>
segment equals THIS FILE's OWN `project` value is rejected
(redundant_cross_project_ref) with a message pointing at the correct 2-part
form. This is not cross-file resolution -- it's a self-contained string
comparison against a value already loaded from this same document, so it
stays within the "format-checked, single-file" boundary above; it exists so
M5's reverse index never carries two different spellings of one edge.

SAME-PROJECT refs (2-part form) MUST resolve to a (kind, name) pair present
in this same file -- that check is local, free, and in scope.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


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

WIKI_DIR_NAME = "wiki"
DEFAULT_FILENAME = "reusable-capabilities.json"


class UsageError(ValueError):
    """A file-level problem that stops parsing/derivation before validation
    even begins (missing file, bad JSON, non-object top level). Maps to
    exit 2."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """object_pairs_hook that raises instead of silently keeping the last
    value (round-2 M3 fix, 2026-08-22: plain json.loads() silently accepts
    a document with e.g. two "schema_version" keys and keeps only the
    last one -- a dual review caught this as a real gap for a schema whose
    entire validity model is "exactly N keys, no more, no less": a
    duplicate key is ambiguous across different JSON consumers and should
    never have been producible by anything that respects this schema)."""
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON object")
        seen[key] = value
    return seen


def load_json_object(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UsageError(f"could not read {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{path} is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path} is not valid JSON: {exc}") from exc
    except ValueError as exc:
        # Round-3 M3 fix, 2026-08-22: a document with a duplicate key is
        # legal per RFC 8259 (which permits repeated names, last one
        # wins) -- json.JSONDecodeError is the wrong exception class and
        # "not valid JSON" was a misleading message for it. Rejecting a
        # duplicate key is THIS validator's own policy choice (this
        # schema's entire validity model is "exactly N keys"), raised as a
        # plain ValueError from _reject_duplicate_keys above, not a JSON
        # syntax error -- give it its own accurate message.
        raise UsageError(f"{path} has a duplicate key, not allowed by this schema: {exc}") from exc


def infer_project_root(file_path: Path) -> Path:
    """The parent of the wiki/ directory containing the validated file.

    (Round-2 M3 fix, 2026-08-22: this used to branch on
    `parent.name == WIKI_DIR_NAME`, but both branches returned the exact
    same value -- dead code that looked like it handled a non-wiki/ layout
    differently, but didn't. Simplified; behavior is unchanged for every
    real call site, since the caller only ever passes a path that already
    lives under wiki/.)
    """
    return file_path.parent.parent


def derive_expected_project_id(project_root: Path) -> str | None:
    """Best-effort derivation of the expected `project` value from a
    project root path, walking up for a 'workspaces' or 'projects' path
    segment.

      workspaces/<topic>/<task>  -> "<topic>/<task>"
      workspaces/<topic>         -> "<topic>"  (shallow layout, no <task>)
      projects/<name>            -> "<name>"

    Returns None when neither marker segment is found (e.g. a detached
    test fixture) -- in that case the project-id cross-check is skipped
    entirely rather than flagged as a mismatch.

    Takes the RESOLVED PROJECT ROOT, not the validated file's own path
    (round-2 M3 fix, 2026-08-22: two bugs a dual review caught in the
    file-path version). First: it sliced a fixed 2 segments after
    "workspaces" unconditionally, so a shallow
    workspaces/<topic>/wiki/reusable-capabilities.json layout (only one
    segment between "workspaces" and "wiki") produced "<topic>/wiki", and
    an even shallower workspaces/<topic>/reusable-capabilities.json (no
    wiki/ subdirectory at all) produced "<topic>/reusable-capabilities.json"
    -- the filename itself leaking into the derived project id. Basing this
    on the project root (which is always exactly the directory whose
    children include wiki/) and taking at most 2 remaining segments fixes
    both. Second: taking the root as a parameter (rather than re-deriving
    it from the file's on-disk location internally) means --project-root
    now actually participates in project-id derivation, matching what
    --help already claimed and making --strict-project-id meaningful when
    combined with --project-root instead of silently no-op'ing.
    """
    resolved = project_root.resolve(strict=False)
    parts = resolved.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "workspaces":
            remainder = parts[index + 1 :]
            if len(remainder) >= 2:
                return "/".join(remainder[:2])
            if len(remainder) == 1:
                return remainder[0]
            return None
        if parts[index] == "projects" and index + 1 < len(parts):
            return parts[index + 1]
    return None


class Finding:
    __slots__ = ("pointer", "code", "message")

    def __init__(self, pointer: str, code: str, message: str) -> None:
        self.pointer = pointer
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"pointer": self.pointer, "code": self.code, "message": self.message}


class ValidationRun:
    """Accumulates errors/warnings for one file. Never raises -- callers
    inspect .errors / .warnings after calling validate_document()."""

    def __init__(self) -> None:
        self.errors: list[Finding] = []
        self.warnings: list[Finding] = []
        self.project: str | None = None
        self.capability_count = 0

    def error(self, pointer: str, code: str, message: str) -> None:
        self.errors.append(Finding(pointer, code, message))

    def warn(self, pointer: str, code: str, message: str) -> None:
        self.warnings.append(Finding(pointer, code, message))


def _is_plain_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_document(
    doc: Any,
    *,
    project_root: Path,
    check_paths: bool,
    strict_project_id: bool,
    expected_project_id: str | None,
    now: datetime,
) -> ValidationRun:
    run = ValidationRun()

    if not isinstance(doc, dict):
        run.error("", "not_an_object", "document root must be a JSON object")
        return run

    # schema_version is checked FIRST and is a true hard stop: an invalid or
    # missing version reports ONLY this one finding, with nothing else about
    # the document inspected -- "hard error, no partial parse" per the
    # design, since schema_version exists precisely to gate how everything
    # else here is interpreted.
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
        run.error(f".{key}", "unknown_field", f"unexpected top-level key {key!r}; only {sorted(TOP_LEVEL_KEYS)} are allowed")

    missing_top = sorted(TOP_LEVEL_KEYS - set(doc.keys()))
    for key in missing_top:
        run.error(f".{key}", "missing_field", f"missing required top-level key {key!r}")

    project = doc.get("project")
    if not _is_plain_str(project) or not project:
        run.error(".project", "bad_project", "project must be a non-empty string")
        project = None
    else:
        if project != project.strip():
            run.error(".project", "bad_project", "project must not have leading/trailing whitespace")
            project = None
        elif ":" in project:
            run.error(".project", "bad_project", "project must not contain ':' (reserved as the depends_on separator)")
            project = None
        # Round-3 M3 fix, 2026-08-22: previously only checked "\n"/"\r"
        # here, while `name`/`summary` were hardened (round 2) to also
        # reject "\t", U+2028, U+2029, and NUL -- a dual review's own
        # verification run demonstrated concrete harm from the gap: with
        # --json emitting ensure_ascii=False, a `project` value containing
        # U+2028 gets written straight into stdout and splits what "one
        # JSON object per line" output into two fragments, neither of
        # which parses -- and `project` is the exact field M4's aggregator
        # keys project identity on and redundant_cross_project_ref
        # string-compares against, so this is not merely a consistency
        # nit. Same character set as name/summary now.
        elif any(ch in project for ch in ("\n", "\r", "\t", "\u2028", "\u2029", "\x00")):
            run.error(".project", "bad_project", "project must not contain a newline, tab, line-separator, or NUL character")
            project = None
    run.project = project

    if project is not None and expected_project_id is not None and project != expected_project_id:
        pointer = ".project"
        message = (
            f"project {project!r} does not match the id derived from the project root "
            f"({expected_project_id!r})"
        )
        if strict_project_id:
            run.error(pointer, "project_id_mismatch", message)
        else:
            run.warn(pointer, "project_id_mismatch", message)

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
            index=index,
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
                run.error(
                    f"{pointer_base}.id",
                    "duplicate_id",
                    f"duplicate id {entry_id!r} (also at capabilities[{seen_ids[key]}])",
                )
            else:
                seen_ids[key] = index

        kind = raw_entry.get("kind")
        name = raw_entry.get("name")
        if _is_plain_str(kind) and kind in KIND_VALUES and _is_plain_str(name) and name.strip():
            kn_key = (kind, name.strip())
            if kn_key in seen_kind_name:
                run.error(
                    f"{pointer_base}.name",
                    "duplicate_kind_name",
                    f"duplicate (kind, name) pair {kn_key!r} (also at capabilities[{seen_kind_name[kn_key]}])",
                )
            else:
                seen_kind_name[kn_key] = index

    run.capability_count = len(entries)

    # depends_on resolution pass, done after every entry's own (kind, name)
    # is known, so same-project refs can resolve regardless of declaration
    # order.
    local_kind_names = set(seen_kind_name.keys())
    for index, raw_entry in enumerate(capabilities):
        if not isinstance(raw_entry, dict):
            continue
        pointer_base = f".capabilities[{index}]"
        _validate_depends_on(
            raw_entry,
            pointer_base=pointer_base,
            run=run,
            local_kind_names=local_kind_names,
            self_project=project,
        )

    return run


def _validate_entry(
    entry: dict[str, Any],
    *,
    index: int,
    pointer_base: str,
    run: ValidationRun,
    project_root: Path,
    check_paths: bool,
    now: datetime,
) -> None:
    unknown = sorted(set(entry.keys()) - ENTRY_KEYS)
    for key in unknown:
        run.error(f"{pointer_base}.{key}", "unknown_field", f"unexpected key {key!r} in capability entry; only {sorted(ENTRY_KEYS)} are allowed")

    missing = sorted(ENTRY_KEYS - set(entry.keys()))
    for key in missing:
        run.error(f"{pointer_base}.{key}", "missing_field", f"capability entry is missing required key {key!r}")

    # id
    entry_id = entry.get("id")
    if not _is_plain_str(entry_id):
        run.error(f"{pointer_base}.id", "bad_id", "id must be a string")
    elif not (1 <= len(entry_id) <= ID_MAX_LEN) or not ID_RE.match(entry_id):
        run.error(
            f"{pointer_base}.id",
            "bad_id",
            f"id {entry_id!r} must match ^[a-z0-9]+(-[a-z0-9]+)*$ and be 1..{ID_MAX_LEN} characters",
        )

    # kind
    kind = entry.get("kind")
    if not _is_plain_str(kind) or kind not in KIND_VALUES:
        run.error(f"{pointer_base}.kind", "bad_kind", f"kind must be one of {list(KIND_VALUES)}, got {kind!r}")
        kind = None

    # name
    # Round-2 M3 fix, 2026-08-22: previously validated len(name.strip()) but
    # stored/joined the raw `name` elsewhere (duplicate_kind_name above DOES
    # use name.strip() as its dict key, but a downstream consumer joining a
    # depends_on string as f"{kind}:{name}" would use the raw, unstripped
    # value) -- a dual review caught that a name with leading/trailing
    # whitespace or an embedded TAB would pass validation while being a
    # different string than what duplicate_kind_name/depends_on resolution
    # keys on, a silent same-project reference miss. Now rejects
    # leading/trailing whitespace outright (matching `project`'s existing
    # rule) instead of tolerating and stripping it, and checks the RAW
    # length, not the stripped length.
    name = entry.get("name")
    if not _is_plain_str(name):
        run.error(f"{pointer_base}.name", "bad_name", "name must be a string")
    else:
        if not name:
            run.error(f"{pointer_base}.name", "bad_name", "name must be non-empty")
        elif name != name.strip():
            run.error(f"{pointer_base}.name", "bad_name", "name must not have leading or trailing whitespace")
        elif len(name) > NAME_MAX_LEN:
            run.error(f"{pointer_base}.name", "bad_name", f"name must be <= {NAME_MAX_LEN} characters")
        elif ":" in name:
            run.error(f"{pointer_base}.name", "bad_name", "name must not contain ':' (reserved as the depends_on separator)")
        elif any(ch in name for ch in ("\n", "\r", "\t", "\u2028", "\u2029", "\x00")):
            run.error(f"{pointer_base}.name", "bad_name", "name must not contain a newline, tab, line-separator, or NUL character")

    # path
    path_value = entry.get("path")
    if not _is_plain_str(path_value) or not path_value:
        run.error(f"{pointer_base}.path", "bad_path", "path must be a non-empty string")
    else:
        _validate_path(
            path_value,
            pointer=f"{pointer_base}.path",
            run=run,
            project_root=project_root,
            check_paths=check_paths,
        )

    # summary
    # Round-2 M3 fix, 2026-08-22: previously checked len(summary.strip())
    # against the limit while checking "\n"/"\r" against the raw string --
    # a dual review found a 302-character summary with leading/trailing
    # whitespace that strips to exactly 300 chars passed the length check
    # against its own documented 300-char limit, and separately that
    # U+2028/U+2029 (line/paragraph separator -- rendered as a real newline
    # by some downstream JS consumers) were not rejected by the
    # "\n"/"\r"-only check. Now checks RAW length, rejects leading/trailing
    # whitespace outright (matching `project`'s existing rule), and rejects
    # the fuller set of line-breaking/control characters.
    summary = entry.get("summary")
    if not _is_plain_str(summary):
        run.error(f"{pointer_base}.summary", "bad_summary", "summary must be a string")
    else:
        if not summary:
            run.error(f"{pointer_base}.summary", "bad_summary", "summary must be non-empty")
        elif summary != summary.strip():
            run.error(f"{pointer_base}.summary", "bad_summary", "summary must not have leading or trailing whitespace")
        elif len(summary) > SUMMARY_MAX_LEN:
            run.error(f"{pointer_base}.summary", "bad_summary", f"summary must be <= {SUMMARY_MAX_LEN} characters")
        elif any(ch in summary for ch in ("\n", "\r", "\t", "\u2028", "\u2029", "\x00")):
            run.error(f"{pointer_base}.summary", "bad_summary", "summary must be a single line (no newline, tab, line-separator, or NUL character)")

    # last_verified_at
    last_verified_at = entry.get("last_verified_at")
    if last_verified_at is not None:
        if not _is_plain_str(last_verified_at) or not TIMESTAMP_RE.match(last_verified_at):
            run.error(
                f"{pointer_base}.last_verified_at",
                "bad_timestamp",
                "last_verified_at must be null or match ^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$",
            )
        else:
            parsed = _parse_strict_utc_timestamp(last_verified_at)
            if parsed is None:
                run.error(
                    f"{pointer_base}.last_verified_at",
                    "bad_timestamp",
                    f"last_verified_at {last_verified_at!r} is not a real calendar date/time",
                )
            elif parsed > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
                run.error(
                    f"{pointer_base}.last_verified_at",
                    "future_verified_at",
                    f"last_verified_at {last_verified_at!r} is more than {MAX_FUTURE_SKEW_SECONDS} seconds in the future",
                )

    # depends_on: structural type-check only here; grammar/resolution is
    # handled by _validate_depends_on() in a second pass.
    depends_on = entry.get("depends_on")
    if not isinstance(depends_on, list):
        run.error(f"{pointer_base}.depends_on", "bad_depends_on", "depends_on must be a list")


def _parse_strict_utc_timestamp(value: str) -> datetime | None:
    """value already matched TIMESTAMP_RE; reject a calendar date that
    doesn't actually exist (e.g. 2026-02-30T00:00:00Z)."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _validate_path(
    path_value: str,
    *,
    pointer: str,
    run: ValidationRun,
    project_root: Path,
    check_paths: bool,
) -> None:
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
        run.error(pointer, "bad_path", "path must use POSIX separators only (no '\\\\')")
        return
    segments = path_value.split("/")
    if any(segment == ".." for segment in segments):
        run.error(pointer, "bad_path", "path must not contain a '..' segment")
        return
    if any(segment == "" for segment in segments):
        run.error(pointer, "bad_path", "path must not contain an empty segment (e.g. a double slash)")
        return

    if not check_paths:
        return

    target = project_root / path_value
    if not os.path.lexists(str(target)):
        run.error(pointer, "path_missing", f"path {path_value!r} does not exist under project root {project_root}")
        return

    # Containment: after resolving symlinks, the target must still be
    # inside the resolved project root (same discipline as
    # build_startup_bundle.py's graphify_path_allowed()).
    try:
        resolved_root = project_root.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
    except OSError as exc:
        run.error(pointer, "path_unresolvable", f"could not resolve path {path_value!r}: {exc}")
        return
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        run.error(
            pointer,
            "path_escapes_root",
            f"path {path_value!r} resolves outside the project root ({resolved_target} is not under {resolved_root})",
        )
        return

    # Round-2 M3 fix, 2026-08-22: os.path.lexists() above only proves a
    # filesystem entry sits at `target` -- for a symlink, that is true even
    # when the link is dangling (its target does not exist), and
    # Path.resolve(strict=False) does not raise for that case either, so a
    # dangling symlink inside the project root previously passed both
    # checks and was reported as "exists". A dual review caught this: the
    # whole point of this check is to confirm the catalogued capability's
    # artifact is real and reachable, and a dangling symlink is neither.
    # os.path.exists() follows symlinks and correctly returns False for
    # one that is broken, which lexists() by design does not.
    if not os.path.exists(str(resolved_target)):
        run.error(
            pointer,
            "path_dangling_symlink",
            f"path {path_value!r} is a symlink whose target does not exist ({resolved_target})",
        )


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
        return  # already reported by _validate_entry

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
        if ref != ref.strip():
            run.error(pointer, "bad_depends_on_entry", "depends_on entry must not have leading/trailing whitespace")
            continue
        # Round-3 M3 fix, 2026-08-22: checked here on the WHOLE ref string
        # before it's split into <project>/<kind>/<name> parts, so this one
        # check covers all of them -- a dual review flagged that the
        # project/kind/name checks elsewhere in this module were hardened
        # against "\t"/U+2028/U+2029/NUL (round 2) but this depends_on path
        # was not, and a control character embedded in a cross-project
        # ref's target name would produce a reference that can never match
        # a legitimate capability's own (hardened, control-character-free)
        # `name` -- a permanently-dangling reference with a misleading
        # diagnosis, not just a cosmetic gap.
        if any(ch in ref for ch in ("\n", "\r", "\t", "\u2028", "\u2029", "\x00")):
            run.error(pointer, "bad_depends_on_entry", "depends_on entry must not contain a newline, tab, line-separator, or NUL character")
            continue

        if ref in seen_refs:
            run.error(pointer, "duplicate_depends_on", f"duplicate depends_on entry {ref!r} within this capability")
            continue
        seen_refs.add(ref)

        parts = ref.split(":")
        stripped_parts = [p.strip() for p in parts]
        if len(parts) not in (2, 3) or any(p == "" for p in stripped_parts) or any(p != raw for p, raw in zip(stripped_parts, parts)):
            run.error(
                pointer,
                "bad_depends_on_grammar",
                f"depends_on entry {ref!r} must split on ':' into exactly 2 (\"<kind>:<name>\") "
                f"or 3 (\"<project>:<kind>:<name>\") non-empty parts with no surrounding whitespace",
            )
            continue

        ref_kind = parts[-2]
        ref_name = parts[-1]

        if ref_kind not in KIND_VALUES:
            run.error(
                pointer,
                "bad_kind",
                f"depends_on entry {ref!r} has kind {ref_kind!r}, must be one of {list(KIND_VALUES)}",
            )
            continue
        if len(ref_name) > NAME_MAX_LEN or "\n" in ref_name or "\r" in ref_name:
            run.error(pointer, "bad_name", f"depends_on entry {ref!r} target name is invalid (>{NAME_MAX_LEN} chars or contains a newline)")
            continue

        if len(parts) == 2:
            ref_key = f"{ref_kind}:{ref_name}"
            if self_key is not None and ref_key == self_key:
                run.error(pointer, "self_reference", f"depends_on entry {ref!r} refers to this same capability")
                continue
            if (ref_kind, ref_name) not in local_kind_names:
                run.error(
                    pointer,
                    "dangling_local_ref",
                    f"same-project depends_on entry {ref!r} does not match any (kind, name) in this file",
                )
        else:
            ref_project = parts[0]
            if self_project is not None and ref_project == self_project:
                # This IS a deliberate exception to "cross-project refs are
                # format-checked only, never resolved" (two independent
                # dual-review passes both flagged this line as worth
                # calling out explicitly, since it reads as contradicting
                # that stated boundary): a 3-part ref whose <project>
                # equals this file's own is not a resolution question at
                # all, it's a self-contained string-equality check against
                # a value already in hand -- no other file is opened,
                # statted, or looked at. It exists so the reverse index
                # M5 builds never carries two different spellings
                # ("<kind>:<name>" and "<project>:<kind>:<name>" naming the
                # same project) for one edge.
                run.error(
                    pointer,
                    "redundant_cross_project_ref",
                    f"depends_on entry {ref!r} names this file's own project ({self_project!r}); "
                    f"use the 2-part same-project form \"{ref_kind}:{ref_name}\" instead",
                )
            # Beyond the same-project-spelling check above, cross-project
            # refs are format-checked only and never resolved -- that is
            # M4's aggregator's job (see module docstring).


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate one or more wiki/reusable-capabilities.json files (structure only, read-only)."
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="path(s) to reusable-capabilities.json to validate (default: <inferred-project-root>/wiki/reusable-capabilities.json)",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        help="override the project root used for path-existence/containment checks and project-id derivation "
        "(default: the parent of the wiki/ directory containing each validated file)",
    )
    parser.add_argument(
        "--no-path-check",
        action="store_true",
        help="skip path existence/containment checks (for a file validated detached from its project tree)",
    )
    parser.add_argument(
        "--strict-project-id",
        action="store_true",
        help="treat a project-id mismatch as an error instead of a warning",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one machine-readable JSON object per file to stdout",
    )
    return parser


def _default_file_for_cwd() -> Path:
    """Default target when no files are given: infer the project root from
    the current working directory's own wiki/ location if possible, else
    fall back to ./wiki/reusable-capabilities.json."""
    cwd = Path.cwd()
    if cwd.name == WIKI_DIR_NAME:
        return cwd / DEFAULT_FILENAME
    return cwd / WIKI_DIR_NAME / DEFAULT_FILENAME


def _validate_one_file(
    file_path: Path,
    *,
    project_root_override: Path | None,
    check_paths: bool,
    strict_project_id: bool,
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    """Returns (exit_code, json_result) for one file. exit_code is 0/1/2."""
    try:
        doc = load_json_object(file_path)
    except UsageError as exc:
        result = {
            "path": str(file_path),
            "ok": False,
            "project": None,
            "capability_count": 0,
            "errors": [{"pointer": "", "code": "usage_error", "message": str(exc)}],
            "warnings": [],
        }
        return 2, result

    if not isinstance(doc, dict):
        # "top level not an object" is a usage/IO-class problem (exit 2),
        # same bucket as a missing file or unparseable JSON -- not an
        # ordinary validation error (exit 1). validate_document() also
        # guards this same condition for direct callers (e.g. tests) that
        # bypass this CLI-facing wrapper, but here it must map to the
        # documented exit-code contract, not the generic "errors present"
        # exit 1 path below.
        result = {
            "path": str(file_path),
            "ok": False,
            "project": None,
            "capability_count": 0,
            "errors": [{"pointer": "", "code": "not_an_object", "message": "document root must be a JSON object"}],
            "warnings": [],
        }
        return 2, result

    project_root = project_root_override if project_root_override is not None else infer_project_root(file_path)
    # Round-2 M3 fix, 2026-08-22: always derive from project_root (whichever
    # source it came from), rather than skipping derivation entirely when
    # --project-root is given. Previously --project-root silently disabled
    # the project-id cross-check no matter what, contradicting --help's own
    # description of that flag and making --strict-project-id a silent
    # no-op whenever it was combined with --project-root.
    expected_project_id = derive_expected_project_id(project_root)

    run = validate_document(
        doc,
        project_root=project_root,
        check_paths=check_paths,
        strict_project_id=strict_project_id,
        expected_project_id=expected_project_id,
        now=now,
    )

    ok = not run.errors
    result = {
        "path": str(file_path),
        "ok": ok,
        "project": run.project,
        "capability_count": run.capability_count,
        "errors": [f.as_dict() for f in run.errors],
        "warnings": [f.as_dict() for f in run.warnings],
    }
    return (0 if ok else 1), result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    files: list[Path] = args.files if args.files else [_default_file_for_cwd()]
    now = datetime.now(timezone.utc)

    worst_exit_code = 0
    for file_path in files:
        exit_code, result = _validate_one_file(
            file_path,
            project_root_override=args.project_root,
            check_paths=not args.no_path_check,
            strict_project_id=args.strict_project_id,
            now=now,
        )
        worst_exit_code = max(worst_exit_code, exit_code)

        if args.json:
            print(_json_line(result))
        else:
            _print_human(result)

    return worst_exit_code


def _json_line(result: dict[str, Any]) -> str:
    """Serialize one --json result as a single line safe to consume with
    "one JSON object per line" parsing.

    Round-3 M3 fix, 2026-08-22: the previous round hardened every INPUT
    field (project/name/summary/depends_on) against U+2028/U+2029, but a
    dual review found the same two characters can still reach the OUTPUT
    through several paths that echo untrusted input back verbatim without
    going through those per-field checks -- an unknown top-level or entry
    key name (reported before any per-field validation even runs), and a
    resolved filesystem path baked into a path_escapes_root /
    path_dangling_symlink message. Chasing each of those individually
    would be the same whack-a-mole this file has already done more than
    once; fixing it once at the actual output boundary closes every
    current site and is immune to any future one that echoes untrusted
    text into a `message` field. json.dumps(..., ensure_ascii=False)
    leaves U+2028/U+2029 as raw, unescaped characters -- they are valid
    inside a JSON string per RFC 8259, but some JS-family consumers treat
    them as line terminators regardless, which is exactly what breaks
    "one object per line" parsing. chr(0x2028)/chr(0x2029) are used here
    instead of literal characters or "\\u2028" string escapes specifically
    so this file never again has a byte-level ambiguity between an actual
    U+2028 character sitting in the source and a 6-character escape
    sequence describing one (a distinction that has already needed a
    dedicated fix once in this same file).
    """
    text = json.dumps(result, ensure_ascii=False)
    return text.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")


def _print_human(result: dict[str, Any]) -> None:
    path = result["path"]
    if result["ok"]:
        print(f"OK  {path}  (project={result['project']!r}, {result['capability_count']} capabilities)")
    else:
        print(f"FAIL  {path}")
    for err in result["errors"]:
        print(f"  ERROR [{err['code']}] {err['pointer']}: {err['message']}")
    for warn in result["warnings"]:
        print(f"  WARN  [{warn['code']}] {warn['pointer']}: {warn['message']}")


if __name__ == "__main__":
    raise SystemExit(main())
