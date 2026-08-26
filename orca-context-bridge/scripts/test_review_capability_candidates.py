#!/usr/bin/env python3
"""Test suite for review_capability_candidates.py (M8-2, Gate C staged
candidate), plus one end-to-end scan -> mark -> rescan -> verify-preserved
flow that exercises both staged tools together.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_capability_candidates as rcc  # noqa: E402
import discover_capability_candidates as dcc  # noqa: E402


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _run_rcc(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rcc.main(argv)
    return code, out.getvalue(), err.getvalue()


def _run_dcc(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dcc.main(argv)
    return code, out.getvalue(), err.getvalue()


def make_hit(hit_id: str, *, state: str = "pending", content_sha256: str | None = "hash-x",
             path: str | None = None, project_id: str = "p", root_real_path: str | None = None,
             signal_type: str = "capability:script",
             noise_signals: list[str] | None = None) -> dict:
    # root_real_path defaults to a value derived from project_id (NOT the
    # same field, just a convenient default so existing callers that only
    # ever set project_id keep behaving the same way as before P1-1's fix
    # moved cluster scoping from project_id to root_real_path -- distinct
    # project_id values still default to distinct root_real_path values
    # unless a test deliberately overrides one or the other to reproduce
    # the real "same project_id, different root_real_path" shape).
    return {
        "hit_id": hit_id,
        "project_id": project_id,
        "root_real_path": root_real_path if root_real_path is not None else f"/root/{project_id}",
        "signal_type": signal_type,
        "path": path or f"scripts/{hit_id}.py",
        "content_sha256": content_sha256,
        "noise_signals": noise_signals or [],
        "duplicate_of": None,
        "state": state,
        "state_note": None,
        "state_set_by": None,
        "state_set_at": None,
        "is_new_discovery": True,
        "already_in_catalog_baseline": False,
    }


def make_hits_doc(hits: list[dict]) -> dict:
    return {"schema_version": 1, "generated_at": "2026-08-23T00:00:00Z", "hits": hits}


class BaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rcc-"))
        self.output_dir = self.tmp / "manifests-output"
        self.output_dir.mkdir(parents=True)
        self.hits_path = self.output_dir / rcc.HITS_NAME

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_hits(self, hits: list[dict]) -> None:
        _write_json(self.hits_path, make_hits_doc(hits))

    def read_hits(self) -> dict:
        return json.loads(self.hits_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# list / show
# ---------------------------------------------------------------------------


class ListShowTests(BaseTestCase):
    def test_list_returns_all_hits_with_exit_0(self) -> None:
        self.write_hits([make_hit("a"), make_hit("b")])
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path), "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["count"], 2)

    def test_list_with_no_matches_is_exit_1(self) -> None:
        self.write_hits([make_hit("a", state="pending")])
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path), "--state", "dismissed", "--json"])
        self.assertEqual(code, 1, out)

    def test_list_filters_by_project_id(self) -> None:
        self.write_hits([make_hit("a", project_id="p1"), make_hit("b", project_id="p2")])
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path), "--project-id", "p1", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([h["hit_id"] for h in payload["hits"]], ["a"])

    def test_list_filters_by_signal_type(self) -> None:
        self.write_hits([make_hit("a", signal_type="capability:script"), make_hit("b", signal_type="capability:skill")])
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path), "--signal-type", "capability:skill", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([h["hit_id"] for h in payload["hits"]], ["b"])

    def test_list_duplicates_only_filter(self) -> None:
        self.write_hits([
            make_hit("a", noise_signals=["content_duplicate"]),
            make_hit("b", noise_signals=[]),
        ])
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path), "--duplicates-only", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([h["hit_id"] for h in payload["hits"]], ["a"])

    def test_list_missing_file_is_exit_4(self) -> None:
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path)])
        self.assertEqual(code, 4)
        self.assertIn("hits_file_missing", err)

    def test_list_reports_skipped_malformed_rows_as_exit_3(self) -> None:
        doc = make_hits_doc([make_hit("a")])
        doc["hits"].append({"not_a_valid_hit": True})
        doc["hits"].append("not even a dict")
        _write_json(self.hits_path, doc)
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path), "--json"])
        self.assertEqual(code, 3, out)
        payload = json.loads(out)
        self.assertEqual(payload["skipped_malformed_count"], 2)
        self.assertEqual(payload["count"], 1)

    def test_show_found_is_exit_0(self) -> None:
        self.write_hits([make_hit("a")])
        code, out, err = _run_rcc(["show", "--hit-id", "a", "--hits-path", str(self.hits_path), "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["hits"][0]["hit_id"], "a")

    def test_show_not_found_is_exit_1(self) -> None:
        self.write_hits([make_hit("a")])
        code, out, err = _run_rcc(["show", "--hit-id", "does-not-exist", "--hits-path", str(self.hits_path), "--json"])
        self.assertEqual(code, 1, out)

    def test_show_empty_hit_id_is_usage_error(self) -> None:
        self.write_hits([make_hit("a")])
        code, out, err = _run_rcc(["show", "--hit-id", "  ", "--hits-path", str(self.hits_path)])
        self.assertEqual(code, 2)
        self.assertIn("empty_hit_id", err)

    def test_show_missing_file_is_exit_4(self) -> None:
        code, out, err = _run_rcc(["show", "--hit-id", "a", "--hits-path", str(self.hits_path)])
        self.assertEqual(code, 4)
        self.assertIn("hits_file_missing", err)

    def test_null_noise_signals_does_not_crash_duplicates_only_filter(self) -> None:
        """A row with `noise_signals: null` (present in the JSON but
        explicitly None, rather than absent) must be treated as "no noise
        signals", not crash the query with an uncaught TypeError.
        `.get(key, default)` only uses `default` when the key is ABSENT, so
        a naive `hit.get("noise_signals", [])` mishandles this case. Built
        by hand rather than via make_hit(): make_hit()'s own `noise_signals
        or []` would coerce a `None` argument back to `[]` before it ever
        reached the file, defeating the point of this test."""
        a = make_hit("a")
        a["noise_signals"] = None
        b = make_hit("b", noise_signals=["content_duplicate"])
        self.write_hits([a, b])
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path), "--duplicates-only", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([h["hit_id"] for h in payload["hits"]], ["b"])

    def test_null_noise_signals_does_not_crash_plain_list_or_show(self) -> None:
        a = make_hit("a")
        a["noise_signals"] = None
        self.write_hits([a])
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path), "--json"])
        self.assertEqual(code, 0, err)
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path)])  # non-JSON print path
        self.assertEqual(code, 0, err)
        code, out, err = _run_rcc(["show", "--hit-id", "a", "--hits-path", str(self.hits_path)])
        self.assertEqual(code, 0, err)


# ---------------------------------------------------------------------------
# mark -- default-off vs explicit --apply-to-duplicate-cluster
# ---------------------------------------------------------------------------


class MarkTests(BaseTestCase):
    def test_mark_single_hit_default_only_affects_named_hit(self) -> None:
        self.write_hits([
            make_hit("a", content_sha256="shared-hash"),
            make_hit("b", content_sha256="shared-hash"),
        ])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed",
            "--marked-by", "tester", "--hits-path", str(self.hits_path), "--json",
        ])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["affected_hit_ids"], ["a"])

        doc = self.read_hits()
        by_id = {h["hit_id"]: h for h in doc["hits"]}
        self.assertEqual(by_id["a"]["state"], "dismissed")
        self.assertEqual(by_id["a"]["state_set_by"], "tester")
        self.assertIsNotNone(by_id["a"]["state_set_at"])
        # the sibling sharing content_sha256 must NOT be touched by default
        self.assertEqual(by_id["b"]["state"], "pending")
        self.assertIsNone(by_id["b"]["state_set_by"])

    def test_mark_with_apply_to_duplicate_cluster_affects_whole_cluster(self) -> None:
        self.write_hits([
            make_hit("a", content_sha256="shared-hash"),
            make_hit("b", content_sha256="shared-hash"),
            make_hit("c", content_sha256="other-hash"),
        ])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--apply-to-duplicate-cluster", "--hits-path", str(self.hits_path), "--json",
        ])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(set(payload["affected_hit_ids"]), {"a", "b"})

        doc = self.read_hits()
        by_id = {h["hit_id"]: h for h in doc["hits"]}
        self.assertEqual(by_id["a"]["state"], "dismissed")
        self.assertEqual(by_id["b"]["state"], "dismissed")
        self.assertEqual(by_id["c"]["state"], "pending")  # different hash, untouched

    def test_mark_note_is_recorded(self) -> None:
        self.write_hits([make_hit("a")])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "triaged_for_promotion", "--marked-by", "tester",
            "--note", "looks reusable", "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 0, err)
        doc = self.read_hits()
        self.assertEqual(doc["hits"][0]["state_note"], "looks reusable")

    def test_mark_unknown_hit_id_is_usage_error(self) -> None:
        self.write_hits([make_hit("a")])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "does-not-exist", "--state", "dismissed",
            "--marked-by", "tester", "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 2)
        self.assertIn("unknown_hit_id", err)

    def test_mark_empty_marked_by_is_usage_error(self) -> None:
        self.write_hits([make_hit("a")])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "  ",
            "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 2)
        self.assertIn("empty_marked_by", err)

    def test_mark_invalid_state_value_is_usage_error_via_argparse(self) -> None:
        # argparse's own `choices=` rejects this before review_capability_
        # candidates.py's own code ever runs, and argparse enforces that by
        # calling sys.exit(2) directly rather than returning -- see module
        # docstring's EXIT CODES section.
        self.write_hits([make_hit("a")])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            rcc.main([
                "mark", "--hit-id", "a", "--state", "not-a-real-state", "--marked-by", "tester",
                "--hits-path", str(self.hits_path),
            ])
        self.assertEqual(ctx.exception.code, 2)

    def test_mark_missing_file_is_fatal(self) -> None:
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 4)
        self.assertIn("hits_file_missing", err)

    def test_mark_never_touches_any_other_file(self) -> None:
        """mark writes ONLY discovery-hits.json -- no catalog.json, no
        project wiki file, nothing else, anywhere."""
        self.write_hits([make_hit("a")])
        sentinel_dir = self.tmp / "some-project" / "wiki"
        sentinel_dir.mkdir(parents=True)
        sentinel = sentinel_dir / "reusable-capabilities.json"
        sentinel.write_text('{"capabilities": []}', encoding="utf-8")
        before = sentinel.read_bytes()
        catalog_sentinel = self.tmp / "catalog.json"
        catalog_sentinel.write_text('{"projects": []}', encoding="utf-8")
        catalog_before = catalog_sentinel.read_bytes()

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(sentinel.read_bytes(), before)
        self.assertEqual(catalog_sentinel.read_bytes(), catalog_before)

    def test_mark_preserves_malformed_rows_untouched(self) -> None:
        doc = make_hits_doc([make_hit("a")])
        doc["hits"].append({"not_a_valid_hit": True})
        _write_json(self.hits_path, doc)

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 0, err)
        doc2 = self.read_hits()
        self.assertEqual(len(doc2["hits"]), 2)
        self.assertIn({"not_a_valid_hit": True}, doc2["hits"])

    def test_apply_to_duplicate_cluster_is_scoped_to_same_project_by_default(self) -> None:
        """P2-1 (Gate C adversarial review): --apply-to-duplicate-cluster
        matched purely on content_sha256, with no project scoping. Because
        this codebase's own "copy, don't import" convention deliberately
        manufactures byte-identical files across projects, a plain
        content_sha256 match let one mark on a hit in project P1 silently
        also dismiss an unrelated, un-reviewed hit in project P2, stamped
        with a note that was written about a different file."""
        self.write_hits([
            make_hit("a", content_sha256="shared-hash", project_id="p1"),
            make_hit("b", content_sha256="shared-hash", project_id="p2"),
        ])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--apply-to-duplicate-cluster", "--hits-path", str(self.hits_path), "--json",
        ])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["affected_hit_ids"], ["a"])

        doc = self.read_hits()
        by_id = {h["hit_id"]: h for h in doc["hits"]}
        self.assertEqual(by_id["a"]["state"], "dismissed")
        # b shares content_sha256 but is in a DIFFERENT project -- must not
        # be touched by the default (project-scoped) cluster application.
        self.assertEqual(by_id["b"]["state"], "pending")

    def test_allow_cross_project_cluster_opts_back_into_matching_purely_by_content_hash(self) -> None:
        self.write_hits([
            make_hit("a", content_sha256="shared-hash", project_id="p1"),
            make_hit("b", content_sha256="shared-hash", project_id="p2"),
        ])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--apply-to-duplicate-cluster", "--allow-cross-project-cluster",
            "--hits-path", str(self.hits_path), "--json",
        ])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(set(payload["affected_hit_ids"]), {"a", "b"})

        doc = self.read_hits()
        by_id = {h["hit_id"]: h for h in doc["hits"]}
        self.assertEqual(by_id["a"]["state"], "dismissed")
        self.assertEqual(by_id["b"]["state"], "dismissed")

    def test_cluster_apply_scoped_by_root_real_path_not_shared_project_id(self) -> None:
        """P1-1 (max-tier Gate C re-review): the P0-2 fix moved hit_id's own
        identity from project_id to root_real_path, but mark
        --apply-to-duplicate-cluster's default safety scope was left keyed
        on project_id. This machine's real catalog.json proves project_id
        is not a reliable proxy for "different project" (e.g. 'hgcloud' ->
        3 distinct real_paths, 'rn邮箱' -> 4 -- shared project_id, genuinely
        different projects). Reproduce that exact shape: two hits sharing
        ONE project_id but with DIFFERENT root_real_path (mirroring two
        scanned roots that happen to share a project_id string) and
        byte-identical content. Confirm the default (no
        --allow-cross-project-cluster) affects ONLY the reviewed hit's own
        root_real_path, and --allow-cross-project-cluster is required to
        also affect the other root."""
        self.write_hits([
            make_hit("a", content_sha256="shared-hash", project_id="dup-id", root_real_path="/wt1"),
            make_hit("b", content_sha256="shared-hash", project_id="dup-id", root_real_path="/wt2"),
        ])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--apply-to-duplicate-cluster", "--hits-path", str(self.hits_path), "--json",
        ])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["affected_hit_ids"], ["a"])

        doc = self.read_hits()
        by_id = {h["hit_id"]: h for h in doc["hits"]}
        self.assertEqual(by_id["a"]["state"], "dismissed")
        # b shares project_id AND content_sha256 with a, but lives under a
        # DIFFERENT root_real_path -- must NOT be touched by the default
        # (root_real_path-scoped) cluster application.
        self.assertEqual(by_id["b"]["state"], "pending")

        # --allow-cross-project-cluster is required to also reach b.
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--apply-to-duplicate-cluster", "--allow-cross-project-cluster",
            "--hits-path", str(self.hits_path), "--json",
        ])
        self.assertEqual(code, 0, err)
        payload2 = json.loads(out)
        self.assertEqual(set(payload2["affected_hit_ids"]), {"a", "b"})
        doc2 = self.read_hits()
        by_id2 = {h["hit_id"]: h for h in doc2["hits"]}
        self.assertEqual(by_id2["b"]["state"], "dismissed")

    def test_re_marking_an_already_terminal_hit_overwrites(self) -> None:
        self.write_hits([make_hit("a", state="dismissed")])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "triaged_for_promotion", "--marked-by", "second-tester",
            "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 0, err)
        doc = self.read_hits()
        self.assertEqual(doc["hits"][0]["state"], "triaged_for_promotion")
        self.assertEqual(doc["hits"][0]["state_set_by"], "second-tester")


# ---------------------------------------------------------------------------
# `mark`'s write-surface confinement (P1-2, Gate C adversarial review):
# --hits-path must name discovery-hits.json, and mark must never create a
# directory that didn't already exist.
# ---------------------------------------------------------------------------


class MarkWriteSurfaceConfinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rcc-confine-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mark_refuses_hits_path_with_wrong_basename(self) -> None:
        """Reproduces the exact scenario from the adversarial review: an
        existing, unrelated file that happens to be shaped like a
        discovery-hits.json document (top-level {"hits": [...]} with a
        matching hit_id) must NOT be silently rewritten just because
        --hits-path was pointed at it."""
        victim_dir = self.tmp / "victim" / "other"
        victim_dir.mkdir(parents=True)
        victim = victim_dir / "search-results.json"
        victim.write_text(
            json.dumps({"kind": "some unrelated tool output", "hits": [{"hit_id": "abc123", "state": "pending"}]}),
            encoding="utf-8",
        )
        before = victim.read_bytes()

        code, out, err = _run_rcc([
            "mark", "--hit-id", "abc123", "--state", "dismissed", "--marked-by", "attacker",
            "--note", "pwned", "--hits-path", str(victim),
        ])
        self.assertEqual(code, 2)
        self.assertIn("invalid_hits_path_name", err)
        self.assertEqual(victim.read_bytes(), before)

    def test_mark_does_not_create_directories_for_a_hits_path_whose_parent_is_missing(self) -> None:
        """Reproduces the adversarial review's case (A): a --hits-path
        pointing into a project that has neither wiki/ nor
        wiki/orca-context-bridge/ must not spring those directories (or the
        lock file) into existence, even though the command ultimately
        fails with hits_file_missing regardless."""
        victim_path = self.tmp / "pretend-project" / "wiki" / "orca-context-bridge" / rcc.HITS_NAME
        self.assertFalse(victim_path.parent.exists())

        code, out, err = _run_rcc([
            "mark", "--hit-id", "deadbeef", "--state", "dismissed", "--marked-by", "x",
            "--hits-path", str(victim_path),
        ])
        self.assertEqual(code, 4)
        self.assertIn("hits_file_missing", err)
        self.assertFalse(victim_path.parent.exists(), "mark must not create wiki/orca-context-bridge/")
        self.assertFalse((self.tmp / "pretend-project" / "wiki").exists(), "mark must not create wiki/")


# ---------------------------------------------------------------------------
# M8-2 authorization notice on a default-path write
# ---------------------------------------------------------------------------


class ProductionPathAuthorizationNoticeTests(unittest.TestCase):
    """`mark` never refuses to run or write -- this is a visibility notice,
    not a gate (see module docstring's M8-2 AUTHORIZATION NOTICE section).
    Every other test in this file passes an explicit --hits-path pointing
    at a tempdir, which already differs from the frozen
    _PRODUCTION_DEFAULT_HITS_PATH reference, so the notice never fires for
    them -- that is exactly test_notice_absent_when_hits_path_is_redirected
    below, made explicit. To exercise the "still at the real production
    default" branch without ever touching the real production path on disk,
    the positive test redirects BOTH the mutable DEFAULT_OUTPUT_DIR (so
    default_hits_path() resolves under a tempdir) and the frozen
    _PRODUCTION_DEFAULT_HITS_PATH (to that same tempdir path) before
    omitting --hits-path entirely, reproducing the equality condition
    cmd_mark() checks."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rcc-notice-"))
        self._orig_output_dir = rcc.DEFAULT_OUTPUT_DIR
        self._orig_frozen_path = rcc._PRODUCTION_DEFAULT_HITS_PATH

    def tearDown(self) -> None:
        rcc.DEFAULT_OUTPUT_DIR = self._orig_output_dir
        rcc._PRODUCTION_DEFAULT_HITS_PATH = self._orig_frozen_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_notice_printed_when_hits_path_equals_frozen_production_default(self) -> None:
        fake_output_dir = self.tmp / "manifests-output"
        fake_output_dir.mkdir()
        rcc.DEFAULT_OUTPUT_DIR = fake_output_dir
        rcc._PRODUCTION_DEFAULT_HITS_PATH = fake_output_dir / rcc.HITS_NAME
        _write_json(fake_output_dir / rcc.HITS_NAME, make_hits_doc([make_hit("a")]))

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--authorize-production-write",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("NOTICE", err)
        self.assertIn("M8-2's independent authorization gate", err)
        self.assertIn("44%-72%", err)

    def test_notice_absent_when_hits_path_is_redirected(self) -> None:
        # Every real test in this file passes an explicit --hits-path
        # pointing at a tempdir file, distinct from the (untouched, still
        # real-production) _PRODUCTION_DEFAULT_HITS_PATH -- this is that
        # same default condition, made explicit.
        hits_path = self.tmp / rcc.HITS_NAME
        _write_json(hits_path, make_hits_doc([make_hit("a")]))
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(hits_path),
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("NOTICE", err)
        self.assertNotIn("M8-2's independent authorization gate", err)

    def test_notice_fires_for_an_aliased_spelling_of_the_same_production_path(self) -> None:
        # Adversarial-review P3: the comparison used to be literal Path/string
        # equality, so a caller who spelled the exact same on-disk production
        # file via a `..` detour compared unequal (notice suppressed) while
        # still writing that same file. It must now be caught by realpath().
        fake_output_dir = self.tmp / "manifests-output"
        fake_output_dir.mkdir()
        rcc.DEFAULT_OUTPUT_DIR = fake_output_dir
        rcc._PRODUCTION_DEFAULT_HITS_PATH = fake_output_dir / rcc.HITS_NAME
        _write_json(fake_output_dir / rcc.HITS_NAME, make_hits_doc([make_hit("a")]))

        detour = self.tmp / "elsewhere"
        detour.mkdir()
        aliased_path = detour / ".." / "manifests-output" / rcc.HITS_NAME
        self.assertNotEqual(Path(aliased_path), rcc._PRODUCTION_DEFAULT_HITS_PATH)
        self.assertEqual(os.path.realpath(str(aliased_path)), os.path.realpath(str(rcc._PRODUCTION_DEFAULT_HITS_PATH)))

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(aliased_path), "--authorize-production-write",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("NOTICE", err)

    def test_notice_not_printed_when_target_directory_does_not_exist(self) -> None:
        # Adversarial-review P3: the notice used to print unconditionally
        # before checking the target directory exists, so it could announce
        # a write ("NOTICE: writing to ...") immediately followed by exit 4
        # hits_file_missing with nothing ever written. The notice must now
        # come after that existence check, so a doomed call prints no notice.
        fake_output_dir = self.tmp / "manifests-output-missing"
        rcc.DEFAULT_OUTPUT_DIR = fake_output_dir
        rcc._PRODUCTION_DEFAULT_HITS_PATH = fake_output_dir / rcc.HITS_NAME
        self.assertFalse(fake_output_dir.exists())

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--authorize-production-write",
        ])
        self.assertEqual(code, 4, err)
        self.assertNotIn("NOTICE", err)


class StagingByDefaultTests(unittest.TestCase):
    """M8-2 staging-default unification: absent --hits-path AND absent
    --authorize-production-write, mark/list/show must resolve under the new
    STAGING_DEFAULT_OUTPUT_DIR, never under the real production
    DEFAULT_OUTPUT_DIR. Follows the same isolation convention as
    ProductionPathAuthorizationNoticeTests: rebind the module constants to a
    fresh tempdir in setUp/tearDown, never touch the real production path."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rcc-staging-"))
        self._orig_output_dir = rcc.DEFAULT_OUTPUT_DIR
        self._orig_staging_dir = rcc.STAGING_DEFAULT_OUTPUT_DIR
        self._orig_frozen_path = rcc._PRODUCTION_DEFAULT_HITS_PATH

    def tearDown(self) -> None:
        rcc.DEFAULT_OUTPUT_DIR = self._orig_output_dir
        rcc.STAGING_DEFAULT_OUTPUT_DIR = self._orig_staging_dir
        rcc._PRODUCTION_DEFAULT_HITS_PATH = self._orig_frozen_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_with_no_flag_and_no_hits_path_resolves_to_staging_dir(self) -> None:
        fake_staging_dir = self.tmp / "staging-output"
        fake_staging_dir.mkdir()
        fake_production_dir = self.tmp / "production-output"
        fake_production_dir.mkdir()
        rcc.STAGING_DEFAULT_OUTPUT_DIR = fake_staging_dir
        rcc.DEFAULT_OUTPUT_DIR = fake_production_dir
        rcc._PRODUCTION_DEFAULT_HITS_PATH = fake_production_dir / rcc.HITS_NAME
        _write_json(fake_staging_dir / rcc.HITS_NAME, make_hits_doc([make_hit("a")]))

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("NOTICE", err)
        doc = json.loads((fake_staging_dir / rcc.HITS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(doc["hits"][0]["state"], "dismissed")
        # Nothing should have been written into the (separate) production dir.
        self.assertFalse((fake_production_dir / rcc.HITS_NAME).exists())

    def test_default_with_authorization_flag_resolves_to_production_dir(self) -> None:
        fake_staging_dir = self.tmp / "staging-output"
        fake_staging_dir.mkdir()
        fake_production_dir = self.tmp / "production-output"
        fake_production_dir.mkdir()
        rcc.STAGING_DEFAULT_OUTPUT_DIR = fake_staging_dir
        rcc.DEFAULT_OUTPUT_DIR = fake_production_dir
        rcc._PRODUCTION_DEFAULT_HITS_PATH = fake_production_dir / rcc.HITS_NAME
        _write_json(fake_production_dir / rcc.HITS_NAME, make_hits_doc([make_hit("a")]))

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--authorize-production-write",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("NOTICE", err)
        doc = json.loads((fake_production_dir / rcc.HITS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(doc["hits"][0]["state"], "dismissed")

    def test_mark_refuses_explicit_production_hits_path_without_authorization(self) -> None:
        fake_production_dir = self.tmp / "production-output"
        fake_production_dir.mkdir()
        rcc.DEFAULT_OUTPUT_DIR = fake_production_dir
        rcc._PRODUCTION_DEFAULT_HITS_PATH = fake_production_dir / rcc.HITS_NAME
        production_hits_path = fake_production_dir / rcc.HITS_NAME
        _write_json(production_hits_path, make_hits_doc([make_hit("a")]))
        before = production_hits_path.read_bytes()

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(production_hits_path),
        ])
        self.assertEqual(code, 2)
        self.assertIn("production_write_requires_authorization", err)
        self.assertEqual(production_hits_path.read_bytes(), before)

    def test_mark_explicit_production_hits_path_succeeds_with_authorization(self) -> None:
        fake_production_dir = self.tmp / "production-output"
        fake_production_dir.mkdir()
        rcc.DEFAULT_OUTPUT_DIR = fake_production_dir
        rcc._PRODUCTION_DEFAULT_HITS_PATH = fake_production_dir / rcc.HITS_NAME
        production_hits_path = fake_production_dir / rcc.HITS_NAME
        _write_json(production_hits_path, make_hits_doc([make_hit("a")]))

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(production_hits_path), "--authorize-production-write",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("NOTICE", err)
        doc = json.loads(production_hits_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["hits"][0]["state"], "dismissed")

    def test_explicit_non_production_hits_path_needs_no_authorization_flag(self) -> None:
        # Regression: an explicit --hits-path to some genuinely different,
        # non-production, non-staging location continues to work with no
        # flag needed -- unchanged behavior. (Already exercised implicitly by
        # every other test in this file that passes --hits-path into a plain
        # tempdir with no flag, e.g. ListShowTests; this test makes the
        # regression explicit per the staging-by-default spec.)
        other_dir = self.tmp / "some-other-location"
        other_dir.mkdir()
        other_hits_path = other_dir / rcc.HITS_NAME
        _write_json(other_hits_path, make_hits_doc([make_hit("a")]))

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(other_hits_path),
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("NOTICE", err)
        doc = json.loads(other_hits_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["hits"][0]["state"], "dismissed")


# ---------------------------------------------------------------------------
# Lock: same file, acquire/release, stale recovery
# ---------------------------------------------------------------------------


class LockTests(BaseTestCase):
    def test_mark_releases_lock_after_success(self) -> None:
        self.write_hits([make_hit("a")])
        code, _, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 0, err)
        self.assertFalse((self.output_dir / rcc.LOCK_NAME).exists())

    def test_held_fresh_lock_blocks_mark(self) -> None:
        (self.output_dir / rcc.LOCK_NAME).write_text('{"pid": 999999}', encoding="utf-8")
        self.write_hits([make_hit("a")])
        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 4)
        self.assertIn("lock_held", err)

    def test_stale_lock_is_recovered(self) -> None:
        lock_path = self.output_dir / rcc.LOCK_NAME
        lock_path.write_text('{"pid": 999999, "started_at": "stale"}', encoding="utf-8")
        stale_time = time.time() - (rcc.LOCK_STALE_SECONDS + 1)
        os.utime(str(lock_path), (stale_time, stale_time))
        self.write_hits([make_hit("a")])

        code, out, err = _run_rcc([
            "mark", "--hit-id", "a", "--state", "dismissed", "--marked-by", "tester",
            "--hits-path", str(self.hits_path),
        ])
        self.assertEqual(code, 0, err)
        self.assertFalse(lock_path.exists())

    def test_stale_lock_grants_exactly_one_concurrent_caller(self) -> None:
        base_dir = self.tmp / "lock-race-dir"
        base_dir.mkdir()
        lock_path = base_dir / rcc.LOCK_NAME
        lock_path.write_text('{"pid": 999999, "started_at": "stale"}', encoding="utf-8")
        stale_time = time.time() - (rcc.LOCK_STALE_SECONDS + 1)
        os.utime(str(lock_path), (stale_time, stale_time))

        barrier = threading.Barrier(2)
        results: list[str] = []
        results_lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            try:
                rcc.acquire_lock(base_dir)
                outcome = "acquired"
            except rcc.ReviewFatal as exc:
                outcome = f"failed:{exc.reason}"
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(results.count("acquired"), 1, results)
        self.assertEqual(results.count("failed:lock_held"), 1, results)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_readonly_base_dir_raises_named_lock_uncreatable_not_generic_error(self) -> None:
        # Mirrors build_mention_evidence.py's acquire_lock() OSError branch:
        # a read-only base_dir must surface as a NAMED ReviewFatal reason,
        # not propagate as an untyped OSError reported as unexpected_error.
        base_dir = self.tmp / "lock-readonly-dir"
        base_dir.mkdir()
        os.chmod(str(base_dir), 0o500)
        try:
            with self.assertRaises(rcc.ReviewFatal) as ctx:
                rcc.acquire_lock(base_dir)
            self.assertEqual(ctx.exception.reason, "lock_uncreatable")
        finally:
            os.chmod(str(base_dir), 0o700)


# ---------------------------------------------------------------------------
# write_only_within containment (independent copy from discover's own)
# ---------------------------------------------------------------------------


class WriteOnlyWithinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rcc-wow-"))
        self.out_dir = self.tmp / "out"
        self.out_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_path_outside_base_dir_rejected(self) -> None:
        resolved, reason = rcc.write_only_within(self.out_dir, str(self.tmp / "elsewhere.json"))
        self.assertIsNone(resolved)
        self.assertEqual(reason, "outside_base_dir")

    def test_symlink_component_rejected(self) -> None:
        real_target = self.tmp / "real-dir"
        real_target.mkdir()
        symlink = self.out_dir / "sub"
        symlink.symlink_to(real_target)
        target = symlink / "file.json"
        resolved, reason = rcc.write_only_within(self.out_dir, str(target))
        self.assertIsNone(resolved)
        self.assertEqual(reason, "symlink_path")


# ---------------------------------------------------------------------------
# End-to-end: scan -> mark -> rescan -> verify preserved state, using both
# staged tools together against the SAME redirected output dir.
# ---------------------------------------------------------------------------


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="e2e-"))
        self.output_dir = self.tmp / "manifests-output"
        self._orig_dcc_output_dir = dcc.DEFAULT_OUTPUT_DIR
        dcc.DEFAULT_OUTPUT_DIR = self.output_dir
        self.hits_path = self.output_dir / dcc.HITS_NAME

    def tearDown(self) -> None:
        dcc.DEFAULT_OUTPUT_DIR = self._orig_dcc_output_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_mark_rescan_preserves_state(self) -> None:
        proj = self.tmp / "proj"
        (proj / "scripts").mkdir(parents=True)
        (proj / "scripts" / "candidate.py").write_text(
            "#!/usr/bin/env python3\nimport argparse\np = argparse.ArgumentParser()\n", encoding="utf-8"
        )
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, {"schema_version": 1, "generated_at": "2026-08-23T00:00:00Z", "projects": [], "capabilities": [], "wiki_pages": []})

        # 1. scan
        code, _, err = _run_dcc(["scan", "--root", str(proj), "--catalog", str(catalog), "--quiet"])
        self.assertEqual(code, 0, err)
        doc = json.loads(self.hits_path.read_text(encoding="utf-8"))
        hit = next(h for h in doc["hits"] if h["path"] == "scripts/candidate.py")
        self.assertEqual(hit["state"], "pending")

        # 2. mark dismissed via the REAL review tool
        code, out, err = _run_rcc([
            "mark", "--hit-id", hit["hit_id"], "--state", "dismissed",
            "--marked-by", "e2e-tester", "--note", "false positive",
            "--hits-path", str(self.hits_path), "--json",
        ])
        self.assertEqual(code, 0, err)

        # 3. rescan with identical input
        code, _, err = _run_dcc(["scan", "--root", str(proj), "--catalog", str(catalog), "--quiet"])
        self.assertEqual(code, 0, err)

        # 4. verify state preserved
        doc2 = json.loads(self.hits_path.read_text(encoding="utf-8"))
        hit2 = next(h for h in doc2["hits"] if h["hit_id"] == hit["hit_id"])
        self.assertEqual(hit2["state"], "dismissed")
        self.assertEqual(hit2["state_set_by"], "e2e-tester")
        self.assertEqual(hit2["state_note"], "false positive")

        # 5. review tool can still see and list it
        code, out, err = _run_rcc(["list", "--hits-path", str(self.hits_path), "--state", "dismissed", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["hits"][0]["hit_id"], hit["hit_id"])


class AtomicWriteWithinShortWriteRegressionTests(unittest.TestCase):
    """Ported round-6-fix P1 (final-gate dedicated review, Grok,
    2026-08-25): the sibling short-write bug found in
    wiki_edit_guard.py's _atomic_write_via_dir_fd() and
    promote_capability.py's atomic_write_in_dir() -- an unchecked
    os.write(fd, payload) that silently accepts a short write and still
    renames the truncated tmp file onto the live target -- was also
    present in this file's own atomic_write_within() (and acquire_lock()'s
    lock-stamp write). POSIX permits os.write() to return fewer bytes than
    requested for a regular file; a truncated hits/output JSON document
    could otherwise land on the live target while the caller still reports
    success. Fixed via _write_all_bytes(), which loops os.write() until the
    full payload lands and raises immediately on zero forward progress."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rcc-shortwrite-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def test_write_all_bytes_raises_on_zero_progress_short_write(self) -> None:
        fd, path = tempfile.mkstemp(dir=str(self.tmp))
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        payload = b"x" * 100
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd_, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[:40])  # short: only 40 of 100
            return 0  # no forward progress on the retry -- must raise, not spin

        with mock.patch.object(rcc.os, "write", side_effect=short_write):
            with self.assertRaises(OSError):
                rcc._write_all_bytes(fd, payload)
        os.close(fd)

    def test_write_all_bytes_loops_to_completion_on_multiple_short_writes(self) -> None:
        fd, path = tempfile.mkstemp(dir=str(self.tmp))
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        payload = b"y" * 100
        real_write = os.write
        chunks = [30, 30, 40]  # sums to 100 -- must NOT raise, must write it all

        def chunked_write(fd_, data):
            n = chunks.pop(0)
            return real_write(fd_, data[:n])

        with mock.patch.object(rcc.os, "write", side_effect=chunked_write):
            rcc._write_all_bytes(fd, payload)
        os.close(fd)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_atomic_write_within_refuses_rather_than_truncates_on_short_write(self) -> None:
        # End-to-end through atomic_write_within() itself (not just the
        # helper in isolation): a short write must never result in the tmp
        # file being renamed onto the live target.
        base_dir = self.tmp / "hits"
        base_dir.mkdir()
        final_path = base_dir / "discovery-hits.json"
        original_bytes = b'{"schema_version": 1, "hits": []}\n'
        final_path.write_bytes(original_bytes)

        new_payload = b'{"schema_version": 1, "hits": [{"hit_id": "x"}]}\n'
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd_, data):
            # First call: write most of it but not all (a genuine short
            # write). Every call after that returns 0 -- no forward
            # progress at all, simulating a persistent condition (e.g. a
            # resource limit already at capacity), not just a slow one.
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[: max(1, len(data) - 20)])
            return 0

        with mock.patch.object(rcc.os, "write", side_effect=short_write):
            with self.assertRaises(OSError):
                rcc.atomic_write_within(base_dir, final_path, new_payload)

        # The live target file must be COMPLETELY untouched -- not renamed
        # over with truncated content, and no leftover tmp file next to it.
        self.assertEqual(final_path.read_bytes(), original_bytes)
        leftovers = [p for p in base_dir.iterdir() if p.name != final_path.name]
        self.assertEqual(leftovers, [], f"leftover tmp files: {leftovers}")


class AtomicWriteWithinBaseDirTocTouTests(unittest.TestCase):
    """Parent-directory TOCTOU hardening: atomic_write_within() used to
    check base_dir's containment only ONCE, in the CALLER, then do its own
    tmp-file create and rename by PATH STRING
    (os.open(str(tmp_path), ..., O_NOFOLLOW) / os.replace(str(tmp_path),
    str(final_path))). O_NOFOLLOW there only refuses a symlinked tmp-file
    BASENAME -- it does nothing about the directory actually holding the
    file being swapped for a symlink to an outside decoy in the gap
    between the caller's earlier check and this call. Repro follows this
    repo's own established dir-fd-swap methodology (see
    promote_capability.py's
    test_wiki_dir_symlink_swap_before_dir_fd_open_fails_closed): hook
    _open_atomic_write_dir_fd()'s call site -- the first thing
    atomic_write_within() does -- to perform the swap at the exact moment
    the real function is about to open the directory, then confirm the
    open refuses outright (O_NOFOLLOW on its own now-symlinked name)
    instead of silently writing through it into the decoy."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rcc-atomic-write-dirfd-"))
        self.real_dir = self.tmp / "real-base"
        self.real_dir.mkdir(mode=0o700)
        self.moved_aside = self.tmp / "real-base-moved-aside"
        self.outside_decoy = self.tmp / "outside-decoy"
        self.outside_decoy.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_base_dir_symlink_swap_before_dir_fd_open_fails_closed(self) -> None:
        final_path = self.real_dir / "discovery-hits.json"
        real_opener = rcc._open_atomic_write_dir_fd

        def swap_then_open(write_dir):
            os.rename(str(self.real_dir), str(self.moved_aside))
            self.real_dir.symlink_to(self.outside_decoy, target_is_directory=True)
            return real_opener(write_dir)

        with mock.patch.object(rcc, "_open_atomic_write_dir_fd", side_effect=swap_then_open):
            with self.assertRaises(OSError):
                rcc.atomic_write_within(self.real_dir, final_path, b'{"x": 1}')

        # The exact exploit this closes: the outside decoy must never
        # receive the write.
        self.assertEqual(list(self.outside_decoy.iterdir()), [])
        self.assertEqual(list(self.moved_aside.iterdir()), [])
        self.assertFalse(final_path.exists())


if __name__ == "__main__":
    unittest.main()
