#!/usr/bin/env python3
"""Unit tests for query_catalog.py (M6 of the cross-project catalog plan).

Covers: the three-tier ranking (exact identity above identity-substring
above summary-only) and its deterministic tie-break, Unicode-correct
case-insensitivity (casefold, not lower) and NFC folding, matching across
BOTH capabilities[] and wiki_pages[], every catalog-unavailable failure mode
(missing / unreadable / FIFO / directory / oversized / unparseable /
duplicate-key / malformed), staleness reporting including the
missing-and-unparseable-count-as-stale rule, the --json U+2028/U+2029 output
boundary, terminal-injection scrubbing of another project's text in human
output, and -- most importantly -- a pair of negative controls proving the
script has no write path at all: an OS-level read-only tree that comes back
byte-identical, and an os.open()/builtins.open() interceptor that fails the
test if any write flag or write mode is ever requested.

Run with: /usr/bin/python3 -m unittest orca-context-bridge/scripts/test_query_catalog.py -v
(from the knowledge root), or plain `/usr/bin/python3 test_query_catalog.py`
from this directory.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import query_catalog as qc  # noqa: E402


NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _capability(**overrides) -> dict:
    entry = {
        "project_id": "proj/alpha",
        "id": "wiki-edit-guard",
        "kind": "script",
        "name": "wiki_edit_guard.py",
        "path": "scripts/wiki_edit_guard.py",
        "summary": "Pre-write barrier for a hand-maintained catalog.",
        "last_verified_at": "2026-08-22T05:13:40Z",
        "global_id": "proj/alpha#wiki-edit-guard",
        "ref_key": "proj/alpha:script:wiki_edit_guard.py",
        "depends_on": [],
    }
    entry.update(overrides)
    return entry


def _page(**overrides) -> dict:
    entry = {
        "project_id": "proj/beta",
        "id": "mail-topology",
        "title": "Mail topology",
        "path": "wiki/mail-topology.md",
        "summary": "Verified mail node layout.",
        "status": "read-only-verified",
        "global_id": "proj/beta#page:mail-topology",
    }
    entry.update(overrides)
    return entry


def make_catalog(capabilities=None, wiki_pages=None, **overrides) -> dict:
    """A catalog shaped like the real one at
    manifests/cross-project-catalog/catalog.json -- same top-level key names
    and the same per-entry field names, so a schema drift in the real
    generator shows up here as a failing test rather than as a query that
    silently stops matching."""
    catalog = {
        "schema_version": qc.CATALOG_SCHEMA_VERSION_SUPPORTED,
        "generated_at": "2026-08-22T11:30:00Z",
        "verified_at": "2026-08-22T11:30:00Z",
        "content_fingerprint": "0" * 64,
        "generator": {"script": "…/build_cross_project_catalog.py", "version": "1.0.0", "sha256": "1" * 64},
        "capabilities": [_capability()] if capabilities is None else capabilities,
        "wiki_pages": [_page()] if wiki_pages is None else wiki_pages,
        "capability_reverse_index": {},
        "unresolved_references": [],
    }
    catalog.update(overrides)
    return catalog


def write_catalog(path: Path, catalog: object) -> Path:
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _make_readonly(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(str(root), topdown=False):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o444)
        os.chmod(dirpath, 0o555)


def _make_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(str(root), topdown=True):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            try:
                os.chmod(os.path.join(dirpath, name), 0o644)
            except OSError:
                pass


def _snapshot(root: Path) -> dict:
    """Recursive lstat snapshot (mode, size, mtime_ns) of everything under
    root -- strictly stronger than "no PermissionError was raised", because
    it also catches a stray cache, lock, or tmp file."""
    out: dict = {}
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        for name in list(dirnames) + list(filenames):
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            out[full] = (st.st_mode, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    return out


def run_cli(argv: list) -> tuple:
    """Drive the real CLI entry point and capture both streams."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = qc.main(argv)
    return code, out.getvalue(), err.getvalue()


def search(catalog: dict, keyword: str, **kwargs) -> dict:
    kwargs.setdefault("now", NOW)
    return qc.search_catalog(catalog, keyword, Path("/tmp/does-not-matter/catalog.json"), **kwargs)


# ---------------------------------------------------------------------------
# T-00 negative control -- mandatory. If this fails, the filesystem is
# ignoring permission bits (running as root, or a permission-ignoring
# volume) and T-90's read-only isolation proof would pass vacuously.
# Do not "fix" a failure here by deleting the control.
# ---------------------------------------------------------------------------


class NegativeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="qc-negctrl-"))
        write_catalog(self.tmp / "catalog.json", make_catalog())
        _make_readonly(self.tmp)

    def tearDown(self) -> None:
        _make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cannot_create_a_new_file_in_the_readonly_dir(self) -> None:
        with self.assertRaises(PermissionError):
            (self.tmp / "new-file.json").write_text("{}", encoding="utf-8")

    def test_cannot_overwrite_the_catalog_in_the_readonly_dir(self) -> None:
        with self.assertRaises(PermissionError):
            (self.tmp / "catalog.json").write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# T-01..T-05: ranking, case-insensitivity, and both entry lists.
# ---------------------------------------------------------------------------


class RankingTests(unittest.TestCase):
    def test_t01_exact_identity_outranks_substring_and_summary(self) -> None:
        """The load-bearing ordering claim. `alpha` is planted LAST in the
        list and with the alphabetically last project_id, so if ranking were
        not applied it would sort last -- passing here cannot be an accident
        of insertion or alphabetical order."""
        catalog = make_catalog(
            capabilities=[
                _capability(
                    project_id="proj/aaa",
                    id="something-else",
                    name="other.py",
                    summary="mentions alpha in passing",
                    global_id="proj/aaa#something-else",
                ),
                _capability(
                    project_id="proj/bbb",
                    id="alpha-helper",
                    name="alpha_helper.py",
                    summary="no keyword here",
                    global_id="proj/bbb#alpha-helper",
                ),
                _capability(
                    project_id="proj/zzz",
                    id="alpha",
                    name="alpha.py",
                    summary="no keyword here",
                    global_id="proj/zzz#alpha",
                ),
            ],
            wiki_pages=[],
        )
        result = search(catalog, "alpha")
        self.assertEqual(result["total_matches"], 3)
        self.assertEqual([m["rank"] for m in result["matches"]], ["exact", "identity-substring", "summary-substring"])
        self.assertEqual([m["id"] for m in result["matches"]], ["alpha", "alpha-helper", "something-else"])

    def test_t02_matched_fields_report_every_hit_but_rank_is_the_best_one(self) -> None:
        catalog = make_catalog(
            capabilities=[
                _capability(
                    id="guard", name="guard.py", summary="the guard rejects writes",
                    # project_id/global_id deliberately do NOT contain "guard",
                    # so this test isolates id/name/summary matching; the
                    # sibling test below covers project_id/global_id matching.
                    project_id="proj/other", global_id="proj/other#zzz-item",
                )
            ],
            wiki_pages=[],
        )
        match = search(catalog, "guard")["matches"][0]
        self.assertEqual(match["rank"], "exact")
        self.assertEqual(match["matched_fields"], ["id", "name", "summary"])

    def test_t02b_global_id_and_project_id_are_searchable_and_round_trip(self) -> None:
        """global_id is the catalog's own documented join key and is printed
        on every result row -- copy-a-hit-and-search-it is the natural next
        query an agent runs. Before this fix, feeding the tool's own printed
        global_id or project_id back into it produced a false "no match"."""
        catalog = make_catalog(
            capabilities=[
                _capability(
                    project_id="orca/完善orca", id="wiki-freshness-check", name="check_wiki_freshness.py",
                    summary="unrelated text", global_id="orca/完善orca#wiki-freshness-check",
                )
            ],
            wiki_pages=[],
        )
        by_global_id = search(catalog, "orca/完善orca#wiki-freshness-check")
        self.assertEqual(by_global_id["total_matches"], 1)
        self.assertEqual(by_global_id["matches"][0]["rank"], "exact")
        self.assertIn("global_id", by_global_id["matches"][0]["matched_fields"])

        by_project_id = search(catalog, "orca/完善orca")
        self.assertEqual(by_project_id["total_matches"], 1)
        self.assertIn("project_id", by_project_id["matches"][0]["matched_fields"])

    def test_t03_case_insensitive_via_casefold_not_lower(self) -> None:
        """'STRASSE'.lower() is 'strasse' but 'straße'.lower() is 'straße',
        so a lower()-based implementation fails this and casefold passes."""
        catalog = make_catalog(capabilities=[_capability(id="strasse-guard", name="straße", summary="x")], wiki_pages=[])
        self.assertEqual("straße".lower(), "straße")  # pins why this test is not vacuous
        result = search(catalog, "STRASSE")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["matches"][0]["rank"], "exact")

    def test_t04_nfc_folding_matches_a_decomposed_catalog_entry(self) -> None:
        """macOS path enumeration can hand back decomposed text; a composed
        keyword must still match it. Without NFC folding the two strings are
        unequal byte-for-byte and the query silently misses."""
        decomposed = "café-runner"  # e + COMBINING ACUTE
        composed = "café-runner"
        self.assertNotEqual(decomposed, composed)
        catalog = make_catalog(capabilities=[_capability(id="cafe-runner", name=decomposed, summary="x")], wiki_pages=[])
        result = search(catalog, composed)
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["matches"][0]["rank"], "exact")

    def test_t05_searches_capabilities_and_wiki_pages_together(self) -> None:
        catalog = make_catalog(
            capabilities=[_capability(id="capacity-preflight", name="agent_capacity.py", summary="x")],
            wiki_pages=[_page(id="capacity-preflight", title="Capacity preflight", summary="y")],
        )
        result = search(catalog, "capacity")
        self.assertEqual(result["total_matches"], 2)
        self.assertEqual({m["entry_type"] for m in result["matches"]}, {"capability", "wiki_page"})
        capability = next(m for m in result["matches"] if m["entry_type"] == "capability")
        page = next(m for m in result["matches"] if m["entry_type"] == "wiki_page")
        self.assertEqual(capability["kind"], "script")
        self.assertNotIn("kind", page)  # a page has no kind; the field is omitted, not nulled
        self.assertEqual(page["title"], "Capacity preflight")
        self.assertEqual(page["status"], "read-only-verified")

    def test_t06_page_title_is_an_identity_field_summary_is_not(self) -> None:
        catalog = make_catalog(
            capabilities=[],
            wiki_pages=[
                _page(id="p-one", title="Topology", summary="z", global_id="g1"),
                _page(id="p-two", title="Unrelated", summary="about topology", global_id="g2"),
            ],
        )
        result = search(catalog, "topology")
        self.assertEqual([m["rank"] for m in result["matches"]], ["exact", "summary-substring"])
        self.assertEqual([m["id"] for m in result["matches"]], ["p-one", "p-two"])

    def test_t07_ordering_is_deterministic_and_total(self) -> None:
        """Same rank, same project, same type: global_id then catalog order
        breaks the tie, so repeated runs cannot reorder."""
        catalog = make_catalog(
            capabilities=[
                _capability(id="k-b", name="b", summary="hit", global_id="g-b"),
                _capability(id="k-a", name="a", summary="hit", global_id="g-a"),
            ],
            wiki_pages=[_page(project_id="proj/alpha", id="k-c", title="c", summary="hit", global_id="g-c")],
        )
        first = [m["global_id"] for m in search(catalog, "hit")["matches"]]
        second = [m["global_id"] for m in search(catalog, "hit")["matches"]]
        self.assertEqual(first, second)
        # capability sorts before wiki_page at equal rank/project, then by global_id.
        self.assertEqual(first, ["g-a", "g-b", "g-c"])

    def test_t08_non_string_fields_are_skipped_not_coerced(self) -> None:
        catalog = make_catalog(
            capabilities=[_capability(id=12345, name=None, summary=["12345"], global_id="g")], wiki_pages=[]
        )
        self.assertEqual(search(catalog, "12345")["total_matches"], 0)

    def test_t09_limit_truncates_the_list_but_never_the_total(self) -> None:
        catalog = make_catalog(
            capabilities=[
                _capability(id=f"cap-{i}", name=f"n{i}", summary="shared", global_id=f"g{i}") for i in range(5)
            ],
            wiki_pages=[],
        )
        result = search(catalog, "shared", limit=2)
        self.assertEqual(result["total_matches"], 5)
        self.assertEqual(result["returned"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["matches"]), 2)

    def test_t10_keyword_is_matched_verbatim_not_stripped(self) -> None:
        """Only the EMPTINESS check strips; a caller who searched for a
        keyword with a space in it gets exactly that query."""
        catalog = make_catalog(capabilities=[_capability(summary="alpha beta gamma")], wiki_pages=[])
        self.assertEqual(search(catalog, "alpha beta")["total_matches"], 1)
        self.assertEqual(search(catalog, "alpha  beta")["total_matches"], 0)


# ---------------------------------------------------------------------------
# T-20..T-26: staleness reporting.
# ---------------------------------------------------------------------------


class StalenessTests(unittest.TestCase):
    def test_t20_fresh_catalog_reports_age_and_is_not_stale(self) -> None:
        stamp = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = search(make_catalog(verified_at=stamp), "wiki")
        self.assertEqual(result["age_seconds"], 7200)
        self.assertEqual(result["age_human"], "2h")
        self.assertFalse(result["stale"])
        self.assertEqual(result["warnings"], [])

    def test_t21_old_catalog_is_stale_against_the_default_threshold(self) -> None:
        stamp = (NOW - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = search(make_catalog(verified_at=stamp), "wiki")
        self.assertEqual(result["age_human"], "1d")
        self.assertTrue(result["stale"])
        self.assertEqual(result["stale_after_seconds"], qc.DEFAULT_STALE_AFTER_HOURS * 3600)

    def test_t22_threshold_is_configurable(self) -> None:
        stamp = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        catalog = make_catalog(verified_at=stamp)
        self.assertFalse(search(catalog, "wiki", stale_after_seconds=3 * 3600)["stale"])
        self.assertTrue(search(catalog, "wiki", stale_after_seconds=1 * 3600)["stale"])

    def test_t23_missing_verified_at_counts_as_stale(self) -> None:
        """Unprovable freshness must never read as proven freshness -- the
        entire value of the field is deciding whether a NO-MATCH is
        trustworthy."""
        catalog = make_catalog()
        del catalog["verified_at"]
        result = search(catalog, "wiki")
        self.assertIsNone(result["age_seconds"])
        self.assertEqual(result["age_human"], "unknown")
        self.assertTrue(result["stale"])
        self.assertEqual([w["code"] for w in result["warnings"]], [qc.WARN_VERIFIED_AT_MISSING])

    def test_t24_unparseable_verified_at_counts_as_stale(self) -> None:
        result = search(make_catalog(verified_at="last tuesday"), "wiki")
        self.assertIsNone(result["age_seconds"])
        self.assertTrue(result["stale"])
        self.assertEqual([w["code"] for w in result["warnings"]], [qc.WARN_VERIFIED_AT_UNPARSEABLE])

    def test_t25_future_verified_at_is_warned_and_IS_stale(self) -> None:
        """A future verified_at is exactly as unprovable as a missing or
        unparseable one -- clock skew, a corrupt field, or a year-9999
        value chosen specifically to never expire are all indistinguishable
        from here, so it must count as stale like its two siblings above
        (T-23, T-24), not be treated as trustworthy just because it parsed."""
        stamp = (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = search(make_catalog(verified_at=stamp), "wiki")
        self.assertEqual(result["age_seconds"], -3600)
        self.assertEqual(result["age_human"], "in the future")
        self.assertTrue(result["stale"])
        self.assertEqual([w["code"] for w in result["warnings"]], [qc.WARN_VERIFIED_AT_IN_FUTURE])

        # Not just "an hour ahead" -- a value with no real expiry at all
        # (the case the independent review actually constructed) must not
        # slip through either.
        far_future = search(make_catalog(verified_at="9999-12-31T23:59:59Z"), "wiki")
        self.assertTrue(far_future["stale"])

    def test_t26_human_output_states_the_age_and_shouts_when_stale(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="qc-stale-"))
        try:
            stamp = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
            path = write_catalog(tmp / "catalog.json", make_catalog(verified_at=stamp))
            code, out, _ = run_cli(["search", "wiki", "--catalog", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("catalog last verified 3d ago", out)
            self.assertIn("STALE (> 6h)", out)
            self.assertIn("build_cross_project_catalog.py build", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_t27_humanize_age_boundaries(self) -> None:
        self.assertEqual(qc.humanize_age(None), "unknown")
        self.assertEqual(qc.humanize_age(-1), "in the future")
        self.assertEqual(qc.humanize_age(0), "0s")
        self.assertEqual(qc.humanize_age(59), "59s")
        self.assertEqual(qc.humanize_age(60), "1m")
        self.assertEqual(qc.humanize_age(3599), "59m")
        self.assertEqual(qc.humanize_age(3600), "1h")
        self.assertEqual(qc.humanize_age(86399), "23h")
        self.assertEqual(qc.humanize_age(86400), "1d")


# ---------------------------------------------------------------------------
# T-40..T-49: catalog-unavailable failure modes. Every one must be a clean
# reason code and a non-zero exit, never a traceback, and never exit 1 --
# "I could not look" must not be spelled the same way as "I found nothing".
# ---------------------------------------------------------------------------


class CatalogFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="qc-fail-"))

    def tearDown(self) -> None:
        _make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reason(self, path: object, keyword: str = "wiki") -> tuple:
        code, _, err = run_cli(["search", keyword, "--catalog", str(path), "--json"])
        payload = json.loads(err) if err.strip() else {}
        return code, payload.get("reason")

    def test_t40_missing_file(self) -> None:
        self.assertEqual(self._reason(self.tmp / "nope.json"), (4, "catalog_missing"))

    def test_t41_missing_parent_directory(self) -> None:
        self.assertEqual(self._reason(self.tmp / "no-such-dir" / "catalog.json"), (4, "catalog_missing"))

    def test_t42_path_is_a_directory(self) -> None:
        (self.tmp / "adir").mkdir()
        self.assertEqual(self._reason(self.tmp / "adir"), (4, "catalog_not_a_regular_file"))

    def test_t43_path_is_a_fifo_and_does_not_hang(self) -> None:
        """O_NONBLOCK is what makes this terminate: a blocking open() of a
        writer-less FIFO sleeps in the kernel, inside os.open() itself,
        before any S_ISREG check downstream could run. If this test hangs
        rather than fails, that flag was dropped."""
        fifo = self.tmp / "catalog.json"
        os.mkfifo(str(fifo))
        self.addCleanup(lambda: os.path.exists(str(fifo)) and os.unlink(str(fifo)))
        self.assertEqual(self._reason(fifo), (4, "catalog_not_a_regular_file"))

    def test_t44_unreadable_file(self) -> None:
        path = write_catalog(self.tmp / "catalog.json", make_catalog())
        os.chmod(str(path), 0o000)
        code, reason = self._reason(path)
        self.assertEqual(code, 4)
        self.assertEqual(reason, "catalog_unreadable")

    def test_t45_unparseable_json(self) -> None:
        path = self.tmp / "catalog.json"
        path.write_text("{not json at all", encoding="utf-8")
        self.assertEqual(self._reason(path), (4, "catalog_unparseable"))

    def test_t46_invalid_utf8(self) -> None:
        path = self.tmp / "catalog.json"
        path.write_bytes(b'{"capabilities": [], "wiki_pages": [], "x": "\xff\xfe"}')
        self.assertEqual(self._reason(path), (4, "catalog_unparseable"))

    def test_t47_top_level_is_not_an_object(self) -> None:
        path = write_catalog(self.tmp / "catalog.json", ["not", "a", "catalog"])
        self.assertEqual(self._reason(path), (4, "catalog_malformed"))

    def test_t48_neither_searchable_list_is_present(self) -> None:
        """A document with no capabilities[] and no wiki_pages[] is not a
        catalog with a problem, it is not a catalog. Answering "0 matches"
        here would be the exact false negative exit 4 exists to prevent."""
        path = write_catalog(self.tmp / "catalog.json", {"schema_version": 1, "verified_at": "2026-08-22T11:30:00Z"})
        self.assertEqual(self._reason(path), (4, "catalog_malformed"))

    def test_t49_non_finite_json_constants_are_rejected(self) -> None:
        """json.loads's default constant handling is a non-standard Python
        extension accepting bare NaN/Infinity/-Infinity tokens -- the real
        aggregator's json.dumps() can never emit one. Left unrejected, it
        flows straight through into this tool's OWN --json output as an
        RFC-8259-invalid bare literal a strict downstream parser rejects."""
        for token in ("NaN", "Infinity", "-Infinity"):
            path = self.tmp / "catalog.json"
            path.write_text(f'{{"schema_version": {token}, "capabilities": [], "wiki_pages": []}}', encoding="utf-8")
            self.assertEqual(self._reason(path), (4, "catalog_unparseable"), token)

    def test_t49_duplicate_top_level_key_is_refused(self) -> None:
        """json.dumps cannot produce one, so a duplicate means the catalog
        was hand-edited; Python would otherwise silently keep the last."""
        path = self.tmp / "catalog.json"
        path.write_text('{"capabilities": [], "capabilities": [], "wiki_pages": []}', encoding="utf-8")
        self.assertEqual(self._reason(path), (4, "catalog_unparseable"))

    def test_t50_oversized_catalog_is_refused_not_read(self) -> None:
        path = self.tmp / "catalog.json"
        with mock.patch.object(qc, "MAX_CATALOG_BYTES", 32):
            write_catalog(path, make_catalog())
            self.assertGreater(path.stat().st_size, 32)
            self.assertEqual(self._reason(path), (4, "catalog_too_large"))

    def test_t51_one_missing_list_still_searches_the_other_with_a_warning(self) -> None:
        catalog = make_catalog(wiki_pages=[])
        del catalog["wiki_pages"]
        result = search(catalog, "wiki")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["counts"]["wiki_pages"], 0)
        self.assertIn(qc.WARN_WIKI_PAGES_NOT_A_LIST, [w["code"] for w in result["warnings"]])

    def test_t52_non_dict_entries_are_counted_and_skipped(self) -> None:
        catalog = make_catalog(capabilities=[_capability(), "junk", 7, None])
        result = search(catalog, "wiki")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["counts"]["skipped_entries"], 3)
        self.assertIn(qc.WARN_SKIPPED_NON_DICT_ENTRIES, [w["code"] for w in result["warnings"]])

    def test_t53_unknown_schema_version_warns_but_still_answers(self) -> None:
        result = search(make_catalog(schema_version=99), "wiki")
        self.assertEqual(result["total_matches"], 1)
        self.assertIn(qc.WARN_SCHEMA_UNSUPPORTED, [w["code"] for w in result["warnings"]])


# ---------------------------------------------------------------------------
# T-60..T-64: CLI surface and exit codes.
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="qc-cli-"))
        self.path = write_catalog(self.tmp / "catalog.json", make_catalog())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t60_exit_zero_on_match_one_on_no_match(self) -> None:
        hit, _, _ = run_cli(["search", "wiki", "--catalog", str(self.path)])
        miss, out, _ = run_cli(["search", "no-such-thing-anywhere", "--catalog", str(self.path)])
        self.assertEqual(hit, 0)
        self.assertEqual(miss, 1)
        self.assertIn('no match for "no-such-thing-anywhere"', out)

    def test_t60b_exit_3_on_no_match_with_a_partial_search(self) -> None:
        """A zero-match result where capabilities[] (or wiki_pages[]) was
        absent/malformed used to be exit 1 -- indistinguishable, under
        --quiet, from a genuinely complete search that found nothing. For
        a tool whose whole purpose is "does this already exist?", that
        conflation is the one silent-false-negative shape this tool's own
        design elsewhere refuses to allow. Exit 3 makes it distinguishable
        by exit code alone, with no need to read warnings/--json."""
        partial_path = write_catalog(
            self.tmp / "partial.json",
            make_catalog(capabilities={"not": "a list"}, wiki_pages=[]),
        )
        code, out, _ = run_cli(["search", "needle-not-anywhere", "--catalog", str(partial_path)])
        self.assertEqual(code, 3)
        self.assertIn('no match for "needle-not-anywhere"', out)

        quiet_code, quiet_out, _ = run_cli(
            ["search", "needle-not-anywhere", "--catalog", str(partial_path), "--quiet"]
        )
        self.assertEqual(quiet_code, 3)  # the distinction survives --quiet
        self.assertEqual(quiet_out, "")

        # Contrast: a COMPLETE search that finds nothing is still exit 1,
        # not 3 -- this isn't "any no-match is now exit 3".
        complete_code, _, _ = run_cli(["search", "no-such-thing-anywhere", "--catalog", str(self.path)])
        self.assertEqual(complete_code, 1)

        # A partial search that DOES find something is still exit 0, not 3.
        partial_with_hit_path = write_catalog(
            self.tmp / "partial_hit.json",
            make_catalog(capabilities={"not": "a list"}, wiki_pages=[_page(id="wiki-topology", title="wiki topology")]),
        )
        found_code, _, _ = run_cli(["search", "wiki-topology", "--catalog", str(partial_with_hit_path)])
        self.assertEqual(found_code, 0)

    def test_t61_empty_or_whitespace_keyword_is_a_usage_error(self) -> None:
        for keyword in ("", "   ", "\t"):
            code, _, err = run_cli(["search", keyword, "--catalog", str(self.path), "--json"])
            self.assertEqual(code, 2, keyword)
            self.assertEqual(json.loads(err)["reason"], "empty_keyword")

    def test_t62_quiet_prints_nothing_on_every_path(self) -> None:
        for argv, expected in (
            (["search", "wiki", "--catalog", str(self.path), "--quiet"], 0),
            (["search", "zzz-nothing", "--catalog", str(self.path), "--quiet"], 1),
            (["search", " ", "--catalog", str(self.path), "--quiet"], 2),
            (["search", "wiki", "--catalog", str(self.tmp / "gone.json"), "--quiet"], 4),
        ):
            code, out, err = run_cli(argv)
            self.assertEqual(code, expected, argv)
            self.assertEqual(out, "", argv)
            self.assertEqual(err, "", argv)

    def test_t63_json_output_is_one_parseable_object_with_the_documented_keys(self) -> None:
        code, out, _ = run_cli(["search", "wiki", "--catalog", str(self.path), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        for key in (
            "ok", "keyword", "catalog", "schema_version", "generated_at", "verified_at", "age_seconds",
            "age_human", "stale", "stale_after_seconds", "counts", "total_matches", "returned", "truncated",
            "warnings", "matches",
        ):
            self.assertIn(key, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["catalog"], str(self.path))

    def test_t64_bad_flag_values_are_rejected_by_argparse(self) -> None:
        for argv in (
            ["search", "wiki", "--limit", "0"],
            ["search", "wiki", "--limit", "-3"],
            ["search", "wiki", "--stale-after-hours", "0"],
            # `inf` passes a naive "> 0" check (inf > 0 is True) and used to
            # reach exit 4 ("could not read the catalog") when the actual
            # problem was the CLI argument -- these must be rejected right
            # here, at argument-parsing time, as a usage error (exit 2).
            ["search", "wiki", "--stale-after-hours", "inf"],
            ["search", "wiki", "--stale-after-hours", "-inf"],
            ["search", "wiki", "--stale-after-hours", "nan"],
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    qc.main(argv)
            self.assertEqual(caught.exception.code, 2, argv)

    def test_t64b_stale_after_hours_overflow_after_multiplication_is_a_usage_error(self) -> None:
        """`positive_number` rejects a non-finite --stale-after-hours, but a
        large FINITE value (e.g. 1e308) still overflows to inf once
        multiplied by 3600 -- the multiplication happens after argparse's
        type= callback runs, so this can't be caught there. It used to
        surface as exit 4 "unexpected_error" (int(inf) raising
        OverflowError), which is the wrong bucket: the catalog was fine,
        the CLI argument was not."""
        code, _, err = run_cli(["search", "wiki", "--catalog", str(self.path), "--stale-after-hours", "1e308", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["reason"], "stale_after_hours_overflow")

    def test_t65_a_bare_invocation_demands_the_subcommand(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                qc.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_t66_default_catalog_path_is_the_real_manifest_location(self) -> None:
        self.assertEqual(
            str(qc.DEFAULT_CATALOG_PATH),
            "/Volumes/Extreme SSD/Orca/manifests/cross-project-catalog/catalog.json",
        )


# ---------------------------------------------------------------------------
# T-70..T-73: output boundaries. Titles and summaries are lifted verbatim
# from OTHER projects' files, so both boundaries carry untrusted text.
# ---------------------------------------------------------------------------


class OutputBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="qc-out-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t70_sanitizer_escapes_both_separators_and_nothing_else(self) -> None:
        raw = f'{{"a": "x{chr(0x2028)}y{chr(0x2029)}z", "b": "中文"}}'
        out = qc._sanitize_line_separators(raw)
        self.assertNotIn(chr(0x2028), out)
        self.assertNotIn(chr(0x2029), out)
        self.assertIn("\\u2028", out)
        self.assertIn("\\u2029", out)
        self.assertEqual(json.loads(out)["a"], f"x{chr(0x2028)}y{chr(0x2029)}z")
        self.assertEqual(json.loads(out)["b"], "中文")

    def test_t71_json_stdout_boundary_escapes_a_planted_separator(self) -> None:
        """A summary in ANOTHER project's file reaches stdout verbatim; a
        raw U+2028 there breaks line-oriented and older JS-family consumers.
        The value must survive the round trip intact after unescaping."""
        poisoned = f"line one{chr(0x2028)}line two{chr(0x2029)}end"
        path = write_catalog(
            self.tmp / "catalog.json",
            make_catalog(capabilities=[_capability(id="poison", name="poison", summary=poisoned)], wiki_pages=[]),
        )
        code, out, _ = run_cli(["search", "poison", "--catalog", str(path), "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn(chr(0x2028), out)
        self.assertNotIn(chr(0x2029), out)
        self.assertIn("\\u2028", out)
        self.assertEqual(json.loads(out)["matches"][0]["summary"], poisoned)

    def test_t72_error_stderr_boundary_is_sanitized_too(self) -> None:
        args = argparse.Namespace(quiet=False, json=True)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = qc._emit_error(args, 4, "unexpected_error", f"boom{chr(0x2028)}bang{chr(0x2029)}")
        err = buf.getvalue()
        self.assertEqual(code, 4)
        self.assertNotIn(chr(0x2028), err)
        self.assertIn("\\u2028", err)
        self.assertEqual(json.loads(err)["message"], f"boom{chr(0x2028)}bang{chr(0x2029)}")

    def test_t73_human_output_cannot_be_driven_by_another_projects_text(self) -> None:
        """Human output is a DIFFERENT boundary with a stricter rule: JSON
        keeps a control character intact (correctly), a terminal would act
        on it. A crafted summary must not forge output lines, erase the
        line above, or emit ANSI."""
        attack = "safe\n[9] FORGED ROW\rerased\x1b[31mred\x1b[0m‮override\ttab"
        path = write_catalog(
            self.tmp / "catalog.json",
            make_catalog(capabilities=[_capability(id="attack", name="attack", summary=attack)], wiki_pages=[]),
        )
        code, out, _ = run_cli(["search", "attack", "--catalog", str(path)])
        self.assertEqual(code, 0)
        for forbidden in ("\r", "\x1b", "\t", "‮"):
            self.assertNotIn(forbidden, out)
        self.assertNotIn("\n[9] FORGED ROW", out)
        self.assertIn("safe [9] FORGED ROW erased", out)  # flattened onto one line, still readable

    def test_t73b_verified_at_header_is_flattened_like_every_other_field(self) -> None:
        """`verified_at` reaches the human-output header straight from the
        catalog (`_print_human`'s first line), unlike every other
        catalog-sourced string in this function which already goes through
        `_flatten_for_terminal()`. An attacker-controlled (or merely
        corrupt) catalog file can set it to anything JSON allows, including
        newlines and raw ESC -- this must not be a second, unhardened
        boundary next to T-73's hardened one."""
        attack_verified_at = "2026-08-22T12:00:00Z\n\x1b[31m[9] exact   FAKE  script  forged\x1b[0m"
        path = write_catalog(
            self.tmp / "catalog.json",
            make_catalog(capabilities=[_capability(id="needle", name="needle")], verified_at=attack_verified_at),
        )
        code, out, _ = run_cli(["search", "needle", "--catalog", str(path)])
        self.assertEqual(code, 0)
        for forbidden in ("\n\x1b", "\x1b[31m", "\r"):
            self.assertNotIn(forbidden, out)
        self.assertNotIn("\n[9] exact   FAKE", out)  # no forged extra result row
        # The header line is still present and still informative -- just flattened.
        self.assertIn("2026-08-22T12:00:00Z", out)

    def test_t74_long_summaries_are_truncated_in_human_output_only(self) -> None:
        long_summary = "keyworded " + ("x" * 5000)
        path = write_catalog(
            self.tmp / "catalog.json",
            make_catalog(capabilities=[_capability(id="long", name="long", summary=long_summary)], wiki_pages=[]),
        )
        _, human, _ = run_cli(["search", "keyworded", "--catalog", str(path)])
        _, machine, _ = run_cli(["search", "keyworded", "--catalog", str(path), "--json"])
        self.assertLess(len(human), 2000)
        self.assertIn(" ...", human)
        self.assertEqual(json.loads(machine)["matches"][0]["summary"], long_summary)  # --json is never truncated

    def test_t75_flatten_preserves_zero_width_joiner(self) -> None:
        """ZWJ/ZWNJ carry real meaning in emoji and Indic/Persian text and
        are deliberately NOT stripped, unlike the bidi override block."""
        self.assertEqual(qc._flatten_for_terminal("a‍b"), "a‍b")
        self.assertEqual(qc._flatten_for_terminal("a‮b"), "a b")


# ---------------------------------------------------------------------------
# T-90..T-93: the read-only proof. This script's entire trust boundary is
# "there is no write path"; these are what make that claim testable rather
# than merely asserted in a docstring.
# ---------------------------------------------------------------------------


class ReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="qc-readonly-"))
        (self.tmp / "manifests").mkdir()
        write_catalog(self.tmp / "manifests" / "catalog.json", make_catalog())
        _make_readonly(self.tmp)
        self.before = _snapshot(self.tmp)

    def tearDown(self) -> None:
        _make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t90_a_run_against_a_readonly_tree_leaves_it_byte_identical(self) -> None:
        path = self.tmp / "manifests" / "catalog.json"
        for argv in (
            ["search", "wiki", "--catalog", str(path)],
            ["search", "wiki", "--catalog", str(path), "--json"],
            ["search", "nothing-matches-this", "--catalog", str(path)],
        ):
            code, _, _ = run_cli(argv)
            self.assertIn(code, (0, 1), argv)
        self.assertEqual(self.before, _snapshot(self.tmp))

    def test_t91_no_file_descriptor_is_ever_opened_for_writing(self) -> None:
        """Strictly stronger than T-90, which only proves nothing was
        written where the test could not write anyway. This proves the
        REQUEST is never made: every os.open() carries O_RDONLY and no
        create/write flag, and builtins.open() is never called in a write
        mode."""
        path = self.tmp / "manifests" / "catalog.json"
        real_os_open = os.open
        real_builtin_open = builtins.open
        seen_flags = []

        def recording_os_open(file, flags, *args, **kwargs):
            seen_flags.append(flags)
            return real_os_open(file, flags, *args, **kwargs)

        def guarded_builtin_open(file, mode="r", *args, **kwargs):
            if any(ch in mode for ch in ("w", "a", "x", "+")):
                raise AssertionError(f"query_catalog opened {file!r} for writing (mode {mode!r})")
            return real_builtin_open(file, mode, *args, **kwargs)

        with mock.patch.object(qc.os, "open", recording_os_open), mock.patch.object(
            builtins, "open", guarded_builtin_open
        ):
            code, _, _ = run_cli(["search", "wiki", "--catalog", str(path), "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(seen_flags, "the catalog read did not go through os.open at all")
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
        for flags in seen_flags:
            self.assertEqual(flags & write_flags, 0, f"os.open requested a write flag: {flags:#o}")
            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)

    def test_t92_source_declares_no_write_api_and_no_trust_anchor_import(self) -> None:
        """Structural pin, parsed rather than grepped -- a substring search
        over the source cannot tell an import from the prose in a docstring
        that merely NAMES the module it does not import.

        Two claims: (1) the whole dependency surface is the standard
        library, so build_startup_bundle.py (the pinned trust anchor,
        untouched by M3/M4 and by this file) and build_cross_project_catalog
        (see this module's docstring for why the two-line sanitizer is
        copied rather than imported) are absent by construction, and any
        future import at all fails this test; (2) no write-capable call or
        flag appears anywhere in the AST.
        """
        source = Path(qc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        self.assertEqual(
            imported,
            {"__future__", "argparse", "json", "math", "os", "stat", "sys", "unicodedata", "datetime", "pathlib", "typing"},
            "query_catalog.py's dependency surface changed",
        )

        def dotted(node) -> str | None:
            parts = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if not isinstance(node, ast.Name):
                return None
            parts.append(node.id)
            return ".".join(reversed(parts))

        # Bare attribute names with no legitimate read-side homonym...
        banned_attrs = {
            "write_text", "write_bytes", "makedirs", "mkdir", "touch", "rmdir", "symlink_to", "hardlink_to",
            "fdopen", "O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC",
        }
        # ...and dotted ones that do (`replace` is also str.replace, which
        # the sanitizer legitimately calls, so only `os.replace` is banned).
        banned_dotted = {
            "os.replace", "os.write", "os.unlink", "os.remove", "os.rename", "os.truncate", "os.mkfifo",
            "os.mknod", "os.link", "os.symlink", "os.chmod", "os.utime",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, banned_attrs, f"write API .{node.attr} appears in query_catalog.py")
                name = dotted(node)
                if name is not None:
                    self.assertNotIn(name, banned_dotted, f"write API {name} appears in query_catalog.py")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                self.fail("query_catalog.py calls builtins.open(); it must read through os.open only")

    def test_t93_search_catalog_is_a_pure_function(self) -> None:
        """No filesystem access, no clock read: `now` is injected, so the
        same catalog and keyword produce byte-identical results, which is
        what makes every staleness test above deterministic."""
        catalog = make_catalog()
        snapshot = json.dumps(catalog, sort_keys=True, ensure_ascii=False)
        first = search(catalog, "wiki")
        second = search(catalog, "wiki")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(snapshot, json.dumps(catalog, sort_keys=True, ensure_ascii=False))  # input untouched


# ---------------------------------------------------------------------------
# T-95: the fixture schema must not drift from the real generator's output.
# ---------------------------------------------------------------------------


class RealCatalogShapeTests(unittest.TestCase):
    """If the real catalog exists on this machine, assert the fixtures above
    still describe it. Skipped rather than failed where it does not -- the
    suite must stay runnable on a machine that has never run the build."""

    def setUp(self) -> None:
        if not qc.DEFAULT_CATALOG_PATH.is_file():
            self.skipTest("no real catalog.json on this machine")
        self.catalog = qc.load_catalog(qc.DEFAULT_CATALOG_PATH)

    def test_t95_real_catalog_carries_every_field_the_fixtures_assume(self) -> None:
        self.assertIsInstance(self.catalog.get("capabilities"), list)
        self.assertIsInstance(self.catalog.get("wiki_pages"), list)
        self.assertIsInstance(self.catalog.get("verified_at"), str)
        self.assertIsNotNone(qc._parse_utc_timestamp(self.catalog["verified_at"]))
        if self.catalog["capabilities"]:
            entry = self.catalog["capabilities"][0]
            for field in ("project_id", "global_id", "id", "kind", "name", "path", "summary"):
                self.assertIn(field, entry, f"real capability rows no longer carry {field!r}")
        if self.catalog["wiki_pages"]:
            entry = self.catalog["wiki_pages"][0]
            for field in ("project_id", "global_id", "id", "title", "path", "summary", "status"):
                self.assertIn(field, entry, f"real wiki_page rows no longer carry {field!r}")

    def test_t96_a_real_search_returns_ranked_results(self) -> None:
        result = qc.search_catalog(
            self.catalog, "wiki", qc.DEFAULT_CATALOG_PATH, now=datetime.now(timezone.utc)
        )
        self.assertGreater(result["total_matches"], 0)
        ranks = [m["rank"] for m in result["matches"]]
        order = {"exact": 0, "identity-substring": 1, "summary-substring": 2}
        self.assertEqual(ranks, sorted(ranks, key=lambda r: order[r]))


if __name__ == "__main__":
    unittest.main()
