#!/usr/bin/env python3
"""Unit tests for detect_capability_changes.py (M8 Gate A Tier-1).

Run with:
    python3 -m unittest test_detect_capability_changes.py -v
(from this directory), or plain `python3 test_detect_capability_changes.py`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import detect_capability_changes as dcc  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def make_dep(raw: str, state: str = "unresolved", scope: str = "same-project", target_global_id=None) -> dict:
    entry = {"raw": raw, "scope": scope, "ref_key": None, "state": state}
    if target_global_id is not None:
        entry["target_global_id"] = target_global_id
    return entry


def make_capability(
    project_id: str,
    cid: str,
    kind: str,
    name: str = "thing",
    path=None,
    summary="a summary",
    last_verified_at="2026-08-22T00:00:00Z",
    depends_on=None,
    global_id=None,
) -> dict:
    return {
        "project_id": project_id,
        "project_real_path": f"/unused/{project_id}",
        "id": cid,
        "kind": kind,
        "name": name,
        "path": path,
        "summary": summary,
        "last_verified_at": last_verified_at,
        "global_id": global_id or f"{project_id}#{cid}",
        "ref_key": f"{project_id}:{kind}:{name}",
        "depends_on": depends_on or [],
    }


def make_project_row(project_id: str, real_path, status: str = "ok") -> dict:
    return {"real_path": str(real_path), "path": str(real_path), "project_id": project_id, "status": status}


def make_catalog(projects=None, capabilities=None, verified_at="2026-08-23T00:00:00Z") -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-08-23T00:00:00Z",
        "verified_at": verified_at,
        "projects": projects or [],
        "capabilities": capabilities or [],
    }


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dcc.main(argv)
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


def _snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        for name in list(dirnames) + list(filenames):
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            out[full] = (st.st_mode, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    return out


# ---------------------------------------------------------------------------
# Allow-list
# ---------------------------------------------------------------------------


class AllowListTests(unittest.TestCase):
    def test_script_and_config_pattern_are_detected_skill_and_unknown_kind_are_not(self) -> None:
        catalog = make_catalog(
            capabilities=[
                make_capability("p", "a", "script", path=None),
                make_capability("p", "b", "config-pattern"),
                make_capability("p", "c", "skill"),
                make_capability("p", "d", "totally-new-future-kind"),
            ]
        )
        result = dcc.detect_capability_hashes(catalog)
        self.assertEqual(set(result["hashes"].keys()), {"p#a", "p#b"})
        self.assertEqual(result["counts"]["considered_count"], 2)
        self.assertEqual(result["counts"]["skipped_kind_count"], 2)
        self.assertEqual(result["counts"]["total_capabilities_in_catalog"], 4)

    def test_capability_missing_global_id_is_counted_malformed_not_silently_dropped(self) -> None:
        cap = make_capability("p", "a", "script")
        del cap["global_id"]
        catalog = make_catalog(capabilities=[cap])
        result = dcc.detect_capability_hashes(catalog)
        self.assertEqual(result["hashes"], {})
        self.assertEqual(result["counts"]["skipped_malformed_count"], 1)
        self.assertEqual(result["counts"]["considered_count"], 0)


# ---------------------------------------------------------------------------
# Hash inclusion / exclusion
# ---------------------------------------------------------------------------


class HashFieldTests(unittest.TestCase):
    def _hash(self, cap: dict) -> str:
        return dcc.compute_capability_hash(cap, {}, {})["content_hash"]

    def test_last_verified_at_change_does_not_change_hash(self) -> None:
        a = make_capability("p", "a", "config-pattern", last_verified_at="2020-01-01T00:00:00Z")
        b = make_capability("p", "a", "config-pattern", last_verified_at="2099-12-31T23:59:59Z")
        self.assertEqual(self._hash(a), self._hash(b))

    def test_name_change_changes_hash(self) -> None:
        a = make_capability("p", "a", "config-pattern", name="x")
        b = make_capability("p", "a", "config-pattern", name="y")
        self.assertNotEqual(self._hash(a), self._hash(b))

    def test_summary_change_changes_hash(self) -> None:
        a = make_capability("p", "a", "config-pattern", summary="s1")
        b = make_capability("p", "a", "config-pattern", summary="s2")
        self.assertNotEqual(self._hash(a), self._hash(b))

    def test_path_change_changes_hash(self) -> None:
        a = make_capability("p", "a", "config-pattern", path="one.md")
        b = make_capability("p", "a", "config-pattern", path="two.md")
        self.assertNotEqual(self._hash(a), self._hash(b))

    def test_depends_on_content_change_changes_hash(self) -> None:
        a = make_capability("p", "a", "config-pattern", depends_on=[make_dep("script:x.py")])
        b = make_capability("p", "a", "config-pattern", depends_on=[make_dep("script:y.py")])
        self.assertNotEqual(self._hash(a), self._hash(b))

    def test_depends_on_is_order_independent(self) -> None:
        a = make_capability(
            "p", "a", "config-pattern", depends_on=[make_dep("script:x.py"), make_dep("script:y.py")]
        )
        b = make_capability(
            "p", "a", "config-pattern", depends_on=[make_dep("script:y.py"), make_dep("script:x.py")]
        )
        self.assertEqual(self._hash(a), self._hash(b))

    def test_depends_on_resolution_state_change_alone_does_not_change_hash(self) -> None:
        """The `state`/`target_global_id` fields are fleet-wide resolution
        facts, not this capability's own authored content -- see the module
        docstring. Only the `raw` string participates in the hash."""
        a = make_capability("p", "a", "config-pattern", depends_on=[make_dep("script:x.py", state="unresolved")])
        b = make_capability(
            "p", "a", "config-pattern",
            depends_on=[make_dep("script:x.py", state="resolved", target_global_id="q#x")],
        )
        self.assertEqual(self._hash(a), self._hash(b))

    def test_excluded_field_name_assertion_fires_on_bad_field_name(self) -> None:
        with self.assertRaises(AssertionError):
            dcc._assert_no_excluded_fields(("name", "last_verified_at"))
        with self.assertRaises(AssertionError):
            dcc._assert_no_excluded_fields(("checked_timestamp",))
        dcc._assert_no_excluded_fields(("name", "kind", "path", "summary", "depends_on"))  # must not raise


# ---------------------------------------------------------------------------
# Script byte hashing -- kind == "script" only
# ---------------------------------------------------------------------------


class ScriptByteHashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dcc-script-"))
        self.project_root = self.tmp / "alpha"
        (self.project_root / "scripts").mkdir(parents=True)
        (self.project_root / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
        self.roots = {"alpha": self.project_root}

    def tearDown(self) -> None:
        _make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_script_content_change_changes_hash_required(self) -> None:
        cap = make_capability("alpha", "s", "script", path="scripts/run.py")
        h1 = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertFalse(h1.get("script_unreadable"))
        (self.project_root / "scripts" / "run.py").write_text("print(2)\n", encoding="utf-8")
        h2 = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertNotEqual(h1["content_hash"], h2["content_hash"])

    def test_config_pattern_never_reads_a_file_even_if_path_points_at_one(self) -> None:
        cap = make_capability("alpha", "c", "config-pattern", path="scripts/run.py")
        h1 = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertNotIn("script_unreadable", h1)
        (self.project_root / "scripts" / "run.py").write_text("completely different\n", encoding="utf-8")
        h2 = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertEqual(h1["content_hash"], h2["content_hash"])

    def test_missing_project_root_degrades_named_not_fatal(self) -> None:
        cap = make_capability("nowhere", "s", "script", path="scripts/run.py")
        result = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertTrue(result["script_unreadable"])
        self.assertEqual(result["script_unreadable_reason"], "project_root_unknown")
        self.assertIn("content_hash", result)

    def test_no_path_degrades_named(self) -> None:
        cap = make_capability("alpha", "s", "script", path=None)
        result = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertTrue(result["script_unreadable"])
        self.assertEqual(result["script_unreadable_reason"], "no_path")

    def test_absolute_path_rejected(self) -> None:
        cap = make_capability("alpha", "s", "script", path="/etc/passwd")
        result = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertTrue(result["script_unreadable"])
        self.assertEqual(result["script_unreadable_reason"], "path_is_absolute")

    def test_path_escaping_project_root_rejected(self) -> None:
        cap = make_capability("alpha", "s", "script", path="../../../../etc/passwd")
        result = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertTrue(result["script_unreadable"])
        self.assertEqual(result["script_unreadable_reason"], "path_escapes_project_root")

    def test_missing_file_degrades_named(self) -> None:
        cap = make_capability("alpha", "s", "script", path="scripts/does-not-exist.py")
        result = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertTrue(result["script_unreadable"])
        self.assertEqual(result["script_unreadable_reason"], "script_missing")

    def test_oversized_script_degrades_named(self) -> None:
        (self.project_root / "scripts" / "big.py").write_bytes(b"x" * (dcc.MAX_SCRIPT_BYTES + 10))
        cap = make_capability("alpha", "s", "script", path="scripts/big.py")
        result = dcc.compute_capability_hash(cap, self.roots, {})
        self.assertTrue(result["script_unreadable"])
        self.assertEqual(result["script_unreadable_reason"], "script_too_large")

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_permission_denied_degrades_named(self) -> None:
        target = self.project_root / "scripts" / "locked.py"
        target.write_text("print(1)\n", encoding="utf-8")
        os.chmod(str(target), 0o000)
        try:
            cap = make_capability("alpha", "s", "script", path="scripts/locked.py")
            result = dcc.compute_capability_hash(cap, self.roots, {})
            self.assertTrue(result["script_unreadable"])
            self.assertEqual(result["script_unreadable_reason"], "script_permission_denied")
        finally:
            os.chmod(str(target), 0o644)


# ---------------------------------------------------------------------------
# projects[] -> real_path resolution, including project_id_ambiguous
# ---------------------------------------------------------------------------


class ProjectRootIndexTests(unittest.TestCase):
    def test_unique_project_id_resolves_directly(self) -> None:
        catalog = make_catalog(projects=[make_project_row("alpha", "/a/alpha", status="ok")])
        roots, ambiguous = dcc.build_project_root_index(catalog)
        self.assertEqual(roots["alpha"], Path("/a/alpha"))
        self.assertFalse(ambiguous["alpha"])

    def test_ambiguous_project_id_prefers_the_unique_ok_row(self) -> None:
        catalog = make_catalog(
            projects=[
                make_project_row("dup", "/a/dup-worktree-1", status="no-wiki-dir"),
                make_project_row("dup", "/a/dup-main", status="ok"),
                make_project_row("dup", "/a/dup-worktree-2", status="no-wiki-dir"),
            ]
        )
        roots, ambiguous = dcc.build_project_root_index(catalog)
        self.assertEqual(roots["dup"], Path("/a/dup-main"))
        self.assertTrue(ambiguous["dup"])

    def test_ambiguous_project_id_with_no_unique_ok_row_falls_back_to_sorted_first(self) -> None:
        catalog = make_catalog(
            projects=[
                make_project_row("dup", "/a/zzz", status="no-wiki-dir"),
                make_project_row("dup", "/a/aaa", status="no-wiki-dir"),
            ]
        )
        roots, ambiguous = dcc.build_project_root_index(catalog)
        self.assertEqual(roots["dup"], Path("/a/aaa"))
        self.assertTrue(ambiguous["dup"])

    def test_malformed_rows_are_skipped_without_raising(self) -> None:
        catalog = make_catalog(projects=[{"project_id": "no-real-path"}, "not-a-dict", None])
        roots, ambiguous = dcc.build_project_root_index(catalog)
        self.assertEqual(roots, {})


# ---------------------------------------------------------------------------
# capability-changes.json four-way classification
# ---------------------------------------------------------------------------


class DiffHashesTests(unittest.TestCase):
    def test_added_removed_changed_unchanged(self) -> None:
        previous = {
            "a": {"content_hash": "h1"},
            "b": {"content_hash": "h2"},
            "c": {"content_hash": "h3"},
        }
        current = {
            "a": {"content_hash": "h1"},  # unchanged
            "b": {"content_hash": "hX"},  # changed
            "d": {"content_hash": "h4"},  # added
            # "c" removed
        }
        diff = dcc.diff_hashes(previous, current)
        self.assertEqual(diff["added"], ["d"])
        self.assertEqual(diff["removed"], ["c"])
        self.assertEqual(diff["changed"], [{"global_id": "b", "previous_hash": "h2", "current_hash": "hX"}])
        self.assertEqual(diff["unchanged"], ["a"])
        self.assertEqual(diff["counts"], {"added": 1, "removed": 1, "changed": 1, "unchanged": 1})


# ---------------------------------------------------------------------------
# CLI: exit codes + write pipeline (DEFAULT_OUTPUT_DIR monkeypatched)
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dcc-cli-"))
        self.output_dir = self.tmp / "manifests-output"
        self._orig_output_dir = dcc.DEFAULT_OUTPUT_DIR
        dcc.DEFAULT_OUTPUT_DIR = self.output_dir

        self.catalog_path = self.tmp / "catalog.json"
        catalog = make_catalog(
            capabilities=[
                make_capability("p", "a", "script", path=None),
                make_capability("p", "b", "config-pattern"),
            ]
        )
        _write_json(self.catalog_path, catalog)

    def tearDown(self) -> None:
        dcc.DEFAULT_OUTPUT_DIR = self._orig_output_dir
        _make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exit_0_on_success_and_writes_hashes_file(self) -> None:
        code, out, err = _run_main(["detect", "--catalog", str(self.catalog_path), "--json", "--quiet"])
        self.assertEqual(code, 0)
        self.assertTrue((self.output_dir / dcc.HASHES_NAME).exists())
        self.assertFalse((self.output_dir / dcc.CHANGES_NAME).exists())

    def test_exit_2_on_empty_catalog_path(self) -> None:
        code, out, err = _run_main(["detect", "--catalog", "", "--quiet"])
        self.assertEqual(code, 2)

    def test_exit_2_on_empty_previous_hashes_path(self) -> None:
        code, out, err = _run_main(
            ["detect", "--catalog", str(self.catalog_path), "--previous-hashes", "", "--quiet"]
        )
        self.assertEqual(code, 2)

    def test_exit_4_on_missing_catalog_and_previous_output_untouched(self) -> None:
        code, out, err = _run_main(["detect", "--catalog", str(self.catalog_path), "--quiet"])
        self.assertEqual(code, 0)
        before = (self.output_dir / dcc.HASHES_NAME).read_bytes()

        missing = self.tmp / "does-not-exist.json"
        code2, out2, err2 = _run_main(["detect", "--catalog", str(missing), "--quiet"])
        self.assertEqual(code2, 4)
        after = (self.output_dir / dcc.HASHES_NAME).read_bytes()
        self.assertEqual(before, after)

    def test_exit_4_on_unparseable_catalog(self) -> None:
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, out, err = _run_main(["detect", "--catalog", str(bad), "--quiet"])
        self.assertEqual(code, 4)

    def test_changes_json_written_only_when_previous_hashes_given(self) -> None:
        code, _, _ = _run_main(["detect", "--catalog", str(self.catalog_path), "--quiet"])
        self.assertEqual(code, 0)
        prev_path = self.output_dir / dcc.HASHES_NAME
        code2, _, _ = _run_main(
            ["detect", "--catalog", str(self.catalog_path), "--previous-hashes", str(prev_path), "--quiet"]
        )
        self.assertEqual(code2, 0)
        self.assertTrue((self.output_dir / dcc.CHANGES_NAME).exists())
        changes = json.loads((self.output_dir / dcc.CHANGES_NAME).read_text(encoding="utf-8"))
        self.assertEqual(changes["counts"], {"added": 0, "removed": 0, "changed": 0, "unchanged": 2})

    def test_previous_hashes_missing_file_treated_as_absent_not_fatal(self) -> None:
        missing = self.tmp / "no-such-prev.json"
        code, _, _ = _run_main(
            ["detect", "--catalog", str(self.catalog_path), "--previous-hashes", str(missing), "--quiet"]
        )
        self.assertEqual(code, 0)
        changes = json.loads((self.output_dir / dcc.CHANGES_NAME).read_text(encoding="utf-8"))
        self.assertEqual(changes["previous_hashes_status"], "absent")
        self.assertEqual(changes["counts"]["added"], 2)

    def test_lock_held_returns_exit_4(self) -> None:
        os.makedirs(str(self.output_dir), mode=0o700, exist_ok=True)
        lock_path = dcc.acquire_lock(self.output_dir)
        try:
            code, out, err = _run_main(["detect", "--catalog", str(self.catalog_path), "--quiet"])
            self.assertEqual(code, 4)
        finally:
            dcc.release_lock(lock_path)
        # Lock released -- a subsequent run must succeed.
        code2, _, _ = _run_main(["detect", "--catalog", str(self.catalog_path), "--quiet"])
        self.assertEqual(code2, 0)


# ---------------------------------------------------------------------------
# write_only_within guard truth table
# ---------------------------------------------------------------------------


class WriteGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dcc-writeguard-"))
        self.out_dir = self.tmp / "out"
        self.out_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_accepts_path_inside_output_dir(self) -> None:
        target = self.out_dir / "file.json"
        resolved, reason = dcc.write_only_within(self.out_dir, str(target))
        self.assertIsNone(reason)
        self.assertEqual(resolved, target.resolve())

    def test_rejects_path_outside_output_dir(self) -> None:
        outside = self.tmp / "elsewhere.json"
        resolved, reason = dcc.write_only_within(self.out_dir, str(outside))
        self.assertIsNone(resolved)
        self.assertEqual(reason, "outside_output_dir")

    def test_rejects_dot_dot_escape_after_resolve(self) -> None:
        escape = self.out_dir / ".." / "escape.json"
        resolved, reason = dcc.write_only_within(self.out_dir, str(escape))
        self.assertIsNone(resolved)
        self.assertEqual(reason, "outside_output_dir")

    def test_rejects_relative_path(self) -> None:
        resolved, reason = dcc.write_only_within(self.out_dir, "relative/file.json")
        self.assertIsNone(resolved)
        self.assertEqual(reason, "non_absolute_path")

    def test_rejects_output_root_itself(self) -> None:
        resolved, reason = dcc.write_only_within(self.out_dir, str(self.out_dir))
        self.assertIsNone(resolved)
        self.assertEqual(reason, "is_output_root")

    def test_rejects_symlink_component_below_root(self) -> None:
        real_dir = self.tmp / "real"
        real_dir.mkdir()
        link = self.out_dir / "link"
        link.symlink_to(real_dir)
        target = link / "file.json"
        resolved, reason = dcc.write_only_within(self.out_dir, str(target))
        self.assertIsNone(resolved)
        self.assertEqual(reason, "symlink_path")


# ---------------------------------------------------------------------------
# OS-level isolation: read-only fake project trees (mandatory, real
# permissions -- not mocks). Mirrors build_cross_project_catalog.py's own
# NegativeControlTests + FixtureTreeTests pattern.
# ---------------------------------------------------------------------------


class NegativeControlTests(unittest.TestCase):
    """Mandatory control: proves the filesystem actually enforces the
    permission bits this suite relies on. If this ever fails, the whole
    isolation suite below would pass vacuously (root, or a
    permission-ignoring filesystem) -- do not delete this to "fix" it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dcc-negctrl-"))
        (self.tmp / "scripts").mkdir()
        (self.tmp / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
        _make_readonly(self.tmp)

    def tearDown(self) -> None:
        _make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_cannot_create_new_file_in_readonly_tree(self) -> None:
        with self.assertRaises(PermissionError):
            (self.tmp / "scripts" / "new-file.json").write_text("{}", encoding="utf-8")


class ReadOnlyFleetIsolationTests(unittest.TestCase):
    """Builds 3 OS-level read-only fake project trees with fake
    capabilities pointing at fake scripts, then runs BOTH the internal
    hashing path and the full CLI against a catalog naming them. Asserts
    (1) no PermissionError anywhere, and (2) every project tree is
    byte-identical (mode/size/mtime) before and after -- strictly stronger
    than "did not raise": proves nothing was created, touched, or cached
    inside any of them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="dcc-fleet-"))
        cls.projects_root = cls.tmp / "projects"
        cls.projects_root.mkdir()

        cls.rows = []
        cls.caps = []
        for name in ("alpha", "beta", "gamma"):
            proj = cls.projects_root / name
            (proj / "scripts").mkdir(parents=True)
            (proj / "scripts" / "run.py").write_text(f"print('{name}')\n", encoding="utf-8")
            cls.rows.append(make_project_row(name, proj, status="ok"))
            cls.caps.append(make_capability(name, "s", "script", path="scripts/run.py"))
            cls.caps.append(make_capability(name, "c", "config-pattern"))

        _make_readonly(cls.projects_root)
        cls.snapshot_before = _snapshot(cls.projects_root)

        cls.catalog_path = cls.tmp / "catalog.json"
        _write_json(cls.catalog_path, make_catalog(projects=cls.rows, capabilities=cls.caps))

        cls.output_dir = cls.tmp / "manifests-output"  # writable, OUTSIDE the read-only tree

    @classmethod
    def tearDownClass(cls) -> None:
        _make_writable(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self._orig_output_dir = dcc.DEFAULT_OUTPUT_DIR
        dcc.DEFAULT_OUTPUT_DIR = self.output_dir

    def tearDown(self) -> None:
        dcc.DEFAULT_OUTPUT_DIR = self._orig_output_dir
        if self.output_dir.exists():
            _make_writable(self.output_dir)
            shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_full_cli_run_against_readonly_fleet_raises_nothing_and_leaves_it_untouched(self) -> None:
        code, out, err = _run_main(["detect", "--catalog", str(self.catalog_path), "--json", "--quiet"])
        self.assertEqual(code, 0, err)
        snapshot_after = _snapshot(self.projects_root)
        self.assertEqual(self.snapshot_before, snapshot_after)

        hashes = json.loads((self.output_dir / dcc.HASHES_NAME).read_text(encoding="utf-8"))
        self.assertEqual(hashes["counts"]["considered_count"], 6)  # 3 script + 3 config-pattern
        self.assertEqual(hashes["counts"]["script_unreadable_count"], 0)

    def test_write_landed_only_under_manifests_output(self) -> None:
        _run_main(["detect", "--catalog", str(self.catalog_path), "--quiet"])
        # The ONLY new filesystem entries anywhere under self.tmp must be
        # under self.output_dir -- proven by walking the parent tmp root
        # (excluding the untouched, still-readonly projects_root and the
        # pre-existing catalog.json) and requiring every new path to be
        # rooted at output_dir.
        for dirpath, dirnames, filenames in os.walk(str(self.tmp)):
            # Skip the pre-existing fixture tree and the pre-existing
            # catalog.json -- only NEW entries created by this run are
            # being checked, and both of those predate it.
            try:
                Path(dirpath).relative_to(self.projects_root)
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


if __name__ == "__main__":
    unittest.main()
