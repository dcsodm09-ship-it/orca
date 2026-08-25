#!/usr/bin/env python3
"""Read-mostly `text_mention` fuzzy-evidence generator (M8-1, Gate C of the
cross-project catalog plan -- M8-DESIGN-FINAL-2026-08-23.md sections 3.2 /
3.2.4).

Reads ONE file -- an already-built `catalog.json` from
build_cross_project_catalog.py -- and produces a SEPARATE, purely-derived
artifact, `mention-evidence.json`, that answers "does this entry's own text
(name/title/summary) appear to mention some OTHER entry, without a declared
dependency between them?". It never writes back into catalog.json, never
touches any project's own tracked files, and never executes anything.

THE MATCHING ALGORITHM IS PORTED, NOT REDESIGNED
--------------------------------------------------
Every normalize/gate/confidence rule below (the `_normalize()` NFC+casefold
copy, the four false-positive gates, the declared-dependency exclusion, the
confidence rule) is a byte-for-byte-equivalent port of
`m8-gate-c-validation-STAGED-review-only/scripts/mention_evidence_prototype.py`,
the throwaway analysis script the M8 Gate-C real-data validation
(`m8-gate-c-validation-STAGED-review-only/M8-GATE-C-VALIDATION-REPORT-2026-08-23.md` section 1) was actually run
against. The point of that front-loaded validation was to measure THIS
algorithm's false-positive behavior on real data -- porting a different
algorithm here would silently invalidate the only real-data check this
feature has ever had. ASCII_MIN_LEN=4, CJK_MIN_LEN=3, and the
STOPWORDS_ASCII/STOPWORDS_CJK literal sets are therefore copied verbatim,
not retuned, per that report's own explicit warning that these gates were
barely triggered on real data and their thresholds are unvalidated.

What changed going from prototype to this production tool is packaging, not
algorithm: a real output schema/path, locking, atomic self-validating
writes, generator exit-code semantics, and a mandatory OS-level read-only
isolation test -- none of which the prototype needed (it never wrote
anywhere but its own --out-dir).

OUTPUT: A FIXED ABSOLUTE PATH, NOT Path(__file__)-RELATIVE
--------------------------------------------------------------------------
`/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/mention-evidence.json`
-- the SAME directory catalog.json itself lives in (not a subdirectory),
because this artifact is catalog.json's sibling: a second, independently
locked derived file next to the first. DEFAULT_OUTPUT_DIR below is an
ABSOLUTE constant for the same reason build_cross_project_catalog.py's own
DEFAULT_OUTPUT_DIR and detect_capability_changes.py's DEFAULT_OUTPUT_DIR are
absolute constants: a `Path(__file__)`-relative pin is correct only while a
script lives in a scratch staging area outside every project's tracked
tree -- once relocated into a project's own `orca-context-bridge/scripts/`
(this tool's eventual home), that same relative pin would silently create a
new, untracked directory INSIDE a project's own tracked path, which is
exactly the AUTHORITY_TRACKED_PATHS trap this codebase has hit three times
already (Gate A's DEFAULT_OUTPUT_DIR, Gate B's test suite's REAL_REPO_ROOT,
and a near-miss in Gate D). There is no flag to point this tool's output
anywhere else; the test suite redirects the pin by rebinding the
module-level DEFAULT_OUTPUT_DIR constant itself, exactly as
detect_capability_changes.py's own test suite documents doing.

M8-1 AUTHORIZATION NOTICE ON EVERY DEFAULT-PATH WRITE
--------------------------------------------------------------------------
`build` still runs and writes normally regardless -- this is a visibility/
audit-trail improvement, not a new gate, and not a refusal. But whenever the
resolved output_dir is still this module's own literal production default
(`_PRODUCTION_DEFAULT_OUTPUT_DIR`, a frozen copy of `DEFAULT_OUTPUT_DIR` --
see that constant's own comment for why the two are kept distinct), `build`
prints one unsuppressible stderr line before writing, the same mechanism
discover_capability_candidates.py uses for its own `--all-projects`
warning: M8-1's own independent authorization gate (design 3.2.4) has not
been granted (Gate C's real-data run produced only 3 candidate edges, all
same-project, zero cross-project -- see module docstring above), so
mention-evidence.json should not be treated as production-authoritative by
anything that reads it. A caller who redirects output elsewhere (today,
only this file's own test suite, by rebinding the mutable
`DEFAULT_OUTPUT_DIR` name) sees nothing here.

LOCKING: A DEDICATED LOCK, NOT catalog.json's OWN .catalog.lock
--------------------------------------------------------------------------
`.mention-evidence.lock` lives in the same directory as `.catalog.lock` but
is a completely separate lockfile. Sharing catalog.json's own lock would
imply this tool and build_cross_project_catalog.py participate in the same
mutual-exclusion domain, which they do not -- they are different producers
writing different files, and conflating their locks risks incorrect
cross-tool blocking assumptions. acquire_lock()/release_lock() below are a
deliberate COPY (not an import) of detect_capability_changes.py's own
implementation, including its LOCK_STALE_SECONDS=300 stale-lock recovery,
per this codebase's "copy a small snippet, never import a sibling script"
convention (confirmed by grep: every M4-M8 sibling script imports only
stdlib).

ATOMIC WRITE WITH SELF-VALIDATION
--------------------------------------------------------------------------
write_only_within()/atomic_write_within() are the same reduced copy of
build_cross_project_catalog.py's pattern that detect_capability_changes.py
already carries. This tool adds one more step atomic_write_within() in
those sibling scripts does not need: before the temp file is ever renamed
over the previous good output, it is reopened and `json.load()`ed back. A
write that produced truncated or corrupt bytes (disk full mid-write, a
future careless edit to the payload builder) must never replace a
previously-good mention-evidence.json -- see
test_atomic_write_self_validation_failure_leaves_previous_file_untouched.

EXIT CODES (generator semantics, matching build_cross_project_catalog.py /
detect_capability_changes.py, per M8-DESIGN-FINAL-2026-08-23.md 3.0.2)
--------------------------------------------------------------------------
    0  the run completed and wrote its output -- true even when zero edges
       were found, and even when rows degraded (skipped_malformed_*, gate
       rejections): all of that is IN the JSON body's skip_stats, never
       silently dropped, and never downgrades the exit code.
    2  usage error (bad flag value).
    4  could not run at all -- catalog.json missing/unreadable/unparseable/
       not shaped like a catalog (neither capabilities[] nor wiki_pages[] is
       a list), the lock is held or could not be created at all
       (lock_uncreatable, e.g. a read-only output_dir), the output
       directory/path could not be secured, the payload would contain a
       non-finite number (payload_contains_non_finite_number -- a
       NaN/Infinity value from catalog.json that would otherwise serialize
       as invalid-by-spec JSON), the encoded payload exceeds
       query_mention_evidence.py's own MAX_BYTES read cap
       (output_exceeds_query_readable_size), or the self-validating write
       failed its own JSON round trip. Every one of these paths returns
       BEFORE atomic_write_within() is ever called (or, for the round-trip
       failure, before the rename half of it runs), so a fatal exit here
       is guaranteed to leave any previous successful output byte-for-byte
       untouched.

Run with:
    python3 build_mention_evidence.py build [--catalog <path>]
                                             [--json] [--quiet]
"""

from __future__ import annotations

import argparse
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

SCRIPT_REL_PATH = "orca-context-bridge/scripts/build_mention_evidence.py"
GENERATOR_VERSION = "1.0.0"
OUTPUT_SCHEMA_VERSION = 1

# Fixed absolute paths -- deliberately NOT derived from Path(__file__). See
# the module docstring's OUTPUT section for why. mention-evidence.json lives
# in the SAME directory as catalog.json (catalog.json's sibling artifact),
# not a subdirectory of it.
#
# os.makedirs() below is called with mode=0o700, matching
# build_cross_project_catalog.py's own os.makedirs(str(output_dir),
# mode=0o700, exist_ok=True) on this SAME directory -- that script is the
# directory's usual first creator, so 0o700 is the established precedent
# for it, not a mode this tool would be inventing. exist_ok=True makes this
# a no-op on the common path (the directory already exists, created by
# build_cross_project_catalog.py, since catalog.json -- read by this tool
# moments earlier -- lives inside it); it only has a real effect on the
# narrow path where this tool is run with a --catalog pointing elsewhere
# before build_cross_project_catalog.py has ever run, in which case it must
# not silently fall back to a permissive umask-derived mode.
DEFAULT_CATALOG_PATH = Path("/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json")
DEFAULT_OUTPUT_DIR = Path("/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog")
# Frozen copy of the literal default above, kept distinct from the mutable
# DEFAULT_OUTPUT_DIR name tests rebind (see module docstring's OUTPUT
# section) so a run can tell "still pointed at the real production default"
# apart from "a test (or, in principle, a future --output-dir flag)
# redirected me elsewhere" -- see the authorization notice print in
# cmd_build().
_PRODUCTION_DEFAULT_OUTPUT_DIR = DEFAULT_OUTPUT_DIR
OUTPUT_NAME = "mention-evidence.json"
LOCK_NAME = ".mention-evidence.lock"
LOCK_STALE_SECONDS = 300

MAX_CATALOG_BYTES = 16 * 1024 * 1024
# Matches query_mention_evidence.py's own MAX_BYTES read cap: the generator
# must never write an artifact its own query tool can't read back. Checked
# right before the write, so a run that exceeds it fails closed (exit 4,
# named reason) instead of silently producing a file that later dies with
# an opaque mention_evidence_unreadable on the query side.
MAX_OUTPUT_BYTES = 16 * 1024 * 1024

# ---------------------------------------------------------------------------
# Algorithm constants -- verbatim port from mention_evidence_prototype.py.
# Do not retune without new real-data validation; see module docstring.
# ---------------------------------------------------------------------------

ASCII_MIN_LEN = 4
CJK_MIN_LEN = 3

STOPWORDS_ASCII = {
    "id", "ids", "name", "names", "path", "paths", "test", "tests",
    "data", "read", "write", "check", "build", "run", "runs", "task",
    "tasks", "script", "scripts", "skill", "skills", "config",
    "config-pattern", "capability", "capabilities", "catalog", "wiki",
    "page", "pages", "project", "projects", "system", "systems", "tool",
    "tools", "file", "files", "user", "users", "node", "nodes", "server",
    "servers", "client", "clients", "panel", "panels", "route", "routes",
    "setup", "install", "guard", "index", "verify", "verified", "review",
    "report", "reports", "status", "state", "agent", "agents", "orca",
    "codex", "claude", "account", "accounts", "service", "services",
    "api", "app", "apps", "web", "home", "main", "default", "common",
    "shared", "global", "local", "remote", "v1", "v2",
}
STOPWORDS_CJK = {
    "系统", "项目", "脚本", "文件", "配置", "方案", "任务", "报告",
    "调研", "设备", "服务器", "节点", "软件", "客户端", "后端", "前端",
    "网页", "账号", "账户", "设置", "管理", "接口", "代理", "验证",
    "检查", "只读", "权限", "路由", "路由器", "流程", "团队", "公司",
    "平台", "应用", "工具", "模块", "组件", "环境", "数据", "版本",
    "更新", "修复", "安装", "部署", "测试", "记录", "方式", "规则",
    "策略", "架构", "迁移", "面板", "终端", "控制", "执行", "运行",
    "调用", "调度", "分析", "总结", "调整", "优化", "工程", "预算",
}

CONFIDENCE_RANK = {"low": 0, "medium": 1}


class BuildFatal(Exception):
    """Cannot run at all -- maps to exit code 4. Never raised after a write
    has happened; see cmd_build()."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# Small shared helpers (deliberately duplicated across this project's
# tools; see module docstring's "copy, don't import" note)
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_line_separators(text: str) -> str:
    """Escape U+2028/U+2029 in already-serialized JSON text. Same two-line
    contract as build_cross_project_catalog.py / query_catalog.py /
    detect_capability_changes.py; copied, not imported."""
    return text.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")


def _reject_duplicate_keys(pairs: list) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON object")
        seen[key] = value
    return seen


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


# ---------------------------------------------------------------------------
# Normalization + gates -- verbatim port from mention_evidence_prototype.py
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Verbatim copy of query_catalog.py::_normalize() -- NFC then
    casefold. Reproduced here per the "copy, don't import" convention."""
    return unicodedata.normalize("NFC", text).casefold()


def _is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
        or 0xF900 <= cp <= 0xFAFF  # CJK Compatibility Ideographs
    )


def _cjk_ratio(text: str) -> float:
    stripped = [ch for ch in text if not ch.isspace()]
    if not stripped:
        return 0.0
    cjk_count = sum(1 for ch in stripped if _is_cjk_char(ch))
    return cjk_count / len(stripped)


def _is_cjk_dominant(normalized_text: str) -> bool:
    return _cjk_ratio(normalized_text) > 0.5


def _ascii_word_boundary_search(needle: str, haystack: str) -> "re.Match[str] | None":
    """Same boundary rule as `_ascii_word_boundary_match()` below, but
    returns the `re.Match` object instead of just a bool, so a caller can
    anchor `_extract_snippet()` on the SAME occurrence that actually
    satisfied the boundary gate. A plain `str.find()` of the needle can
    land on an earlier, non-boundary-satisfying occurrence (e.g. needle
    "guid" inside unrelated "guide" earlier in the same haystack) even
    when the real, boundary-flanked occurrence is somewhere else entirely
    -- see `_extract_snippet()`'s `match_start` parameter."""
    pattern = re.compile(
        r"(?<![0-9A-Za-z_-])" + re.escape(needle) + r"(?![0-9A-Za-z_-])"
    )
    return pattern.search(haystack)


def _ascii_word_boundary_match(needle: str, haystack: str) -> bool:
    """True if `needle` occurs in `haystack` flanked by non-word characters
    (or string start/end) on both sides. `needle` is used literally
    (already normalized by the caller); regex-escaped so any regex
    metacharacter in an id/name is treated literally. Boolean-only view of
    `_ascii_word_boundary_search()` above -- kept as its own function
    (rather than inlined at the one call site) because it is also the gate
    this module's docstring documents as a verbatim port, independent of
    the snippet-anchoring concern `_ascii_word_boundary_search()` exists
    for."""
    return _ascii_word_boundary_search(needle, haystack) is not None


def _extract_snippet(haystack: str, needle_normalized: str, context: int = 24, match_start: int | None = None) -> str:
    """Best-effort: find needle_normalized inside a casefolded copy of
    haystack, then slice the ORIGINAL haystack around that span so the
    output shows real (non-casefolded) text. Falls back to a truncated
    haystack if normalization shifted lengths enough that the naive index
    doesn't line up (rare, but casefold can change string length for a few
    codepoints).

    `match_start`, when given, is the index -- into this same function's
    own `hay_casefold` transform of `haystack` -- of the occurrence that
    actually satisfied the ASCII word-boundary gate (from
    `_ascii_word_boundary_search()`). Without it, this function fell back
    to `hay_casefold.find(needle_normalized)`, which returns the FIRST raw
    substring occurrence regardless of word boundaries -- for a haystack
    containing both a non-boundary occurrence (earlier) and the real
    boundary-flanked one (later), that produced a snippet anchored on the
    wrong, gate-rejected occurrence while the actual matched text was
    never shown at all. `match_start` is validated against
    `needle_normalized` before use and silently ignored (falling back to
    `.find()`, same as before) if it does not actually line up -- this
    keeps the function correct even if a future caller passes a stale or
    mismatched index. The CJK-dominant substring gate has no boundary
    rule, so passing `match_start=None` there (its first occurrence IS a
    valid match location) is unchanged from before."""
    hay_casefold = unicodedata.normalize("NFC", haystack).casefold()
    idx = -1
    if match_start is not None and 0 <= match_start <= len(hay_casefold) - len(needle_normalized):
        if hay_casefold[match_start : match_start + len(needle_normalized)] == needle_normalized:
            idx = match_start
    if idx == -1:
        idx = hay_casefold.find(needle_normalized)
    if idx == -1:
        return haystack[: context * 2] + ("..." if len(haystack) > context * 2 else "")
    start = max(0, idx - context)
    end = min(len(haystack), idx + len(needle_normalized) + context)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(haystack) else ""
    return prefix + haystack[start:end] + suffix


# ---------------------------------------------------------------------------
# Read-only catalog loading
# ---------------------------------------------------------------------------


def _read_bounded_file(path: Path, max_bytes: int) -> bytes:
    """Open, size-cap, and fully read one regular file, read-only.

    O_NONBLOCK: a FIFO planted at this path would otherwise block inside
    os.open() itself, in the kernel, before any S_ISREG check downstream
    could run. S_ISREG then rejects a FIFO/device/directory outright.
    O_NOFOLLOW is deliberately NOT used here, matching query_catalog.py's
    own read_catalog_bytes() reasoning: this path is either the documented
    default or one the caller typed, read with the caller's own
    credentials -- there is no privilege boundary a symlink could cross.
    """
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


def load_catalog(path: Path) -> dict:
    try:
        raw = _read_bounded_file(path, MAX_CATALOG_BYTES)
    except FileNotFoundError:
        raise BuildFatal("catalog_missing", str(path))
    except NotADirectoryError:
        raise BuildFatal("catalog_missing", str(path))
    except OSError as exc:
        raise BuildFatal("catalog_unreadable", str(exc))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildFatal("catalog_unparseable", str(exc))
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BuildFatal("catalog_unparseable", str(exc))
    if not isinstance(doc, dict):
        raise BuildFatal("catalog_malformed", "top level is not a JSON object")
    capabilities = doc.get("capabilities")
    wiki_pages = doc.get("wiki_pages")
    if not isinstance(capabilities, list) and not isinstance(wiki_pages, list):
        # Neither searchable list is present: this parsed as JSON but is not
        # a catalog. Same severity call as query_catalog.py's
        # search_catalog(): producing an empty-but-successful result here
        # would be a false "zero edges" rather than the fatal it should be.
        raise BuildFatal("catalog_malformed", "neither capabilities[] nor wiki_pages[] is a list")
    return doc


# ---------------------------------------------------------------------------
# Entity extraction -- ported from mention_evidence_prototype.py, with one
# production hardening added: malformed rows (non-dict, or missing/empty
# global_id) are COUNTED rather than silently included with a None join key,
# matching detect_capability_changes.py's own skipped_malformed convention.
# This is a judgment call beyond what the prototype needed (its throwaway
# JSON output never had to survive a global_id of None reaching an edge).
# ---------------------------------------------------------------------------


def build_entries(catalog: dict) -> tuple[list, int, int]:
    entries: list = []
    skipped_malformed_capabilities = 0
    skipped_malformed_wiki_pages = 0

    capabilities = catalog.get("capabilities")
    if isinstance(capabilities, list):
        for cap in capabilities:
            if not isinstance(cap, dict):
                skipped_malformed_capabilities += 1
                continue
            global_id = cap.get("global_id")
            if not _is_str(global_id) or not global_id:
                skipped_malformed_capabilities += 1
                continue
            depends_on_raw = cap.get("depends_on")
            entries.append(
                {
                    "entry_type": "capability",
                    "project_id": cap.get("project_id"),
                    "global_id": global_id,
                    "id": cap.get("id"),
                    "name": cap.get("name"),
                    "title": None,
                    "summary": cap.get("summary"),
                    "ref_key": cap.get("ref_key"),
                    # A malformed catalog.json could carry a non-list value
                    # here (e.g. a stray int/dict/string survives JSON
                    # parsing fine); coerce to [] rather than let it reach
                    # build_declared_pairs()'s `for dep in ...` and either
                    # silently iterate a string's characters or raise
                    # TypeError on a non-iterable, matching the same
                    # malformed-input tolerance already applied to
                    # global_id/name/title/summary above.
                    "depends_on": depends_on_raw if isinstance(depends_on_raw, list) else [],
                }
            )

    wiki_pages = catalog.get("wiki_pages")
    if isinstance(wiki_pages, list):
        for page in wiki_pages:
            if not isinstance(page, dict):
                skipped_malformed_wiki_pages += 1
                continue
            global_id = page.get("global_id")
            if not _is_str(global_id) or not global_id:
                skipped_malformed_wiki_pages += 1
                continue
            entries.append(
                {
                    "entry_type": "wiki_page",
                    "project_id": page.get("project_id"),
                    "global_id": global_id,
                    "id": page.get("id"),
                    "name": None,
                    "title": page.get("title"),
                    "summary": page.get("summary"),
                    "ref_key": None,
                    "depends_on": [],
                }
            )

    return entries, skipped_malformed_capabilities, skipped_malformed_wiki_pages


def build_declared_pairs(entries: list) -> set:
    """Set of frozenset({from_global_id, to_global_id}) pairs that already
    have a declared_dependency edge in EITHER direction -- verbatim port of
    mention_evidence_prototype.py's build_declared_pairs(), including its
    documented interpretation choice: the exclusion is NOT restricted to
    same-project pairs (see that file's module docstring for the reasoning
    a human reviewer can override)."""
    # ref_key is filtered to isinstance(..., str) before it is ever used as
    # a dict key below: a malformed catalog.json can carry a non-string,
    # possibly unhashable ref_key (a list/dict survive JSON parsing fine),
    # and `{unhashable: ...}` raises TypeError -- the same production-
    # hardening rationale as the depends_on coercion in build_entries()
    # above. global_id needs no such guard: build_entries() already
    # enforces it is a non-empty str for every entry reaching this point.
    by_ref_key = {e["ref_key"]: e for e in entries if isinstance(e.get("ref_key"), str) and e.get("ref_key")}
    by_global_id = {e["global_id"]: e for e in entries if e.get("global_id")}
    declared = set()
    for entry in entries:
        if entry["entry_type"] != "capability":
            continue
        for dep in entry.get("depends_on", []):
            if not isinstance(dep, dict):
                continue
            target = None
            target_global_id = dep.get("target_global_id")
            dep_ref_key = dep.get("ref_key")
            # Same reasoning as by_ref_key/by_global_id above: dep's own
            # target_global_id/ref_key values are unvalidated catalog.json
            # content and must be isinstance-checked before use as a dict
            # key or a `... in dict` membership test, either of which
            # raises TypeError on an unhashable value (e.g. a list).
            if isinstance(target_global_id, str) and target_global_id and target_global_id in by_global_id:
                target = by_global_id[target_global_id]
            elif isinstance(dep_ref_key, str) and dep_ref_key and dep_ref_key in by_ref_key:
                target = by_ref_key[dep_ref_key]
            if target is not None and target.get("global_id") and entry.get("global_id"):
                declared.add(frozenset((entry["global_id"], target["global_id"])))
    return declared


def needle_fields_for(entry: dict) -> list:
    if entry["entry_type"] == "capability":
        return [("id", entry.get("id")), ("name", entry.get("name"))]
    return [("id", entry.get("id")), ("title", entry.get("title"))]


def haystack_fields_for(entry: dict) -> list:
    if entry["entry_type"] == "capability":
        return [("name", entry.get("name")), ("summary", entry.get("summary"))]
    return [("title", entry.get("title")), ("summary", entry.get("summary"))]


def confidence_for(needle_field: str, haystack_field: str, cjk_dominant: bool) -> str:
    if cjk_dominant:
        return "low"
    if needle_field == "id":
        return "medium"
    if needle_field in ("name", "title") and haystack_field in ("name", "title"):
        return "medium"
    return "low"


def find_candidate_edges(entries: list) -> tuple[list, dict]:
    """Verbatim port of mention_evidence_prototype.py's
    find_candidate_edges(). Returns (edges, skip_stats)."""
    declared_pairs = build_declared_pairs(entries)
    edges: list = []
    skip_stats = {
        "self_pair": 0,
        "declared_dependency_pair": 0,
        "empty_needle_or_haystack": 0,
        "length_floor": 0,
        "stopword": 0,
        "ascii_boundary": 0,
        "raw_combinations_checked": 0,
        "raw_hits_before_declared_dep_filter": 0,
    }

    n = len(entries)
    for i in range(n):
        frm = entries[i]
        for j in range(n):
            if i == j:
                skip_stats["self_pair"] += 1
                continue
            to = entries[j]
            if frm.get("global_id") == to.get("global_id"):
                skip_stats["self_pair"] += 1
                continue

            matched_combinations = []
            for needle_field, needle_raw in needle_fields_for(to):
                if not needle_raw or not isinstance(needle_raw, str):
                    continue
                needle_norm = _normalize(needle_raw)
                if not needle_norm.strip():
                    continue
                cjk_dominant = _is_cjk_dominant(needle_norm)
                min_len = CJK_MIN_LEN if cjk_dominant else ASCII_MIN_LEN
                stopword_set = STOPWORDS_CJK if cjk_dominant else STOPWORDS_ASCII

                for haystack_field, haystack_raw in haystack_fields_for(frm):
                    skip_stats["raw_combinations_checked"] += 1
                    if not haystack_raw or not isinstance(haystack_raw, str):
                        skip_stats["empty_needle_or_haystack"] += 1
                        continue
                    haystack_norm = _normalize(haystack_raw)

                    if len(needle_norm) < min_len:
                        skip_stats["length_floor"] += 1
                        continue
                    if needle_norm in stopword_set:
                        skip_stats["stopword"] += 1
                        continue

                    match_start = None
                    if cjk_dominant:
                        hit = needle_norm in haystack_norm
                    else:
                        ascii_match = _ascii_word_boundary_search(needle_norm, haystack_norm)
                        hit = ascii_match is not None
                        if hit:
                            match_start = ascii_match.start()
                        elif needle_norm in haystack_norm:
                            skip_stats["ascii_boundary"] += 1

                    if not hit:
                        continue

                    skip_stats["raw_hits_before_declared_dep_filter"] += 1
                    snippet = _extract_snippet(haystack_raw, needle_norm, match_start=match_start)
                    conf = confidence_for(needle_field, haystack_field, cjk_dominant)
                    matched_combinations.append(
                        {
                            "needle_field": needle_field,
                            "needle_value": needle_raw,
                            "haystack_field": haystack_field,
                            "snippet": snippet,
                            "confidence": conf,
                            "cjk_dominant": cjk_dominant,
                        }
                    )

            if not matched_combinations:
                continue

            pair_key = frozenset((frm.get("global_id"), to.get("global_id")))
            if pair_key in declared_pairs:
                skip_stats["declared_dependency_pair"] += 1
                continue

            overall_confidence = max(
                matched_combinations, key=lambda m: CONFIDENCE_RANK[m["confidence"]]
            )["confidence"]

            edges.append(
                {
                    "from_global_id": frm.get("global_id"),
                    "from_project_id": frm.get("project_id"),
                    "from_entry_type": frm.get("entry_type"),
                    "to_global_id": to.get("global_id"),
                    "to_project_id": to.get("project_id"),
                    "to_entry_type": to.get("entry_type"),
                    "same_project": frm.get("project_id") == to.get("project_id"),
                    "confidence": overall_confidence,
                    "matched_combinations": matched_combinations,
                }
            )
    return edges, skip_stats


# ---------------------------------------------------------------------------
# Write surface: write_only_within / atomic_write_within (self-validating) /
# lockfile -- a reduced, independently-maintained copy of
# detect_capability_changes.py's own pattern (see module docstring: not
# imported, on purpose).
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


def _reject_nonfinite_json_constant(constant: str) -> None:
    """`json.load`'s `parse_constant` hook: called instead of returning a
    value whenever the parser meets the bare tokens `NaN`/`Infinity`/
    `-Infinity`. Raising here turns them into a caught ValueError rather
    than a silently-accepted float('nan')/float('inf'). Defense in depth
    for atomic_write_within()'s round-trip self-validation -- _encode_json()
    below already refuses to produce these tokens in the first place via
    `allow_nan=False`, so this only fires if a future change bypasses that."""
    raise ValueError(f"disallowed non-finite JSON constant: {constant}")


def _encode_json(payload: dict) -> bytes:
    """`allow_nan=False`: a catalog.json field carrying a literal `NaN`/
    `Infinity`/`-Infinity` token parses fine under Python's (non-strict)
    default json.loads and can be copied verbatim into an edge's
    from_project_id/to_project_id. Without allow_nan=False, json.dumps
    would silently emit that same bare token here -- valid Python `repr`,
    invalid JSON by spec -- and the round-trip self-validation would not
    catch it either, since json.load is equally permissive by default (see
    _reject_nonfinite_json_constant() above, which closes that second
    door). Raising here, before any byte is ever written, turns a bad
    catalog.json value into a named exit-4 BuildFatal instead of a
    silently-corrupt mention-evidence.json."""
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=False, indent=1, allow_nan=False)
    except ValueError as exc:
        raise BuildFatal("payload_contains_non_finite_number", str(exc))
    return _sanitize_line_separators(text).encode("utf-8")


def atomic_write_within(output_dir: Path, final_path: Path, payload: bytes) -> None:
    """Tempfile + O_EXCL + json.load()-back self-validation + os.replace.

    The self-validation step is the one addition beyond
    detect_capability_changes.py's own atomic_write_within(): the temp file
    is reopened and parsed as JSON BEFORE it is ever allowed to replace the
    previous good output. A write that produced truncated or corrupt bytes
    must never become the visible mention-evidence.json -- see
    test_atomic_write_self_validation_failure_leaves_previous_file_untouched.
    """
    tmp_path = final_path.parent / f".{final_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            with open(str(tmp_path), "r", encoding="utf-8") as fh:
                json.load(fh, parse_constant=_reject_nonfinite_json_constant)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise BuildFatal("write_self_validation_failed", str(exc))
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
    """Deliberate copy of detect_capability_changes.py's acquire_lock(),
    same LOCK_STALE_SECONDS=300 stale-lock recovery -- see module docstring
    LOCKING section for why this is a separate lockfile from
    catalog.json's own .catalog.lock."""
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
            raise BuildFatal("lock_held")
        except OSError as exc:
            # Anything other than "already exists" -- most commonly
            # PermissionError on a read-only output_dir -- is a genuine
            # failure to create the lock, not a lock-held race. Name it
            # explicitly (exit 4, "lock_uncreatable") instead of letting it
            # propagate past cmd_build() to main()'s generic
            # unexpected_error catch-all, which the docstring's exit-4 list
            # already promises callers a named reason for.
            raise BuildFatal("lock_uncreatable", str(exc))
    raise BuildFatal("lock_held")


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
        description="Generate mention-evidence.json: deterministic text-mention "
        "fuzzy evidence derived read-only from catalog.json."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="Scan capabilities[] union wiki_pages[] for text-mention edges and write mention-evidence.json."
    )
    build.add_argument(
        "--catalog",
        type=str,
        default=None,
        help=f"Path to catalog.json (read-only; default: {DEFAULT_CATALOG_PATH}).",
    )
    build.add_argument("--json", action="store_true", help="Print the run summary as one JSON object to stdout.")
    build.add_argument("--quiet", action="store_true", help="Suppress human-readable text; rely on the exit code.")
    return parser


def _emit_error(args: argparse.Namespace, code: int, reason: str, message: str | None = None) -> int:
    if not getattr(args, "quiet", False):
        payload: dict = {"ok": False, "reason": reason}
        if message:
            payload["message"] = message
        if getattr(args, "json", False):
            print(_sanitize_line_separators(json.dumps(payload, ensure_ascii=False)), file=sys.stderr)
        else:
            suffix = f" ({message})" if message else ""
            print(f"error: {reason}{suffix}", file=sys.stderr)
    return code


def cmd_build(args: argparse.Namespace) -> int:
    if args.catalog is not None and not args.catalog.strip():
        return _emit_error(args, 2, "empty_catalog_path")

    catalog_path = Path(args.catalog).expanduser() if args.catalog is not None else DEFAULT_CATALOG_PATH

    # Every fatal path (all catalog reading, all matching) MUST complete
    # before any write is attempted, so a failed run never disturbs a
    # previous successful output.
    try:
        catalog = load_catalog(catalog_path)
    except BuildFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)

    entries, skipped_malformed_capabilities, skipped_malformed_wiki_pages = build_entries(catalog)
    edges, skip_stats = find_candidate_edges(entries)
    skip_stats["skipped_malformed_capabilities"] = skipped_malformed_capabilities
    skip_stats["skipped_malformed_wiki_pages"] = skipped_malformed_wiki_pages

    raw_capabilities = catalog.get("capabilities")
    raw_wiki_pages = catalog.get("wiki_pages")
    entry_counts = {
        "capabilities": len(raw_capabilities) if isinstance(raw_capabilities, list) else 0,
        "wiki_pages": len(raw_wiki_pages) if isinstance(raw_wiki_pages, list) else 0,
        "total_entries": len(entries),
    }
    gate_config = {
        "ascii_min_len": ASCII_MIN_LEN,
        "cjk_min_len": CJK_MIN_LEN,
        "stopwords_ascii_count": len(STOPWORDS_ASCII),
        "stopwords_cjk_count": len(STOPWORDS_CJK),
    }

    output_dir = DEFAULT_OUTPUT_DIR
    if output_dir == _PRODUCTION_DEFAULT_OUTPUT_DIR:
        # Unsuppressible, same mechanism as discover_capability_candidates.
        # py's own --all-projects warning: no --quiet gate, printed
        # unconditionally before any write. Only fires when output_dir is
        # still this module's literal production default -- a caller (today,
        # only this file's own test suite, by rebinding DEFAULT_OUTPUT_DIR
        # itself) who has redirected output elsewhere sees nothing here.
        print(
            "NOTICE: writing to manifests/cross-project-catalog/mention-evidence.json, this tool's own "
            "default production path. M8-1's independent authorization gate (M8-DESIGN-FINAL-2026-08-23.md "
            "section 3.2.4) has not been granted: the Gate C validation run against the real catalog.json "
            "produced only 3 candidate edges total, all 3 same-project and 0 cross-project "
            "(m8-gate-c-validation-STAGED-review-only/M8-GATE-C-VALIDATION-REPORT-2026-08-23.md section 1). This output should not be treated as "
            "production-authoritative by anything that reads it.",
            file=sys.stderr,
        )
    try:
        os.makedirs(str(output_dir), mode=0o700, exist_ok=True)
    except OSError as exc:
        return _emit_error(args, 4, "output_dir_uncreatable", str(exc))
    if os.path.islink(str(output_dir)):
        return _emit_error(args, 4, "output_dir_is_symlink")

    final_path, reason = write_only_within(output_dir, str(output_dir / OUTPUT_NAME))
    if reason or final_path is None:
        return _emit_error(args, 4, reason or "invalid_path")
    output_dir = final_path.parent

    try:
        lock_path = acquire_lock(output_dir)
    except BuildFatal as exc:
        return _emit_error(args, 4, exc.reason)

    try:
        generated_at = now_iso()
        payload = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "generator": {"script": SCRIPT_REL_PATH, "version": GENERATOR_VERSION},
            "generated_at": generated_at,
            "catalog_path": str(catalog_path),
            "catalog_generated_at": catalog.get("generated_at") if _is_str(catalog.get("generated_at")) else None,
            "catalog_verified_at": catalog.get("verified_at") if _is_str(catalog.get("verified_at")) else None,
            "entry_counts": entry_counts,
            "gate_config": gate_config,
            "skip_stats": skip_stats,
            "edges": edges,
        }
        try:
            encoded = _encode_json(payload)
            if len(encoded) > MAX_OUTPUT_BYTES:
                raise BuildFatal(
                    "output_exceeds_query_readable_size",
                    f"encoded payload is {len(encoded)} bytes, exceeding the "
                    f"{MAX_OUTPUT_BYTES}-byte limit query_mention_evidence.py's own MAX_BYTES enforces on read",
                )
            atomic_write_within(output_dir, final_path, encoded)
        except BuildFatal as exc:
            return _emit_error(args, 4, exc.reason, exc.message)
    finally:
        release_lock(lock_path)

    if args.json and not args.quiet:
        summary = {
            "ok": True,
            "output": str(final_path),
            "entry_counts": entry_counts,
            "edge_count": len(edges),
            "skip_stats": skip_stats,
        }
        print(_sanitize_line_separators(json.dumps(summary, ensure_ascii=False, indent=1)))
    elif not args.quiet:
        print(
            f"scanned {entry_counts['total_entries']} entries "
            f"({entry_counts['capabilities']} capabilities, {entry_counts['wiki_pages']} wiki_pages); "
            f"found {len(edges)} text-mention edge(s)"
        )
        print(f"wrote {final_path}")

    return 0


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "build":
        return 2
    try:
        return cmd_build(args)
    except BuildFatal as exc:
        return _emit_error(args, 4, exc.reason, exc.message)
    except BaseException as exc:
        _emit_error(args, 4, "unexpected_error", str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
