#!/usr/bin/env python3
"""Read-only cross-project catalog aggregator (M4 of the cross-project
catalog plan).

Walks every project Orca knows about (via `orca repo list --json` and
`orca worktree list --json` -- never a hardcoded path list) and, from each
one, opens exactly three files under its `wiki/` directory:

    wiki/orca-context-wiki.json
    wiki/orca-cli-capability-inventory.json
    wiki/reusable-capabilities.json

Nothing else in any project's tree is ever opened, with exactly two
deliberate, benign, read-only carve-outs -- both of them this process
reading its OWN source, never any project's content: (1) the sibling-module
`from validate_reusable_capabilities import ...` below, and (2)
`generator_path.read_bytes()` in cmd_build(), which self-hashes this file
for the catalog's `generator.sha256` provenance field. Neither reads across
a project boundary in the sense the allow-list is defending; they are the
interpreter loading and fingerprinting the tool itself.

The consolidated result is written to ONE file outside every project's own
git tree:

    /Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json

Two boundaries hold in both directions:
  - READ-ONLY across every project boundary (read_only_from()), modulo the
    two self-reads noted above.
  - WRITE-ONLY within the declared output directory. `--output` is PINNED
    UNCONDITIONALLY: only DEFAULT_OUTPUT_DIR or a descendant of it is
    accepted, so NO invocation -- documented or otherwise -- can aim writes
    at a project tree. There is no override flag and no escape hatch of any
    kind; the test suite redirects the pin by rebinding DEFAULT_OUTPUT_DIR
    itself, which is a property of the test process, not of the CLI surface.
    Containment below that directory is then STRUCTURAL, not per-call
    gating: the catalog file, its tmp sibling, and the lockfile are all
    derived from the one validated `output_dir` (write_only_within() is
    called once, on
    the catalog path, and its return value IS the path written). Do not
    describe this as "every write-capable call passes through the guard" --
    it does not, and did not; the property that holds is single-root
    derivation plus O_NOFOLLOW|O_EXCL on every create.

This script has ZERO dependency on build_startup_bundle.py or any other
trust-anchor code -- catalog.json is deliberately NOT one of
reviewed-startup-pack-manifest.json's pinned shared_source_sha256s entries.
It imports exactly two names from validate_reusable_capabilities.py, both
for the same reason -- they are SHARED contracts that two independently
maintained copies would let drift silently, so they are imported rather
than reproduced:

    derive_expected_project_id()  the join-key derivation M5's depends_on
                                  resolution will match against
    ID_RE                         the capability-`id` grammar, which is
                                  what keeps the page and capability
                                  global_id namespaces disjoint (see
                                  summarize_reusable_capabilities)

That module's top level is constants and regexes only (see its own
docstring; pinned here by test T-14), so importing it has no side effects.

MANUAL / ON-DEMAND ONLY. This script is not wired into any hook and must
not be. It has no `--background` flag and no `spawn_background()` helper on
purpose: `auto_index.py` has those because a SessionStart hook must return
in milliseconds, whereas nothing calls this one but a human or an agent
that is about to read the result. Registering it in `settings.json`,
`startup_context.py`, or any other hook path is M7 of the cross-project
catalog plan and requires its own separate dual review -- adding it here,
as a side effect of some other change, bypasses that gate.

Run with:
    python3 build_cross_project_catalog.py build [--force] [--json] [--quiet]
                                                  [--output DIR] [--orca-bin PATH]
                                                  [--timeout-budget N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Set BEFORE the sibling import below. That import is the one thing this
# process loads from disk that is not an allow-listed wiki file, and its
# scripts/ directory lives INSIDE orca/完善orca -- one of the very projects
# this aggregator reads. Without this, CPython drops a __pycache__/*.pyc
# into a content-bearing project tree as a side effect of a "read-only"
# run. Doing it here (rather than relying on PYTHONDONTWRITEBYTECODE being
# exported) makes the guarantee independent of the caller's environment.
#
# SCOPE, stated precisely because the obvious stronger reading is false:
# this covers (a) every module imported from here on -- the sibling import
# below is the one that matters -- and (b) this module's OWN bytecode when
# it is RUN as a script (`python3 build_cross_project_catalog.py ...`),
# where CPython never caches __main__ in the first place. It does NOT and
# structurally CANNOT cover this module's own .pyc when this module is
# itself IMPORTED as a library (as the test suite does): the import system
# writes a module's bytecode cache around executing its body, so an
# assignment inside that body is by construction too late for itself.
# Importing this module therefore does drop
# __pycache__/build_cross_project_catalog.cpython-*.pyc next to it. That is
# accepted, not overlooked: __pycache__/ is gitignored at .gitignore:1, the
# directory is not under any project's wiki/, and neither git nor the
# catalog can see it.
sys.dont_write_bytecode = True

# ID_RE is imported for the same reason derive_expected_project_id is (see
# the module docstring): it is a shared GRAMMAR that this aggregator and the
# M3 validator must agree on exactly, and two independently maintained
# copies would drift silently. Both are module-level, side-effect-free
# public constants of a module whose top level is constants and regexes only
# (pinned by test T-14).
from validate_reusable_capabilities import ID_MAX_LEN, ID_RE, derive_expected_project_id  # noqa: E402


GENERATOR_VERSION = "1.0.0"
GENERATOR_SCRIPT_REL_PATH = "orca-context-bridge/scripts/build_cross_project_catalog.py"
CATALOG_SCHEMA_VERSION = 1

DEFAULT_OUTPUT_DIR = Path("/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog")
CATALOG_NAME = "catalog.json"
LOCK_NAME = ".catalog.lock"

WIKI_DIR_NAME = "wiki"
ALLOWED_FILENAMES = (
    "orca-context-wiki.json",
    "orca-cli-capability-inventory.json",
    "reusable-capabilities.json",
)
KIND_VALUES = ("skill", "script", "config-pattern")

FALLBACK_ORCA_BIN = "/Applications/Orca.app/Contents/Resources/bin/orca"
DEFAULT_TIMEOUT_BUDGET = 30
PER_CALL_TIMEOUT = 15
MAX_ORCA_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_PREVIOUS_CATALOG_BYTES = 16 * 1024 * 1024
LOCK_STALE_SECONDS = 300

# Characters a depends_on ref may never contain (see _parse_depends_on_ref).
# chr(0x2028)/chr(0x2029) rather than the literal characters so the constant
# stays visible in a diff and can never be mistaken for stray whitespace.
_CONTROL_CHARS = ("\n", "\r", "\t", chr(0x2028), chr(0x2029), "\x00")


class CatalogFatal(Exception):
    """Cannot build at all -- maps to exit code 4. Never raised after a
    write has happened; every fatal path in cmd_build() runs before
    atomic_write_within() so a failed run leaves any previous catalog.json
    byte-identical."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Same 5-tuple auto_index.py uses as a read-then-recheck TOCTOU guard
    (see run_step()/reviewed_manifest_declares_routes() there) -- not a
    rebuild cache, just provenance plus a change-during-read detector."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000)),
        getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000)),
    )


def _sanitize_line_separators(text: str) -> str:
    """Escape U+2028/U+2029 in already-serialized JSON text.

    json.dumps(..., ensure_ascii=False) leaves them as raw unescaped
    characters -- valid inside a JSON string per RFC 8259, but some
    JS-family consumers treat them as line terminators, which breaks both a
    naive line-oriented reader and `JSON.parse` on older runtimes. Fixed
    ONCE per output boundary rather than per field that might echo
    untrusted text, exactly as validate_reusable_capabilities.py's
    _json_line() does. chr(0x2028)/chr(0x2029) are spelled out instead of
    literal characters so the source itself cannot contain the very
    character being escaped.

    Every json.dumps() in this module that reaches a file descriptor must
    go through here: the catalog file, the --json run summary on stdout,
    and the fatal-error payload on stderr.
    """
    return text.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


# ---------------------------------------------------------------------------
# Guard contracts
#
# Both return the house tuple[Path | None, str | None] shape from
# build_startup_bundle.py's graphify_path_allowed() -- value on success,
# reason code on rejection. Local reimplementations, not imports (this
# script has zero dependency on the trust anchor).
# ---------------------------------------------------------------------------


def _has_symlink_component(path: Path, root: Path) -> bool:
    """Walk each component from root downward; True if any is a symlink,
    or if path is not lexically under root. The trusted root itself is
    never checked -- only what sits below it."""
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


def write_only_within(catalog_dir: Path, path_value: object) -> tuple[Path | None, str | None]:
    """Refuse any path that is not a real, non-symlinked location inside
    catalog_dir. Step 5 (post-resolve containment) is load-bearing: step 3
    uses Path.absolute(), which does not normalize '..', so
    'out/../escape.json' passes step 3 and is only caught here."""
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        return None, "invalid_path"
    path = Path(path_value)
    if not path.is_absolute():
        return None, "non_absolute_path"
    lexical_root = catalog_dir.absolute()
    lexical_path = path.absolute()
    try:
        lexical_path.relative_to(lexical_root)
    except ValueError:
        return None, "outside_catalog_dir"
    if _has_symlink_component(lexical_path, lexical_root):
        return None, "symlink_path"
    resolved_root = catalog_dir.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None, "outside_catalog_dir"
    if resolved_path == resolved_root:
        return None, "is_catalog_root"
    return resolved_path, None


def read_only_from(
    allowed_filenames: tuple[str, ...], project_root: Path, path_value: object
) -> tuple[Path | None, str | None]:
    """Refuse any path that is not exactly one of allowed_filenames sitting
    directly inside project_root/wiki/. Asymmetric with write_only_within
    by design: a symlinked PROJECT ROOT is canonicalized and accepted (2 of
    145 real registered roots on this machine are symlinks whose targets
    are not separately registered), while every symlink component BELOW
    the wiki/ file itself is still rejected via the resolved-path
    containment checks below. A wiki/ symlink that stays inside the
    (resolved) project root is accepted -- containment is the security
    property, not symlink-ness."""
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        return None, "invalid_path"
    path = Path(path_value)
    if not path.is_absolute():
        return None, "non_absolute_path"
    if path.name not in allowed_filenames:
        return None, "filename_not_allowlisted"
    if path.parent.name != WIKI_DIR_NAME:
        return None, "not_in_wiki_dir"

    real_root = project_root.resolve(strict=False)
    real_wiki = (project_root / WIKI_DIR_NAME).resolve(strict=False)
    try:
        real_wiki.relative_to(real_root)
    except ValueError:
        return None, "wiki_escapes_project_root"
    if real_wiki == real_root:
        return None, "wiki_is_project_root"

    try:
        exists_dangling_ok = os.path.lexists(str(path))
        resolved = path.resolve(strict=False)
        exists_final = os.path.exists(str(resolved))
    except OSError:
        return None, "missing_or_dangling"
    if not exists_dangling_ok or not exists_final:
        return None, "missing_or_dangling"

    try:
        resolved.relative_to(real_wiki)
    except ValueError:
        return None, "file_escapes_wiki"
    if resolved.parent != real_wiki:
        return None, "file_not_direct_child"
    if resolved.name not in allowed_filenames:
        return None, "resolved_name_not_allowlisted"
    if not os.path.isfile(str(resolved)):
        return None, "not_a_regular_file"
    return resolved, None


# ---------------------------------------------------------------------------
# Per-source reading
# ---------------------------------------------------------------------------


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Local, deliberately divergent copy of validate_reusable_capabilities's
    check (importing its private name across modules would be worse than a
    6-line duplicate whose policy differs: this one degrades the source to
    parse-error and continues the run, it never raises UsageError)."""
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON object")
        seen[key] = value
    return seen


def read_one_source(project_root: Path, name: str) -> tuple[dict[str, Any], Any]:
    """Read one of the three allow-listed files. Returns (entry, doc):
    entry is what goes into catalog.json's per-project sources map; doc is
    the parsed document (None unless status is "ok"), consumed transiently
    by the summarize_* functions and never itself written to the catalog."""
    candidate = project_root / WIKI_DIR_NAME / name
    checked, reason = read_only_from(ALLOWED_FILENAMES, project_root, candidate)
    if reason == "missing_or_dangling":
        return {"status": "absent"}, None
    if reason:
        return {"status": "read-rejected", "reason": reason, "guard_reason": reason}, None

    fd = -1
    try:
        # O_NONBLOCK closes the residual window between read_only_from()'s
        # os.path.isfile() pre-check and this open: a regular file swapped
        # for a FIFO in between would otherwise block in the kernel inside
        # os.open() itself, before the S_ISREG check below can run. Same
        # reasoning (and the same one-flag fix) as _read_previous_catalog();
        # no-op for the regular files this normally opens, and behaviour for
        # every already-tested input is unchanged -- the isfile() pre-check
        # still rejects a FIFO that is already in place, with the same
        # "not_a_regular_file" reason as before.
        fd = os.open(str(checked), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return {"status": "read-error", "reason": str(exc)}, None
    try:
        try:
            before = os.fstat(fd)
        except OSError as exc:
            return {"status": "read-error", "reason": str(exc)}, None
        if not stat.S_ISREG(before.st_mode):
            return (
                {"status": "read-rejected", "reason": "not_a_regular_file", "guard_reason": "not_a_regular_file"},
                None,
            )
        if before.st_size > MAX_SOURCE_BYTES:
            return (
                {"status": "too-large", "reason": f"exceeds {MAX_SOURCE_BYTES} bytes",
                 "path": str(checked), "bytes": before.st_size},
                None,
            )
        try:
            raw = os.read(fd, MAX_SOURCE_BYTES + 1)
            after = os.fstat(fd)
        except OSError as exc:
            return {"status": "read-error", "reason": str(exc)}, None
    finally:
        os.close(fd)

    if len(raw) != before.st_size or _stat_identity(before) != _stat_identity(after):
        return {"status": "read-error", "reason": "changed_during_read"}, None

    mtime_ns = getattr(before, "st_mtime_ns", int(before.st_mtime * 1_000_000_000))
    base_fields = {
        "path": str(checked),
        "bytes": before.st_size,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "mtime_ns": mtime_ns,
        "mtime": _iso_from_ns(mtime_ns),
    }

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return {"status": "parse-error", "reason": f"not valid UTF-8: {exc}", **base_fields}, None
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        return {"status": "parse-error", "reason": str(exc), **base_fields}, None
    if not isinstance(doc, dict):
        return {"status": "schema-invalid", "reason": "document root is not a JSON object", **base_fields}, None

    entry: dict[str, Any] = {"status": "ok", **base_fields}
    return entry, doc


# ---------------------------------------------------------------------------
# Per-type summary extraction (only called when the source's status is
# "ok"). Every field is type-checked at use -- a malformed document degrades
# this ONE source to "schema-invalid" (by mutating entry in place) and never
# raises. The aggregator must not assume validator-clean input: M3's
# validator is an opt-in tool, not a gate.
# ---------------------------------------------------------------------------


def summarize_reusable_capabilities(
    doc: Any, project_id: str, real_path: str, entry: dict[str, Any]
) -> list[dict[str, Any]]:
    capabilities_raw = doc.get("capabilities") if isinstance(doc, dict) else None
    if not isinstance(capabilities_raw, list):
        entry["status"] = "schema-invalid"
        entry["reason"] = "capabilities is not a list"
        return []

    schema_version = doc.get("schema_version")
    declared_project = doc.get("project") if isinstance(doc.get("project"), str) else None
    entry["schema_version"] = schema_version if _is_int_not_bool(schema_version) else None
    entry["declared_project"] = declared_project
    entry["declared_project_matches_derived"] = (
        declared_project == project_id if declared_project is not None else None
    )

    caps: list[dict[str, Any]] = []
    dropped = 0
    for raw in capabilities_raw:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        cid, kind, name = raw.get("id"), raw.get("kind"), raw.get("name")
        # `id` is checked against the SHARED grammar (ID_RE, imported from
        # the M3 validator), not merely "non-empty string". Two independent
        # reasons, both load-bearing:
        #   1. `id` is half of a capability's global_id
        #      ("<project_id>#<id>"), and a page's global_id is
        #      "<project_id>#page:<id>". An id of literally "page:home"
        #      would mint a capability global_id byte-identical to a
        #      page's; ID_RE forbids ':', which closes that one axis. It
        #      does NOT make the two namespaces unconditionally disjoint --
        #      the page-mint site below (summarize_context_wiki) has the
        #      full account of the remaining project_id axis, the concrete
        #      counter-example, and how assemble_catalog() measures and
        #      publishes any real overlap rather than assuming there is
        #      none. Read that comment, not this one, for the disjointness
        #      claim.
        #   2. Same rationale as `kind` below: an id outside the grammar is
        #      an id no validator-clean file could produce, so admitting it
        #      would publish a join key M5 can never legitimately be handed.
        # This module must not assume validator-clean input (M3's validator
        # is opt-in, not a gate), so the grammar is enforced HERE rather
        # than assumed to have been enforced upstream. The length bound
        # (ID_MAX_LEN, imported alongside ID_RE) is enforced too: ID_RE by
        # itself has no upper bound on length, so a capability id of, say,
        # 200 characters would match the regex and mint a valid-looking
        # global_id that the M3 validator's own hard `bad_id` length check
        # would reject as unpublishable -- an id this aggregator accepted
        # but the validator calls dirty, the exact drift the shared-grammar
        # import exists to prevent.
        #
        # Known shared quirk, deliberately NOT patched on one side only:
        # `.match` (not `.fullmatch`) with a `$`-anchored pattern admits one
        # trailing newline, so id "home\n" is accepted and mints global_id
        # "<project>#home\n". validate_reusable_capabilities.py -- the module
        # ID_RE is imported FROM -- uses `.match` identically at its own
        # three call sites, so this is a property of the shared grammar, not
        # drift introduced here. Tightening it in this file alone would
        # create exactly the divergence the import exists to prevent (a
        # capability this aggregator drops but the validator calls clean),
        # and the M3 validator is out of scope for this round. It is also
        # not a disjointness hole: ID_RE.match("page:home\n") is False, so
        # ':' stays barred, and json.dumps() escapes the newline, so the
        # --json one-object-per-line contract survives. If it is ever fixed,
        # both files must change in the same commit.
        if not isinstance(cid, str) or not (1 <= len(cid) <= ID_MAX_LEN) or not ID_RE.match(cid):
            dropped += 1
            continue
        # kind is checked against the enum, not merely "non-empty string".
        # _parse_depends_on_ref() already refuses any kind outside
        # KIND_VALUES, so a capability with kind "bogus" would be indexed
        # under a ref_key that no legal depends_on string could ever
        # address -- an unreachable entry masquerading as a live one.
        if not isinstance(kind, str) or kind not in KIND_VALUES:
            dropped += 1
            continue
        # `name` gets the same treatment for the same reason: it is the
        # other half of a ref_key, and a name containing ':' (or leading /
        # trailing whitespace, or a control character) is unaddressable --
        # see _name_is_addressable().
        if not isinstance(name, str) or not name or not _name_is_addressable(kind, name):
            dropped += 1
            continue
        path_value = raw.get("path") if isinstance(raw.get("path"), str) else None
        summary_value = raw.get("summary") if isinstance(raw.get("summary"), str) else None
        last_verified_at = raw.get("last_verified_at") if isinstance(raw.get("last_verified_at"), str) else None
        # depends_on is DEDUPLICATED here, before any inbound edge is
        # credited downstream. The same ref repeated N times used to credit
        # the target with N inbound edges and N referenced_by rows from ONE
        # referencing capability, while referencing_project_ids -- computed
        # from the same loop -- was correctly deduped: the catalog
        # contradicted itself, and in_degree (the headline metric of the
        # whole depends_on subsystem) was simply wrong.
        # validate_reusable_capabilities.py already has a dedicated
        # "duplicate_depends_on" error for exactly this input, so it is a
        # known real shape, not a hypothetical.
        # The duplicate is NOT treated as a dropped malformed entry and does
        # NOT degrade the source to "partial": deduplication here is
        # lossless (the resulting graph is identical to the one the author
        # meant), unlike a dropped entry, which loses information. It is
        # recorded on the capability instead so it is never invisible.
        #
        # A non-string (or empty-string) depends_on entry is a different
        # case: unlike the lossless dedup above, silently `continue`-ing
        # past it DOES lose information -- a capability that declared
        # `depends_on: [1, null, "script:a.py"]` would publish only
        # `["script:a.py"]`, indistinguishable from one that only ever
        # wrote that one ref. It gets the same "dropped, counted, made
        # visible" treatment as a malformed capability entry, via
        # malformed_depends_on_count below, rather than vanishing.
        depends_on_raw = raw.get("depends_on")
        depends_on_list: list[str] = []
        duplicate_depends_on = 0
        malformed_depends_on = 0
        if isinstance(depends_on_raw, list):
            seen_refs: set[str] = set()
            for dep_raw in depends_on_raw:
                if not isinstance(dep_raw, str) or not dep_raw:
                    malformed_depends_on += 1
                    continue
                if dep_raw in seen_refs:
                    duplicate_depends_on += 1
                    continue
                seen_refs.add(dep_raw)
                depends_on_list.append(dep_raw)
        caps.append(
            {
                "project_id": project_id,
                "project_real_path": real_path,
                "id": cid,
                "kind": kind,
                "name": name,
                "path": path_value,
                "summary": summary_value,
                "last_verified_at": last_verified_at,
                "depends_on_raw": depends_on_list,
                "duplicate_depends_on_count": duplicate_depends_on,
                "malformed_depends_on_count": malformed_depends_on,
            }
        )
    entry["capability_count"] = len(caps)
    entry["dropped_count"] = dropped
    # Deliberately a SEPARATE field from dropped_count: dropped_count means
    # "this many raw capability objects were removed from caps[]", and
    # capability_count + dropped_count == len(capabilities_raw) is an
    # invariant consumers (and the test suite) rely on. A malformed
    # depends_on entry drops a ref WITHIN a capability that is otherwise
    # kept, so folding it into dropped_count would break that invariant.
    malformed_depends_on_total = sum(c["malformed_depends_on_count"] for c in caps)
    entry["malformed_depends_on_count"] = malformed_depends_on_total
    reasons = []
    if dropped:
        # A silent `continue` past a malformed sub-entry made a 5-entry file
        # with 3 bad entries indistinguishable from a clean 2-entry file.
        # "partial" says the source WAS usable but is not the whole story.
        reasons.append(f"dropped {dropped} malformed capability entr{'y' if dropped == 1 else 'ies'}")
    if malformed_depends_on_total:
        reasons.append(
            f"dropped {malformed_depends_on_total} malformed depends_on "
            f"entr{'y' if malformed_depends_on_total == 1 else 'ies'}"
        )
    if reasons:
        entry["status"] = "partial"
        entry["reason"] = "; ".join(reasons)
    return caps


def summarize_context_wiki(
    doc: Any, project_id: str, real_path: str, entry: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[Any]]:
    pages_raw = doc.get("pages") if isinstance(doc, dict) else None
    if not isinstance(pages_raw, list):
        entry["status"] = "schema-invalid"
        entry["reason"] = "pages is not a list"
        return [], []

    version = doc.get("version")
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    content_version = meta.get("content_version")
    project_field = doc.get("project")
    declared_path = project_field.get("path") if isinstance(project_field, dict) else None
    declared_path_matches: bool | None = None
    if isinstance(declared_path, str) and declared_path:
        try:
            declared_path_matches = os.path.realpath(declared_path) == real_path
        except OSError:
            declared_path_matches = False

    pages: list[dict[str, Any]] = []
    dropped = 0
    for raw in pages_raw:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        pid = raw.get("id")
        if not isinstance(pid, str) or not pid:
            dropped += 1
            continue
        pages.append(
            {
                "project_id": project_id,
                "project_real_path": real_path,
                # "#page:" keeps the page namespace disjoint from the
                # capability namespace along the `id` axis, and that half is
                # ENFORCED rather than assumed: summarize_reusable_
                # capabilities() above rejects (and counts as dropped) any
                # capability whose `id` fails ID_RE, and
                # ID_RE = ^[a-z0-9]+(-[a-z0-9]+)*$ admits no ':'. Before
                # that check existed, a capability with id "page:home"
                # produced a global_id byte-identical to page "home"'s,
                # silently.
                #
                # The `project_id` axis is NOT enforced, and this comment
                # used to overclaim that no collision "can ever be
                # constructed". It can. Both global_ids are
                # "<project_id>#<suffix>", and project_id comes from the
                # filesystem (see the note at the capability mint site
                # below, which this paragraph exists to stay honest with):
                # a project directory literally named with BOTH '#' and ':'
                # can push the ':' into the capability suffix from the left.
                # Concretely, project "foo#page:x" with capability id "bar"
                # mints "foo#page:x#bar", and project "foo" with page id
                # "x#bar" mints the same string. So disjointness holds only
                # under the precondition that Orca's own project
                # registration never produces such a directory name.
                #
                # Rather than restate that precondition as a promise, it is
                # MEASURED: assemble_catalog() intersects the two global_id
                # sets for real and publishes any overlap as
                # cross_namespace_global_id_collisions[] /
                # counts.cross_namespace_global_id_collisions. The related
                # project_id_addressable signal on each capability is the
                # partial early-warning for the same class (a project_id
                # containing ':' always makes its ref_key unparseable, so
                # that flag is False for every capability that could take
                # part in such a collision).
                #
                # Disjointness across the two namespaces is NOT the same
                # property as uniqueness WITHIN this one: page ids come from
                # orca-context-wiki.json, which no tool in this repo
                # validates, so two pages in one project can share an id.
                # assemble_catalog() therefore runs the same two-pass
                # duplicate detection over page global_ids that it runs over
                # capability global_ids (see duplicate_page_global_id /
                # ambiguous_page_global_ids there).
                "global_id": f"{project_id}#page:{pid}",
                "id": pid,
                "title": raw.get("title") if isinstance(raw.get("title"), str) else None,
                "path": raw.get("path") if isinstance(raw.get("path"), str) else None,
                "summary": raw.get("summary") if isinstance(raw.get("summary"), str) else None,
                "status": raw.get("status") if isinstance(raw.get("status"), str) else None,
            }
        )

    # A present-but-wrong-type `links` (a dict, a string, ...) used to be
    # silently coerced to [] with no trace -- the exact "wrong type reported
    # as healthy-and-empty" shape `pages`-is-not-a-list is treated as
    # schema-invalid for, two paragraphs up, just inconsistently applied.
    # `links` is optional (a project with no cross-page links legitimately
    # omits the key), so absence stays silent; presence-with-the-wrong-type
    # is a real schema violation and is now recorded via links_malformed,
    # a field separate from dropped_count for the same reason
    # malformed_depends_on_count is kept separate from it above: dropped
    # pages were removed from a list, but a malformed `links` is a whole
    # top-level field being wrong, not an entry within one.
    links_raw = doc.get("links")
    links_malformed = links_raw is not None and not isinstance(links_raw, list)
    links = links_raw if isinstance(links_raw, list) else []

    entry["version"] = version if _is_int_not_bool(version) else None
    entry["content_version"] = content_version if _is_int_not_bool(content_version) else None
    # page_count (and counts.wiki_pages) count ROWS, not distinct page ids:
    # a project whose wiki declares the same page id twice contributes two
    # rows and is counted as two. That is deliberate -- the rows really are
    # both in wiki_pages[] -- and the duplication is separately visible via
    # each row's duplicate_page_global_id flag and the top-level
    # ambiguous_page_global_ids[] list.
    entry["page_count"] = len(pages)
    entry["link_count"] = len(links)
    entry["declared_path"] = declared_path
    entry["declared_path_matches"] = declared_path_matches
    entry["dropped_count"] = dropped
    entry["links_malformed"] = links_malformed
    reasons = []
    if dropped:
        reasons.append(f"dropped {dropped} malformed page entr{'y' if dropped == 1 else 'ies'}")
    if links_malformed:
        reasons.append("links is not a list")
    if reasons:
        entry["status"] = "partial"
        entry["reason"] = "; ".join(reasons)
    return pages, links


# Every key summarize_cli_inventory() actually reads. A document carrying
# NONE of them is not an inventory that happens to be empty -- it is some
# other file (or garbage) sitting at the allow-listed name.
_INVENTORY_SCHEMA_KEYS = ("schemaVersion", "commandCount", "verificationCounts", "liveProbe", "commands")


def summarize_cli_inventory(
    doc: Any, project_id: str, real_path: str, entry: dict[str, Any]
) -> dict[str, Any] | None:
    # Minimal schema gate, symmetric with the other two summarizers (which
    # degrade to "schema-invalid" when capabilities/pages is not a list).
    # Without it this function had NO degrade path at all: an unrelated JSON
    # object at orca-cli-capability-inventory.json was reported
    # status:"ok", command_count:0, counted in inventory_projects, exit 0 --
    # the operator got no signal whatsoever that the file was garbage.
    # Deliberately permissive: ANY one recognized key is enough, so a
    # genuinely empty-but-real inventory ({"schemaVersion": 1,
    # "commandCount": 0}) still passes, and only a document with no
    # inventory shape at all is rejected. `version` is not in the key set --
    # it is too generic to be evidence of anything.
    if not isinstance(doc, dict) or not any(key in doc for key in _INVENTORY_SCHEMA_KEYS):
        entry["status"] = "schema-invalid"
        entry["reason"] = f"none of {list(_INVENTORY_SCHEMA_KEYS)} present; not a CLI capability inventory"
        return None

    commands = doc.get("commands") if isinstance(doc, dict) else None
    if isinstance(commands, list):
        command_count = len(commands)
    else:
        declared_count = doc.get("commandCount")
        command_count = declared_count if _is_int_not_bool(declared_count) else 0

    schema_version = doc.get("schemaVersion")
    version = doc.get("version")
    verification_counts = doc.get("verificationCounts") if isinstance(doc.get("verificationCounts"), dict) else {}
    # The real schema nests this: {"liveProbe": {"ok": false, "passed": 17,
    # "failed": ["accounts"], ...}}. Reading a flat "live_probe_ok" key --
    # which no inventory on this machine has ever had -- silently reported
    # null for every project, so the field looked "not applicable" rather
    # than "the probe failed". Every other field in this function already
    # uses the real camelCase names.
    live_probe = doc.get("liveProbe") if isinstance(doc.get("liveProbe"), dict) else {}
    live_probe_ok = live_probe.get("ok") if isinstance(live_probe.get("ok"), bool) else None

    entry["schema_version"] = schema_version if _is_int_not_bool(schema_version) else None
    entry["version"] = version if _is_int_not_bool(version) else None
    entry["command_count"] = command_count
    entry["verification_counts"] = verification_counts
    entry["live_probe_ok"] = live_probe_ok

    return {
        "project_id": project_id,
        "project_real_path": real_path,
        "schema_version": entry["schema_version"],
        "version": entry["version"],
        "command_count": command_count,
        "verification_counts": verification_counts,
        "live_probe_ok": live_probe_ok,
    }


# ---------------------------------------------------------------------------
# Fleet enumeration -- orca repo list / worktree list are the ONLY source of
# project paths. Never a shell pipeline: bash-captured `orca ... --json` can
# silently truncate on this machine, so subprocess.run() with a list argv is
# used unconditionally.
# ---------------------------------------------------------------------------


def run_orca_json(orca_bin: str, argv: tuple[str, ...], timeout: float) -> dict[str, Any]:
    label = argv[0]
    if timeout <= 0:
        raise CatalogFatal(f"enumeration_failed:{label}")
    try:
        proc = subprocess.run([orca_bin, *argv], capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        raise CatalogFatal(f"enumeration_failed:{label}")
    if proc.returncode != 0:
        raise CatalogFatal(f"enumeration_failed:{label}")
    if len(proc.stdout) > MAX_ORCA_OUTPUT_BYTES:
        raise CatalogFatal(f"enumeration_failed:{label}")
    try:
        text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise CatalogFatal(f"enumeration_failed:{label}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise CatalogFatal(f"enumeration_failed:{label}")
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("result"), dict):
        raise CatalogFatal("enumeration_envelope_invalid")
    return payload


def enumerate_fleet(orca_bin: str, budget_remaining) -> tuple[list[Any], list[Any], dict[str, Any]]:
    repo_payload = run_orca_json(orca_bin, ("repo", "list", "--json"), min(PER_CALL_TIMEOUT, budget_remaining()))
    repos = repo_payload["result"].get("repos")
    if not isinstance(repos, list):
        raise CatalogFatal("enumeration_failed:repo")

    wt_payload = run_orca_json(orca_bin, ("worktree", "list", "--json"), min(PER_CALL_TIMEOUT, budget_remaining()))
    wt_result = wt_payload["result"]
    worktrees = wt_result.get("worktrees")
    if not isinstance(worktrees, list):
        raise CatalogFatal("enumeration_failed:worktree")

    # Only worktree list carries truncated/totalCount -- repo list's result
    # has ONLY the key "repos" (verified live), so this assertion must not
    # be applied to the repo call.
    truncated = wt_result.get("truncated")
    if truncated is not False:
        raise CatalogFatal("enumeration_truncated")
    total_count = wt_result.get("totalCount")
    if _is_int_not_bool(total_count) and total_count != len(worktrees):
        raise CatalogFatal("enumeration_count_mismatch")

    enum_meta = {
        "repo_count": len(repos),
        "worktree_count": len(worktrees),
        "worktree_total_count": total_count,
        "worktree_truncated": truncated,
    }
    return repos, worktrees, enum_meta


def build_scan_targets(repos: list[Any], worktrees: list[Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Union of repo rows and worktree rows, deduplicated by realpath (NOT
    Path.resolve() -- os.path.realpath on a non-existent path returns the
    normalized path rather than raising). Worktrees are first-class
    projects here, not merely children of repos: several wiki-bearing
    projects on this machine are worktrees with no separate repo row."""
    malformed_rows = 0
    literal_paths: set[str] = set()
    by_real: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def ingest(rows: Any, kind: str) -> None:
        nonlocal malformed_rows
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                malformed_rows += 1
                continue
            path_value = row.get("path")
            if not isinstance(path_value, str) or not path_value:
                malformed_rows += 1
                continue
            literal_paths.add(path_value)
            real = os.path.realpath(path_value)
            if real not in by_real:
                by_real[real] = {
                    "real_path": real,
                    "path": path_value,
                    "aliases": [],
                    "orca_kinds": set(),
                    "repo_ids": set(),
                    "worktree_ids": set(),
                    "branch": None,
                    "is_main_worktree": False,
                    "is_archived": False,
                    "workspace_status": None,
                }
                order.append(real)
            target = by_real[real]
            if path_value != target["path"] and path_value not in target["aliases"]:
                target["aliases"].append(path_value)
            target["orca_kinds"].add(kind)
            if kind == "repo":
                repo_id = row.get("id")
                if isinstance(repo_id, str):
                    target["repo_ids"].add(repo_id)
            else:
                wt_id = row.get("id")
                if isinstance(wt_id, str):
                    target["worktree_ids"].add(wt_id)
                branch = row.get("branch")
                if isinstance(branch, str):
                    target["branch"] = branch
                # Never filter on isArchived/workspaceStatus -- record only.
                # workspaceStatus is free-form (same bug class as this
                # repo's own agent_capacity.py WORKTREE_STATES gap).
                if row.get("isMainWorktree") is True:
                    target["is_main_worktree"] = True
                if row.get("isArchived") is True:
                    target["is_archived"] = True
                status_value = row.get("workspaceStatus")
                if isinstance(status_value, str):
                    target["workspace_status"] = status_value

    ingest(repos, "repo")
    ingest(worktrees, "worktree")

    targets = [by_real[real] for real in order]
    targets.sort(key=lambda t: t["real_path"])
    return targets, malformed_rows, len(literal_paths)


def assign_project_ids(targets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    """real_path is always the row key and dedup identity. project_id is
    always non-null (never the key) via a fallback-basename tier -- a null
    join key would make ~2 of 7 real content-bearing projects unaddressable
    by M5/M6. Ambiguous project_ids (only possible via the fallback tier;
    0 collisions among 134 derived ids today) are flagged on every member
    and excluded from capability_ref_index rather than silently
    last-write-wins joined."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        derived = derive_expected_project_id(Path(target["real_path"]))
        if derived is not None:
            target["project_id"] = derived
            target["project_id_source"] = "derived"
        else:
            base = os.path.basename(target["real_path"].rstrip("/"))
            target["project_id"] = base or target["real_path"]
            target["project_id_source"] = "fallback-basename"
        target["display_name"] = os.path.basename(target["real_path"].rstrip("/")) or target["real_path"]
        groups.setdefault(target["project_id"], []).append(target)

    collisions: list[dict[str, Any]] = []
    for pid, members in groups.items():
        ambiguous = len(members) > 1
        for member in members:
            member["project_id_ambiguous"] = ambiguous
        if ambiguous:
            collisions.append(
                {
                    "project_id": pid,
                    "real_paths": [m["real_path"] for m in members],
                    "project_id_sources": [m["project_id_source"] for m in members],
                }
            )
    ambiguous_ids = {c["project_id"] for c in collisions}
    return collisions, ambiguous_ids


# ---------------------------------------------------------------------------
# Per-project source scanning
# ---------------------------------------------------------------------------


def process_target(target: dict[str, Any]) -> dict[str, Any]:
    real_path = target["real_path"]
    project_id = target["project_id"]
    outcome: dict[str, Any] = {
        "status": "path-missing",
        "sources": None,
        "capabilities": [],
        "wiki_pages": [],
        "wiki_links_raw": [],
        "wiki_page_ids": set(),
        "cli_inventory": None,
    }
    if not os.path.lexists(real_path):
        return outcome
    if not os.path.isdir(os.path.join(real_path, WIKI_DIR_NAME)):
        outcome["status"] = "no-wiki-dir"
        return outcome

    sources: dict[str, Any] = {}
    any_bad = False
    for name in ALLOWED_FILENAMES:
        entry, doc = read_one_source(Path(real_path), name)
        sources[name] = entry
        if entry["status"] not in ("ok", "absent"):
            any_bad = True
            continue
        if entry["status"] != "ok" or doc is None:
            continue
        # A summarize_* call can downgrade `entry` two different ways:
        # "schema-invalid" means nothing usable came back and the returned
        # value must be discarded; "partial" means some sub-entries were
        # dropped but what survived is real and MUST be kept. Both count as
        # degraded for the project's own status.
        if name == "reusable-capabilities.json":
            caps = summarize_reusable_capabilities(doc, project_id, real_path, entry)
            if entry["status"] in ("ok", "partial"):
                outcome["capabilities"] = caps
            if entry["status"] != "ok":
                any_bad = True
        elif name == "orca-context-wiki.json":
            pages, links = summarize_context_wiki(doc, project_id, real_path, entry)
            if entry["status"] in ("ok", "partial"):
                outcome["wiki_pages"] = pages
                outcome["wiki_links_raw"] = links
                outcome["wiki_page_ids"] = {p["id"] for p in pages}
            if entry["status"] != "ok":
                any_bad = True
        elif name == "orca-cli-capability-inventory.json":
            inv = summarize_cli_inventory(doc, project_id, real_path, entry)
            if entry["status"] in ("ok", "partial"):
                outcome["cli_inventory"] = inv
            if entry["status"] != "ok":
                any_bad = True

    outcome["status"] = "partial" if any_bad else "ok"
    outcome["sources"] = sources
    return outcome


def build_project_row(target: dict[str, Any], sources: dict[str, Any] | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "real_path": target["real_path"],
        "path": target["path"],
        "aliases": target["aliases"],
        "project_id": target["project_id"],
        "project_id_source": target["project_id_source"],
        "project_id_ambiguous": target["project_id_ambiguous"],
        "display_name": target["display_name"],
        "orca_kinds": sorted(target["orca_kinds"]),
        "orca": {
            "repo_ids": sorted(target["repo_ids"]),
            "worktree_ids": sorted(target["worktree_ids"]),
            "branch": target["branch"],
            "is_main_worktree": target["is_main_worktree"],
            "is_archived": target["is_archived"],
            "workspace_status": target["workspace_status"],
        },
        "status": target["status"],
    }
    # Compaction: omit "sources" entirely for no-wiki-dir/path-missing rows
    # -- three identical stub objects carry nothing the status doesn't.
    if sources is not None:
        row["sources"] = sources
    return row


# ---------------------------------------------------------------------------
# depends_on grammar -- reproduces validate_reusable_capabilities.py's
# grammar exactly (kept as a local copy: it is policy logic tied to this
# aggregator's own resolve-vs-degrade behaviour, unlike derive_expected_
# project_id which is a shared join key).
# ---------------------------------------------------------------------------


def _parse_depends_on_ref(raw: str) -> tuple[str, str, str, str | None] | None:
    """Returns (kind, name, scope, cross_project_id_or_None), or None if
    raw is malformed."""
    if any(ch in raw for ch in _CONTROL_CHARS):
        return None
    if raw != raw.strip():
        return None
    parts = raw.split(":")
    if len(parts) not in (2, 3):
        return None
    if any(p == "" for p in parts) or any(p != p.strip() for p in parts):
        return None
    kind = parts[-2]
    name = parts[-1]
    if kind not in KIND_VALUES:
        return None
    if len(parts) == 2:
        return kind, name, "same-project", None
    return kind, name, "cross-project", parts[0]


def _name_is_addressable(kind: str, name: str) -> bool:
    """True iff some legal depends_on string can actually name this
    (kind, name) pair.

    Mirrors validate_reusable_capabilities.py's `bad_name` rules -- no ':'
    (reserved as the depends_on separator), no leading/trailing whitespace,
    no newline/tab/line-separator/NUL -- but DERIVES them by round-tripping
    through this module's own grammar instead of restating them, so the
    check can never drift from the thing it is protecting. (The validator's
    NAME_MAX_LEN limit is deliberately not mirrored: it is a style bound,
    and _parse_depends_on_ref imposes no length limit, so a long name is
    still perfectly addressable here.)

    Why this matters: `name` is half of a ref_key, and a capability whose
    name fails this test gets indexed into capability_ref_index under a key
    that _parse_depends_on_ref() can never produce -- an unreachable entry
    masquerading as a live resolution target. Exactly the defect the `kind`
    enum check closes, on the other half of the same key.

    The `parsed[1] == name` comparison is what catches an embedded ':':
    "script:has:colon" parses as a CROSS-project ref to name "colon", not
    as a name of "has:colon", so the round trip does not return what went
    in.
    """
    parsed = _parse_depends_on_ref(f"{kind}:{name}")
    return parsed is not None and parsed[0] == kind and parsed[1] == name


# ---------------------------------------------------------------------------
# Whole-fleet assembly: ties every per-project outcome together into the
# global capability/wiki-link/reference indices and the final catalog dict.
# ---------------------------------------------------------------------------


def assemble_catalog(
    targets: list[dict[str, Any]],
    enum_meta: dict[str, Any],
    orca_bin: str,
    malformed_rows: int,
    literal_path_count: int,
    generator_sha256: str,
) -> dict[str, Any]:
    collisions, ambiguous_ids = assign_project_ids(targets)

    project_rows: list[dict[str, Any]] = []
    all_caps: list[dict[str, Any]] = []
    all_pages: list[dict[str, Any]] = []
    wiki_link_jobs: list[tuple[str, str, list[Any], set[str]]] = []
    all_inventories: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    project_status_histogram: dict[str, int] = {}
    source_status_histogram: dict[str, dict[str, int]] = {name: {} for name in ALLOWED_FILENAMES}
    fallback_count = 0
    # R11: counted per project by actually looking at that project's source
    # statuses, NOT by counting "ok"/"partial" project rows. A project whose
    # wiki/ directory exists but holds none of the three allow-listed files
    # has status "ok" (nothing was wrong) with all three sources "absent" --
    # it contributed nothing, and counting it inflated the human summary's
    # "N of M enumerated project paths contributed sources" line. "Usable"
    # here means the same thing it means in _contributing() below: status
    # "ok" or "partial" on at least one of the three files.
    projects_with_sources = 0
    # A project whose orca-context-wiki.json has a `links` key present but
    # not a list (a real schema violation, not mere absence).
    projects_with_malformed_links = 0
    # Split deliberately (see the unresolved-reference classifier below):
    # "has the file at all" is NOT the same question as "the file parsed".
    projects_with_capabilities_file: set[str] = set()
    projects_with_usable_capabilities_file: set[str] = set()

    for target in targets:
        if target["project_id_source"] == "fallback-basename":
            fallback_count += 1
        outcome = process_target(target)
        status = outcome["status"]
        target["status"] = status
        project_status_histogram[status] = project_status_histogram.get(status, 0) + 1

        sources = outcome["sources"]
        if sources is not None:
            if any(entry["status"] in ("ok", "partial") for entry in sources.values()):
                projects_with_sources += 1
            if sources.get("orca-context-wiki.json", {}).get("links_malformed"):
                projects_with_malformed_links += 1
            for name, entry in sources.items():
                bucket = source_status_histogram[name]
                bucket[entry["status"]] = bucket.get(entry["status"], 0) + 1
                if entry["status"] not in ("ok", "absent"):
                    degraded.append(
                        {
                            "project_id": target["project_id"],
                            "real_path": target["real_path"],
                            "source": name,
                            "status": entry["status"],
                            "reason": entry.get("reason"),
                        }
                    )
            caps_status = sources.get("reusable-capabilities.json", {}).get("status")
            if caps_status != "absent":
                # Present-but-broken still counts as "this project adopted
                # the file". Folding it into the has-no-file bucket made
                # SKILL.md's "resolves itself as adoption spreads" claim
                # false for exactly the case that never will.
                projects_with_capabilities_file.add(target["project_id"])
            if caps_status in ("ok", "partial"):
                projects_with_usable_capabilities_file.add(target["project_id"])
        else:
            # B2: no-wiki-dir / path-missing rows omit `sources` entirely
            # (compaction rule), but all three files genuinely ARE absent
            # from the fleet's point of view. Counting only the handful of
            # projects that HAVE a sources dict gave the histogram a
            # denominator of ~7 instead of the real scan-target count.
            for name in ALLOWED_FILENAMES:
                bucket = source_status_histogram[name]
                bucket["absent"] = bucket.get("absent", 0) + 1

        project_rows.append(build_project_row(target, sources))
        all_caps.extend(outcome["capabilities"])
        all_pages.extend(outcome["wiki_pages"])
        if outcome["wiki_links_raw"] or outcome["wiki_page_ids"]:
            wiki_link_jobs.append(
                (target["project_id"], target["real_path"], outcome["wiki_links_raw"], outcome["wiki_page_ids"])
            )
        if outcome["cli_inventory"] is not None:
            all_inventories.append(outcome["cli_inventory"])

    known_project_ids = {t["project_id"] for t in targets}

    # Page global_id uniqueness -- the SAME two-pass count/flag treatment
    # applied to capability global_ids below, applied to the page namespace
    # that previously had no enforcement of any kind.
    #
    # Deliberate asymmetry with capabilities: a duplicated page is FLAGGED
    # AND RETAINED in wiki_pages[], never dropped and never degrading its
    # source to "partial". orca-context-wiki.json has no validator in this
    # repo (unlike reusable-capabilities.json, whose M3 validator is what
    # gives capability sub-entries a documented drop/partial precedent), so
    # dropping page rows here would be this tool inventing a schema rule for
    # a file it does not own. What a duplicate DOES lose is its standing as
    # a safe join target: the natural M5 consumer idiom
    # `{p["global_id"]: p for p in wiki_pages}` keeps exactly one of the
    # colliding rows, so global_id no longer identifies a unique page and a
    # consumer must consult duplicate_page_global_id before joining on it --
    # the same "resolving to a corrupted identity is worse than not
    # resolving" rule the capability side enforces by exclusion.
    page_global_id_counts: dict[str, int] = {}
    for page in all_pages:
        page_global_id_counts[page["global_id"]] = page_global_id_counts.get(page["global_id"], 0) + 1
    duplicate_page_global_ids = {g for g, n in page_global_id_counts.items() if n > 1}
    ambiguous_page_global_ids: list[str] = []
    for page in all_pages:
        page_dup = page["global_id"] in duplicate_page_global_ids
        page["duplicate_page_global_id"] = page_dup
        if page_dup and page["global_id"] not in ambiguous_page_global_ids:
            ambiguous_page_global_ids.append(page["global_id"])

    # global_id / ref_key assignment, in two passes.
    #
    # Pass 1 only assigns keys and COUNTS them. Both indices used to be
    # built with unguarded `index[key] = value` assignments, so a second
    # capability sharing a (kind, name) -- or an `id` -- inside one project
    # silently last-write-wins: one entry vanished from
    # capability_ref_index, and the other's referenced_by/in_degree was
    # actually a merge of two different capabilities' inbound edges.
    #
    # Pass 2 admits a capability as a resolution TARGET only if its keys are
    # unambiguous on all three axes. Every exclusion is RECORDED (never
    # silently dropped) and never raises -- a duplicate is a data-quality
    # fact about someone else's file, matching this script's
    # degrade-don't-abort policy throughout. Excluded capabilities still
    # appear in capabilities[] and can still be a resolution SOURCE.
    ref_key_counts: dict[str, int] = {}
    global_id_counts: dict[str, int] = {}
    for cap in all_caps:
        global_id = f"{cap['project_id']}#{cap['id']}"
        ref_key = f"{cap['project_id']}:{cap['kind']}:{cap['name']}"
        cap["global_id"] = global_id
        cap["ref_key"] = ref_key
        # project_id is derived from the filesystem (a path segment, or a
        # basename fallback), never from the capabilities file, so it cannot
        # be validated the way `id`/`kind`/`name` are -- rejecting a project
        # because of how its directory is named would be this tool refusing
        # to catalog real content over a cosmetic fact. It is not
        # structurally safe either, though: a directory literally named with
        # a ':' yields a ref_key of 4+ colon-separated parts, which
        # _parse_depends_on_ref() cannot produce.
        #
        # Crucially this does NOT justify excluding the ref_key. Both sides
        # of a SAME-project reference build the key the same way
        # (f"{project_id}:{kind}:{name}"), so same-project resolution works
        # perfectly regardless; only a CROSS-project ref to such a project is
        # unspellable. Excluding would therefore break working resolution to
        # fix nothing. Recorded instead, so the condition is machine-visible.
        cap["project_id_addressable"] = _parse_depends_on_ref(ref_key) is not None
        ref_key_counts[ref_key] = ref_key_counts.get(ref_key, 0) + 1
        global_id_counts[global_id] = global_id_counts.get(global_id, 0) + 1

    duplicate_ref_keys = {k for k, n in ref_key_counts.items() if n > 1}
    duplicate_global_ids = {g for g, n in global_id_counts.items() if n > 1}

    # Cross-namespace disjointness, MEASURED rather than argued.
    #
    # summarize_context_wiki()'s mint site explains why the `id` axis is
    # closed by ID_RE but the `project_id` axis is not: a project directory
    # named with both '#' and ':' can mint a capability global_id
    # byte-identical to some page's. This intersection is the ground truth
    # for that claim, so the module publishes the answer instead of asserting
    # the premise. It is pure reporting -- no row is dropped, no source is
    # degraded, the exit code is untouched -- for the same reason
    # project_id_addressable is recorded rather than enforced: refusing to
    # catalog real content over how someone named a directory would break
    # working resolution to fix nothing. A consumer that merges pages and
    # capabilities into ONE global_id keyspace must consult this list first;
    # a consumer that keeps them in two maps (the documented M5 idiom) is
    # unaffected either way.
    cross_namespace_global_id_collisions = sorted(
        {page["global_id"] for page in all_pages} & set(global_id_counts)
    )

    capability_ref_index: dict[str, str] = {}
    capability_reverse_index: dict[str, dict[str, Any]] = {}
    # Two separate concepts, deliberately NOT one list any more:
    #
    #   ambiguous_ref_keys[]  -- this literal ref_key string is claimed by
    #                            2+ capabilities. "Which one?" genuinely has
    #                            no answer. This is the ONLY thing the word
    #                            "ambiguous" is now used for here.
    #   excluded_ref_keys[]   -- this ref_key is barred from
    #                            capability_ref_index, with the reason WHY.
    #                            A superset: it also covers the cases where
    #                            the ref_key itself is perfectly unique and
    #                            something about its sole owner disqualifies
    #                            it (a colliding global_id, an ambiguous
    #                            project_id).
    #
    # They used to be the same list, which published provably false data: a
    # project with two capabilities sharing an `id` but having DISTINCT
    # (kind, name) pairs listed both of its ref_keys as "ambiguous" when
    # each had multiplicity exactly 1. counts.ambiguous_ref_keys was
    # therefore a mislabelled count of exclusions, and a reference to such a
    # target could not be told apart from a reference to a capability that
    # genuinely does not exist (see the unresolved classifier below).
    ambiguous_ref_keys: list[str] = []
    excluded_ref_keys: list[dict[str, str]] = []
    # ref_key -> single winning exclusion reason, consulted by the
    # unresolved-reference classifier. Highest-priority reason wins so the
    # reported reason is stable no matter what order duplicates appear in.
    excluded_ref_key_reasons: dict[str, str] = {}
    _EXCLUSION_PRIORITY = ("duplicate-ref-key", "duplicate-global-id", "ambiguous-project-id")
    ambiguous_global_ids: list[str] = []
    for cap in all_caps:
        global_id = cap["global_id"]
        ref_key = cap["ref_key"]
        # Why each axis disqualifies a target:
        #   project_id ambiguous -> the key names >1 real directory;
        #   ref_key duplicated   -> "which of the two?" has no answer;
        #   global_id duplicated -> the reverse-index row would be a merge.
        # A duplicated global_id also bars the ref_key: resolving to an
        # entry that has no trustworthy reverse row is worse than not
        # resolving. This invariant is what lets the resolution loop below
        # index capability_reverse_index[target_global] directly -- all
        # three axes must keep barring the ref_key, or that direct index
        # raises KeyError and takes the whole fleet run down.
        project_ambiguous = cap["project_id"] in ambiguous_ids
        ref_dup = ref_key in duplicate_ref_keys
        gid_dup = global_id in duplicate_global_ids
        cap["duplicate_ref_key"] = ref_dup
        cap["duplicate_global_id"] = gid_dup

        if project_ambiguous or ref_dup or gid_dup:
            if ref_dup:
                reason = "duplicate-ref-key"
            elif gid_dup:
                reason = "duplicate-global-id"
            else:
                reason = "ambiguous-project-id"
            previous = excluded_ref_key_reasons.get(ref_key)
            if previous is None:
                excluded_ref_key_reasons[ref_key] = reason
                excluded_ref_keys.append({"ref_key": ref_key, "reason": reason})
            elif _EXCLUSION_PRIORITY.index(reason) < _EXCLUSION_PRIORITY.index(previous):
                excluded_ref_key_reasons[ref_key] = reason
                for row in excluded_ref_keys:
                    if row["ref_key"] == ref_key:
                        row["reason"] = reason
                        break
            if ref_dup and ref_key not in ambiguous_ref_keys:
                ambiguous_ref_keys.append(ref_key)
        else:
            capability_ref_index[ref_key] = global_id

        if gid_dup:
            if global_id not in ambiguous_global_ids:
                ambiguous_global_ids.append(global_id)
        else:
            capability_reverse_index[global_id] = {
                "ref_key": ref_key,
                "in_degree": 0,
                "referencing_project_ids": [],
                "referenced_by": [],
            }

    unresolved_references: list[dict[str, Any]] = []
    self_references: list[dict[str, Any]] = []
    duplicate_edges = 0
    for cap in all_caps:
        depends_on_out: list[dict[str, Any]] = []
        # Exact-duplicate raw strings were already removed upstream (see
        # summarize_reusable_capabilities). This catches the remaining way
        # one capability can credit one target twice: two DIFFERENT raw
        # spellings of the same target, e.g. "script:t.py" and
        # "myproj:script:t.py" from inside myproj. Both are legal, both
        # resolve to the same global_id, and crediting both would reintroduce
        # exactly the in_degree inflation the raw-string dedup removes.
        # With both dedups in place the graph invariant is total:
        # in_degree == len(referenced_by), and both count DISTINCT
        # (referencing capability -> target) edges.
        credited_targets: set[str] = set()
        for raw in cap["depends_on_raw"]:
            parsed = _parse_depends_on_ref(raw)
            if parsed is None:
                depends_on_out.append({"raw": raw, "scope": None, "ref_key": None, "state": "malformed"})
                unresolved_references.append(
                    {
                        "ref_key": None,
                        "raw": raw,
                        "scope": None,
                        "reason": "malformed-ref",
                        "target_project_id": None,
                        "target_project_state": "n/a",
                        "from": {"project_id": cap["project_id"], "capability_global_id": cap["global_id"]},
                    }
                )
                continue

            kind, name, scope, cross_project_id = parsed
            target_project_id = cross_project_id if scope == "cross-project" else cap["project_id"]
            ref_key = f"{target_project_id}:{kind}:{name}"
            redundant = scope == "cross-project" and target_project_id == cap["project_id"]

            if ref_key == cap["ref_key"]:
                # A capability that depends_on itself. Left as "resolved" it
                # inflated its own in_degree with a self-loop and put itself
                # in its own referenced_by -- a fake inbound edge that any
                # M5/M6 graph consumer would take at face value.
                # validate_reusable_capabilities.py already flags this as
                # error code "self_reference"; a distinct state here keeps
                # the catalog consistent with the validator instead of
                # quietly disagreeing with it.
                #
                # Detected STRUCTURALLY -- ref_key against this capability's
                # OWN ref_key -- and BEFORE the forward index is consulted.
                # It used to be detected as "capability_ref_index[ref_key]
                # happens to equal my own global_id", which silently failed
                # whenever the self-referencing capability's ref_key was
                # barred from that index (a duplicate (kind, name) sibling,
                # a duplicate id, an ambiguous project_id). The catalog then
                # reported a DANGLING reference to a capability sitting in
                # its own capabilities[], and disagreed with the validator's
                # self_reference verdict -- the exact disagreement this
                # state exists to prevent. A plain string comparison cannot
                # be defeated that way. It also matches the validator, which
                # likewise tests self-reference before looking the target up
                # among the file's own (kind, name) pairs.
                dep_entry: dict[str, Any] = {
                    "raw": raw,
                    "scope": scope,
                    "ref_key": ref_key,
                    "state": "self-reference",
                    "target_global_id": cap["global_id"],
                }
                if redundant:
                    dep_entry["redundant_spelling"] = True
                depends_on_out.append(dep_entry)
                self_references.append(
                    {
                        "ref_key": ref_key,
                        "raw": raw,
                        "scope": scope,
                        "project_id": cap["project_id"],
                        "capability_global_id": cap["global_id"],
                    }
                )
                continue

            # Only now, having ruled out self-reference, is the forward
            # index the right question to ask.
            target_global = capability_ref_index.get(ref_key)

            if target_global is not None:
                dep_entry = {
                    "raw": raw,
                    "scope": scope,
                    "ref_key": ref_key,
                    "state": "resolved",
                    "target_global_id": target_global,
                }
                if redundant:
                    dep_entry["redundant_spelling"] = True
                already_credited = target_global in credited_targets
                if already_credited:
                    dep_entry["duplicate_edge"] = True
                    duplicate_edges += 1
                depends_on_out.append(dep_entry)
                if not already_credited:
                    credited_targets.add(target_global)
                    # Safe by the pass-2 invariant above: every value in
                    # capability_ref_index has a row in
                    # capability_reverse_index.
                    rev = capability_reverse_index[target_global]
                    rev["in_degree"] += 1
                    if cap["project_id"] not in rev["referencing_project_ids"]:
                        rev["referencing_project_ids"].append(cap["project_id"])
                    rev["referenced_by"].append(
                        {
                            "project_id": cap["project_id"],
                            "capability_global_id": cap["global_id"],
                            "scope": scope,
                            "raw": raw,
                        }
                    )
            else:
                # R14: redundant_spelling is attached on ALL THREE parsed
                # arms (resolved / self-reference / unresolved), not just the
                # two that happen to resolve. It is a fact about how the
                # AUTHOR spelled the ref -- "my own project id, written out
                # in full, where the bare form would do" -- and is exactly as
                # true, and exactly as worth surfacing to whoever is cleaning
                # up a wiki file, when the target does not exist. Attaching
                # it to only two arms made the field's absence ambiguous: a
                # consumer could not tell "not redundant" from "this arm
                # never computes it".
                dep_entry = {"raw": raw, "scope": scope, "ref_key": ref_key, "state": "unresolved"}
                if redundant:
                    dep_entry["redundant_spelling"] = True
                depends_on_out.append(dep_entry)
                excluded_reason = excluded_ref_key_reasons.get(ref_key)
                if target_project_id not in known_project_ids:
                    reason, target_state = "project-unknown", "not-enumerated"
                elif target_project_id not in projects_with_capabilities_file:
                    reason, target_state = "project-has-no-capabilities-file", "enumerated-not-adopted"
                elif target_project_id not in projects_with_usable_capabilities_file:
                    # The project HAS reusable-capabilities.json; it just
                    # doesn't parse (or its root isn't an object, or its
                    # capabilities[] isn't a list). This never "resolves
                    # itself as adoption spreads" -- adoption already
                    # happened; the file is broken. Its own row in
                    # degraded[] carries the parse reason.
                    reason, target_state = "capabilities-file-invalid", "enumerated-file-unreadable"
                elif excluded_reason is not None and excluded_reason != "duplicate-ref-key":
                    # The target capability EXISTS and is right there in
                    # capabilities[]; it is barred from being a join target
                    # because its own identity is corrupted (a colliding
                    # global_id, or an ambiguous project_id). Reporting that
                    # as "capability-not-found" was actively false --
                    # SKILL.md documents that reason as "the target project
                    # does not have that capability", and it does.
                    #
                    # The genuinely-duplicated-ref_key case is deliberately
                    # NOT folded in here: there "which capability did you
                    # mean?" has no answer at all, and it already reports
                    # itself through ambiguous_ref_keys[] /
                    # counts.ambiguous_ref_keys. target_excluded_reason below
                    # carries the precise axis for every excluded case, so
                    # neither is a silent misdiagnosis.
                    reason, target_state = "capability-ambiguous", "in-catalog-target-excluded"
                else:
                    reason, target_state = "capability-not-found", "in-catalog-with-capabilities"
                unresolved_references.append(
                    {
                        "ref_key": ref_key,
                        "raw": raw,
                        "scope": scope,
                        "reason": reason,
                        "target_project_id": target_project_id,
                        "target_project_state": target_state,
                        # None whenever the ref_key was never a candidate
                        # for the forward index in the first place.
                        "target_excluded_reason": excluded_reason,
                        "from": {"project_id": cap["project_id"], "capability_global_id": cap["global_id"]},
                    }
                )
        cap["depends_on"] = depends_on_out
        del cap["depends_on_raw"]

    # orca-context-wiki.json links[] resolution: same-file page-id
    # references only, near-zero extra cost, never abort the run.
    unresolved_wiki_links: list[dict[str, Any]] = []
    for project_id, real_path, links_raw, page_ids in wiki_link_jobs:
        for link in links_raw:
            if not isinstance(link, dict):
                unresolved_wiki_links.append(
                    {
                        "project_id": project_id,
                        "project_real_path": real_path,
                        "from": None,
                        "to": None,
                        "relation": None,
                        "reason": "malformed-link",
                    }
                )
                continue
            from_id = link.get("from")
            to_id = link.get("to")
            relation = link.get("relation") if isinstance(link.get("relation"), str) else None
            if not isinstance(from_id, str) or not isinstance(to_id, str):
                unresolved_wiki_links.append(
                    {
                        "project_id": project_id,
                        "project_real_path": real_path,
                        "from": from_id if isinstance(from_id, str) else None,
                        "to": to_id if isinstance(to_id, str) else None,
                        "relation": relation,
                        "reason": "malformed-link",
                    }
                )
                continue
            reason = None
            if from_id not in page_ids:
                reason = "from-page-unknown"
            elif to_id not in page_ids:
                reason = "to-page-unknown"
            if reason:
                unresolved_wiki_links.append(
                    {
                        "project_id": project_id,
                        "project_real_path": real_path,
                        "from": from_id,
                        "to": to_id,
                        "relation": relation,
                        "reason": reason,
                    }
                )

    wiki_links_count = sum(len(links) for (_, _, links, _) in wiki_link_jobs)
    depends_on_refs = sum(len(cap["depends_on"]) for cap in all_caps)
    # "resolved" is no longer the complement of "unresolved": a third state,
    # "self-reference", exists. Counting states explicitly keeps
    # resolved + unresolved + self_reference == depends_on_refs true instead
    # of silently folding self-loops into the unresolved bucket.
    depends_on_state_histogram: dict[str, int] = {}
    for cap in all_caps:
        for dep in cap["depends_on"]:
            depends_on_state_histogram[dep["state"]] = depends_on_state_histogram.get(dep["state"], 0) + 1
    resolved_refs = depends_on_state_histogram.get("resolved", 0)
    self_reference_refs = depends_on_state_histogram.get("self-reference", 0)
    unresolved_refs = depends_on_state_histogram.get("unresolved", 0) + depends_on_state_histogram.get("malformed", 0)
    def _contributing(name: str) -> int:
        """Projects whose copy of `name` was USABLE -- parsed, right shape,
        and its surviving rows kept. That is what "ok" + "partial" means and
        all it means.

        It is deliberately NOT "contributed >= 1 row": in the degenerate case
        where every sub-entry in a file is malformed, the source is
        "partial" with a count of 0 and is still counted here. Counting only
        "ok" is the strictly worse alternative -- process_target() keeps the
        survivors of a "partial" source, so a file with 10 good and 1 bad
        capability would report 0 projects and 10 capabilities. Cleanliness
        is carried separately and correctly by degraded[] /
        counts.degraded_sources / exit 1."""
        bucket = source_status_histogram[name]
        return bucket.get("ok", 0) + bucket.get("partial", 0)

    counts = {
        "scan_targets": len(targets),
        "projects_with_sources": projects_with_sources,
        "project_status_histogram": project_status_histogram,
        # Denominator is now every scan target, not just the handful with a
        # `sources` dict -- sum(bucket.values()) == scan_targets per file.
        "source_status_histogram": {k: v for k, v in source_status_histogram.items() if v},
        "wiki_projects": _contributing("orca-context-wiki.json"),
        "wiki_pages": len(all_pages),
        "wiki_links": wiki_links_count,
        "inventory_projects": _contributing("orca-cli-capability-inventory.json"),
        "inventory_commands": sum(inv["command_count"] for inv in all_inventories),
        "capability_projects": _contributing("reusable-capabilities.json"),
        "capabilities": len(all_caps),
        "depends_on_refs": depends_on_refs,
        "depends_on_state_histogram": depends_on_state_histogram,
        "resolved_refs": resolved_refs,
        "unresolved_refs": unresolved_refs,
        "self_reference_refs": self_reference_refs,
        # Exact-duplicate depends_on strings removed before any edge was
        # credited, summed over every capability.
        "duplicate_depends_on_entries": sum(cap["duplicate_depends_on_count"] for cap in all_caps),
        # Non-string / empty depends_on entries dropped (information lost,
        # unlike the lossless dedup above), summed over every capability.
        "malformed_depends_on_entries": sum(cap["malformed_depends_on_count"] for cap in all_caps),
        # wiki `links` present but not a list -- a whole-field schema
        # violation, counted per PROJECT rather than per entry.
        "projects_with_malformed_links": projects_with_malformed_links,
        # Distinct-spelling refs from one capability that resolved to a
        # target that capability had already credited.
        "duplicate_edges": duplicate_edges,
        "ambiguous_ref_keys": len(ambiguous_ref_keys),
        "excluded_ref_keys": len(excluded_ref_keys),
        "ambiguous_global_ids": len(ambiguous_global_ids),
        "ambiguous_page_global_ids": len(ambiguous_page_global_ids),
        "cross_namespace_global_id_collisions": len(cross_namespace_global_id_collisions),
        "capabilities_with_unaddressable_project_id": sum(
            1 for cap in all_caps if not cap["project_id_addressable"]
        ),
        "project_id_fallback_count": fallback_count,
        "degraded_sources": len(degraded),
    }

    catalog: dict[str, Any] = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": None,
        "verified_at": None,
        "content_fingerprint": None,
        "generator": {
            "script": GENERATOR_SCRIPT_REL_PATH,
            "version": GENERATOR_VERSION,
            "sha256": generator_sha256,
        },
        "enumeration": {
            "orca_bin": orca_bin,
            "sources": ["repo list --json", "worktree list --json"],
            "repo_count": enum_meta["repo_count"],
            "worktree_count": enum_meta["worktree_count"],
            "worktree_total_count": enum_meta["worktree_total_count"],
            "worktree_truncated": enum_meta["worktree_truncated"],
            "malformed_rows": malformed_rows,
            "literal_path_count": literal_path_count,
            "scan_target_count": len(targets),
        },
        "counts": counts,
        "projects": project_rows,
        "capabilities": all_caps,
        "wiki_pages": all_pages,
        "cli_inventories": all_inventories,
        "capability_ref_index": capability_ref_index,
        "capability_reverse_index": capability_reverse_index,
        "ambiguous_ref_keys": ambiguous_ref_keys,
        "excluded_ref_keys": excluded_ref_keys,
        "ambiguous_global_ids": ambiguous_global_ids,
        "ambiguous_page_global_ids": ambiguous_page_global_ids,
        "cross_namespace_global_id_collisions": cross_namespace_global_id_collisions,
        "unresolved_references": unresolved_references,
        "self_references": self_references,
        "unresolved_wiki_links": unresolved_wiki_links,
        "project_id_collisions": collisions,
        "degraded": degraded,
    }
    return catalog


# ---------------------------------------------------------------------------
# Output encoding, fingerprinting, atomic write, lockfile
# ---------------------------------------------------------------------------


def _encode_catalog(catalog: dict[str, Any]) -> bytes:
    """One of this module's three output boundaries; see
    _sanitize_line_separators() for why the escaping happens here rather
    than per field."""
    text = json.dumps(catalog, ensure_ascii=False, sort_keys=False, indent=1)
    return _sanitize_line_separators(text).encode("utf-8")


_FINGERPRINT_EXCLUDED_TOP_KEYS = ("generated_at", "verified_at", "content_fingerprint")
_FINGERPRINT_EXCLUDED_ANY_DEPTH_KEYS = ("mtime", "mtime_ns")


def _strip_for_fingerprint(value: Any) -> Any:
    """Deep-copy `value`, dropping mtime/mtime_ns wherever they occur (only
    inside per-source entries in practice). A touch with no content edit
    must not flip `rebuilt` -- mtime is provenance, not content."""
    if isinstance(value, dict):
        return {
            k: _strip_for_fingerprint(v) for k, v in value.items() if k not in _FINGERPRINT_EXCLUDED_ANY_DEPTH_KEYS
        }
    if isinstance(value, list):
        return [_strip_for_fingerprint(v) for v in value]
    return value


def compute_fingerprint(catalog: dict[str, Any]) -> str:
    stripped = {k: v for k, v in catalog.items() if k not in _FINGERPRINT_EXCLUDED_TOP_KEYS}
    stripped = _strip_for_fingerprint(stripped)
    blob = json.dumps(stripped, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _read_previous_catalog(path: Path) -> dict[str, Any] | None:
    """Any failure to read or parse the previous catalog is "no previous
    catalog" -- it must never block a rebuild.

    Opened O_NOFOLLOW: this read happens seconds after cmd_build()'s write
    guard ran, on the far side of the entire fleet scan, so a symlink
    swapped in at this exact path during that window would previously have
    been followed and read. ELOOP (and every other OSError) lands in the
    same bucket as "there is no previous catalog", so the swap can at worst
    force a rebuild, never a read outside the output directory and never an
    exception out of this function.

    Opened O_NONBLOCK for the same window, against a different plant.
    O_NOFOLLOW does not apply to a FIFO -- a FIFO is not a symlink, it is
    the real file at that path -- and a blocking open() of a FIFO with no
    writer sleeps in the KERNEL, inside os.open() itself. The S_ISREG guard
    below is downstream of that call and so could never fire: the process
    simply stopped, indefinitely, WHILE HOLDING .catalog.lock, which made
    every subsequent run fail `lock_held` until the 300s staleness window
    (and then wedge in turn). Measured before the flag: still blocked after
    8s, SIGKILL required. With O_NONBLOCK the open returns immediately even
    with no writer, and S_ISREG then rejects the FIFO the way it always
    meant to. O_NONBLOCK is a no-op for reads from the regular file this
    normally opens.

    With both flags in place the docstring's opening sentence is now
    literally true for every input this function can be handed: it returns,
    and it returns None or a dict."""
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None
            if st.st_size > MAX_PREVIOUS_CATALOG_BYTES:
                return None
            chunks: list[bytes] = []
            remaining = MAX_PREVIOUS_CATALOG_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError:
            return None
    finally:
        os.close(fd)

    raw = b"".join(chunks)
    if len(raw) > MAX_PREVIOUS_CATALOG_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _write_all_bytes(fd: int, payload: bytes) -> None:
    """os.write(fd, payload) does not guarantee the full payload is written
    in one call -- POSIX permits a short write for a regular file (this
    codebase's own round-6-fix final-gate review, applied to
    wiki_edit_guard.py and promote_capability.py, empirically demonstrated a
    genuine short write on this exact machine via RLIMIT_FSIZE: os.write()
    returned fewer bytes than requested with no exception raised). The
    caller of this function relies on the tmp file it writes being either
    the COMPLETE intended payload or absent -- silently accepting a short
    write here would let a truncated, invalid-JSON tmp file get
    os.replace()'d onto the live catalog file while the caller still reports
    success. Loop until every byte is written; a zero-progress write (or any
    OSError from a subsequent write, e.g. the OS refusing further writes
    past a resource limit) raises immediately so the caller's existing
    `except BaseException: unlink tmp; raise` cleanup fires -- fail-closed,
    matching every other write path in this module, rather than fail-open
    with a silently truncated result."""
    view = memoryview(payload)
    while view:
        n = os.write(fd, view)
        if n == 0:
            raise OSError("short write: os.write() returned 0 (no forward progress)")
        view = view[n:]


def atomic_write_within(catalog_dir: Path, final_path: Path, payload: bytes) -> None:
    tmp_path = final_path.parent / f".{final_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    # The cleanup bracket covers os.replace() too, not just write/fsync. It
    # used to stop short of it, so a replace that raised (destination is a
    # directory, cross-device rename, EPERM on a sticky dir) orphaned a
    # .catalog.json.tmp-<pid>-<ms> file in the output directory on every
    # attempt. The tmp file must not survive a failed write for ANY reason.
    try:
        try:
            _write_all_bytes(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp_path), str(final_path))  # replaces a symlink at final_path, not its target
    except BaseException:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(catalog_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # best-effort durability; not every filesystem supports directory fsync


def acquire_lock(catalog_dir: Path) -> Path:
    lock_path = catalog_dir / LOCK_NAME
    for attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                _write_all_bytes(fd, json.dumps({"pid": os.getpid(), "started_at": now_iso()}).encode("utf-8"))
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
            raise CatalogFatal("lock_held")
    raise CatalogFatal("lock_held")


def release_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        os.unlink(str(lock_path))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only aggregator: consolidate every Orca project's public wiki files into one cross-project catalog."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="Enumerate the Orca fleet, read each project's public wiki files, and write catalog.json."
    )
    build.add_argument("--force", action="store_true", help="Ignore any previous catalog; always rebuilt=true.")
    build.add_argument(
        "--json", action="store_true", help="Print the run summary (NOT the catalog) as one JSON object to stdout."
    )
    build.add_argument("--quiet", action="store_true", help="Suppress human-readable text; rely on the exit code.")
    build.add_argument(
        "--output",
        type=str,
        default=None,
        help=f"Catalog output subdirectory, must be {DEFAULT_OUTPUT_DIR} or a descendant of it (default: the same).",
    )
    # There is deliberately NO --output override flag, hidden or otherwise.
    # One briefly existed (argparse.SUPPRESS'd, justified as "so the test
    # suite can point --output at a tempdir") and it reopened the exact hole
    # the pin closes: with it, --output <a live project's git tree> wrote
    # catalog.json inside that tree at exit 0. It was also unnecessary --
    # the suite redirects the pin by rebinding DEFAULT_OUTPUT_DIR, which
    # OutputPinningTests already did at four sites. A hidden hatch whose
    # stated justification is falsified by the same suite's own code is not
    # a hatch, it is a bypass. Do not add one back: SKILL.md documents the
    # pin as absolute, and it now is.
    build.add_argument(
        "--orca-bin", type=str, default=None, help="Path to the orca binary (default: PATH, then the Orca.app bundled binary)."
    )
    build.add_argument(
        "--timeout-budget",
        type=positive_int,
        default=DEFAULT_TIMEOUT_BUDGET,
        help="Whole-run monotonic budget in seconds covering both orca calls.",
    )
    return parser


def _emit_error(args: argparse.Namespace, code: int, reason: str, message: str | None = None) -> int:
    if not getattr(args, "quiet", False):
        payload = {"ok": False, "reason": reason}
        if message:
            payload["message"] = message
        if getattr(args, "json", False):
            # Output boundary #3 (stderr). `message` can echo an OS error
            # string built from an attacker-influenced path, so it gets the
            # same U+2028/U+2029 treatment as the catalog file.
            print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False)), file=sys.stderr)
        else:
            suffix = f" ({message})" if message else ""
            print(f"error: {reason}{suffix}", file=sys.stderr)
    return code


def _print_human_summary(catalog: dict[str, Any], rebuilt: bool, output_path: Path) -> None:
    counts = catalog["counts"]
    hist = counts["project_status_histogram"]
    no_wiki = hist.get("no-wiki-dir", 0)
    path_missing = hist.get("path-missing", 0)
    print(f"catalog: {counts['projects_with_sources']} of {counts['scan_targets']} enumerated project paths contributed sources")
    print(f"  wiki pages ............ {counts['wiki_projects']} project(s), {counts['wiki_pages']} pages")
    print(f"  cli inventories ....... {counts['inventory_projects']} project(s), {counts['inventory_commands']} commands")
    print(f"  reusable capabilities . {counts['capability_projects']} project(s), {counts['capabilities']} capabilities")
    print(
        f"  references ............ {counts['depends_on_refs']} refs "
        f"({counts['resolved_refs']} resolved, {counts['unresolved_refs']} unresolved)"
    )
    print(f"  skipped ............... {no_wiki} no wiki/, {path_missing} path missing   -- use --json for the list")
    print(f"  degraded .............. {counts['degraded_sources']}")
    print(f"{'rebuilt' if rebuilt else 'unchanged'} {output_path}")


def cmd_build(args: argparse.Namespace) -> int:
    start = time.monotonic()
    generator_path = Path(__file__).resolve()
    try:
        generator_sha256 = hashlib.sha256(generator_path.read_bytes()).hexdigest()
    except OSError as exc:
        return _emit_error(args, 4, "generator_unreadable", str(exc))

    output_dir = Path(args.output).expanduser() if args.output is not None else DEFAULT_OUTPUT_DIR
    if not output_dir.is_absolute():
        return _emit_error(args, 2, "output_dir_must_be_absolute")

    # --output used to be an UNPINNED write boundary: write_only_within()
    # is parameterized on the chosen directory itself, so it could only
    # ever confine the catalog FILE to whatever directory was named -- it
    # structurally could not reject the directory. Any absolute path,
    # including one inside a live project's git tree, was accepted.
    # Pinning happens here, before anything touches the filesystem, and is
    # UNCONDITIONAL -- there is no flag, environment variable, or argument
    # combination that skips it (see build_parser()).
    pinned_root = DEFAULT_OUTPUT_DIR.absolute()
    permitted = True
    try:
        output_dir.absolute().relative_to(pinned_root)
    except ValueError:
        permitted = False
    if permitted:
        # Second pass post-resolve: Path.absolute() does not normalize
        # '..', so ".../cross-project-catalog/../../projects" clears the
        # lexical check and is only caught here.
        try:
            output_dir.resolve(strict=False).relative_to(pinned_root.resolve(strict=False))
        except ValueError:
            permitted = False
    if not permitted:
        return _emit_error(args, 2, "output_dir_not_permitted")

    # Defense in depth: the write side of this script's asymmetric symlink
    # policy bans every symlink with no exceptions, and write_only_within()
    # explicitly never checks the root itself -- only what sits below it. So
    # the root is checked here.
    if os.path.islink(str(output_dir)):
        return _emit_error(args, 2, "output_dir_is_symlink")

    # Only now, after both rejections, may a directory be created -- a
    # rejected run must not leave a freshly-made directory behind.
    try:
        os.makedirs(str(output_dir), mode=0o700, exist_ok=True)
    except OSError as exc:
        return _emit_error(args, 4, "output_dir_uncreatable", str(exc))

    checked_final, reason = write_only_within(output_dir, str(output_dir / CATALOG_NAME))
    if reason or checked_final is None:
        return _emit_error(args, 2, reason or "invalid_path")
    # Use the guard's OWN return value rather than re-deriving the path:
    # every later write (tmp sibling, lockfile) is derived from this same
    # validated location, which is what makes containment structural.
    final_path = checked_final
    # Re-root on the guard-resolved directory so the lockfile and the tmp
    # sibling are derived from the exact same validated location as the
    # catalog file, not from a second, differently-spelled path.
    output_dir = final_path.parent

    try:
        lock_path = acquire_lock(output_dir)
    except CatalogFatal as exc:
        return _emit_error(args, 4, exc.reason)

    try:
        orca_bin = args.orca_bin or shutil.which("orca") or FALLBACK_ORCA_BIN
        if not orca_bin or not os.access(orca_bin, os.X_OK):
            return _emit_error(args, 4, "orca_binary_missing")

        def budget_remaining() -> float:
            return args.timeout_budget - (time.monotonic() - start)

        try:
            repos, worktrees, enum_meta = enumerate_fleet(orca_bin, budget_remaining)
        except CatalogFatal as exc:
            return _emit_error(args, 4, exc.reason)

        targets, malformed_rows, literal_path_count = build_scan_targets(repos, worktrees)
        catalog = assemble_catalog(targets, enum_meta, orca_bin, malformed_rows, literal_path_count, generator_sha256)

        fingerprint = compute_fingerprint(catalog)
        now = now_iso()
        previous = None if args.force else _read_previous_catalog(final_path)
        if previous is not None and previous.get("content_fingerprint") == fingerprint:
            catalog["generated_at"] = previous.get("generated_at") or now
            catalog["verified_at"] = now
            rebuilt = False
        else:
            catalog["generated_at"] = now
            catalog["verified_at"] = now
            rebuilt = True
        catalog["content_fingerprint"] = fingerprint

        payload = _encode_catalog(catalog)
        atomic_write_within(output_dir, final_path, payload)
    finally:
        release_lock(lock_path)

    elapsed = time.monotonic() - start
    if args.json:
        summary = {
            "ok": True,
            "rebuilt": rebuilt,
            "output": str(final_path),
            "elapsed_seconds": round(elapsed, 3),
            "generated_at": catalog["generated_at"],
            "verified_at": catalog["verified_at"],
            "enumeration": catalog["enumeration"],
            "counts": catalog["counts"],
            "degraded_count": len(catalog["degraded"]),
            "degraded": catalog["degraded"],
        }
        if not args.quiet:
            # Output boundary #2 (stdout). degraded[] carries reason strings
            # lifted verbatim from other projects' files.
            print(_sanitize_line_separators(json.dumps(summary, ensure_ascii=False, indent=1)))
    elif not args.quiet:
        _print_human_summary(catalog, rebuilt, final_path)

    return 1 if catalog["degraded"] else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "build":
        return 2
    try:
        return cmd_build(args)
    except CatalogFatal as exc:
        return _emit_error(args, 4, exc.reason)
    except BaseException as exc:
        # This tool must never crash without an exit code -- and `except
        # Exception` did not deliver that, because KeyboardInterrupt and
        # SystemExit are BaseException, not Exception, and walked straight
        # past it. Ctrl-C therefore produced a raw traceback and exit 1
        # under the comment promising it could not.
        #
        # Both are reported through the same boundary as any other failure
        # and then RE-RAISED rather than converted to exit 4: swallowing a
        # Ctrl-C would break the caller's own interrupt handling, and
        # swallowing a SystemExit would discard the exit code something
        # deliberately requested. Cleanup is already guaranteed regardless:
        # cmd_build()'s `finally: release_lock(...)` and
        # atomic_write_within()'s `except BaseException` tmp-file bracket
        # both run before the exception ever reaches this frame -- which is
        # the property that actually matters for an interrupt, and is
        # pinned by its own test.
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
