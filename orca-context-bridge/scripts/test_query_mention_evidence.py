#!/usr/bin/env python3
"""Unit tests for query_mention_evidence.py (M8-1 Gate C, query half).

Run with:
    python3 -m unittest test_query_mention_evidence.py -v
(from this directory), or plain `python3 test_query_mention_evidence.py`.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import query_mention_evidence as qme  # noqa: E402


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _run_main(argv: list) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = qme.main(argv)
    return code, out.getvalue(), err.getvalue()


def make_edge(from_global_id, to_global_id, confidence="medium") -> dict:
    return {
        "from_global_id": from_global_id,
        "from_project_id": from_global_id.split("#")[0],
        "from_entry_type": "capability",
        "to_global_id": to_global_id,
        "to_project_id": to_global_id.split("#")[0],
        "to_entry_type": "capability",
        "same_project": from_global_id.split("#")[0] == to_global_id.split("#")[0],
        "confidence": confidence,
        "matched_combinations": [],
    }


def make_mention_evidence(
    edges=None,
    catalog_generated_at="2026-08-23T00:00:00Z",
    generated_at="2026-08-23T01:00:00Z",
    catalog_path=None,
) -> dict:
    return {
        "schema_version": 1,
        "generator": {"script": "x", "version": "1.0.0"},
        "generated_at": generated_at,
        # None here means "not yet known" -- BaseTempDirTestCase.write_me()
        # fills in the real catalog.json path it wrote for this test, so
        # the new catalog_path_mismatch check (P2-2) does not spuriously
        # fire on tests that were never about path mismatch in the first
        # place. Tests that DO want to exercise a mismatch pass an explicit
        # (wrong) catalog_path here, which write_me() leaves untouched.
        "catalog_path": catalog_path,
        "catalog_generated_at": catalog_generated_at,
        "catalog_verified_at": catalog_generated_at,
        "entry_counts": {"capabilities": 0, "wiki_pages": 0, "total_entries": 0},
        "gate_config": {},
        "skip_stats": {},
        "edges": edges or [],
    }


def make_catalog(global_ids, generated_at="2026-08-23T00:00:00Z") -> dict:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "verified_at": generated_at,
        "capabilities": [{"project_id": gid.split("#")[0], "global_id": gid} for gid in global_ids],
        "wiki_pages": [],
    }


class BaseTempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="qme-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_me(self, payload: dict) -> Path:
        path = self.tmp / "mention-evidence.json"
        if payload.get("catalog_path") is None:
            payload = dict(payload)
            payload["catalog_path"] = str(self.tmp / "catalog.json")
        _write_json(path, payload)
        return path

    def write_catalog(self, payload: dict) -> Path:
        path = self.tmp / "catalog.json"
        _write_json(path, payload)
        return path


# ---------------------------------------------------------------------------
# Exit 0: found a definite answer
# ---------------------------------------------------------------------------


class FoundTests(BaseTempDirTestCase):
    def test_from_side_match_is_exit_0(self) -> None:
        me_path = self.write_me(make_mention_evidence(edges=[make_edge("p1#a", "p2#b")]))
        cat_path = self.write_catalog(make_catalog(["p1#a", "p2#b"]))
        code, out, err = _run_main(
            ["search", "--from-global-id", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["total_matches"], 1)

    def test_to_side_match_is_exit_0(self) -> None:
        me_path = self.write_me(make_mention_evidence(edges=[make_edge("p1#a", "p2#b")]))
        cat_path = self.write_catalog(make_catalog(["p1#a", "p2#b"]))
        code, out, err = _run_main(
            ["search", "--to-global-id", "p2#b", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        self.assertEqual(code, 0, err)

    def test_either_side_matches_both_directions(self) -> None:
        me_path = self.write_me(make_mention_evidence(edges=[make_edge("p1#a", "p2#b"), make_edge("p3#c", "p1#a")]))
        cat_path = self.write_catalog(make_catalog(["p1#a", "p2#b", "p3#c"]))
        code, out, err = _run_main(
            ["search", "--global-id-either-side", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["total_matches"], 2)

    def test_from_side_only_does_not_match_to_side_occurrence(self) -> None:
        me_path = self.write_me(make_mention_evidence(edges=[make_edge("p1#a", "p2#b")]))
        cat_path = self.write_catalog(make_catalog(["p1#a", "p2#b"]))
        code, out, err = _run_main(
            ["search", "--from-global-id", "p2#b", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        payload = json.loads(out)
        self.assertEqual(payload["total_matches"], 0)
        self.assertEqual(code, 1)  # confirmed none: p2#b exists but is never a `from`


# ---------------------------------------------------------------------------
# Exit 1: confirmed none
# ---------------------------------------------------------------------------


class ConfirmedNoneTests(BaseTempDirTestCase):
    def test_zero_edges_but_global_id_exists_is_exit_1(self) -> None:
        me_path = self.write_me(make_mention_evidence(edges=[]))
        cat_path = self.write_catalog(make_catalog(["p1#a"]))
        code, out, err = _run_main(
            ["search", "--global-id-either-side", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        self.assertEqual(code, 1, err)
        payload = json.loads(out)
        self.assertTrue(payload["exists_in_catalog"])
        self.assertEqual(payload["total_matches"], 0)


# ---------------------------------------------------------------------------
# Exit 2: usage errors
# ---------------------------------------------------------------------------


class UsageErrorTests(BaseTempDirTestCase):
    def test_empty_global_id_is_exit_2(self) -> None:
        me_path = self.write_me(make_mention_evidence())
        code, _out, _err = _run_main(["search", "--from-global-id", "   ", "--mention-evidence", str(me_path), "--quiet"])
        self.assertEqual(code, 2)

    def test_no_side_flag_is_exit_2_via_argparse(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _run_main(["search", "--quiet"])
        self.assertEqual(ctx.exception.code, 2)

    def test_multiple_side_flags_is_exit_2_via_argparse(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _run_main(["search", "--from-global-id", "x", "--to-global-id", "y", "--quiet"])
        self.assertEqual(ctx.exception.code, 2)

    def test_empty_mention_evidence_path_is_exit_2(self) -> None:
        code, _out, _err = _run_main(["search", "--from-global-id", "x", "--mention-evidence", "  ", "--quiet"])
        self.assertEqual(code, 2)


# ---------------------------------------------------------------------------
# Exit 3: partial trust -- staleness
# ---------------------------------------------------------------------------


class StalenessTests(BaseTempDirTestCase):
    def test_mention_evidence_older_than_current_catalog_is_exit_3(self) -> None:
        me_path = self.write_me(
            make_mention_evidence(edges=[make_edge("p1#a", "p2#b")], catalog_generated_at="2026-08-20T00:00:00Z")
        )
        cat_path = self.write_catalog(make_catalog(["p1#a", "p2#b"], generated_at="2026-08-23T00:00:00Z"))
        code, out, err = _run_main(
            ["search", "--from-global-id", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        self.assertEqual(code, 3, err)
        payload = json.loads(out)
        self.assertTrue(payload["stale"])
        self.assertTrue(any(w["code"] == qme.WARN_STALE for w in payload["warnings"]))
        # Matches are still reported for reference even though the answer
        # is not "definite" -- staleness is a trust caveat, not data loss.
        self.assertEqual(payload["total_matches"], 1)

    def test_mention_evidence_same_snapshot_as_current_catalog_is_fresh(self) -> None:
        ts = "2026-08-23T00:00:00Z"
        me_path = self.write_me(make_mention_evidence(edges=[], catalog_generated_at=ts))
        cat_path = self.write_catalog(make_catalog(["p1#a"], generated_at=ts))
        code, out, err = _run_main(
            ["search", "--global-id-either-side", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        payload = json.loads(out)
        self.assertFalse(payload["stale"])
        self.assertEqual(code, 1)  # confirmed none, not stale

    def test_mention_evidence_newer_than_current_catalog_is_not_stale(self) -> None:
        # catalog_generated_at pinned by mention-evidence.json is AHEAD of
        # (>=) the current catalog's -- fresh iff ME_ts >= CAT_ts.
        me_path = self.write_me(make_mention_evidence(edges=[], catalog_generated_at="2026-08-25T00:00:00Z"))
        cat_path = self.write_catalog(make_catalog(["p1#a"], generated_at="2026-08-23T00:00:00Z"))
        code, out, err = _run_main(
            ["search", "--global-id-either-side", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        payload = json.loads(out)
        self.assertFalse(payload["stale"])

    def test_catalog_unreadable_is_treated_as_stale(self) -> None:
        me_path = self.write_me(make_mention_evidence(edges=[make_edge("p1#a", "p2#b")]))
        missing_catalog = self.tmp / "does-not-exist.json"
        code, out, err = _run_main(
            ["search", "--from-global-id", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(missing_catalog), "--json"]
        )
        self.assertEqual(code, 3, err)
        payload = json.loads(out)
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["catalog_status"], "missing")
        self.assertTrue(any(w["code"] == qme.WARN_CATALOG_UNAVAILABLE for w in payload["warnings"]))


# ---------------------------------------------------------------------------
# Exit 3: partial trust -- unknown global_id (not in the current catalog at
# all, so exit 1's "confirmed none" cannot honestly be claimed)
# ---------------------------------------------------------------------------


class UnknownGlobalIdTests(BaseTempDirTestCase):
    def test_zero_edges_and_unknown_global_id_is_exit_3_not_exit_1(self) -> None:
        me_path = self.write_me(make_mention_evidence(edges=[]))
        cat_path = self.write_catalog(make_catalog(["p1#a"]))  # does not contain p9#ghost
        code, out, err = _run_main(
            ["search", "--global-id-either-side", "p9#ghost", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        self.assertEqual(code, 3, err)
        payload = json.loads(out)
        self.assertFalse(payload["exists_in_catalog"])
        self.assertTrue(any(w["code"] == qme.WARN_GLOBAL_ID_UNKNOWN for w in payload["warnings"]))


# ---------------------------------------------------------------------------
# Exit 4: cannot answer at all
# ---------------------------------------------------------------------------


class CannotAnswerTests(BaseTempDirTestCase):
    def test_missing_mention_evidence_is_exit_4(self) -> None:
        missing = self.tmp / "does-not-exist.json"
        code, _out, _err = _run_main(["search", "--from-global-id", "x", "--mention-evidence", str(missing), "--quiet"])
        self.assertEqual(code, 4)

    def test_corrupt_mention_evidence_json_is_exit_4(self) -> None:
        path = self.tmp / "mention-evidence.json"
        path.write_text("{not valid json", encoding="utf-8")
        code, _out, _err = _run_main(["search", "--from-global-id", "x", "--mention-evidence", str(path), "--quiet"])
        self.assertEqual(code, 4)

    def test_mention_evidence_without_edges_list_is_exit_4(self) -> None:
        path = self.tmp / "mention-evidence.json"
        _write_json(path, {"schema_version": 1})
        code, _out, _err = _run_main(["search", "--from-global-id", "x", "--mention-evidence", str(path), "--quiet"])
        self.assertEqual(code, 4)

    def test_missing_catalog_does_not_prevent_exit_4_precedence(self) -> None:
        # Even if catalog.json is ALSO missing, a broken mention-evidence.json
        # is still exit 4 (cannot answer at all takes precedence over the
        # staleness/existence machinery, which never runs).
        missing_me = self.tmp / "does-not-exist.json"
        missing_cat = self.tmp / "also-missing.json"
        code, _out, _err = _run_main(
            ["search", "--from-global-id", "x", "--mention-evidence", str(missing_me), "--catalog", str(missing_cat), "--quiet"]
        )
        self.assertEqual(code, 4)


# ---------------------------------------------------------------------------
# Exit 3: partial trust -- catalog_path mismatch (P2-2)
# ---------------------------------------------------------------------------


class CatalogPathMismatchTests(BaseTempDirTestCase):
    def test_foreign_catalog_path_with_future_generated_at_is_exit_3_not_exit_1(self) -> None:
        # A mention-evidence.json built from some OTHER catalog.json (e.g.
        # `build --catalog /anything.json`) that happens to carry a
        # future-dated generated_at must not be able to defeat the
        # staleness gate by pure timestamp coincidence: the recorded
        # catalog_path does not match the catalog.json this query run
        # actually reads, so it must be reported as untrustworthy (exit 3),
        # never as a confirmed exit 1/0 answer about the real catalog.
        me_path = self.write_me(
            make_mention_evidence(
                edges=[],
                catalog_generated_at="2099-01-01T00:00:00Z",
                catalog_path=str(self.tmp / "some-other-catalog.json"),
            )
        )
        cat_path = self.write_catalog(make_catalog(["p1#q"], generated_at="2026-08-23T00:00:00Z"))
        code, out, err = _run_main(
            ["search", "--global-id-either-side", "p1#q", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        self.assertEqual(code, 3, err)
        payload = json.loads(out)
        self.assertTrue(payload["stale"])
        self.assertTrue(any(w["code"] == qme.WARN_CATALOG_PATH_MISMATCH for w in payload["warnings"]))
        # The mismatch warning fires instead of the timestamp-based
        # mention_evidence_stale warning -- the timestamp comparison was
        # never meaningful once the catalog identity itself is wrong.
        self.assertFalse(any(w["code"] == qme.WARN_STALE for w in payload["warnings"]))

    def test_matching_catalog_path_is_not_flagged_as_mismatch(self) -> None:
        ts = "2026-08-23T00:00:00Z"
        cat_path = self.write_catalog(make_catalog(["p1#a"], generated_at=ts))
        me_path = self.write_me(
            make_mention_evidence(edges=[], catalog_generated_at=ts, catalog_path=str(cat_path))
        )
        code, out, err = _run_main(
            ["search", "--global-id-either-side", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        payload = json.loads(out)
        self.assertFalse(payload["stale"])
        self.assertFalse(any(w["code"] == qme.WARN_CATALOG_PATH_MISMATCH for w in payload["warnings"]))
        self.assertEqual(code, 1)  # confirmed none, path matches, fresh

    def test_default_write_me_catalog_path_matches_default_write_catalog_path(self) -> None:
        # Sanity check on the test fixture itself: write_me()'s auto-filled
        # catalog_path (when the caller passes catalog_path=None) must
        # match write_catalog()'s own path, or every other test in this
        # file that relies on "no mismatch by default" would be silently
        # wrong for the wrong reason.
        ts = "2026-08-23T00:00:00Z"
        me_path = self.write_me(make_mention_evidence(edges=[], catalog_generated_at=ts))
        cat_path = self.write_catalog(make_catalog(["p1#a"], generated_at=ts))
        code, out, err = _run_main(
            ["search", "--global-id-either-side", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        payload = json.loads(out)
        self.assertFalse(any(w["code"] == qme.WARN_CATALOG_PATH_MISMATCH for w in payload["warnings"]))


# ---------------------------------------------------------------------------
# Exit 3: partial trust -- malformed edge rows skipped (P2-1)
# ---------------------------------------------------------------------------


class MalformedEdgeRowTests(BaseTempDirTestCase):
    def test_malformed_edge_rows_are_counted_and_force_exit_3_even_with_matches(self) -> None:
        ts = "2026-08-23T00:00:00Z"
        edges = [
            make_edge("p1#a", "p2#b"),  # well-formed match
            "not-a-dict",  # malformed: not an object at all
            {"from_global_id": "p1#a"},  # malformed: missing to_global_id
            {"to_global_id": "p2#b"},  # malformed: missing from_global_id
            {"from_global_id": None, "to_global_id": "p2#b"},  # malformed: non-string from_global_id
        ]
        me_path = self.write_me(make_mention_evidence(edges=edges, catalog_generated_at=ts))
        cat_path = self.write_catalog(make_catalog(["p1#a", "p2#b"], generated_at=ts))
        code, out, err = _run_main(
            ["search", "--from-global-id", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        payload = json.loads(out)
        # The well-formed match is still surfaced for reference...
        self.assertEqual(payload["total_matches"], 1)
        self.assertEqual(payload["skipped_malformed_edges"], 4)
        self.assertTrue(any(w["code"] == qme.WARN_MALFORMED_EDGES_SKIPPED for w in payload["warnings"]))
        # ...but the exit code must NOT be the confident 0: some rows could
        # not be evaluated, so this is not a complete retrieval.
        self.assertEqual(code, 3, err)

    def test_malformed_edge_rows_force_exit_3_even_when_zero_matches(self) -> None:
        ts = "2026-08-23T00:00:00Z"
        edges = ["garbage", {"from_global_id": "p1#a"}]
        me_path = self.write_me(make_mention_evidence(edges=edges, catalog_generated_at=ts))
        cat_path = self.write_catalog(make_catalog(["p1#a", "p9#ghost"], generated_at=ts))
        code, out, err = _run_main(
            ["search", "--global-id-either-side", "p9#ghost", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        payload = json.loads(out)
        self.assertEqual(payload["total_matches"], 0)
        self.assertEqual(payload["skipped_malformed_edges"], 2)
        # Would otherwise have been exit 1 ("confirmed none") -- must not be,
        # since two of four rows were never actually evaluated.
        self.assertEqual(code, 3, err)

    def test_zero_malformed_rows_reports_zero_and_no_warning(self) -> None:
        ts = "2026-08-23T00:00:00Z"
        me_path = self.write_me(make_mention_evidence(edges=[make_edge("p1#a", "p2#b")], catalog_generated_at=ts))
        cat_path = self.write_catalog(make_catalog(["p1#a", "p2#b"], generated_at=ts))
        code, out, err = _run_main(
            ["search", "--from-global-id", "p1#a", "--mention-evidence", str(me_path), "--catalog", str(cat_path), "--json"]
        )
        payload = json.loads(out)
        self.assertEqual(payload["skipped_malformed_edges"], 0)
        self.assertFalse(any(w["code"] == qme.WARN_MALFORMED_EDGES_SKIPPED for w in payload["warnings"]))
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
