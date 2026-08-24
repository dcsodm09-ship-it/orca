#!/usr/bin/env python3
"""Unit tests for build_mention_evidence.py (M8-1 Gate C, generator half).

Run with:
    python3 -m unittest test_build_mention_evidence.py -v
(from this directory), or plain `python3 test_build_mention_evidence.py`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_mention_evidence as bme  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_cap(project_id, cid, name=None, summary=None, ref_key=None, depends_on=None, global_id=None) -> dict:
    return {
        "project_id": project_id,
        "id": cid,
        "kind": "script",
        "name": name if name is not None else cid,
        "summary": summary if summary is not None else "",
        "ref_key": ref_key,
        "depends_on": depends_on or [],
        "global_id": global_id or f"{project_id}#{cid}",
    }


def make_page(project_id, pid, title=None, summary=None, global_id=None) -> dict:
    return {
        "project_id": project_id,
        "id": pid,
        "title": title if title is not None else pid,
        "summary": summary if summary is not None else "",
        "global_id": global_id or f"{project_id}#{pid}",
    }


def make_catalog(capabilities=None, wiki_pages=None, generated_at="2026-08-23T00:00:00Z", verified_at="2026-08-23T00:00:00Z") -> dict:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "verified_at": verified_at,
        "capabilities": capabilities if capabilities is not None else [],
        "wiki_pages": wiki_pages if wiki_pages is not None else [],
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _run_main(argv: list) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bme.main(argv)
    return code, out.getvalue(), err.getvalue()


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


class BaseTempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bme-test-"))
        self._orig_output_dir = bme.DEFAULT_OUTPUT_DIR
        self.out_dir = self.tmp / "manifests-output"
        self.out_dir.mkdir()
        bme.DEFAULT_OUTPUT_DIR = self.out_dir

    def tearDown(self) -> None:
        bme.DEFAULT_OUTPUT_DIR = self._orig_output_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_catalog(self, catalog: dict) -> Path:
        path = self.tmp / "catalog.json"
        _write_json(path, catalog)
        return path


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class NormalizeTests(unittest.TestCase):
    def test_nfc_and_casefold(self) -> None:
        # 'e' + combining acute vs precomposed é must normalize equal.
        decomposed = "é"
        precomposed = "é"
        self.assertEqual(bme._normalize(decomposed), bme._normalize(precomposed))

    def test_casefold_handles_sharp_s(self) -> None:
        self.assertEqual(bme._normalize("STRASSE"), bme._normalize("straße"))


# ---------------------------------------------------------------------------
# Gate 1: self-match exclusion
# ---------------------------------------------------------------------------


class SelfMatchExclusionTests(unittest.TestCase):
    def test_entry_never_matches_itself(self) -> None:
        entries, _, _ = bme.build_entries(
            make_catalog(capabilities=[make_cap("p1", "solo-widget-thing", name="solo-widget-thing", summary="solo-widget-thing does solo-widget-thing")])
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertEqual(edges, [])
        self.assertEqual(skip_stats["self_pair"], 1)


# ---------------------------------------------------------------------------
# Gate 2: length floor (ASCII >= 4, CJK >= 3)
# ---------------------------------------------------------------------------


class LengthFloorGateTests(unittest.TestCase):
    def test_ascii_needle_below_floor_is_rejected(self) -> None:
        # needle "cat" (id) is 3 chars, below ASCII_MIN_LEN=4.
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "cat", name="catnip-tool"),
                    make_cap("p2", "user-of-cat", name="user-of-cat", summary="depends on cat somehow"),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertGreater(skip_stats["length_floor"], 0)
        # No edge should target p1#cat via the short "cat" id needle.
        self.assertFalse(any(e["to_global_id"] == "p1#cat" for e in edges))

    def test_ascii_needle_at_floor_is_not_rejected_by_length_gate(self) -> None:
        # needle "tool" (id) is exactly 4 chars == ASCII_MIN_LEN.
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "tool", name="tool-alpha"),
                    make_cap("p2", "user-of-tool", name="user-of-tool", summary="references tool-alpha directly"),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertTrue(any(e["to_global_id"] == "p1#tool" for e in edges))

    def test_cjk_needle_below_floor_is_rejected(self) -> None:
        # CJK needle "报告" (2 chars) below CJK_MIN_LEN=3.
        entries, _, _ = bme.build_entries(
            make_catalog(
                wiki_pages=[
                    make_page("p1", "page-a", title="报告"),
                    make_page("p2", "page-b", title="something", summary="这是一份报告的说明文字"),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertGreater(skip_stats["length_floor"], 0)
        self.assertFalse(any(e["to_global_id"] == "p1#page-a" for e in edges))

    def test_cjk_needle_at_floor_is_not_rejected_by_length_gate(self) -> None:
        # CJK needle "调研笔记" style 3-char title, not a stopword.
        entries, _, _ = bme.build_entries(
            make_catalog(
                wiki_pages=[
                    make_page("p1", "page-a", title="鹏鹏笔记"),
                    make_page("p2", "page-b", title="something", summary="这里提到了鹏鹏笔记的内容"),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertTrue(any(e["to_global_id"] == "p1#page-a" for e in edges))


# ---------------------------------------------------------------------------
# Gate 3: stopword list (whole-needle match only)
# ---------------------------------------------------------------------------


class StopwordGateTests(unittest.TestCase):
    def test_ascii_stopword_needle_is_rejected(self) -> None:
        # id/name "config" is a whole-needle ASCII stopword.
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "config", name="config"),
                    make_cap("p2", "user-of-config", name="user-of-config", summary="reads the config value"),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertGreater(skip_stats["stopword"], 0)
        self.assertFalse(any(e["to_global_id"] == "p1#config" for e in edges))

    def test_ascii_non_stopword_needle_is_not_rejected_by_stopword_gate(self) -> None:
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "widget", name="widget"),
                    make_cap("p2", "user-of-widget", name="user-of-widget", summary="reads the widget value"),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertEqual(skip_stats["stopword"], 0)
        self.assertTrue(any(e["to_global_id"] == "p1#widget" for e in edges))

    def test_cjk_stopword_needle_is_rejected(self) -> None:
        # "服务器" (3 chars) is both >= CJK_MIN_LEN=3 (so it is NOT caught by
        # the length-floor gate first) and a whole-needle CJK stopword,
        # isolating this test to the stopword gate specifically.
        entries, _, _ = bme.build_entries(
            make_catalog(
                wiki_pages=[
                    make_page("p1", "page-a", title="服务器"),
                    make_page("p2", "page-b", title="something", summary="这台服务器很重要"),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertGreater(skip_stats["stopword"], 0)
        self.assertFalse(any(e["to_global_id"] == "p1#page-a" for e in edges))


# ---------------------------------------------------------------------------
# Gate 4a: ASCII word boundary
# ---------------------------------------------------------------------------


class AsciiWordBoundaryGateTests(unittest.TestCase):
    def test_needle_inside_longer_word_is_rejected(self) -> None:
        # needle "guid" (4 chars, not a stopword) must NOT match inside
        # "guide" -- no word boundary at that position.
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "guid", name="guid"),
                    make_cap("p2", "user-of-guide", name="user-of-guide", summary="reads the setup guide carefully"),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertGreater(skip_stats["ascii_boundary"], 0)
        self.assertFalse(any(e["to_global_id"] == "p1#guid" for e in edges))

    def test_needle_flanked_by_boundaries_is_not_rejected(self) -> None:
        # Consumer's own id/name deliberately does NOT contain "guid" glued
        # to another word character (unlike the rejection case above), so
        # the only place "guid" appears is the summary, cleanly
        # space-flanked -- isolating this test to the boundary gate alone.
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "guid", name="guid"),
                    make_cap("p2", "widget-consumer", name="widget-consumer", summary="reads the guid value carefully"),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertEqual(skip_stats["ascii_boundary"], 0)
        self.assertTrue(any(e["to_global_id"] == "p1#guid" for e in edges))


# ---------------------------------------------------------------------------
# Snippet anchoring: the reported `snippet` must come from the SAME
# occurrence that satisfied the ASCII word-boundary gate, not from an
# earlier, gate-rejected occurrence of the same raw substring elsewhere in
# the haystack. Found via independent adversarial re-review of the staged
# candidate: _extract_snippet() used to always anchor on
# hay_casefold.find(needle_normalized) -- the FIRST raw substring
# occurrence -- even when that occurrence was not the one the boundary gate
# actually matched on, silently showing unrelated text as "evidence".
# ---------------------------------------------------------------------------


class SnippetAnchoringTests(unittest.TestCase):
    def test_snippet_anchors_on_the_boundary_matched_occurrence_not_an_earlier_rejected_one(self) -> None:
        # "guid" appears twice in the haystack: first glued inside "guide"
        # (rejected by the boundary gate, far from any real word boundary)
        # and later cleanly space-flanked ("the guid value"). The reported
        # snippet must show the SECOND occurrence -- the one that actually
        # made this an edge -- not the first.
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "guid", name="guid"),
                    make_cap(
                        "p2",
                        "widget-consumer",
                        name="widget-consumer",
                        summary=(
                            "This is the setup guide for our system, please read the guid "
                            "value carefully before running anything else in the pipeline."
                        ),
                    ),
                ]
            )
        )
        edges, _ = bme.find_candidate_edges(entries)
        matching = [e for e in edges if e["to_global_id"] == "p1#guid"]
        self.assertEqual(len(matching), 1)
        snippet = matching[0]["matched_combinations"][0]["snippet"]
        self.assertIn("read the guid value", snippet)
        self.assertNotIn("setup guide", snippet)

    def test_cjk_snippet_still_anchors_on_first_occurrence_no_boundary_rule(self) -> None:
        # CJK substring matching has no boundary rule, so the first raw
        # occurrence IS a genuinely valid match location -- this must keep
        # working exactly as before the fix (match_start=None path).
        entries, _, _ = bme.build_entries(
            make_catalog(
                wiki_pages=[
                    make_page("p1", "page-a", title="独立节点搭建方案"),
                    make_page("p2", "page-b", title="something", summary="参考了独立节点搭建方案的思路"),
                ]
            )
        )
        edges, _ = bme.find_candidate_edges(entries)
        matching = [e for e in edges if e["to_global_id"] == "p1#page-a"]
        self.assertEqual(len(matching), 1)
        snippet = matching[0]["matched_combinations"][0]["snippet"]
        self.assertIn("独立节点搭建方案", snippet)


# ---------------------------------------------------------------------------
# Gate 4b: CJK degradation (real substring hit, but forced-low confidence)
# ---------------------------------------------------------------------------


class CjkDegradationGateTests(unittest.TestCase):
    def test_cjk_hit_is_always_low_confidence_even_from_id_field(self) -> None:
        entries, _, _ = bme.build_entries(
            make_catalog(
                wiki_pages=[
                    make_page("p1", "page-a", title="独立节点搭建方案"),
                    make_page("p2", "page-b", title="something", summary="参考了独立节点搭建方案的思路"),
                ]
            )
        )
        edges, _ = bme.find_candidate_edges(entries)
        matching = [e for e in edges if e["to_global_id"] == "p1#page-a"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["confidence"], "low")

    def test_ascii_id_field_hit_reaches_medium_not_low(self) -> None:
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "widget-alpha", name="widget-alpha"),
                    make_cap("p2", "beta", name="widget-alpha", summary="unrelated summary text"),
                ]
            )
        )
        edges, _ = bme.find_candidate_edges(entries)
        matching = [e for e in edges if e["to_global_id"] == "p1#widget-alpha"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["confidence"], "medium")


# ---------------------------------------------------------------------------
# Declared-dependency exclusion
# ---------------------------------------------------------------------------


class DeclaredDependencyExclusionTests(unittest.TestCase):
    def test_target_global_id_resolved_dependency_excludes_pair(self) -> None:
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "widget-alpha", name="widget-alpha"),
                    make_cap(
                        "p2",
                        "beta",
                        name="beta-consumer",
                        summary="uses widget-alpha directly",
                        depends_on=[{"raw": "p1:widget-alpha", "target_global_id": "p1#widget-alpha", "state": "resolved", "scope": "cross-project"}],
                    ),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertEqual(skip_stats["declared_dependency_pair"], 1)
        self.assertFalse(any(e["to_global_id"] == "p1#widget-alpha" for e in edges))

    def test_ref_key_resolved_dependency_excludes_pair(self) -> None:
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "widget-alpha", name="widget-alpha", ref_key="p1:script:widget-alpha"),
                    make_cap(
                        "p2",
                        "beta",
                        name="beta-consumer",
                        summary="uses widget-alpha directly",
                        depends_on=[{"raw": "widget-alpha", "ref_key": "p1:script:widget-alpha", "state": "resolved", "scope": "cross-project"}],
                    ),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertEqual(skip_stats["declared_dependency_pair"], 1)
        self.assertFalse(any(e["to_global_id"] == "p1#widget-alpha" for e in edges))

    def test_undeclared_pair_is_not_excluded(self) -> None:
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "widget-alpha", name="widget-alpha"),
                    make_cap("p2", "beta", name="beta-consumer", summary="uses widget-alpha directly", depends_on=[]),
                ]
            )
        )
        edges, skip_stats = bme.find_candidate_edges(entries)
        self.assertEqual(skip_stats["declared_dependency_pair"], 0)
        self.assertTrue(any(e["to_global_id"] == "p1#widget-alpha" for e in edges))


# ---------------------------------------------------------------------------
# Edge aggregation: multiple matched combinations -> ONE edge, MAX confidence
# ---------------------------------------------------------------------------


class EdgeAggregationTests(unittest.TestCase):
    def test_multiple_matches_for_same_pair_collapse_to_one_edge_with_max_confidence(self) -> None:
        # from=p2#beta mentions p1#widget-alpha's "id" in its own "name"
        # (-> medium) AND its "name" in its own "summary" (-> low). Both
        # needle fields target the SAME (from,to) pair and must collapse to
        # exactly one edge, whose confidence is the MAX across all hits.
        entries, _, _ = bme.build_entries(
            make_catalog(
                capabilities=[
                    make_cap("p1", "widget-alpha", name="widget-alpha"),
                    make_cap(
                        "p2",
                        "beta",
                        name="widget-alpha",  # matches p1's id (needle_field=id) -> medium
                        summary="also mentions widget-alpha again in prose",  # matches p1's name in summary -> low
                    ),
                ]
            )
        )
        edges, _ = bme.find_candidate_edges(entries)
        matching = [e for e in edges if e["from_global_id"] == "p2#beta" and e["to_global_id"] == "p1#widget-alpha"]
        self.assertEqual(len(matching), 1, "expected exactly one aggregated edge, not one per matched combination")
        self.assertEqual(matching[0]["confidence"], "medium")
        self.assertGreaterEqual(len(matching[0]["matched_combinations"]), 2)


# ---------------------------------------------------------------------------
# Confidence rules for every needle/haystack field combination
# ---------------------------------------------------------------------------


class ConfidenceRuleTests(unittest.TestCase):
    def test_id_needle_is_always_medium_when_ascii(self) -> None:
        self.assertEqual(bme.confidence_for("id", "summary", cjk_dominant=False), "medium")
        self.assertEqual(bme.confidence_for("id", "name", cjk_dominant=False), "medium")

    def test_name_or_title_needle_vs_name_or_title_haystack_is_medium(self) -> None:
        self.assertEqual(bme.confidence_for("name", "name", cjk_dominant=False), "medium")
        self.assertEqual(bme.confidence_for("name", "title", cjk_dominant=False), "medium")
        self.assertEqual(bme.confidence_for("title", "name", cjk_dominant=False), "medium")
        self.assertEqual(bme.confidence_for("title", "title", cjk_dominant=False), "medium")

    def test_name_or_title_needle_vs_summary_haystack_is_low(self) -> None:
        self.assertEqual(bme.confidence_for("name", "summary", cjk_dominant=False), "low")
        self.assertEqual(bme.confidence_for("title", "summary", cjk_dominant=False), "low")

    def test_cjk_dominant_is_always_low_regardless_of_fields(self) -> None:
        for needle_field in ("id", "name", "title"):
            for haystack_field in ("name", "title", "summary"):
                self.assertEqual(bme.confidence_for(needle_field, haystack_field, cjk_dominant=True), "low")


# ---------------------------------------------------------------------------
# Malformed entity rows: counted, not silently dropped
# ---------------------------------------------------------------------------


class MalformedEntityTests(unittest.TestCase):
    def test_non_dict_capability_row_is_counted_and_skipped(self) -> None:
        catalog = make_catalog(capabilities=["not-a-dict", make_cap("p1", "ok-one")])
        entries, skipped_caps, skipped_pages = bme.build_entries(catalog)
        self.assertEqual(skipped_caps, 1)
        self.assertEqual(skipped_pages, 0)
        self.assertEqual(len(entries), 1)

    def test_capability_missing_global_id_is_counted_and_skipped(self) -> None:
        cap = make_cap("p1", "no-gid")
        cap["global_id"] = None
        catalog = make_catalog(capabilities=[cap])
        entries, skipped_caps, _ = bme.build_entries(catalog)
        self.assertEqual(skipped_caps, 1)
        self.assertEqual(entries, [])

    def test_non_dict_wiki_page_row_is_counted_and_skipped(self) -> None:
        catalog = make_catalog(wiki_pages=[42, make_page("p1", "ok-page")])
        entries, skipped_caps, skipped_pages = bme.build_entries(catalog)
        self.assertEqual(skipped_pages, 1)
        self.assertEqual(len(entries), 1)


# ---------------------------------------------------------------------------
# Malformed depends_on / ref_key / target_global_id: catalog.json content a
# different producer script could plausibly emit (still valid JSON, wrong
# shape) must degrade gracefully -- never raise TypeError out of
# build_declared_pairs()/find_candidate_edges(). Found via independent
# adversarial re-review of the staged candidate: `cap.get("depends_on") or
# []` lets a non-list truthy value (e.g. an int) through unchanged, and
# `dep.get("target_global_id")`/`dep.get("ref_key")` were used as dict keys
# / `in dict` operands without an isinstance(..., str) guard, both of which
# are unhashable-type TypeErrors waiting to happen on a list/dict value.
# ---------------------------------------------------------------------------


class MalformedDependsOnTests(unittest.TestCase):
    def test_non_list_depends_on_is_treated_as_no_dependencies(self) -> None:
        cap_a = make_cap("p1", "a", name="a")
        cap_a["depends_on"] = 5  # malformed: not a list
        cap_b = make_cap("p2", "b", name="b")
        entries, _, _ = bme.build_entries(make_catalog(capabilities=[cap_a, cap_b]))
        self.assertEqual(entries[0]["depends_on"], [])
        # Must not raise -- this is the actual regression repro.
        edges, _ = bme.find_candidate_edges(entries)
        self.assertEqual(edges, [])

    def test_non_string_ref_key_does_not_raise(self) -> None:
        cap_a = make_cap("p1", "a", name="a")
        cap_a["ref_key"] = ["not", "a", "string"]  # malformed: unhashable
        cap_b = make_cap(
            "p2",
            "b",
            name="b",
            summary="unrelated",
            depends_on=[{"raw": "x", "ref_key": ["not", "a", "string"], "state": "resolved"}],
        )
        entries, _, _ = bme.build_entries(make_catalog(capabilities=[cap_a, cap_b]))
        # Must not raise TypeError: unhashable type: 'list'.
        edges, _ = bme.find_candidate_edges(entries)
        self.assertEqual(edges, [])

    def test_non_string_target_global_id_does_not_raise(self) -> None:
        cap_a = make_cap("p1", "a", name="a")
        cap_b = make_cap(
            "p2",
            "b",
            name="b",
            summary="unrelated",
            depends_on=[{"raw": "x", "target_global_id": ["not", "a", "string"], "state": "resolved"}],
        )
        entries, _, _ = bme.build_entries(make_catalog(capabilities=[cap_a, cap_b]))
        # Must not raise TypeError: unhashable type: 'list'.
        edges, _ = bme.find_candidate_edges(entries)
        self.assertEqual(edges, [])

    def test_full_cli_run_with_malformed_depends_on_still_exits_0(self) -> None:
        # End-to-end: a catalog with these malformed shapes must still be a
        # SUCCESSFUL run (exit 0), not degrade into exit 4 "unexpected_error".
        cap_a = make_cap("p1", "a", name="a")
        cap_a["depends_on"] = 5
        cap_a["ref_key"] = {"nested": "dict"}
        cap_b = make_cap("p2", "b", name="b", summary="mentions nothing relevant")
        tmp = Path(tempfile.mkdtemp(prefix="bme-malformed-"))
        orig_output_dir = bme.DEFAULT_OUTPUT_DIR
        out_dir = tmp / "out"
        out_dir.mkdir()
        bme.DEFAULT_OUTPUT_DIR = out_dir
        try:
            catalog_path = tmp / "catalog.json"
            _write_json(catalog_path, make_catalog(capabilities=[cap_a, cap_b]))
            code, _out, err = _run_main(["build", "--catalog", str(catalog_path), "--quiet"])
            self.assertEqual(code, 0, err)
        finally:
            bme.DEFAULT_OUTPUT_DIR = orig_output_dir
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Full CLI: fatal paths
# ---------------------------------------------------------------------------


class FatalPathTests(BaseTempDirTestCase):
    def test_missing_catalog_is_exit_4(self) -> None:
        missing = self.tmp / "does-not-exist.json"
        code, _out, err = _run_main(["build", "--catalog", str(missing), "--quiet"])
        self.assertEqual(code, 4)

    def test_corrupt_json_is_exit_4(self) -> None:
        path = self.tmp / "catalog.json"
        path.write_text("{not valid json", encoding="utf-8")
        code, _out, _err = _run_main(["build", "--catalog", str(path), "--quiet"])
        self.assertEqual(code, 4)

    def test_catalog_missing_both_lists_is_exit_4(self) -> None:
        path = self.tmp / "catalog.json"
        _write_json(path, {"schema_version": 1})
        code, _out, _err = _run_main(["build", "--catalog", str(path), "--quiet"])
        self.assertEqual(code, 4)

    def test_empty_catalog_path_is_exit_2(self) -> None:
        code, _out, _err = _run_main(["build", "--catalog", "   ", "--quiet"])
        self.assertEqual(code, 2)

    def test_fatal_catalog_error_leaves_no_output_written(self) -> None:
        missing = self.tmp / "does-not-exist.json"
        _run_main(["build", "--catalog", str(missing), "--quiet"])
        self.assertFalse((self.out_dir / bme.OUTPUT_NAME).exists())

    def test_fatal_error_does_not_disturb_previous_good_output(self) -> None:
        good_path = self.write_catalog(make_catalog(capabilities=[make_cap("p1", "a")]))
        code, _, _ = _run_main(["build", "--catalog", str(good_path), "--quiet"])
        self.assertEqual(code, 0)
        before = (self.out_dir / bme.OUTPUT_NAME).read_bytes()

        missing = self.tmp / "does-not-exist.json"
        code2, _, _ = _run_main(["build", "--catalog", str(missing), "--quiet"])
        self.assertEqual(code2, 4)
        after = (self.out_dir / bme.OUTPUT_NAME).read_bytes()
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# Zero-edges is still a successful (exit 0) run, degradation named in JSON
# ---------------------------------------------------------------------------


class ZeroEdgesStillSuccessTests(BaseTempDirTestCase):
    def test_zero_edges_is_exit_0_with_named_skip_stats(self) -> None:
        path = self.write_catalog(make_catalog(capabilities=[make_cap("p1", "lonely")]))
        code, out, _err = _run_main(["build", "--catalog", str(path), "--json", "--quiet"])
        # --quiet suppresses stdout for --json too; re-run without --quiet
        code, out, _err = _run_main(["build", "--catalog", str(path), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["edge_count"], 0)
        self.assertIn("skip_stats", payload)


# ---------------------------------------------------------------------------
# Atomic write self-validation: a round-trip failure must not corrupt the
# previously-good file (simulated write failure via monkeypatched _encode_json)
# ---------------------------------------------------------------------------


class AtomicWriteSelfValidationTests(BaseTempDirTestCase):
    def test_atomic_write_self_validation_failure_leaves_previous_file_untouched(self) -> None:
        good_path = self.write_catalog(make_catalog(capabilities=[make_cap("p1", "a")]))
        code, _, _ = _run_main(["build", "--catalog", str(good_path), "--quiet"])
        self.assertEqual(code, 0)
        good_bytes = (self.out_dir / bme.OUTPUT_NAME).read_bytes()
        json.loads(good_bytes)  # sanity: really is valid JSON

        # Simulate a write that produces truncated/corrupt bytes: patch
        # _encode_json to return non-JSON bytes for the next call only.
        original_encode = bme._encode_json

        def _broken_encode(payload):
            real = original_encode(payload)
            return real[: len(real) // 2]  # truncate mid-document -> invalid JSON

        bme._encode_json = _broken_encode
        try:
            code2, _, err2 = _run_main(["build", "--catalog", str(good_path), "--quiet"])
        finally:
            bme._encode_json = original_encode

        self.assertEqual(code2, 4)
        after_bytes = (self.out_dir / bme.OUTPUT_NAME).read_bytes()
        self.assertEqual(good_bytes, after_bytes)
        # No leftover temp file either.
        leftovers = [p for p in self.out_dir.iterdir() if p.name.startswith(f".{bme.OUTPUT_NAME}.tmp-")]
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# P3-2: NaN/Infinity in catalog.json must never reach the JSON output as a
# bare (invalid-by-spec) token -- allow_nan=False on the encode side, plus
# a parse_constant guard on the round-trip self-validation as defense in
# depth.
# ---------------------------------------------------------------------------


class NonFiniteValueRejectionTests(BaseTempDirTestCase):
    def test_nan_project_id_is_named_exit_4_not_silently_written(self) -> None:
        # Two entries so an actual edge is produced -- a NaN project_id on
        # an entry with no edges at all would never reach the payload, so
        # this needs cap_a's name to be undeclared-mentioned by cap_b's
        # summary (a real, matched combination) for cap_a's (NaN)
        # project_id to be copied into that edge's to_project_id field.
        catalog = make_catalog(
            capabilities=[
                make_cap("p1", "special-widget", name="special-widget", summary=""),
                make_cap("p2", "consumer", name="consumer-job", summary="wraps special-widget nicely"),
            ]
        )
        # json.loads (default, non-strict) happily parses a bare NaN token;
        # inject one directly into the serialized catalog text so it
        # survives load_catalog() as float('nan') on cap_a's project_id.
        path = self.tmp / "catalog.json"
        text = json.dumps(catalog, ensure_ascii=False, indent=1)
        text = text.replace('"project_id": "p1"', '"project_id": NaN', 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        # Sanity: this file really does parse (Python's json is permissive),
        # and the edge this test depends on really is produced.
        parsed = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotEqual(parsed["capabilities"][0]["project_id"], parsed["capabilities"][0]["project_id"])  # NaN != NaN

        code, _out, err = _run_main(["build", "--catalog", str(path), "--json"])
        self.assertEqual(code, 4, err)
        payload = json.loads(err)
        self.assertEqual(payload["reason"], "payload_contains_non_finite_number")
        self.assertFalse((self.out_dir / bme.OUTPUT_NAME).exists())

    def test_encode_json_raises_build_fatal_for_nan(self) -> None:
        with self.assertRaises(bme.BuildFatal) as ctx:
            bme._encode_json({"x": float("nan")})
        self.assertEqual(ctx.exception.reason, "payload_contains_non_finite_number")

    def test_encode_json_raises_build_fatal_for_infinity(self) -> None:
        with self.assertRaises(bme.BuildFatal) as ctx:
            bme._encode_json({"x": float("inf")})
        self.assertEqual(ctx.exception.reason, "payload_contains_non_finite_number")

    def test_round_trip_guard_rejects_nan_even_if_encode_is_bypassed(self) -> None:
        # Defense in depth: simulate a future _encode_json that forgets
        # allow_nan=False by writing a NaN token directly, and confirm the
        # round-trip self-validation's parse_constant guard still catches
        # it rather than accepting it as a "successful" round trip.
        good_path = self.write_catalog(make_catalog(capabilities=[make_cap("p1", "a")]))
        code, _, _ = _run_main(["build", "--catalog", str(good_path), "--quiet"])
        self.assertEqual(code, 0)
        good_bytes = (self.out_dir / bme.OUTPUT_NAME).read_bytes()

        original_encode = bme._encode_json

        def _nan_encode(payload):
            return b'{"broken": NaN}'

        bme._encode_json = _nan_encode
        try:
            code2, _, err2 = _run_main(["build", "--catalog", str(good_path), "--quiet"])
        finally:
            bme._encode_json = original_encode

        self.assertEqual(code2, 4)
        after_bytes = (self.out_dir / bme.OUTPUT_NAME).read_bytes()
        self.assertEqual(good_bytes, after_bytes)


# ---------------------------------------------------------------------------
# P3-3: the generator must never write an artifact its own query tool can't
# read back -- enforce the same size cap query_mention_evidence.py's own
# MAX_BYTES imposes on read, checked before the write happens.
# ---------------------------------------------------------------------------


class OutputSizeCapTests(BaseTempDirTestCase):
    def test_oversized_payload_is_named_exit_4_before_any_write(self) -> None:
        path = self.write_catalog(make_catalog(capabilities=[make_cap("p1", "a")]))
        original_max = bme.MAX_OUTPUT_BYTES
        bme.MAX_OUTPUT_BYTES = 10  # any real payload exceeds 10 bytes
        try:
            code, _out, err = _run_main(["build", "--catalog", str(path), "--json"])
        finally:
            bme.MAX_OUTPUT_BYTES = original_max
        self.assertEqual(code, 4, err)
        payload = json.loads(err)
        self.assertEqual(payload["reason"], "output_exceeds_query_readable_size")
        self.assertFalse((self.out_dir / bme.OUTPUT_NAME).exists())

    def test_realistic_payload_stays_under_cap(self) -> None:
        path = self.write_catalog(make_catalog(capabilities=[make_cap("p1", "a")]))
        code, _out, err = _run_main(["build", "--catalog", str(path), "--quiet"])
        self.assertEqual(code, 0, err)
        self.assertTrue((self.out_dir / bme.OUTPUT_NAME).exists())


# ---------------------------------------------------------------------------
# Lock acquire/release + stale-lock recovery
# ---------------------------------------------------------------------------


class LockTests(BaseTempDirTestCase):
    def test_acquire_then_release_removes_lock_file(self) -> None:
        lock_path = bme.acquire_lock(self.out_dir)
        self.assertTrue(lock_path.exists())
        bme.release_lock(lock_path)
        self.assertFalse(lock_path.exists())

    def test_held_fresh_lock_raises_lock_held(self) -> None:
        lock_path = self.out_dir / bme.LOCK_NAME
        lock_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(bme.BuildFatal) as ctx:
            bme.acquire_lock(self.out_dir)
        self.assertEqual(ctx.exception.reason, "lock_held")

    def test_stale_lock_is_recovered_and_reacquired(self) -> None:
        lock_path = self.out_dir / bme.LOCK_NAME
        lock_path.write_text("{}", encoding="utf-8")
        old_time = os.path.getmtime(lock_path) - (bme.LOCK_STALE_SECONDS + 60)
        os.utime(lock_path, (old_time, old_time))
        acquired = bme.acquire_lock(self.out_dir)
        self.assertEqual(acquired, lock_path)
        bme.release_lock(acquired)

    def test_full_build_run_acquires_and_releases_lock(self) -> None:
        path = self.write_catalog(make_catalog(capabilities=[make_cap("p1", "a")]))
        code, _, _ = _run_main(["build", "--catalog", str(path), "--quiet"])
        self.assertEqual(code, 0)
        self.assertFalse((self.out_dir / bme.LOCK_NAME).exists())

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_readonly_output_dir_raises_named_lock_uncreatable_not_generic_error(self) -> None:
        # P4-1: a read-only output_dir must surface as a NAMED BuildFatal
        # reason (this docstring's own exit-4 list promises "the output
        # directory/path could not be secured"), not propagate as an
        # untyped OSError that main()'s catch-all reports as
        # "unexpected_error".
        os.chmod(str(self.out_dir), 0o500)
        try:
            with self.assertRaises(bme.BuildFatal) as ctx:
                bme.acquire_lock(self.out_dir)
            self.assertEqual(ctx.exception.reason, "lock_uncreatable")
        finally:
            os.chmod(str(self.out_dir), 0o700)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_full_cli_run_against_readonly_output_dir_is_named_exit_4(self) -> None:
        path = self.write_catalog(make_catalog(capabilities=[make_cap("p1", "a")]))
        os.chmod(str(self.out_dir), 0o500)
        try:
            code, _, err = _run_main(["build", "--catalog", str(path), "--json"])
        finally:
            os.chmod(str(self.out_dir), 0o700)
        self.assertEqual(code, 4)
        payload = json.loads(err)
        self.assertEqual(payload["reason"], "lock_uncreatable")


# ---------------------------------------------------------------------------
# Output directory permissions -- os.makedirs() must pass mode=0o700,
# matching build_cross_project_catalog.py's own precedent for creating this
# SAME shared directory (see the module docstring's DEFAULT_OUTPUT_DIR
# comment). exist_ok=True means this only has an observable effect when the
# directory does not already exist, so this test deliberately points
# DEFAULT_OUTPUT_DIR at a path that has never been created, unlike
# BaseTempDirTestCase's fixture (which pre-creates self.out_dir in setUp()).
# ---------------------------------------------------------------------------


class OutputDirPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bme-dirmode-test-"))
        self._orig_output_dir = bme.DEFAULT_OUTPUT_DIR
        # A path under self.tmp that does not exist yet -- the build run
        # itself must be the one to create it.
        self.fresh_out_dir = self.tmp / "manifests-output-fresh"
        bme.DEFAULT_OUTPUT_DIR = self.fresh_out_dir

    def tearDown(self) -> None:
        bme.DEFAULT_OUTPUT_DIR = self._orig_output_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_creates_previously_absent_output_dir_with_mode_0700(self) -> None:
        catalog_path = self.tmp / "catalog.json"
        _write_json(catalog_path, make_catalog(capabilities=[make_cap("p1", "a")]))
        self.assertFalse(self.fresh_out_dir.exists())

        code, _, err = _run_main(["build", "--catalog", str(catalog_path), "--quiet"])
        self.assertEqual(code, 0, err)
        self.assertTrue(self.fresh_out_dir.is_dir())

        mode = stat.S_IMODE(self.fresh_out_dir.stat().st_mode)
        self.assertEqual(oct(mode), oct(0o700))


# ---------------------------------------------------------------------------
# OS-level read-only isolation: mandatory real permissions, not mocks.
# Mirrors detect_capability_changes.py's own NegativeControlTests +
# ReadOnlyFleetIsolationTests pattern.
# ---------------------------------------------------------------------------


class NegativeControlTests(unittest.TestCase):
    """Proves the filesystem actually enforces the permission bits this
    suite relies on. If this ever fails, the isolation suite below would
    pass vacuously (root, or a permission-ignoring filesystem)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bme-negctrl-"))
        (self.tmp / "sentinel.txt").write_text("x", encoding="utf-8")
        _make_readonly(self.tmp)

    def tearDown(self) -> None:
        _make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_cannot_create_new_file_in_readonly_tree(self) -> None:
        with self.assertRaises(PermissionError):
            (self.tmp / "new-file.json").write_text("{}", encoding="utf-8")


class ReadOnlyIsolationTests(unittest.TestCase):
    """Builds an OS-level read-only fixture standing in for "the rest of
    the filesystem / other projects", runs the full CLI with a catalog that
    lives OUTSIDE that read-only tree, and asserts (1) no PermissionError
    anywhere and (2) the read-only tree is byte-identical before and after
    -- this tool never even reads inside it (it has no reason to touch
    anything but catalog.json and its own designated output path), so this
    also proves it makes no stray filesystem calls into unrelated trees."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="bme-readonly-"))
        cls.other_projects_root = cls.tmp / "other-projects-readonly"
        (cls.other_projects_root / "some-project").mkdir(parents=True)
        (cls.other_projects_root / "some-project" / "wiki.json").write_text("{}", encoding="utf-8")
        _make_readonly(cls.other_projects_root)
        cls.snapshot_before = cls._snapshot(cls.other_projects_root)

        cls.catalog_path = cls.tmp / "catalog.json"
        _write_json(
            cls.catalog_path,
            make_catalog(
                capabilities=[make_cap("p1", "alpha-widget", name="alpha-widget")],
                wiki_pages=[make_page("p2", "notes", title="notes on alpha-widget", summary="mentions alpha-widget")],
            ),
        )
        cls.output_dir = cls.tmp / "manifests-output"  # writable, OUTSIDE the read-only tree

    @classmethod
    def tearDownClass(cls) -> None:
        _make_writable(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def _snapshot(root: Path) -> dict:
        out = {}
        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
            for name in list(dirnames) + list(filenames):
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                out[full] = (st.st_mode, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        return out

    def setUp(self) -> None:
        self._orig_output_dir = bme.DEFAULT_OUTPUT_DIR
        bme.DEFAULT_OUTPUT_DIR = self.output_dir

    def tearDown(self) -> None:
        bme.DEFAULT_OUTPUT_DIR = self._orig_output_dir
        if self.output_dir.exists():
            _make_writable(self.output_dir)
            shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_full_cli_run_raises_no_permission_error_and_leaves_readonly_tree_untouched(self) -> None:
        code, out, err = _run_main(["build", "--catalog", str(self.catalog_path), "--json", "--quiet"])
        self.assertEqual(code, 0, err)
        snapshot_after = self._snapshot(self.other_projects_root)
        self.assertEqual(self.snapshot_before, snapshot_after)

    def test_write_landed_only_under_designated_output_dir(self) -> None:
        _run_main(["build", "--catalog", str(self.catalog_path), "--quiet"])
        for dirpath, dirnames, filenames in os.walk(str(self.tmp)):
            try:
                Path(dirpath).relative_to(self.other_projects_root)
                continue
            except ValueError:
                pass
            for name in filenames:
                full = Path(dirpath) / name
                if full == self.catalog_path:
                    continue
                try:
                    full.relative_to(self.output_dir)
                except ValueError:
                    self.fail(f"unexpected write outside manifests-output: {full}")


# ---------------------------------------------------------------------------
# Full end-to-end run against a realistic multi-entity fixture, cross-checked
# against a hand-computed expected edge set.
# ---------------------------------------------------------------------------


class EndToEndFixtureTests(BaseTempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Hand-designed fixture covering: an undeclared ASCII mention
        # (medium, id-field), a declared-dependency pair that must be
        # excluded despite text overlap, a stopword-only needle that must
        # never surface, a too-short needle that must never surface, a CJK
        # mention that must degrade to low, and a same-project pair.
        self.catalog = make_catalog(
            capabilities=[
                make_cap("proj-a", "queue-runner", name="queue-runner", ref_key="proj-a:script:queue-runner"),
                make_cap(
                    "proj-b",
                    "consumer",
                    name="consumer-job",
                    summary="wraps queue-runner to process items",
                ),  # undeclared ASCII mention of proj-a#queue-runner via name -> medium
                make_cap(
                    "proj-a",
                    "declared-user",
                    name="declared-user",
                    summary="explicitly depends on queue-runner already",
                    depends_on=[
                        {
                            "raw": "queue-runner",
                            "ref_key": "proj-a:script:queue-runner",
                            "state": "resolved",
                            "scope": "same-project",
                            "target_global_id": "proj-a#queue-runner",
                        }
                    ],
                ),  # DECLARED dependency on queue-runner -> must be excluded
                make_cap("proj-a", "api", name="api", summary="just the generic word api here"),  # stopword id/name
                make_cap("proj-b", "sql", name="sql-thing", summary="short id sql should not match anything"),  # 3-char id below ASCII floor
            ],
            wiki_pages=[
                make_page("proj-b", "notes-a", title="鹏鹏拆解笔记", summary="just some notes, nothing else"),
                make_page(
                    "proj-c", "notes-b", title="独立笔记", summary="这里提到了鹏鹏拆解笔记的方法论"
                ),  # CJK mention of proj-b#notes-a -> low
            ],
        )
        self.catalog_path = self.write_catalog(self.catalog)

    def test_end_to_end_matches_hand_computed_expected_edges(self) -> None:
        code, out, err = _run_main(["build", "--catalog", str(self.catalog_path), "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        edges = payload if isinstance(payload, list) else None
        # --json on cmd_build prints a SUMMARY, not the full edges array;
        # read the real written output file for the full edge list.
        full = json.loads((self.out_dir / bme.OUTPUT_NAME).read_text(encoding="utf-8"))
        edges = full["edges"]

        pairs = {(e["from_global_id"], e["to_global_id"]): e["confidence"] for e in edges}

        # Expected: exactly two edges.
        # 1. proj-b#consumer -> proj-a#queue-runner (undeclared, name-field id match -> medium)
        self.assertIn(("proj-b#consumer", "proj-a#queue-runner"), pairs)
        self.assertEqual(pairs[("proj-b#consumer", "proj-a#queue-runner")], "medium")

        # 2. proj-c#notes-b -> proj-b#notes-a (CJK substring -> low)
        self.assertIn(("proj-c#notes-b", "proj-b#notes-a"), pairs)
        self.assertEqual(pairs[("proj-c#notes-b", "proj-b#notes-a")], "low")

        # The declared dependency pair must NOT appear as a text-mention edge.
        self.assertNotIn(("proj-a#declared-user", "proj-a#queue-runner"), pairs)

        # The stopword "api" and the too-short "sql" must never surface as
        # a `to` target.
        self.assertFalse(any(e["to_global_id"] == "proj-a#api" for e in edges))
        self.assertFalse(any(e["to_global_id"] == "proj-b#sql" for e in edges))

        self.assertEqual(len(edges), 2, f"expected exactly 2 edges, got: {pairs}")


# ---------------------------------------------------------------------------
# Cross-file constant-equality tests -- M8-DESIGN-FINAL-2026-08-23.md 3.0.2
# hard-mandates a test like this for any "copy, don't import" constant/rule
# duplication (motivated by name by the already-diverged agent_capacity.py
# copies -- see reference_agent_capacity_py_diverged_copies). Without this,
# a silent retune of STOPWORDS_ASCII/ASCII_MIN_LEN/etc. here would
# invalidate the M8 Gate-C real-data false-positive validation and nothing
# else in this suite would fail.
# ---------------------------------------------------------------------------


def _first_existing(*candidates: Path) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_THIS_DIR = Path(__file__).resolve().parent
# Two path candidates each: the deployed layout (this tool's eventual home
# is orca-context-bridge/scripts/, the SAME directory query_catalog.py
# already lives in) and the current staging layout, where
# mention_evidence_prototype.py lives at
# m8-gate-c-validation-STAGED-review-only/scripts/ -- a SIBLING of
# orca-context-bridge/ (i.e. two directories up from this file, at the repo
# root), NOT a sibling of orca-context-bridge/scripts/ itself. P2-2
# (max-tier Gate C re-review): an earlier revision used `_THIS_DIR.parent`
# (one level up, landing inside orca-context-bridge/) for this candidate --
# off by one directory level from the prototype's real location, so
# is_file() was always False and every PrototypeConstantEquivalenceTests
# case silently skipped in both the repo and once deployed, defeating the
# M8 design 3.0.2-mandated drift guard entirely. Neither candidate is fatal
# if missing; each equivalence class below is individually skipped (not the
# whole file) when its source is not found, so an unrelated missing file
# never masks the other's real check.
_PROTOTYPE_PATH = _first_existing(
    _THIS_DIR / "mention_evidence_prototype.py",
    _THIS_DIR.parent.parent / "m8-gate-c-validation-STAGED-review-only" / "scripts" / "mention_evidence_prototype.py",
)
_QUERY_CATALOG_PATH = _first_existing(
    _THIS_DIR / "query_catalog.py",
    _THIS_DIR.parent.parent / "m8-gate-c-validation-STAGED-review-only" / "scripts" / "query_catalog.py",
)

_NORMALIZE_PROBES = [
    "Hello World",
    "ＦULL-WIDTH",
    "café",
    "CAFÉ",
    "ß",
    "鹏鹏拆解",
    "  spaced  ",
    "",
    "MiXeD大小写Test",
]


@unittest.skipIf(_PROTOTYPE_PATH is None, "mention_evidence_prototype.py not found relative to this test file")
class PrototypeConstantEquivalenceTests(unittest.TestCase):
    """Asserts, by direct import and comparison (not just a docstring
    claim), that the algorithm constants/rules this module says it
    verbatim-ported from mention_evidence_prototype.py are still identical
    to the prototype's own copies."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.proto = _load_module_from_path("mention_evidence_prototype_for_test", _PROTOTYPE_PATH)

    def test_ascii_min_len_matches_prototype(self) -> None:
        self.assertEqual(bme.ASCII_MIN_LEN, self.proto.ASCII_MIN_LEN)

    def test_cjk_min_len_matches_prototype(self) -> None:
        self.assertEqual(bme.CJK_MIN_LEN, self.proto.CJK_MIN_LEN)

    def test_stopwords_ascii_matches_prototype(self) -> None:
        self.assertEqual(bme.STOPWORDS_ASCII, self.proto.STOPWORDS_ASCII)

    def test_stopwords_cjk_matches_prototype(self) -> None:
        self.assertEqual(bme.STOPWORDS_CJK, self.proto.STOPWORDS_CJK)

    def test_normalize_behaviorally_matches_prototype_on_probe_strings(self) -> None:
        for probe in _NORMALIZE_PROBES:
            self.assertEqual(bme._normalize(probe), self.proto._normalize(probe), f"probe={probe!r}")

    def test_ascii_word_boundary_match_behaviorally_matches_prototype(self) -> None:
        cases = [
            ("guid", "the guide tool"),
            ("guid", "a real guid here"),
            ("a.*b", "a.*b literal"),
            ("a.*b", "aXXb should not match"),
            ("test", "a test-case"),
            ("id", "valid-id-here"),
        ]
        for needle, haystack in cases:
            needle_norm = bme._normalize(needle)
            haystack_norm = bme._normalize(haystack)
            self.assertEqual(
                bme._ascii_word_boundary_match(needle_norm, haystack_norm),
                self.proto._ascii_word_boundary_match(needle_norm, haystack_norm),
                f"needle={needle!r} haystack={haystack!r}",
            )


@unittest.skipIf(_QUERY_CATALOG_PATH is None, "query_catalog.py not found relative to this test file")
class QueryCatalogNormalizeEquivalenceTests(unittest.TestCase):
    """bme._normalize() is documented as a verbatim copy of
    query_catalog.py::_normalize() (both NFC-then-casefold) -- assert the
    two are behaviorally identical, not just that a comment claims it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.qc = _load_module_from_path("query_catalog_for_test", _QUERY_CATALOG_PATH)

    def test_normalize_behaviorally_matches_query_catalog(self) -> None:
        for probe in _NORMALIZE_PROBES:
            self.assertEqual(bme._normalize(probe), self.qc._normalize(probe), f"probe={probe!r}")


if __name__ == "__main__":
    unittest.main()
